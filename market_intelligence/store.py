"""SQLite storage for Seerfar/Ozon market observations.

Keep source-native values and field names. A Seerfar search-heat value and an
Ozon client-count value are different measures, even when attached to the same
Russian query.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SOURCES = {"seerfar", "ozon_seller_api"}
DATASETS = {"categories", "keywords", "products"}
METHODS = {"browser_extension", "official_api", "file_import"}
PERIOD_KINDS = {"calendar_month", "rolling_30d"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _first(row: dict[str, Any], *keys: str) -> str:
    return next((_text(row.get(key)) for key in keys if _text(row.get(key))), "")


def _entity_key(source: str, dataset: str, row: dict[str, Any]) -> str:
    if dataset == "categories":
        return _first(row, "categoryId", "category_id", "id", "类目ID", "类目", "category", "label", "name")
    if dataset == "keywords":
        return _first(row, "query", "关键词", "keyword", "text").casefold()
    return _first(row, "sku", "SKU", "product_id", "商品ID", "商品链接", "url")


def _category_key(dataset: str, row: dict[str, Any], fallback: str | None) -> str:
    if dataset == "categories":
        return _first(row, "categoryId", "category_id", "id", "类目ID", "类目", "category", "label", "name")
    return _first(row, "categoryId", "category_id", "类目ID", "类目", "category") or _text(fallback)


def connect(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingest_batches (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            dataset TEXT NOT NULL,
            capture_method TEXT NOT NULL,
            period TEXT NOT NULL DEFAULT '',
            period_kind TEXT NOT NULL DEFAULT 'calendar_month',
            page_url TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL UNIQUE,
            record_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY,
            batch_id INTEGER NOT NULL REFERENCES ingest_batches(id),
            source TEXT NOT NULL,
            dataset TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            category_key TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            period_kind TEXT NOT NULL DEFAULT 'calendar_month',
            captured_at TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            row_hash TEXT NOT NULL,
            UNIQUE(source, dataset, entity_key, category_key, period, row_hash)
        );
        CREATE INDEX IF NOT EXISTS ix_observations_lookup
            ON observations(dataset, source, category_key, period, entity_key);
        CREATE INDEX IF NOT EXISTS ix_observations_batch ON observations(batch_id);
        CREATE TABLE IF NOT EXISTS category_mappings (
            seerfar_category_key TEXT PRIMARY KEY,
            ozon_description_category_id INTEGER NOT NULL,
            ozon_type_id INTEGER NOT NULL,
            verified_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        );
        """
    )
    # Existing databases predate period_kind. Their historical YYYY-MM values
    # represented calendar-month reports, so migrate without rewriting rows.
    for table in ("ingest_batches", "observations"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "period_kind" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN period_kind TEXT NOT NULL DEFAULT 'calendar_month'")
    return conn


def ingest_snapshot(path: Path | str, *, source: str, dataset: str, capture_method: str,
                    period: str, page_url: str, captured_at: str, records: list[dict[str, Any]],
                    category_key: str | None = None, period_kind: str = "calendar_month") -> dict[str, Any]:
    if source not in SOURCES or dataset not in DATASETS or capture_method not in METHODS:
        raise ValueError("不支持的数据来源、数据集或采集方式")
    if period_kind not in PERIOD_KINDS:
        raise ValueError("统计周期口径仅支持 calendar_month 或 rolling_30d")
    if source == "ozon_seller_api" and capture_method == "browser_extension":
        raise ValueError("Ozon 官方 API 数据必须由 API 适配器导入，不能由网页采集冒充")
    if capture_method == "browser_extension" and source != "seerfar":
        raise ValueError("浏览器插件仅采集 Seerfar 可见报表")
    if capture_method == "browser_extension":
        parsed = urlparse(page_url)
        if parsed.scheme != "https" or parsed.hostname not in {"seerfar.cn", "www.seerfar.cn"}:
            raise ValueError("插件采集来源必须是 Seerfar HTTPS 页面")
    if not records or len(records) > 200:
        raise ValueError("每批须含 1–200 行数据")
    if not all(isinstance(row, dict) for row in records):
        raise ValueError("每行必须是 JSON 对象")
    if period and (len(period) != 7 or period[4] != "-" or not period[:4].isdigit()
                   or not period[5:].isdigit() or not 1 <= int(period[5:]) <= 12):
        raise ValueError("报表月份必须是 YYYY-MM")
    if period_kind == "rolling_30d" and not period:
        raise ValueError("最近 30 天滚动数据须填写采集月份 YYYY-MM")
    if capture_method == "browser_extension" and not period:
        raise ValueError("插件采集须注明报表月份，避免错把采集日当统计月")
    if len(page_url) > 2048 or len(captured_at) > 64:
        raise ValueError("来源 URL 或时间过长")
    normalized = []
    for row in records:
        entity = _entity_key(source, dataset, row)
        if not entity:
            raise ValueError("存在缺少类目、关键词或 SKU/商品 ID 的数据行")
        normalized.append((entity, _category_key(dataset, row, category_key), _json(row)))
    # Endpoints such as Ozon top queries do not expose an explicit reporting
    # month. Keep one observation per capture day even if figures happen to be
    # unchanged; otherwise a later month's identical figure vanishes forever.
    dedupe_bucket = period or (captured_at or _now())[:10]
    # Keep the legacy calendar-month hashes stable for existing databases.
    # Rolling observations with the same visible numbers are a different grain.
    batch_key = [source, dataset, capture_method, dedupe_bucket, page_url, normalized]
    if period_kind != "calendar_month":
        batch_key.append(period_kind)
    batch_hash = hashlib.sha256(_json(batch_key).encode()).hexdigest()
    with closing(connect(path)) as conn, conn:
        prior = conn.execute("SELECT id, record_count FROM ingest_batches WHERE payload_hash=?", (batch_hash,)).fetchone()
        if prior:
            return {"batch_id": prior["id"], "received": len(records), "inserted": 0, "duplicate_batch": True}
        cur = conn.execute(
            "INSERT INTO ingest_batches(source,dataset,capture_method,period,period_kind,page_url,captured_at,imported_at,payload_hash,record_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source, dataset, capture_method, period, period_kind, page_url, captured_at or _now(), _now(), batch_hash, len(records)),
        )
        inserted = 0
        for entity, category, raw in normalized:
            row_key = [dedupe_bucket, raw]
            if period_kind != "calendar_month":
                row_key.append(period_kind)
            row_hash = hashlib.sha256(_json(row_key).encode()).hexdigest()
            result = conn.execute(
                "INSERT OR IGNORE INTO observations(batch_id,source,dataset,entity_key,category_key,period,period_kind,captured_at,raw_json,row_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, source, dataset, entity, category, period, period_kind, captured_at or _now(), raw, row_hash),
            )
            inserted += result.rowcount
        return {"batch_id": cur.lastrowid, "received": len(records), "inserted": inserted, "duplicate_batch": False}


def list_observations(path: Path | str, *, dataset: str, source: str | None = None,
                      category_key: str | None = None, period: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if dataset not in DATASETS or (source is not None and source not in SOURCES):
        raise ValueError("无效的数据集或来源")
    clauses = ["dataset=?"]
    args: list[Any] = [dataset]
    for column, value in (("source", source), ("category_key", category_key), ("period", period)):
        if value is not None:
            clauses.append(f"{column}=?")
            args.append(value)
    args.append(limit)
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT id,batch_id,source,dataset,entity_key,category_key,period,period_kind,captured_at,raw_json "
            f"FROM observations WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", args,
        ).fetchall()
    return [{key: value for key, value in dict(row).items() if key != "raw_json"}
            | {"raw": json.loads(row["raw_json"])} for row in rows]


def database_stats(path: Path | str) -> dict[str, Any]:
    with closing(connect(path)) as conn:
        batches = conn.execute("SELECT count(*) FROM ingest_batches").fetchone()[0]
        counts = conn.execute("SELECT source,dataset,count(*) AS rows FROM observations GROUP BY source,dataset").fetchall()
    return {"batches": batches, "counts": [dict(row) for row in counts]}
