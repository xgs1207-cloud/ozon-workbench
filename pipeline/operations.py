"""Isolated, read-only post-listing monitoring with persistent provenance.

No supplier files, keyword libraries or Ozon cards are changed by this module.
Daily analytics, rolling query windows and current listing health are deliberately
different records. Missing permission/data is never represented as a zero.
API contracts: official Seller OpenAPI snapshot verified 2026-10-03.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping
import urllib.error
import uuid

INFO_PATH = "/v3/product/info/list"
METRICS_PATH = "/v1/analytics/data"
QUERIES_PATH = "/v1/analytics/product-queries/details"
BASIC_METRICS = ("revenue", "ordered_units")
TRAFFIC_METRICS = BASIC_METRICS + (
    "hits_view_search", "hits_view_pdp", "hits_tocart_search", "hits_tocart_pdp",
    "session_view_search", "session_view_pdp",
)
SCHEMA_VERSION = 1
MAX_QUERY_PAGES = 3
MAX_JOB_ATTEMPTS = 3
ACTIVE_STATES = ("queued", "retry_wait", "running")


def _iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key(value: Any, label: str, *, shop: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label}无效")
    if shop and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value):
        raise ValueError("店铺格式无效")
    return value


def _days(value: Any) -> int:
    if isinstance(value, bool) or value not in (7, 30):
        raise ValueError("监测周期只支持 7 或 30 天")
    return int(value)


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else round(number, 6)


def _identifier(value: Any) -> str | None:
    value = str(value or "")
    return value if re.fullmatch(r"[1-9][0-9]{0,19}", value) else None


def _text(value: Any, limit: int = 500) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def safe_error(error: Exception) -> dict:
    """Never persist provider body/message: either could echo credentials."""
    # The real Seller transport wraps URLError/HTTPError in OzonHttpError.
    # Inspect type/status only, never reason/message/body. Nearest HTTP status
    # wins over a nested timeout, and malformed/cyclic chains cannot hang work.
    status, network, current, seen = None, False, error, set()
    for _ in range(6):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        seen.add(id(current))
        if status is None:
            for field in ("status", "http_status", "code"):
                candidate = getattr(current, field, None)
                if isinstance(candidate, int) and not isinstance(candidate, bool) and 100 <= candidate <= 599:
                    status = candidate
                    break
        network |= isinstance(current, (TimeoutError, ConnectionError)) or (
            isinstance(current, urllib.error.URLError) and not isinstance(current, urllib.error.HTTPError))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    codes = {400: "invalid_request", 401: "unauthorized", 403: "permission_required",
             404: "not_found", 429: "rate_limited"}
    code = codes.get(status, "provider_unavailable" if isinstance(status, int) and status >= 500 else "request_failed")
    if status is None and network:
        code = "network_unavailable"
    return {"code": code, "http_status": status if isinstance(status, int) else None,
            "retryable": code in {"rate_limited", "network_unavailable", "provider_unavailable"}}


class Store:
    def __init__(self, path: Path | str, *, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    shop TEXT NOT NULL, offer_id TEXT NOT NULL, payload TEXT NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(shop,offer_id));
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, shop TEXT NOT NULL, offer_id TEXT NOT NULL,
                    kind TEXT NOT NULL, observed_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_scope ON snapshots(shop,offer_id,kind,id DESC);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, shop TEXT NOT NULL, offer_id TEXT NOT NULL,
                    days INTEGER NOT NULL, include_traffic INTEGER NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    next_run_at REAL NOT NULL, lease_until REAL,
                    worker_id TEXT, result TEXT, error_code TEXT);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,next_run_at,lease_until);
                CREATE TABLE IF NOT EXISTS schedules (
                    shop TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                    days INTEGER NOT NULL, interval_hours INTEGER NOT NULL,
                    include_traffic INTEGER NOT NULL, next_run_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS analytics_budget (
                    account_hash TEXT PRIMARY KEY, utc_day TEXT NOT NULL,
                    calls INTEGER NOT NULL, last_call_at REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def discover(self, rows) -> dict:
        """Import ONLY ledger identities; do not reset API-observed state."""
        discovered = updated = conflicts = 0
        now = self.clock()
        with self._db() as db:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                shop = _key(row.get("shop"), "店铺", shop=True)
                offer = _key(row.get("offer_id"), "货号")
                stored = db.execute("SELECT payload FROM products WHERE shop=? AND offer_id=?", (shop, offer)).fetchone()
                old = json.loads(stored["payload"]) if stored else {}
                if old and any(old.get(k) and row.get(k) and old[k] != row[k] for k in ("product_id", "source_sku_id")):
                    conflicts += 1
                    continue
                product = dict(old)
                for field in ("product_id", "source_sku_id", "source_url", "source_note", "import_status", "stock_status", "warehouse_name"):
                    if field in row:
                        product[field] = _text(row[field], 2000)
                for field in ("ozon_product_id", "ozon_sku", "warehouse_id"):
                    candidate = _identifier(row.get(field))
                    if candidate:
                        product[field] = candidate
                product.update(shop=shop, offer_id=offer, discovered_at=old.get("discovered_at", _iso(now)), ledger_updated_at=_text(row.get("updated_at")), monitoring="read_only")
                db.execute("INSERT INTO products VALUES(?,?,?,?) ON CONFLICT(shop,offer_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (shop, offer, _json(product), now))
                discovered += not bool(old)
                updated += bool(old)
        return {"discovered": discovered, "updated": updated, "conflicts": conflicts}

    def list_products(self, shop=None, q="", limit=30, offset=0) -> dict:
        if shop is not None:
            shop = _key(shop, "店铺", shop=True)
        if not isinstance(q, str) or len(q) > 100 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100 or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("筛选参数无效")
        clauses, args = [], []
        if shop is not None:
            clauses.append("shop=?")
            args.append(shop)
        if q:
            # Literal matching, including '%' and '_'; no raw JSON-key or
            # unrelated metadata matches and no interpolation of user text.
            fields = ("offer_id", "COALESCE(json_extract(payload,'$.product_id'),'')",
                      "COALESCE(json_extract(payload,'$.source_note'),'')",
                      "COALESCE(json_extract(payload,'$.name'),'')")
            clauses.append("(" + " OR ".join("instr(" + field + ",?)>0" for field in fields) + ")")
            args.extend([q] * len(fields))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as db:
            total = db.execute("SELECT count(*) FROM products" + where, args).fetchone()[0]
            rows = db.execute("SELECT payload FROM products" + where + " ORDER BY updated_at DESC,shop,offer_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [json.loads(r[0]) for r in rows], "total": total, "limit": limit, "offset": offset, "next_offset": offset + limit if offset + limit < total else None}

    def product(self, shop, offer_id) -> dict:
        shop, offer_id = _key(shop, "店铺", shop=True), _key(offer_id, "货号")
        with self._db() as db:
            row = db.execute("SELECT payload FROM products WHERE shop=? AND offer_id=?", (shop, offer_id)).fetchone()
        if row is None:
            raise ValueError("货号未登记在该店铺的上架记录中")
        return json.loads(row[0])

    def _observe_product(self, shop, offer_id, values):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM products WHERE shop=? AND offer_id=?", (shop, offer_id)).fetchone()
            if row is None:
                raise ValueError("货号未登记")
            product = {**json.loads(row[0]), **values, "last_synced_at": _iso(self.clock())}
            db.execute("UPDATE products SET payload=?,updated_at=? WHERE shop=? AND offer_id=?", (_json(product), self.clock(), shop, offer_id))

    def record_snapshot(self, shop, offer_id, kind, status, *, endpoint=None, data=None, date_from=None, date_to=None, error_code=None, retry_at=None) -> dict:
        self.product(shop, offer_id)
        if kind not in {"health", "metrics", "queries"}:
            raise ValueError("快照类型无效")
        if status not in {"available", "no_data", "permission_required", "deferred", "error", "not_ready"}:
            raise ValueError("快照状态无效")
        now = self.clock()
        snapshot = {"kind": kind, "status": status, "endpoint": endpoint, "fetched_at": _iso(now), "date_from": date_from, "date_to": date_to, "data": data or {}, "error_code": error_code, "retry_at": retry_at}
        with self._db() as db:
            db.execute("INSERT INTO snapshots(shop,offer_id,kind,observed_at,payload) VALUES(?,?,?,?,?)", (shop, offer_id, kind, now, _json(snapshot)))
            # Bounded per-product history; no image/video bytes enter this database.
            db.execute("DELETE FROM snapshots WHERE shop=? AND offer_id=? AND id NOT IN (SELECT id FROM snapshots WHERE shop=? AND offer_id=? ORDER BY id DESC LIMIT 300)", (shop, offer_id, shop, offer_id))
        return snapshot

    def detail(self, shop, offer_id) -> dict:
        product = self.product(shop, offer_id)
        with self._db() as db:
            snapshots = [json.loads(r[0]) for r in db.execute("SELECT payload FROM snapshots WHERE shop=? AND offer_id=? ORDER BY id DESC LIMIT 300", (shop, offer_id))]
            jobs = [self._job(r) for r in db.execute("SELECT * FROM jobs WHERE shop=? AND offer_id=? ORDER BY created_at DESC LIMIT 30", (shop, offer_id))]
        latest = {kind: next((s for s in snapshots if s["kind"] == kind), None) for kind in ("health", "metrics", "queries")}
        query = latest["queries"]
        return {"product": product, "snapshots": snapshots, "queries": ({**query["data"], **{key: query[key] for key in ("status", "date_from", "date_to", "fetched_at", "error_code")}} if query else {"items": [], "status": "not_ready"}), "diagnostics": diagnose(latest), "jobs": jobs}

    @staticmethod
    def _job(row) -> dict:
        result = dict(row)
        result["include_traffic"] = bool(result["include_traffic"])
        result["result"] = json.loads(result["result"]) if result.get("result") else None
        for field in ("created_at", "updated_at", "next_run_at", "lease_until"):
            result[field] = _iso(result[field]) if result.get(field) is not None else None
        # Lease ownership is executor-only, not a credential.
        return result

    def enqueue(self, shop, offer_id, days=7, include_traffic=True) -> dict:
        self.product(shop, offer_id)
        days = _days(days)
        if not isinstance(include_traffic, bool):
            raise ValueError("流量指标选项必须为布尔值")
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT * FROM jobs WHERE shop=? AND offer_id=? AND status IN ('queued','retry_wait','running') ORDER BY created_at LIMIT 1", (shop, offer_id)).fetchone()
            if active:
                job = self._job(active)
                job["deduplicated"] = True
                return job
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','retry_wait','running')").fetchone()[0] >= 500:
                raise ValueError("监测队列已满，请等待已有任务完成")
            identifier = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,shop,offer_id,days,include_traffic,status,created_at,updated_at,next_run_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (identifier, shop, offer_id, days, int(include_traffic), now, now, now))
            job = self._job(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())
        return job

    def jobs(self, shop=None, limit=30) -> list:
        if shop is not None:
            _key(shop, "店铺", shop=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("任务数量无效")
        with self._db() as db:
            rows = db.execute("SELECT * FROM jobs" + (" WHERE shop=?" if shop else "") + " ORDER BY created_at DESC LIMIT ?", (shop, limit) if shop else (limit,)).fetchall()
        return [self._job(r) for r in rows]

    def claim_job(self, worker_id, lease_seconds=300) -> dict | None:
        _key(worker_id, "执行器")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 1800:
            raise ValueError("租约无效")
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='failed',error_code='attempts_exhausted',worker_id=NULL,lease_until=NULL,updated_at=? WHERE attempts>=? AND ((status='running' AND lease_until<=?) OR status IN ('queued','retry_wait'))", (now, MAX_JOB_ATTEMPTS, now))
            row = db.execute("SELECT * FROM jobs WHERE attempts<? AND ((status IN ('queued','retry_wait') AND next_run_at<=?) OR (status='running' AND lease_until<=?)) ORDER BY next_run_at,created_at LIMIT 1", (MAX_JOB_ATTEMPTS, now, now)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',worker_id=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?", (worker_id, now + lease_seconds, now, row["id"]))
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def _finish(self, job_id, worker_id, status, *, result=None, error_code=None, retry_at=None, refund_attempt=False):
        now = self.clock()
        with self._db() as db:
            cursor = db.execute("UPDATE jobs SET status=?,result=?,error_code=?,next_run_at=?,updated_at=?,worker_id=NULL,lease_until=NULL,attempts=attempts-? WHERE id=? AND worker_id=? AND status='running' AND lease_until>?", (status, _json(result) if result is not None else None, error_code, retry_at or now, now, int(refund_attempt), job_id, worker_id, now))
            if cursor.rowcount != 1:
                raise ValueError("任务租约失效，禁止旧执行器覆盖结果")

    def complete_job(self, job_id, worker_id, result=None):
        self._finish(job_id, worker_id, "partial" if result and result.get("partial") else "completed", result=result)

    def fail_job(self, job_id, worker_id, safe_error="sync_failed", retryable=False, *, retry_at=None):
        if safe_error not in {"sync_failed", "request_failed", "unauthorized", "permission_required", "not_found", "invalid_request", "rate_limited", "network_unavailable", "provider_unavailable", "invalid_response", "no_credentials", "shop_disabled"}:
            safe_error = "sync_failed"
        with self._db() as db:
            row = db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        retry = bool(retryable and row and row[0] < MAX_JOB_ATTEMPTS)
        backoff = self.clock() + (30 * (2 ** (max(1, row[0]) - 1))) if retry else None
        self._finish(job_id, worker_id, "retry_wait" if retry else "failed", error_code=safe_error, retry_at=max(backoff, retry_at or backoff) if backoff else None)

    def reserve_analytics(self, account_id) -> dict:
        """Atomic, restart-safe per Client-ID rate/quota reservation before HTTP."""
        if not isinstance(account_id, str) or not account_id.strip():
            return {"allowed": False, "code": "account_identity_unavailable", "retry_at": None}
        digest = hashlib.sha256(account_id.encode()).hexdigest()
        now = self.clock()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM analytics_budget WHERE account_hash=?", (digest,)).fetchone()
            calls = row["calls"] if row and row["utc_day"] == today.isoformat() else 0
            if calls >= 50:
                next_day = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()
                return {"allowed": False, "code": "daily_quota_guard", "retry_at": _iso(next_day), "retry_epoch": next_day}
            if row and now < row["last_call_at"] + 61:
                retry = row["last_call_at"] + 61
                return {"allowed": False, "code": "minute_rate_guard", "retry_at": _iso(retry), "retry_epoch": retry}
            db.execute("INSERT INTO analytics_budget VALUES(?,?,?,?) ON CONFLICT(account_hash) DO UPDATE SET utc_day=excluded.utc_day,calls=excluded.calls,last_call_at=excluded.last_call_at", (digest, today.isoformat(), calls + 1, now))
        return {"allowed": True, "remaining": 49 - calls}

    def schedule(self, shop) -> dict:
        shop = _key(shop, "店铺", shop=True)
        with self._db() as db:
            row = db.execute("SELECT * FROM schedules WHERE shop=?", (shop,)).fetchone()
        if row is None:
            return {"shop": shop, "enabled": False, "days": 7, "interval_hours": 24, "include_traffic": True, "next_run_at": None}
        return {**dict(row), "enabled": bool(row["enabled"]), "include_traffic": bool(row["include_traffic"]), "next_run_at": _iso(row["next_run_at"])}

    def save_schedule(self, shop, enabled, days=7, interval_hours=24, include_traffic=True) -> dict:
        shop, days = _key(shop, "店铺", shop=True), _days(days)
        if not isinstance(enabled, bool) or not isinstance(include_traffic, bool) or isinstance(interval_hours, bool) or not isinstance(interval_hours, int) or not 1 <= interval_hours <= 168:
            raise ValueError("定时监测参数无效")
        with self._db() as db:
            db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?) ON CONFLICT(shop) DO UPDATE SET enabled=excluded.enabled,days=excluded.days,interval_hours=excluded.interval_hours,include_traffic=excluded.include_traffic,next_run_at=excluded.next_run_at", (shop, int(enabled), days, interval_hours, int(include_traffic), self.clock() + interval_hours * 3600))
        return self.schedule(shop)

    def enqueue_due(self) -> dict:
        now, count = self.clock(), 0
        with self._db() as db:
            # Queue insertion and advancing the schedule share one transaction:
            # a killed cron worker cannot lose this run between those two steps.
            db.execute("BEGIN IMMEDIATE")
            schedules = list(db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?", (now,)))
            room = max(0, 500 - db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','retry_wait','running')").fetchone()[0])
            for schedule in schedules:
                offers = [r[0] for r in db.execute("SELECT offer_id FROM products WHERE shop=? ORDER BY offer_id LIMIT 500", (schedule["shop"],))]
                blocked = False
                for offer in offers:
                    exists = db.execute("SELECT 1 FROM jobs WHERE shop=? AND offer_id=? AND status IN ('queued','retry_wait','running') LIMIT 1", (schedule["shop"], offer)).fetchone()
                    if exists:
                        continue
                    if room <= 0:
                        blocked = True
                        break
                    db.execute("INSERT INTO jobs(id,shop,offer_id,days,include_traffic,status,created_at,updated_at,next_run_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (uuid.uuid4().hex, schedule["shop"], offer, schedule["days"], schedule["include_traffic"], now, now, now))
                    count, room = count + 1, room - 1
                # Saturation preserves a near-term retry instead of silently
                # omitting the remaining products for the entire interval.
                db.execute("UPDATE schedules SET next_run_at=? WHERE shop=?", (now + (300 if blocked else schedule["interval_hours"] * 3600), schedule["shop"]))
        return {"enqueued": count}


def _window(now, days, lag):
    end = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=lag)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _health(item):
    statuses = item.get("statuses") if isinstance(item.get("statuses"), Mapping) else {}
    visibility = item.get("visibility_details") if isinstance(item.get("visibility_details"), Mapping) else {}
    stocks = item.get("stocks") if isinstance(item.get("stocks"), Mapping) else {}
    codes = [_text(e.get("code"), 100) for e in item.get("errors", []) if isinstance(e, Mapping) and e.get("level") != "WARNING"][:50]
    sku = _identifier(item.get("sku"))
    if not sku and isinstance(item.get("sources"), list):
        source_skus = {_identifier(source.get("sku")) for source in item["sources"] if isinstance(source, Mapping)} - {None}
        sku = next(iter(source_skus)) if len(source_skus) == 1 else None
    return {"name": _text(item.get("name"), 2000), "ozon_product_id": _identifier(item.get("id")), "ozon_sku": sku,
            "currency": _text(item.get("currency_code"), 10), "price": _number(item.get("price")), "status": _text(statuses.get("status")),
            "moderate_status": _text(statuses.get("moderate_status")), "validation_status": _text(statuses.get("validation_status")),
            "status_failed": _text(statuses.get("status_failed")),
            "is_created": statuses.get("is_created") if isinstance(statuses.get("is_created"), bool) else None,
            "is_archived": item.get("is_archived") if isinstance(item.get("is_archived"), bool) else None,
            "is_autoarchived": item.get("is_autoarchived") if isinstance(item.get("is_autoarchived"), bool) else None,
            "has_stock": stocks.get("has_stock") if isinstance(stocks.get("has_stock"), bool) else visibility.get("has_stock") if isinstance(visibility.get("has_stock"), bool) else None,
            "has_price": visibility.get("has_price") if isinstance(visibility.get("has_price"), bool) else None,
            "error_codes": [c for c in codes if c]}


def _metric_rows(response, sku, names, date_from, date_to):
    result = response.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("data"), list):
        raise ValueError("invalid_response")
    if len(result["data"]) > 1000:
        raise ValueError("invalid_response")
    rows, seen_days = [], set()
    for raw in result["data"][:1000]:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid_response")
        dims, values = raw.get("dimensions"), raw.get("metrics")
        if not isinstance(dims, list) or len(dims) != 2 or not all(isinstance(d, Mapping) for d in dims) or not isinstance(values, list):
            raise ValueError("invalid_response")
        if str(dims[0].get("id")) != sku:
            raise ValueError("cross_sku_response")
        day = str(dims[1].get("id") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not date_from <= day <= date_to or day in seen_days:
            raise ValueError("invalid_response")
        seen_days.add(day)
        rows.append({"sku": sku, "day": day, "metrics": {name: _number(values[i]) if i < len(values) else None for i, name in enumerate(names)}})
    # Missing days are not silently zero-filled. Totals retain provider semantics.
    totals = result.get("totals") or []
    return {"metrics": list(names), "items": sorted(rows, key=lambda r: r["day"]), "totals": {name: _number(totals[i]) if i < len(totals) else None for i, name in enumerate(names)}, "missing_days_are_zero": False, "includes_paid_traffic": None}


def _query_rows(response, sku):
    raw_rows = response.get("queries")
    if not isinstance(raw_rows, list):
        raise ValueError("invalid_response")
    if len(raw_rows) > 100:
        raise ValueError("invalid_response")
    rows = []
    for raw in raw_rows[:100]:
        if not isinstance(raw, Mapping) or str(raw.get("sku")) != sku or not isinstance(raw.get("query"), str) or not raw["query"].strip():
            raise ValueError("invalid_response")
        row = {"sku": sku, "query": raw["query"][:500], "currency": _text(raw.get("currency"), 10)}
        for key in ("gmv", "order_count", "position", "unique_search_users", "unique_view_users", "view_conversion"):
            row[key] = _number(raw.get(key))
        rows.append(row)
    return rows


def sync_product(store: Store, shop, offer_id, transport, period_days=7, include_traffic=True, *, account_id=None) -> dict:
    """Bounded three-capability read, never sleeps to satisfy provider quotas."""
    days = _days(period_days)
    product = store.product(shop, offer_id)
    fetched, partial, deferred, retryable_error = [], False, None, None
    try:
        response = transport.post(INFO_PATH, {"offer_id": [offer_id]})
        items = response.get("items") if isinstance(response, Mapping) else None
        if not isinstance(items, list):
            raise ValueError("invalid_response")
        matches = [item for item in items if isinstance(item, Mapping) and item.get("offer_id") == offer_id]
        if len(matches) > 1:
            raise ValueError("invalid_response")
        health = _health(matches[0]) if matches else {}
        if health.get("ozon_product_id") and product.get("ozon_product_id") and health["ozon_product_id"] != product["ozon_product_id"]:
            raise ValueError("identity_conflict")
        fetched.append(store.record_snapshot(shop, offer_id, "health", "available" if matches else "no_data", endpoint=INFO_PATH, data=health))
        if matches:
            store._observe_product(shop, offer_id, {k: v for k, v in health.items() if k in {"name", "ozon_product_id", "ozon_sku", "currency", "price"} and v is not None})
    except Exception as error:
        safe = safe_error(error)
        fetched.append(store.record_snapshot(shop, offer_id, "health", "permission_required" if safe["code"] == "permission_required" else "error", endpoint=INFO_PATH, error_code=safe["code"]))
        health, partial = {}, True
        retryable_error = safe["code"] if safe["retryable"] else None
    # Require identity recovered from this shop's fresh response before analytics.
    sku = health.get("ozon_sku")
    if not sku:
        for kind, endpoint in (("metrics", METRICS_PATH), ("queries", QUERIES_PATH)):
            fetched.append(store.record_snapshot(shop, offer_id, kind, "not_ready", endpoint=endpoint, error_code="ozon_sku_unavailable"))
        return {"partial": True, "capabilities": {s["kind"]: s["status"] for s in fetched}, "deferred_until": None, "retryable_error": retryable_error}

    start, end = _window(store.clock(), days, 1)
    names = TRAFFIC_METRICS if include_traffic else BASIC_METRICS
    identity = account_id or getattr(getattr(transport, "credentials", None), "client_id", None)
    budget = store.reserve_analytics(identity)
    if not budget["allowed"]:
        fetched.append(store.record_snapshot(shop, offer_id, "metrics", "deferred", endpoint=METRICS_PATH, date_from=start, date_to=end, data={"metrics": list(names), "items": [], "totals": {name: None for name in names}, "currency": None, "price_currency": health.get("currency")}, error_code=budget["code"], retry_at=budget.get("retry_at")))
        partial, deferred = True, budget.get("retry_epoch")
    else:
        try:
            response = transport.post(METRICS_PATH, {"date_from": start, "date_to": end, "metrics": list(names), "dimension": ["sku", "day"], "filters": [{"key": "sku", "op": "EQ", "value": sku}], "limit": 1000, "offset": 0})
            data = _metric_rows(response, sku, names, start, end)
            # Seller analytics/data does not return a currency; listing price
            # currency is not proof of reporting revenue currency.
            data["currency"] = None
            data["price_currency"] = health.get("currency")
            data["revenue_currency_verified"] = False
            fetched.append(store.record_snapshot(shop, offer_id, "metrics", "available" if data["items"] else "no_data", endpoint=METRICS_PATH, data=data, date_from=start, date_to=end))
        except Exception as error:
            safe = safe_error(error)
            fetched.append(store.record_snapshot(shop, offer_id, "metrics", "permission_required" if safe["code"] == "permission_required" else "error", endpoint=METRICS_PATH, date_from=start, date_to=end, data={"metrics": list(names), "items": [], "totals": {name: None for name in names}, "currency": None, "price_currency": health.get("currency")}, error_code=safe["code"]))
            partial = True
            if safe["retryable"]:
                retryable_error, deferred = safe["code"], store.clock() + 61

    # Recent-month details are available without historical-week subscription.
    # A requested 30-day horizon therefore contains 28 mature calendar days,
    # explicitly labelled instead of silently calling an inaccessible older day.
    query_days = min(days, 28)
    start, end = _window(store.clock(), query_days, 2)
    try:
        rows, seen = [], set()
        truncated, provider_period = False, None
        for page in range(MAX_QUERY_PAGES):
            response = transport.post(QUERIES_PATH, {"date_from": start + "T00:00:00Z", "date_to": end + "T23:59:59Z", "limit_by_sku": 15, "page": page, "page_size": 100, "skus": [sku], "sort_by": "BY_SEARCHES", "sort_dir": "DESCENDING"})
            if not isinstance(response, Mapping):
                raise ValueError("invalid_response")
            provider_period = response.get("analytics_period") or provider_period
            chunk = _query_rows(response, sku)
            for row in chunk:
                if row["query"] not in seen:
                    seen.add(row["query"])
                    rows.append(row)
            page_count = _number(response.get("page_count"))
            if not chunk or page_count is None or page + 1 >= page_count or len(rows) >= 15:
                truncated = len(rows) > 15 or bool(page_count and page + 1 < page_count)
                break
            if page + 1 == MAX_QUERY_PAGES:
                truncated = True
        period = {key: _text(provider_period.get(key), 50) for key in ("date_from", "date_to")} if isinstance(provider_period, Mapping) else None
        fetched.append(store.record_snapshot(shop, offer_id, "queries", "available" if rows else "no_data", endpoint=QUERIES_PATH, date_from=start, date_to=end, data={"items": rows[:15], "limit_by_sku": 15, "partial": truncated, "provider_period": period, "report_grain": "sku_query_date_window", "requested_days": days, "effective_days": query_days, "calculation_delay_days": 2, "not_all_search_terms": True}))
    except Exception as error:
        safe = safe_error(error)
        fetched.append(store.record_snapshot(shop, offer_id, "queries", "permission_required" if safe["code"] == "permission_required" else "error", endpoint=QUERIES_PATH, date_from=start, date_to=end, data={"items": [], "limit_by_sku": 15}, error_code=safe["code"]))
        partial = True
        if safe["retryable"]:
            retryable_error = safe["code"]
            deferred = max(deferred or 0, store.clock() + 61)
    return {"partial": partial, "capabilities": {s["kind"]: s["status"] for s in fetched}, "deferred_until": _iso(deferred) if deferred else None, "retryable_error": retryable_error, "_retry_epoch": deferred}


def run_job(store: Store, job, transport) -> dict:
    worker = job.get("worker_id")
    if not worker:
        raise ValueError("任务尚未被执行器领取")
    try:
        result = sync_product(store, job["shop"], job["offer_id"], transport, job["days"], job.get("include_traffic", True))
        retry_at = result.pop("_retry_epoch", None)
        deferrals = int((job.get("result") or {}).get("defer_count") or 0)
        if result.get("retryable_error"):
            # Actual provider failures use the attempts budget, unlike a local
            # quota reservation refusal. Even partial successful reads survive.
            store.fail_job(job["id"], worker, result["retryable_error"], True, retry_at=retry_at)
        elif retry_at and deferrals < 3:
            result["defer_count"] = deferrals + 1
            store._finish(job["id"], worker, "retry_wait", result=result, error_code="analytics_quota_deferred", retry_at=retry_at, refund_attempt=True)
        else:
            if retry_at:
                result["defer_count"] = deferrals
                result["automatic_retry_exhausted"] = True
            store.complete_job(job["id"], worker, result=result)
        return result
    except Exception as error:
        safe = safe_error(error)
        store.fail_job(job["id"], worker, safe["code"], safe["retryable"])
        return {"partial": True, "error_code": safe["code"]}


def diagnose(latest) -> list:
    notes = []
    health = latest.get("health")
    if not health or health["status"] != "available":
        notes.append({"code": "health_not_verified", "severity": "warning", "message": "可售状态尚未核实；先同步审核、价格和库存，再判断关键词或广告。", "evidence": INFO_PATH})
    else:
        data = health["data"]
        rejected = str(data.get("moderate_status") or "").lower() in {"declined", "rejected", "failed"} or str(data.get("validation_status") or "").lower() in {"failed", "error", "invalid"}
        failed_stage = str(data.get("status_failed") or "").lower() not in {"", "none", "null"}
        if data.get("is_archived") or data.get("is_autoarchived") or data.get("error_codes") or data.get("is_created") is False or rejected or failed_stage:
            notes.append({"code": "listing_not_ready", "severity": "critical", "message": "商品存在归档、未建卡或校验错误，优先处理上架状态。", "evidence": {"at": health["fetched_at"], "error_codes": data.get("error_codes"), "status": data.get("status")}})
        for key, message in (("has_stock", "平台回读显示无库存，先核对仓库和可售数量。"), ("has_price", "平台回读显示缺少价格，先修复价格状态。")):
            if data.get(key) is False:
                notes.append({"code": "no_stock" if key == "has_stock" else "no_price", "severity": "critical", "message": message, "evidence": health["fetched_at"]})
        if data.get("has_stock") is None or data.get("has_price") is None:
            notes.append({"code": "availability_unknown", "severity": "warning", "message": "库存或价格可用性未返回，不能直接认定商品正常可售。", "evidence": INFO_PATH})
    for kind in ("metrics", "queries"):
        snapshot = latest.get(kind)
        if not snapshot or snapshot["status"] != "available":
            notes.append({"code": kind + "_not_available", "severity": "info", "message": "流量/销量指标尚不可用。缺失不等于零。" if kind == "metrics" else "搜索词报表尚不可用；可能尚在计算、暂无记录或权限受限，不据此判断词无效。", "evidence": {"status": snapshot["status"] if snapshot else "not_ready", "error_code": snapshot.get("error_code") if snapshot else None}})
    queries = latest.get("queries")
    if queries and queries["status"] == "available":
        for row in queries["data"].get("items", [])[:15]:
            orders = row.get("order_count")
            sold = orders is not None and orders > 0
            zero_views = row.get("unique_view_users") == 0
            message = (f"搜索词「{row['query']}」在该报表期间产生 {orders} 笔订单，可作为相关性核对的候选依据；不保证继续转化。" if sold else
                       f"搜索词「{row['query']}」报告浏览人数为零，不等于关键词无效；先核对时效、权限与商品可售状态。" if zero_views else
                       f"搜索词「{row['query']}」仅作为真实报表观察；需核对与商品事实的相关性后才能采用，不自动修改卡片。")
            notes.append({"code": "query_sales_observed" if sold else "query_zero_views" if zero_views else "query_observation", "severity": "info", "message": message, "evidence": {"query": row["query"], "unique_view_users": row.get("unique_view_users"), "order_count": orders, "sample_sufficient": None, "date_from": queries.get("date_from"), "date_to": queries.get("date_to"), "endpoint": QUERIES_PATH}})
    return notes
