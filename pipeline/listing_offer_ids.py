"""Transactional shop-scoped offer IDs; read paths never allocate a number."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .listing_form import read_json, write_json, _require_editable
from .product_edit_lock import serialized_product_edit
from .sku_selection import active_skus

MANIFEST_FILE = "input/listing-offer-ids.json"
DEFAULT_PREFIX = "xzj.jp"
BEIJING = timezone(timedelta(hours=8))
_SCHEMA = """
CREATE TABLE IF NOT EXISTS offer_profiles (
 profile_id TEXT NOT NULL, shop_id TEXT NOT NULL, prefix TEXT NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(profile_id,shop_id));
CREATE TABLE IF NOT EXISTS offer_counters (
 shop_id TEXT NOT NULL, prefix TEXT NOT NULL, day TEXT NOT NULL, next_number INTEGER NOT NULL,
 PRIMARY KEY(shop_id,prefix,day));
CREATE TABLE IF NOT EXISTS offer_bindings (
 shop_id TEXT NOT NULL, product_id TEXT NOT NULL, sku_id TEXT NOT NULL, offer_id TEXT NOT NULL,
 profile_id TEXT NOT NULL, prefix TEXT NOT NULL, day TEXT NOT NULL, sequence INTEGER,
 source TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(shop_id,product_id,sku_id), UNIQUE(shop_id,offer_id));
"""


def registry_path(directory: Path | str | None = None) -> Path:
    explicit = os.environ.get("WORKBENCH_OFFER_DB_PATH")
    if explicit:
        return Path(explicit)
    application = sys.modules.get("api")
    if application is not None and getattr(application, "MARKET_DB_PATH", None) is not None:
        if directory is None or Path(directory).resolve().is_relative_to(Path(application.PRODUCTS_ROOT).resolve()):
            return Path(application.MARKET_DB_PATH).parent / "listing-offer-ids.sqlite3"
    if directory is not None:
        return Path(directory).resolve().parent.parent / "runtime/listing-offer-ids.sqlite3"
    return Path(__file__).resolve().parent.parent / "runtime/listing-offer-ids.sqlite3"


def _id(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", clean):
        raise ValueError(f"{label}格式无效")
    return clean


def normalize_prefix(value: Any) -> str:
    clean = str(value or "").strip().rstrip(".")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,27}", clean):
        raise ValueError("员工前缀需为 1–28 个英文字母、数字、点、下划线或短横线")
    return clean


def _offer(value: Any) -> str:
    clean = str(value or "")
    if not clean.strip() or len(clean) > 50 or any(ord(char) < 32 for char in clean):
        raise ValueError("已有货号格式无效，不能自动修改或截断已有 Ozon offer_id")
    return clean


def _initialize(connection: sqlite3.Connection) -> None:
    """Retry only idempotent setup: first-open WAL locks can bypass timeout."""
    deadline = time.monotonic() + 30
    for initialize in (lambda: connection.execute("PRAGMA journal_mode=WAL"),
                       lambda: connection.executescript(_SCHEMA)):
        delay = .01
        while True:
            try:
                initialize()
                break
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, .2)


def _open(path: Path, *, write: bool = False) -> sqlite3.Connection | None:
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            _initialize(connection)
        except Exception:
            connection.close()
            raise
    else:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        # A concurrent first writer can have created the database file before
        # all CREATE TABLE statements finish. Reads must not create its schema
        # or fail simply because explicit initialization is still in flight.
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"offer_profiles", "offer_counters", "offer_bindings"}.issubset(tables):
            connection.close()
            return None
    connection.row_factory = sqlite3.Row
    return connection


def read_profile(profile_id: str, shop: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    profile_id, shop = _id(profile_id, "员工配置 ID"), _id(shop, "店铺 ID")
    connection = _open(Path(db_path) if db_path else registry_path())
    row = None
    if connection is not None:
        try:
            row = connection.execute("SELECT prefix,updated_at FROM offer_profiles WHERE profile_id=? AND shop_id=?", (profile_id, shop)).fetchone()
        finally:
            connection.close()
    return {"profile_id": profile_id, "shop": shop, "prefix": row["prefix"] if row else DEFAULT_PREFIX,
            "saved": bool(row), "updated_at": row["updated_at"] if row else None}


def save_profile(profile_id: str, shop: str, prefix: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    profile_id, shop, prefix = _id(profile_id, "员工配置 ID"), _id(shop, "店铺 ID"), normalize_prefix(prefix)
    path = Path(db_path) if db_path else registry_path()
    connection = _open(path, write=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO offer_profiles VALUES (?,?,?,?) ON CONFLICT(profile_id,shop_id) DO UPDATE SET prefix=excluded.prefix,updated_at=excluded.updated_at",
                           (profile_id, shop, prefix, datetime.now(timezone.utc).isoformat()))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return read_profile(profile_id, shop, db_path=path)


def _legacy_offers(directory: Path, shop: str, source: Mapping[str, Any]) -> dict[str, str]:
    offers = {}
    for sku in source.get("skus") or []:
        if isinstance(sku, Mapping) and sku.get("offer_id"):
            offers[str(sku["sku_id"])] = _offer(sku["offer_id"])
    # Submitted receipts outrank stale source values. Preserve the real IDs
    # Ozon received even if source data has subsequently changed.
    stores = read_json(directory / "output/store-publications.json").get("stores") or {}
    publication = stores.get(shop) or {}
    for row in publication.get("sku_publications") or []:
        if isinstance(row, Mapping) and row.get("offer_id") and row.get("sku_id") not in (None, "", "*"):
            offers[str(row["sku_id"])] = _offer(row["offer_id"])
    run = directory / "output/store-runs" / shop
    filenames = ["submission-receipt.json", "ozon-upload-payload.json", "upload-payload.json"]
    if publication.get("task_id") or any(row.get("task_id") or row.get("ozon_product_id") for row in publication.get("sku_publications") or [] if isinstance(row, Mapping)):
        filenames.append("payload.json")
    for filename in filenames:
        receipt = read_json(run / filename)
        for row in receipt.get("expected_offers") or receipt.get("variants") or []:
            if isinstance(row, Mapping) and row.get("offer_id") and row.get("source_sku_id"):
                offers[str(row["source_sku_id"])] = _offer(row["offer_id"])
    return offers


def _seed_known_history(connection: sqlite3.Connection, directory: Path, shop: str, created_at: str) -> None:
    """Index existing local offer receipts so old M.D numbers cannot collide.

    This reads product records, not Ozon. It does not invent external history or
    change any old product files; the final API preflight still checks Ozon.
    """
    for sibling in directory.parent.glob("P[0-9]*"):
        if not sibling.is_dir() or sibling.is_symlink() or not re.fullmatch(r"P\d+", sibling.name):
            continue
        source = read_json(sibling / "input/source.json")
        for sku_id, offer in _legacy_offers(sibling, shop, source).items():
            existing = connection.execute("SELECT product_id,sku_id,offer_id FROM offer_bindings WHERE shop_id=? AND ((product_id=? AND sku_id=?) OR offer_id=?)",
                                          (shop, sibling.name, sku_id, offer)).fetchall()
            if any(row["product_id"] != sibling.name or row["sku_id"] != sku_id or row["offer_id"] != offer for row in existing):
                raise ValueError("历史店铺货号与本地登记发生冲突，请核对旧发布台账，不能覆盖或重新编号")
            if not existing:
                connection.execute("INSERT INTO offer_bindings VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (shop, sibling.name, sku_id, offer, "legacy", "", "", None, "legacy", created_at))


def read_offer_ids(directory: Path | str, shop: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    directory, shop = Path(directory), _id(shop, "店铺 ID")
    source = read_json(directory / "input/source.json")
    skus = active_skus(directory, source.get("skus") or [])
    legacy = _legacy_offers(directory, shop, source)
    connection = _open(Path(db_path) if db_path else registry_path(directory))
    bindings = {}
    if connection is not None:
        try:
            bindings = {row["sku_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM offer_bindings WHERE shop_id=? AND product_id=?", (shop, directory.name))}
        finally:
            connection.close()
    items = []
    for sku in skus:
        sku_id = str(sku["sku_id"])
        row = bindings.get(sku_id)
        if row:
            if sku_id in legacy and legacy[sku_id] != row["offer_id"]:
                raise ValueError("货号登记与已有 Ozon 提交记录不一致，不能修改已发布货号")
            item = {"source_sku_id": sku_id, "offer_id": row["offer_id"], "source": row["source"],
                    "prefix": row["prefix"], "beijing_date": row["day"], "sequence": row["sequence"], "reserved_at": row["created_at"]}
        else:
            item = {"source_sku_id": sku_id, "offer_id": legacy.get(sku_id), "source": "legacy" if sku_id in legacy else "unreserved"}
        items.append(item)
    return {"shop": shop, "product_id": directory.name, "items": items,
            "offers": {row["source_sku_id"]: row["offer_id"] for row in items if row.get("offer_id")},
            "complete": bool(items) and all(row.get("offer_id") for row in items), "readonly": True,
            "api_writes_performed": False}


@serialized_product_edit
def reserve_offer_ids(directory: Path | str, shop: str, profile_id: str, prefix: str | None = None,
                      *, db_path: Path | str | None = None, now: datetime | None = None) -> dict[str, Any]:
    directory, shop, profile_id = Path(directory), _id(shop, "店铺 ID"), _id(profile_id, "员工配置 ID")
    _require_editable(directory)
    selection = read_json(directory / "input/category-selection.json")
    if selection.get("shop_id") and selection["shop_id"] != shop:
        raise ValueError("货号预留店铺与已确认的上架类目不一致")
    source = read_json(directory / "input/source.json")
    skus = active_skus(directory, source.get("skus") or [])
    if not skus:
        raise ValueError("请先选择至少一个真实商品规格")
    if len({str(row["sku_id"]) for row in skus}) != len(skus):
        raise ValueError("商品规格 ID 重复，不能分配货号")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("货号日期必须包含时区")
    day = instant.astimezone(BEIJING).date()
    created_at = instant.astimezone(timezone.utc).isoformat()
    path = Path(db_path) if db_path else registry_path(directory)
    legacy = _legacy_offers(directory, shop, source)
    connection = _open(path, write=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_known_history(connection, directory, shop, created_at)
        saved = connection.execute("SELECT prefix FROM offer_profiles WHERE profile_id=? AND shop_id=?", (profile_id, shop)).fetchone()
        chosen = normalize_prefix(prefix) if prefix is not None else saved["prefix"] if saved else DEFAULT_PREFIX
        connection.execute("INSERT INTO offer_profiles VALUES (?,?,?,?) ON CONFLICT(profile_id,shop_id) DO UPDATE SET prefix=excluded.prefix,updated_at=excluded.updated_at",
                           (profile_id, shop, chosen, created_at))
        for sku in skus:
            sku_id = str(sku["sku_id"])
            old = connection.execute("SELECT offer_id FROM offer_bindings WHERE shop_id=? AND product_id=? AND sku_id=?", (shop, directory.name, sku_id)).fetchone()
            if old:
                if sku_id in legacy and legacy[sku_id] != old["offer_id"]:
                    raise ValueError("已有 Ozon 货号与本地登记不一致，不能重新编号")
                continue
            sequence = None
            origin = "legacy" if sku_id in legacy else "reserved"
            if origin == "legacy":
                offer = legacy[sku_id]
            else:
                counter = connection.execute("SELECT next_number FROM offer_counters WHERE shop_id=? AND prefix=? AND day=?", (shop, chosen, day.isoformat())).fetchone()
                sequence = int(counter["next_number"]) if counter else 1
                while True:
                    if sequence > 1_000_000_000_000:
                        raise ValueError("此日期的货号流水已达到安全上限")
                    offer = f"{chosen}.{day.month}.{day.day}.{sequence}"
                    if not connection.execute("SELECT 1 FROM offer_bindings WHERE shop_id=? AND offer_id=?", (shop, offer)).fetchone():
                        break
                    sequence += 1
                connection.execute("INSERT INTO offer_counters VALUES (?,?,?,?) ON CONFLICT(shop_id,prefix,day) DO UPDATE SET next_number=excluded.next_number",
                                   (shop, chosen, day.isoformat(), sequence + 1))
            try:
                connection.execute("INSERT INTO offer_bindings VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (shop, directory.name, sku_id, _offer(offer), profile_id, chosen, day.isoformat(), sequence, origin, created_at))
            except sqlite3.IntegrityError:
                raise ValueError("已有货号已被该店铺其他商品占用，不能覆盖或自动改写") from None
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    result = read_offer_ids(directory, shop, db_path=path)
    # A crash after SQL commit is harmless: retry finds the same rows and
    # reconstructs this mirror; no second number is consumed.
    old_manifest = read_json(directory / MANIFEST_FILE)
    shops = old_manifest.get("shops") or {}
    shops[shop] = {"profile_id": profile_id, "prefix": chosen, "offers": result["offers"], "items": result["items"]}
    write_json(directory / MANIFEST_FILE, {"schema_version": "1.0.0", "product_id": directory.name, "shops": shops})
    bound_days = sorted({row["beijing_date"] for row in result["items"] if row.get("beijing_date")})
    return {**result, "readonly": False, "prefix": chosen, "profile_id": profile_id,
            "beijing_date": bound_days[0] if len(bound_days) == 1 else None,
            "beijing_dates": bound_days, "requested_date": day.isoformat()}


def offer_for_variant(directory: Path | str, shop: str, sku: Mapping[str, Any], index: int,
                      *, db_path: Path | str | None = None) -> str | None:
    directory = Path(directory)
    found = read_offer_ids(directory, shop, db_path=db_path)["offers"].get(str(sku.get("sku_id")))
    if found:
        return found
    if not (directory / "input/guided-workflow.json").is_file() and not (directory / MANIFEST_FILE).is_file():
        from .upload import offer_id_for
        return offer_id_for(directory.name, sku, index)
    return None
