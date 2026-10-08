"""Shop/offer publication history and explicit, bounded FBS/rFBS stock writes.

GETs only read caches. Import and stock are separate intents; an unknown stock
result never causes a second import or an automatic stock retry.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid

from .context import read_json, write_json
from .product_edit_lock import product_edit_lock

CONFIG_FILE = "input/listing-publication-config.json"
INTENT_FILE = "runtime/listing-publication-intent.json"
WAREHOUSE_PATH = "/v2/warehouse/list"
STOCK_PATH = "/v2/products/stocks"
INFO_PATH = "/v3/product/info/list"
STOCK_READ_PATH = "/v2/product/info/stocks-by-warehouse/fbs"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouse_cache(shop TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listing_publications(
 shop TEXT NOT NULL, offer_id TEXT NOT NULL, product_id TEXT NOT NULL,
 payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(shop,offer_id));
CREATE INDEX IF NOT EXISTS publications_product ON listing_publications(product_id,shop);
CREATE TABLE IF NOT EXISTS publication_attempts(
 id TEXT PRIMARY KEY, shop TEXT NOT NULL, offer_id TEXT NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_offer ON publication_attempts(shop,offer_id);
CREATE TABLE IF NOT EXISTS stock_requests(
 id TEXT PRIMARY KEY, shop TEXT NOT NULL, started_epoch REAL NOT NULL,
 state TEXT NOT NULL, payload TEXT NOT NULL);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_post(transport, path: str, body: dict):
    try:
        return transport.post(path, body)
    except Exception:
        raise ValueError("官方数据读取失败，请检查店铺权限和网络；未据此写入或重试库存") from None


def registry_path(directory: Path | str | None = None) -> Path:
    explicit = os.environ.get("WORKBENCH_PUBLICATION_DB_PATH")
    if explicit:
        return Path(explicit)
    application = sys.modules.get("api")
    if application is not None and getattr(application, "MARKET_DB_PATH", None) is not None:
        root = getattr(application, "PRODUCTS_ROOT", None)
        if directory is None or (root and Path(directory).resolve().is_relative_to(Path(root).resolve())):
            return Path(application.MARKET_DB_PATH).parent / "listing-publications.sqlite3"
    if directory is not None:
        return Path(directory).resolve().parent.parent / "runtime/listing-publications.sqlite3"
    return Path(__file__).resolve().parent.parent / "runtime/listing-publications.sqlite3"


def _connect(path: Path, *, readonly: bool = False):
    if readonly:
        if not path.is_file():
            return None
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=8)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=8)
        connection.executescript(_SCHEMA)
    connection.row_factory = sqlite3.Row
    return connection


def _shop(value: Any) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value):
        raise ValueError("店铺格式无效")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or len(str(value)) > 16:
        raise ValueError(f"{label}必须为整数")
    value = int(value)
    if value < (1 if positive else 0):
        raise ValueError(f"{label}不能小于{1 if positive else 0}")
    return value


def source_url(directory: Path) -> str:
    source = read_json(directory / "input/source.json")
    value = str(source.get("source_url") or source.get("url") or "")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return ""
    # Source links are employee metadata, never product copy or Ozon attributes.
    if (parsed.hostname or "").lower() == "detail.1688.com":
        found = re.fullmatch(r"/offer/(\d+)\.html", parsed.path)
        return f"https://detail.1688.com/offer/{found[1]}.html" if found else ""
    return ""


def source_specifications(directory: Path | str) -> dict[str, str]:
    """Supplier specifications, per source SKU; never translate or invent values."""
    from .sku_selection import active_skus
    root = Path(directory)
    source = read_json(root / "input/source.json")
    result = {}
    for sku in active_skus(root, source.get("skus") or []):
        values = []
        for option in sku.get("option_values") or []:
            if not isinstance(option, Mapping):
                continue
            value = option.get("value_cn") or option.get("value")
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
        specification = " ".join(values) or str(sku.get("name_cn") or sku.get("name") or "").strip()
        if specification and specification not in {"未指定", "unknown", "未知"}:
            result[str(sku.get("sku_id"))] = specification
    return result


def transport_for_shop(shop: str, *, registry=None, transport_factory=None):
    from .stores import load_registry, list_shops
    from .ozon_http import OzonCredentials, UrllibTransport
    record = next((row for row in list_shops(load_registry(registry))
                   if str(row.get("id")) == _shop(shop) and row.get("enabled")), None)
    if record is None:
        raise ValueError("请选择已授权并启用的店铺")
    return (transport_factory or UrllibTransport)(OzonCredentials.from_shop(record))


def read_warehouses(shop: str, *, db_path=None) -> dict:
    shop = _shop(shop)
    path = Path(db_path) if db_path else registry_path()
    connection = _connect(path, readonly=True)
    try:
        row = connection.execute("SELECT payload FROM warehouse_cache WHERE shop=?", (shop,)).fetchone() if connection else None
        payload = json.loads(row["payload"]) if row else {}
        return {"ok": True, "shop": shop, "items": payload.get("items", []),
                "checked_at": payload.get("checked_at"), "cached": bool(row),
                "complete": payload.get("complete", False)}
    finally:
        if connection:
            connection.close()


def refresh_warehouses(shop: str, *, db_path=None, transport=None, registry=None, transport_factory=None) -> dict:
    shop = _shop(shop)
    transport = transport or transport_for_shop(shop, registry=registry, transport_factory=transport_factory)
    cursor, seen, rows = "", set(), []
    for _ in range(20):
        body = {"limit": 200}
        if cursor:
            body["cursor"] = cursor
        reply = _read_post(transport, WAREHOUSE_PATH, body)
        if not isinstance(reply, dict) or not isinstance(reply.get("warehouses"), list) or not isinstance(reply.get("has_next"), bool):
            raise ValueError("仓库接口响应不完整，未覆盖上次完整缓存")
        for item in reply["warehouses"]:
            if not isinstance(item, dict):
                raise ValueError("仓库接口返回了无效行")
            warehouse_id = str(_integer(item.get("warehouse_id"), "仓库 ID", positive=True))
            if warehouse_id in seen:
                raise ValueError("仓库接口重复返回仓库 ID，未保存不完整列表")
            seen.add(warehouse_id)
            status = str(item.get("status") or "").lower()
            paused = item.get("pause_at") not in (None, "")
            rows.append({"warehouse_id": warehouse_id, "name": str(item.get("name") or warehouse_id),
                         "status": status, "is_rfbs": bool(item.get("is_rfbs")), "pause_at": item.get("pause_at"),
                         "eligible": status in {"created", "active"} and not paused})
        if not reply["has_next"]:
            break
        next_cursor = str(reply.get("cursor") or "")
        if not next_cursor or next_cursor == cursor:
            raise ValueError("仓库分页游标无效，未保存不完整列表")
        cursor = next_cursor
    else:
        raise ValueError("仓库分页超出安全上限，未保存不完整列表")
    result = {"ok": True, "shop": shop, "items": rows, "complete": True,
              "cached": True, "checked_at": _now()}
    connection = _connect(Path(db_path) if db_path else registry_path())
    try:
        with connection:
            connection.execute("INSERT INTO warehouse_cache VALUES (?,?) ON CONFLICT(shop) DO UPDATE SET payload=excluded.payload", (shop, _json(result)))
    finally:
        connection.close()
    return result


def _scope(directory: Path, shop: str) -> dict:
    from .sku_selection import active_skus
    from .listing_offer_ids import read_offer_ids
    source = read_json(directory / "input/source.json")
    skus = [str(row.get("sku_id")) for row in active_skus(directory, source.get("skus") or [])]
    offers = read_offer_ids(directory, shop).get("offers") or {}
    return {"shop": shop, "sku_ids": skus, "offers": {sku: offers.get(sku) for sku in skus},
            "source_url": source_url(directory), "source_note_by_sku": source_specifications(directory)}


def read_config(directory: Path | str, *, shop: str, db_path=None) -> dict:
    directory, shop = Path(directory), _shop(shop)
    scope = _scope(directory, shop)
    stored = (read_json(directory / CONFIG_FILE).get("shops") or {}).get(shop) or {}
    fingerprint = _hash(scope)
    warehouse = next((row for row in read_warehouses(shop, db_path=db_path or registry_path(directory))["items"]
                      if row["warehouse_id"] == stored.get("warehouse_id")), None)
    stale = bool(stored) and stored.get("input_fingerprint") != fingerprint
    from .listing_draft import submission_editable
    intent_exists = bool((read_json(directory / INTENT_FILE).get("shops") or {}).get(shop))
    existing = list_publications(shop=shop, product_id=directory.name, db_path=db_path or registry_path(directory), limit=1)["items"]
    recorded_attempt = any(row.get("task_id") or row.get("ozon_product_id") or row.get("import_status") in
                           {"started", "processing", "unknown", "unknown_requires_readback", "submitted", "imported"} for row in existing)
    return {**stored, "shop": shop, "warehouse_id": stored.get("warehouse_id"),
            "warehouse_name": warehouse.get("name") if warehouse else stored.get("warehouse_name"),
            "stock": stored.get("stock", 100), "stock_by_sku": stored.get("stock_by_sku", {}),
            "source_note": "；".join(dict.fromkeys(scope["source_note_by_sku"].values()))[:500] or stored.get("source_note", ""),
            "source_note_by_sku": scope["source_note_by_sku"],
            "source_note_auto": bool(scope["source_note_by_sku"]), "source_url": scope["source_url"],
            "sku_ids": scope["sku_ids"], "saved": bool(stored) and not stale,
            "stale": stale, "eligible": bool(warehouse and warehouse.get("eligible")),
            "frozen": intent_exists or recorded_attempt or not submission_editable(directory),
            "input_fingerprint": fingerprint}


def save_config(directory: Path | str, *, shop: str, warehouse_id: Any, stock: int = 100,
                stock_by_sku: Mapping[str, Any] | None = None, source_note: str = "", db_path=None) -> dict:
    directory, shop = Path(directory), _shop(shop)
    with product_edit_lock(directory):
        from .listing_form import _require_editable
        _require_editable(directory)
        if read_config(directory, shop=shop, db_path=db_path).get("frozen"):
            raise ValueError("此商品已封存提交范围，仓库库存及备注不能修改；继续库存仅使用已确认快照")
        scope = _scope(directory, shop)
        if not scope["sku_ids"] or any(not value for value in scope["offers"].values()):
            raise ValueError("请先选择规格并保存每个规格的店铺货号")
        warehouse_id = str(_integer(warehouse_id, "仓库 ID", positive=True))
        cache = read_warehouses(shop, db_path=db_path or registry_path(directory))
        warehouse = next((row for row in cache["items"] if row["warehouse_id"] == warehouse_id), None)
        if not cache["complete"] or not warehouse or not warehouse.get("eligible"):
            raise ValueError("请刷新该店铺的真实仓库并明确选择可用仓库；暂停仓库不可选择")
        stock = _integer(stock, "库存")
        if stock > 1_000_000:
            raise ValueError("库存超出工作台安全上限")
        overrides = {str(key): _integer(value, "规格库存") for key, value in (stock_by_sku or {}).items()}
        if set(overrides) - set(scope["sku_ids"]) or any(value > 1_000_000 for value in overrides.values()):
            raise ValueError("规格库存只能填写当前所选规格，且不得超出安全上限")
        if not isinstance(source_note, str) or len(source_note) > 500:
            raise ValueError("员工备注最多 500 字")
        saved = {"shop": shop, "warehouse_id": warehouse_id, "warehouse_name": warehouse["name"],
                 "stock": stock, "stock_by_sku": overrides,
                 "source_note": "；".join(dict.fromkeys(scope["source_note_by_sku"].values()))[:500] or source_note,
                 "source_note_by_sku": scope["source_note_by_sku"],
                 "input_fingerprint": _hash(scope), "saved_at": _now()}
        body = read_json(directory / CONFIG_FILE)
        body.setdefault("shops", {})[shop] = saved
        write_json(directory / CONFIG_FILE, body)
        return read_config(directory, shop=shop, db_path=db_path)


def seal_config(directory: Path, *, shop: str, payload: Mapping, db_path=None) -> dict:
    config = read_config(directory, shop=shop, db_path=db_path)
    if not config["saved"] or not config["eligible"] or not config["warehouse_id"]:
        raise ValueError("请保存当前店铺、规格和货号的可用仓库及库存，再确认提交")
    scope = _scope(directory, shop)
    actual = {str(row.get("source_sku_id")): str(row.get("offer_id")) for row in payload.get("variants") or []}
    if actual != scope["offers"]:
        raise ValueError("将提交的货号与已确认库存范围不一致，请重新保存配置")
    intent = {**config, "product_id": directory.name, "offers": actual,
              "sealed_at": _now(), "confirmed_action": "SUBMIT", "stock_semantics": "available_excluding_reserved"}
    intent["intent_sha256"] = _hash(intent)
    body = read_json(directory / INTENT_FILE)
    body.setdefault("shops", {})[shop] = intent
    write_json(directory / INTENT_FILE, body)
    return intent


def _sealed(directory: Path, shop: str) -> dict:
    intent = (read_json(directory / INTENT_FILE).get("shops") or {}).get(shop) or {}
    if intent and intent.get("intent_sha256") != _hash({key: value for key, value in intent.items() if key != "intent_sha256"}):
        raise ValueError("库存提交快照已变化，禁止自动写入")
    if intent and (intent.get("shop") != shop or intent.get("product_id") != directory.name):
        raise ValueError("库存提交快照不属于当前商品店铺")
    return intent


def _row(connection, shop: str, offer: str) -> dict:
    found = connection.execute("SELECT payload FROM listing_publications WHERE shop=? AND offer_id=?", (shop, offer)).fetchone()
    return json.loads(found["payload"]) if found else {}


def _save(connection, row: dict) -> None:
    row["updated_at"] = _now()
    connection.execute("INSERT INTO listing_publications VALUES (?,?,?,?,?) ON CONFLICT(shop,offer_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                       (row["shop"], row["offer_id"], row["product_id"], _json(row), row["updated_at"]))


def _event(connection, row: dict, kind: str, state: str, *, details=None, event_id=None) -> None:
    stamp = _now()
    event = {"id": event_id or uuid.uuid4().hex, "kind": kind, "state": state,
             "started_at": stamp, "finished_at": None if state == "started" else stamp,
             "details": details or {}}
    connection.execute("INSERT INTO publication_attempts VALUES (?,?,?,?,?,?)",
                       (event["id"], row["shop"], row["offer_id"], kind, state, _json(event)))


def record_import_attempt(directory: Path | str, shop: str, payload: Mapping, *, state: str,
                          task_id=None, error_codes=None, db_path=None) -> None:
    directory, shop = Path(directory), _shop(shop)
    intent = _sealed(directory, shop)
    connection = _connect(Path(db_path) if db_path else registry_path(directory))
    try:
        with connection:
            for variant in payload.get("variants") or []:
                offer, sku = str(variant.get("offer_id") or ""), str(variant.get("source_sku_id") or "")
                if not offer or not sku:
                    raise ValueError("实际提交的货号或来源规格缺失，不能登记或提交")
                row = _row(connection, shop, offer)
                if row and (row["product_id"] != directory.name or row["source_sku_id"] != sku):
                    raise ValueError("此店铺货号已绑定其他来源商品，不能覆盖上架记录")
                configured = bool(intent and intent.get("offers", {}).get(sku) == offer)
                row = {**row, "shop": shop, "offer_id": offer, "product_id": directory.name,
                       "source_sku_id": sku, "source_url": source_url(directory),
                       "source_note": (intent.get("source_note_by_sku", {}).get(sku, "" if intent.get("source_note_auto") else intent.get("source_note", ""))
                                       if configured else row.get("source_note") or source_specifications(directory).get(sku, "")),
                       "import_status": state, "task_id": str(task_id) if task_id else row.get("task_id"),
                       "stock_status": row.get("stock_status") or ("pending_price" if configured else "not_configured")}
                if configured:
                    row.update(warehouse_id=intent["warehouse_id"], warehouse_name=intent["warehouse_name"],
                               stock=intent["stock_by_sku"].get(sku, intent["stock"]), intent_sha256=intent["intent_sha256"])
                _save(connection, row)
                _event(connection, row, "import", state, details={"task_id": row.get("task_id"),
                       "request_sha256": _hash(payload), "error_codes": list(error_codes or [])[:10]})
    finally:
        connection.close()


def record_import_observation(directory: Path | str, shop: str, *, sku_id: str, offer_id: str | None,
                              task_id=None, ozon_product_id=None, status=None, errors=(), db_path=None) -> None:
    if not offer_id or sku_id == "*":
        return
    directory, shop = Path(directory), _shop(shop)
    path = Path(db_path) if db_path else registry_path(directory)
    if not (str(task_id or "").isdigit() and int(task_id) > 0) and not ozon_product_id:
        existing = _connect(path, readonly=True)
        try:
            if not existing or not _row(existing, shop, str(offer_id)):
                return  # Local/dry-run events are not an actual listing history.
        finally:
            if existing:
                existing.close()
    connection = _connect(path)
    try:
        with connection:
            row = _row(connection, shop, str(offer_id))
            if row and (row["product_id"] != directory.name or row["source_sku_id"] != str(sku_id)):
                raise ValueError("回读货号与上架记录的来源绑定不一致")
            row = {**row, "shop": shop, "offer_id": str(offer_id), "product_id": directory.name,
                   "source_sku_id": str(sku_id), "source_url": source_url(directory),
                   "source_note": row.get("source_note") or source_specifications(directory).get(str(sku_id), ""),
                   "import_status": status or row.get("import_status", "unknown"),
                   "stock_status": row.get("stock_status", "not_configured")}
            if task_id:
                row["task_id"] = str(task_id)
            if ozon_product_id:
                row["ozon_product_id"] = str(ozon_product_id)
            codes = [str(error.get("code") or "IMPORT_ERROR") for error in errors if isinstance(error, Mapping)]
            row["import_error_codes"] = codes
            _save(connection, row)
            _event(connection, row, "import_status", row["import_status"], details={"task_id": row.get("task_id"), "error_codes": codes})
    finally:
        connection.close()


def list_publications(*, shop=None, q="", product_id=None, limit=30, offset=0, db_path=None) -> dict:
    if shop is not None:
        shop = _shop(shop)
    if len(q) > 100 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100 or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
        raise ValueError("记录筛选参数无效")
    clauses, arguments = [], []
    for column, value in (("shop", shop), ("product_id", product_id)):
        if value is not None:
            clauses.append(column + "=?")
            arguments.append(value)
    if q:
        clauses.append("instr(offer_id,?)>0")
        arguments.append(q)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = _connect(Path(db_path) if db_path else registry_path(), readonly=True)
    try:
        if not connection:
            return {"ok": True, "items": [], "total": 0, "limit": limit, "offset": offset, "next_offset": None}
        total = connection.execute("SELECT COUNT(*) FROM listing_publications" + where, arguments).fetchone()[0]
        records = connection.execute("SELECT payload FROM listing_publications" + where + " ORDER BY updated_at DESC,shop,offer_id LIMIT ? OFFSET ?", (*arguments, limit, offset)).fetchall()
        items = []
        for record in records:
            row = json.loads(record["payload"])
            events = connection.execute("SELECT payload FROM publication_attempts WHERE shop=? AND offer_id=? ORDER BY rowid DESC LIMIT 100", (row["shop"], row["offer_id"])).fetchall()
            row["attempts"] = [json.loads(event["payload"]) for event in events]
            items.append(row)
        return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None}
    finally:
        if connection:
            connection.close()


def _price_ready(item: Mapping) -> bool:
    statuses = item.get("statuses") or {}
    stage = str(statuses.get("status") or "").lower()
    if item.get("is_archived") or item.get("is_autoarchived") or item.get("errors") or statuses.get("status_failed"):
        return False
    if statuses.get("is_created") is not True:
        return False
    if stage == "price_sent":
        return True
    # The live schema also returns active after price_sent, not an enum. Later
    # lifecycle states need positive price evidence rather than a guessed order.
    try:
        price = float(item.get("price"))
    except (ValueError, TypeError):
        price = 0
    return stage in {"stock_sent", "sale", "active"} and (item.get("visibility_details") or {}).get("has_price") is True and math.isfinite(price) and price > 0


def _process_birth(pid: int) -> str | None:
    try:
        if os.name != "nt":
            parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return parts[19]
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation, exit_time, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in (creation, exit_time, cpu_kernel, cpu_user)]):
                return None
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    except (OSError, ValueError, IndexError):
        return None


def active_stock_requests(*, db_path=None) -> list[dict]:
    """Deployment can refuse restart while a durable stock request is running."""
    connection = _connect(Path(db_path) if db_path else registry_path(), readonly=True)
    try:
        rows = connection.execute("SELECT payload FROM stock_requests WHERE state='started'").fetchall() if connection else []
        return [json.loads(row["payload"]) for row in rows]
    finally:
        if connection:
            connection.close()


def _stock_snapshot(transport, offers: list[str], warehouse_id: str) -> dict[str, dict]:
    cursor, rows = "", {}
    for _ in range(20):
        body = {"offer_id": offers, "limit": 1000}
        if cursor:
            body["cursor"] = cursor
        reply = _read_post(transport, STOCK_READ_PATH, body)
        if not isinstance(reply, Mapping) or not isinstance(reply.get("products"), list) or not isinstance(reply.get("has_next"), bool):
            raise ValueError("库存只读响应不完整，不会盲目写入库存")
        for item in reply["products"]:
            if not isinstance(item, Mapping) or str(item.get("offer_id")) not in offers:
                raise ValueError("库存回读包含范围外货号")
            if str(item.get("warehouse_id")) != warehouse_id:
                continue
            offer = str(item["offer_id"])
            if offer in rows:
                raise ValueError("库存回读重复货号和仓库")
            values = {key: _integer(item.get(key), "回读库存") for key in ("present", "reserved", "free_stock")}
            rows[offer] = {**values, "warehouse_id": warehouse_id, "checked_at": _now(), "product_id": str(item.get("product_id") or "")}
        if not reply["has_next"]:
            # A complete empty result is normal for the first stock assignment.
            # Absence is not an observed zero and must not invent reserved units.
            for offer in offers:
                rows.setdefault(offer, {"warehouse_id": warehouse_id, "checked_at": _now(),
                                       "absence": True, "observed": False,
                                       "status": "no_entry", "query_complete": True})
            return rows
        following = str(reply.get("cursor") or "")
        if not following or following == cursor:
            raise ValueError("库存回读分页游标异常")
        cursor = following
    raise ValueError("库存回读分页超过安全上限")


def _begin_stock(path: Path, shop: str, candidates: list[dict], *, retry_unknown: bool, epoch: float,
                 account_scope: str | None = None) -> tuple[str | None, list[dict]]:
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        recent = connection.execute("SELECT * FROM stock_requests WHERE started_epoch>?", (epoch - 60,)).fetchall()
        recent = [row for row in recent if row["shop"] == shop or
                  (account_scope is not None and json.loads(row["payload"]).get("account_scope") == account_scope)]
        if len(recent) >= 80:
            return None, []
        allowed = []
        for candidate in candidates:
            row = _row(connection, shop, candidate["offer_id"])
            if row.get("stock_status") == "updated":
                continue
            if row.get("stock_status") == "running":
                last = connection.execute("SELECT payload FROM stock_requests WHERE id=?", (row.get("stock_request_id"),)).fetchone()
                previous = json.loads(last["payload"]) if last else {}
                birth = _process_birth(int(previous.get("pid") or 0))
                if previous and previous.get("process_birth") and birth == previous["process_birth"] and epoch - float(previous.get("started_epoch") or 0) < 600:
                    continue
                row["stock_status"] = "unknown"
                _save(connection, row)
                if last:
                    connection.execute("UPDATE stock_requests SET state='unknown' WHERE id=?", (previous["id"],))
            if row.get("stock_status") == "unknown" and not retry_unknown:
                continue
            account_collision = False
            for prior in recent:
                request = json.loads(prior["payload"])
                matching_pair = any(str(pair.get("offer_id")) == row["offer_id"] and str(pair.get("warehouse_id")) == row["warehouse_id"] for pair in request.get("pairs") or [])
                if matching_pair and epoch - prior["started_epoch"] < 30:
                    account_collision = True
                    break
            if account_collision:
                continue
            last_epoch = float(row.get("stock_requested_epoch") or -1_000_000)
            if epoch - last_epoch < 30:
                if row.get("stock_status") != "unknown":
                    row["stock_status"] = "rate_limited"
                    _save(connection, row)
                continue
            allowed.append(candidate)
        if not allowed:
            connection.commit()
            return None, []
        request_id = uuid.uuid4().hex
        intent = {"id": request_id, "shop": shop, "pairs": [{key: row[key] for key in ("offer_id", "warehouse_id", "stock")} for row in allowed],
                  "pid": os.getpid(), "process_birth": _process_birth(os.getpid()), "started_at": _now(), "started_epoch": epoch,
                  "state": "started", "no_automatic_retry": True}
        if account_scope:
            intent["account_scope"] = account_scope
        connection.execute("INSERT INTO stock_requests VALUES (?,?,?,?,?)", (request_id, shop, epoch, "started", _json(intent)))
        for row in allowed:
            current = _row(connection, shop, row["offer_id"])
            current.update(stock_status="running", stock_request_id=request_id, stock_requested_epoch=epoch)
            _save(connection, current)
            _event(connection, current, "stock", "started", details={"request_id": request_id, "warehouse_id": row["warehouse_id"], "stock": row["stock"]})
        connection.commit()
        return request_id, allowed
    finally:
        connection.close()


def _finish_stock(path: Path, shop: str, request_id: str, rows: list[dict], reply: Any) -> None:
    results = reply.get("result") if isinstance(reply, Mapping) else None
    expected = {(row["offer_id"], row["warehouse_id"]) for row in rows}
    seen, indexed, valid = set(), {}, isinstance(results, list)
    for item in results or []:
        if not isinstance(item, Mapping):
            valid = False
            continue
        key = (str(item.get("offer_id") or ""), str(item.get("warehouse_id") or ""))
        if key not in expected or key in seen:
            valid = False
        seen.add(key)
        indexed[key] = item
    valid = valid and seen == expected
    connection = _connect(path)
    try:
        with connection:
            states = []
            for candidate in rows:
                row = _row(connection, shop, candidate["offer_id"])
                item = indexed.get((candidate["offer_id"], candidate["warehouse_id"])) or {}
                codes = [str(error.get("code") or "STOCK_ERROR") for error in item.get("errors") or [] if isinstance(error, Mapping)]
                identity_ok = str(item.get("product_id") or "") == str(row.get("ozon_product_id") or "")
                if not valid or not identity_ok or not isinstance(item.get("updated"), bool) or not isinstance(item.get("errors"), list):
                    state = "unknown"
                elif item["updated"] is True and not item["errors"]:
                    state = "updated"
                elif item["updated"] is False and item["errors"]:
                    state = "failed"
                else:
                    state = "unknown"
                row.update(stock_status=state, stock_error_codes=codes, stock_finished_at=_now())
                _save(connection, row)
                _event(connection, row, "stock", state, details={"request_id": request_id, "error_codes": codes})
                states.append(state)
            connection.execute("UPDATE stock_requests SET state=? WHERE id=?", ("unknown" if "unknown" in states else "complete", request_id))
    finally:
        connection.close()


def _stock_result(directory: Path, shop: str, path: Path, writes: int, *, warning=None) -> dict:
    records = list_publications(shop=shop, product_id=directory.name, db_path=path, limit=100)["items"]
    configured = [row for row in records if row.get("warehouse_id")]
    states = {row.get("stock_status") for row in configured}
    if states and states <= {"updated", "readback_confirmed"} and all(row.get("stock_readback_matches") is True for row in configured):
        state = "stock_complete"
    elif states and states <= {"updated", "readback_confirmed"}:
        state = "stock_acknowledged"
    elif "unknown" in states:
        state = "stock_unknown"
    elif states & {"failed", "readback_mismatch"}:
        state = "stock_partial"
    else:
        state = "stock_pending"
    return {"ok": bool(configured) and state == "stock_complete", "status": state, "items": records,
            "api_writes": writes, "pending": state != "stock_complete", "warning": warning,
            "automatic_retry": False}


def continue_stocks(directory: Path | str, *, shop: str, confirm: str,
                    retry_unknown: bool = False, db_path=None, transport=None,
                    registry=None, transport_factory=None, epoch: float | None = None) -> dict:
    if confirm != "UPDATE_STOCK":
        raise ValueError("库存写入须明确确认 UPDATE_STOCK")
    directory, shop = Path(directory), _shop(shop)
    path = Path(db_path) if db_path else registry_path(directory)
    with product_edit_lock(directory):
        records = list_publications(shop=shop, product_id=directory.name, db_path=path, limit=100)["items"]
        configured = [row for row in records if row.get("warehouse_id")]
        if not configured:
            raise ValueError("没有已提交且已确认的仓库库存快照，不能从 GET 默认值写库存")
        intent = _sealed(directory, shop)
        if not intent or any(row.get("intent_sha256") != intent["intent_sha256"] or intent["offers"].get(row["source_sku_id"]) != row["offer_id"] for row in configured):
            raise ValueError("库存记录不属于本次已确认的提交快照")
        remaining = [row for row in configured if row.get("stock_status") not in {"updated", "readback_confirmed"} or row.get("stock_readback_matches") is not True]
        if not remaining:
            return _stock_result(directory, shop, path, 0)
        transport = transport or transport_for_shop(shop, registry=registry, transport_factory=transport_factory)
        # Revalidate the real warehouse before every explicit stock action.
        warehouses = refresh_warehouses(shop, db_path=path, transport=transport)
        target = next((row for row in warehouses["items"] if row["warehouse_id"] == intent["warehouse_id"]), None)
        if not target or not target["eligible"]:
            raise ValueError("目标真实仓库已暂停或不可用，未写入库存")
        from .ozon_status import confirm_product
        tasks = {str(row.get("task_id") or "") for row in remaining}
        if any(not task.isdigit() or int(task) <= 0 for task in tasks):
            return _stock_result(directory, shop, path, 0, warning="导入结果未知，先明确回读任务，不会重复导入")
        confirm_product(directory, shop, max_attempts=1, interval_seconds=0,
                        transport_factory=lambda credentials: transport, registry_path=registry,
                        publication_db_path=path)
        records = list_publications(shop=shop, product_id=directory.name, db_path=path, limit=100)["items"]
        remaining = [row for row in records if row.get("warehouse_id") and
                     (row.get("stock_status") not in {"updated", "readback_confirmed"} or row.get("stock_readback_matches") is not True)]
        imported = [row for row in remaining if row.get("import_status") == "imported" and not row.get("import_error_codes") and str(row.get("ozon_product_id") or "").isdigit() and int(row["ozon_product_id"]) > 0]
        if not imported:
            return _stock_result(directory, shop, path, 0, warning="导入尚未确认成功，库存待导入及价格就绪")
        reply = _read_post(transport, INFO_PATH, {"offer_id": [row["offer_id"] for row in imported]})
        items = reply.get("items") if isinstance(reply, Mapping) else None
        expected = {row["offer_id"] for row in imported}
        if not isinstance(items, list) or any(not isinstance(row, Mapping) for row in items) or len(items) != len(expected) or {str(row.get("offer_id")) for row in items} != expected:
            raise ValueError("商品状态回读不完整，未写入库存")
        indexed = {str(row["offer_id"]): row for row in items}
        candidates = []
        connection = _connect(path)
        try:
            with connection:
                for row in imported:
                    info = indexed[row["offer_id"]]
                    identity_ok = str(info.get("id") or "") == row["ozon_product_id"]
                    ready = identity_ok and _price_ready(info)
                    row["product_status"] = str((info.get("statuses") or {}).get("status") or "unknown")
                    row["product_status_checked_at"] = _now()
                    row["price_ready"] = ready
                    if row.get("stock_status") not in {"unknown", "running", "failed", "rate_limited", "updated", "readback_confirmed"}:
                        row["stock_status"] = "ready" if ready else "pending_price"
                    _save(connection, row)
                    _event(connection, row, "product_status", row["product_status"], details={"price_ready": ready})
                    if ready:
                        candidates.append(row)
        finally:
            connection.close()
        if not candidates:
            return _stock_result(directory, shop, path, 0, warning="商品价格尚未 price_sent 或后续已定价状态")
        if len(candidates) > 100:
            raise ValueError("本次库存范围超过 100 商品-仓库对，请拆分确认")
        before = _stock_snapshot(transport, [row["offer_id"] for row in candidates], intent["warehouse_id"])
        if any(row["offer_id"] not in before or (not before[row["offer_id"]].get("absence") and before[row["offer_id"]].get("product_id") != row["ozon_product_id"]) for row in candidates):
            return _stock_result(directory, shop, path, 0, warning="真实仓库库存/预留数量未能完整回读，未盲写库存")
        connection = _connect(path)
        try:
            with connection:
                for row in candidates:
                    current = _row(connection, shop, row["offer_id"])
                    current["stock_readback"] = before[row["offer_id"]]
                    current["stock_readback_matches"] = before[row["offer_id"]].get("free_stock") == row["stock"] and not before[row["offer_id"]].get("absence")
                    if current.get("stock_status") == "unknown" and current["stock_readback_matches"]:
                        current["stock_status"] = "readback_confirmed"
                        _event(connection, current, "stock_readback", "resolved_unknown", details=before[row["offer_id"]])
                    _save(connection, current)
        finally:
            connection.close()
        current_records = {row["offer_id"]: row for row in list_publications(shop=shop, product_id=directory.name, db_path=path, limit=100)["items"]}
        candidates = [row for row in candidates if current_records[row["offer_id"]].get("stock_status") not in {"updated", "readback_confirmed"}]
        candidates = [row for row in candidates if not before[row["offer_id"]].get("absence") or
                      not current_records[row["offer_id"]].get("stock_request_id") or
                      (retry_unknown and current_records[row["offer_id"]].get("stock_status") == "unknown") or
                      current_records[row["offer_id"]].get("stock_status") in {"failed", "rate_limited"}]
        if not candidates:
            return _stock_result(directory, shop, path, 0)
        client_id = getattr(getattr(transport, "credentials", None), "client_id", None)
        account_scope = _hash({"seller_client_id": str(client_id)}) if client_id else None
        request_id, allowed = _begin_stock(path, shop, candidates, retry_unknown=retry_unknown, epoch=time.time() if epoch is None else epoch,
                                          account_scope=account_scope)
        if not request_id:
            return _stock_result(directory, shop, path, 0, warning="未自动重试未知写入；运行中或限流项目请稍后明确继续")
        from .status import load_status, save_status
        current_status = load_status(directory)
        current_status["api_write_count"] = int(current_status.get("api_write_count") or 0) + 1
        save_status(directory, current_status)
        try:
            reply = transport.post(STOCK_PATH, {"stocks": [{"offer_id": row["offer_id"], "stock": row["stock"], "warehouse_id": int(row["warehouse_id"])} for row in allowed]})
        except Exception:
            _finish_stock(path, shop, request_id, allowed, None)
            return _stock_result(directory, shop, path, 1, warning="库存请求结果未知，请先回读；明确确认后才可重试，不会重复导入")
        _finish_stock(path, shop, request_id, allowed, reply)
        try:
            after = _stock_snapshot(transport, [row["offer_id"] for row in allowed], intent["warehouse_id"])
        except Exception:
            after = {}
        connection = _connect(path)
        try:
            with connection:
                for attempted in allowed:
                    row = _row(connection, shop, attempted["offer_id"])
                    observed = after.get(row["offer_id"])
                    if observed and (observed.get("absence") or observed.get("product_id") == row["ozon_product_id"]):
                        row["stock_readback"] = observed
                        row["stock_readback_matches"] = observed.get("free_stock") == row["stock"] and not observed.get("absence")
                        # Inventory propagation can be delayed. Keep API receipt
                        # and observation separate, never claim buyer visibility.
                        _save(connection, row)
                        _event(connection, row, "stock_readback", "matches" if row["stock_readback_matches"] else "pending_propagation", details=observed)
        finally:
            connection.close()
        return _stock_result(directory, shop, path, 1)
