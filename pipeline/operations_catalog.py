"""Explicit, read-only Seller catalogue selection for the operations center.

Products fetched from the shop are not supplier captures or publication records.
Only a one-hour server-owned page token can bind selected offers for analysis.
Official contracts: Seller OpenAPI snapshot 2026-10-03; no write API is used.
"""
from __future__ import annotations

import json
import re
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit
import uuid

from .operations import INFO_PATH, Store, _health, _identifier, _iso, _json, _key, _number, _text, safe_error

LIST_PATH = "/v3/product/list"
PAGE_TTL_SECONDS = 3600
MAX_TRACE_PAGES = 200
MAX_SELECTED = 100
VISIBILITIES = frozenset({"ALL", "VISIBLE", "INVISIBLE", "EMPTY_STOCK", "NOT_MODERATED", "MODERATED", "DISABLED", "STATE_FAILED", "READY_TO_SUPPLY", "VALIDATION_STATE_PENDING", "VALIDATION_STATE_FAIL", "VALIDATION_STATE_SUCCESS", "TO_SUPPLY", "IN_SALE", "REMOVED_FROM_SALE", "OVERPRICED", "CRITICALLY_OVERPRICED", "EMPTY_BARCODE", "BARCODE_EXISTS", "QUARANTINE", "ARCHIVED", "OVERPRICED_WITH_STOCK", "PARTIAL_APPROVED", "AUTO_ARCHIVED", "MANUAL_ARCHIVED", "SEASONAL_AUTO_ARCHIVED", "VISIBLE_WITH_FBO_STOCK", "SHOWCASE_SELECT_ACTIVE"})
MESSAGES = {
    "invalid_request": "店铺商品请求参数不正确，请检查过滤条件。",
    "unauthorized": "Seller 授权无效或已过期，请重新授权。",
    "permission_required": "Seller API 权限不足，无法读取店铺商品。",
    "not_found": "未找到店铺商品，请检查店铺授权。",
    "rate_limited": "Ozon 请求限流，请稍后重试。",
    "provider_unavailable": "Ozon 暂时不可用，请稍后重试。",
    "network_unavailable": "暂时无法连接 Ozon，请稍后重试。",
    "request_failed": "店铺商品读取失败，请检查授权和网络。",
    "invalid_response": "店铺商品响应格式异常，本页未导入。",
    "identity_conflict": "店铺商品标识不一致，本页未导入，请重新读取核对。",
    "page_expired": "商品选择页已过期，请重新读取店铺商品。",
    "page_not_found": "未找到该店铺的商品选择页，请重新读取。",
    "invalid_selection": "请选择本页已验证的商品，每次最多 100 条。",
    "page_limit": "已达到 200 页浏览上限，请重新开始或缩小过滤范围。",
}


class CatalogError(ValueError):
    def __init__(self, code, *, http_status=None):
        self.code = code if code in MESSAGES else "request_failed"
        self.http_status = http_status
        super().__init__(MESSAGES[self.code])


def _cursor(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise CatalogError("invalid_request", http_status=422)
    return value


def _post(transport, endpoint, body):
    try:
        response = transport.post(endpoint, body)
    except Exception as error:
        safe = safe_error(error)
        raise CatalogError(safe["code"], http_status=safe["http_status"] or 502) from None
    if not isinstance(response, Mapping):
        raise CatalogError("invalid_response", http_status=502)
    return response


def _thumbnail(info):
    candidates = []
    for field in ("primary_image", "images"):
        values = info.get(field)
        candidates.extend(values if isinstance(values, list) else [values] if isinstance(values, str) else [])
    for value in candidates[:100]:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            continue
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
            _ = parsed.port
            credential_keys = {"authorization", "auth", "api-key", "api_key", "apikey", "client_secret", "access_token", "password", "token"}
            valid = valid and not any(key.casefold() in credential_keys for key, _ in parse_qsl(parsed.query, max_num_fields=100))
        except ValueError:
            continue
        if valid:
            return value
    return None


def _total(result):
    for key in ("total_items", "total"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value, key
    return None, None


def browse_catalog(shop, transport, *, store: Store, last_id="", limit=50, query="", visibility="ALL", seen_cursors=()) -> dict:
    shop = _key(shop, "店铺", shop=True)
    last_id = _cursor(last_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000 or not isinstance(query, str) or len(query) > 100 or visibility not in VISIBILITIES:
        raise CatalogError("invalid_request", http_status=422)
    if not isinstance(seen_cursors, (list, tuple)) or len(seen_cursors) >= MAX_TRACE_PAGES:
        raise CatalogError("page_limit", http_status=422)
    trace = [_cursor(cursor) for cursor in seen_cursors]
    if last_id in trace or len(set(trace)) != len(trace):
        raise CatalogError("invalid_request", http_status=422)
    credential_shop = getattr(getattr(transport, "credentials", None), "shop_id", None)
    if credential_shop is not None and str(credential_shop) != shop:
        raise CatalogError("identity_conflict", http_status=409)
    response = _post(transport, LIST_PATH, {"filter": {"visibility": visibility}, "last_id": last_id, "limit": limit})
    result = response.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("items"), list) or len(result["items"]) > limit:
        raise CatalogError("invalid_response", http_status=502)
    rows, identities, offers, ids = [], {}, set(), set()
    for item in result["items"]:
        if not isinstance(item, Mapping):
            raise CatalogError("invalid_response", http_status=502)
        try:
            offer = _key(item.get("offer_id"), "货号")
        except ValueError:
            raise CatalogError("invalid_response", http_status=502) from None
        product_id, sku = _identifier(item.get("product_id")), _identifier(item.get("sku"))
        if not product_id or offer in offers or product_id in ids:
            raise CatalogError("identity_conflict", http_status=409)
        offers.add(offer)
        ids.add(product_id)
        row = {"shop": shop, "offer_id": offer, "ozon_product_id": product_id, "ozon_sku": sku,
               "name": None, "thumbnail": None, "status": None, "moderate_status": None,
               "price": None, "currency": None, "is_archived": item.get("archived") if isinstance(item.get("archived"), bool) else None,
               "is_autoarchived": None, "details_available": False, "source": "shop_readonly_import"}
        rows.append(row)
        identities[product_id] = row
    warnings = []
    if rows:
        details = _post(transport, INFO_PATH, {"product_id": list(identities)})
        if not isinstance(details.get("items"), list) or len(details["items"]) > len(rows):
            raise CatalogError("invalid_response", http_status=502)
        detailed = set()
        for item in details["items"]:
            if not isinstance(item, Mapping):
                raise CatalogError("invalid_response", http_status=502)
            remote_id = _identifier(item.get("id"))
            row = identities.get(remote_id)
            if row is None or remote_id in detailed or item.get("offer_id") != row["offer_id"]:
                raise CatalogError("identity_conflict", http_status=409)
            detailed.add(remote_id)
            health = _health(item)
            if health.get("ozon_sku") and row["ozon_sku"] and health["ozon_sku"] != row["ozon_sku"]:
                raise CatalogError("identity_conflict", http_status=409)
            for field in ("name", "ozon_sku", "status", "moderate_status", "price", "currency", "is_archived", "is_autoarchived"):
                if health.get(field) is not None:
                    row[field] = health[field]
            row.update(thumbnail=_thumbnail(item), details_available=True)
        if len(detailed) < len(rows):
            warnings.append("details_missing")
    total, total_source = _total(result)
    if total is not None and total < len(rows):
        raise CatalogError("invalid_response", http_status=502)
    next_cursor = result.get("last_id")
    if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 4096 or any(ord(c) < 32 for c in next_cursor)):
        raise CatalogError("invalid_response", http_status=502)
    trace.append(last_id)
    # Empty pages terminate even if a provider repeats a cursor or stale total.
    has_more = bool(rows and next_cursor and len(rows) == limit)
    # Continuations accepted by the API derive this trace/limit from a server
    # receipt. Every preceding continued page was full, so count actual rows,
    # not the locally filtered matches, against the provider's current total.
    fetched_count = (len(trace) - 1) * limit + len(rows)
    if total is not None and total <= fetched_count:
        has_more = False
    if has_more and next_cursor in trace:
        warnings.append("cursor_loop")
        has_more = False
    if has_more and len(trace) >= MAX_TRACE_PAGES:
        warnings.append("page_limit")
        has_more = False
    if not has_more:
        next_cursor = None
    filtered = rows
    if query.strip():
        needle = query.strip().casefold()
        filtered = [row for row in rows if needle in row["offer_id"].casefold() or needle in (row["name"] or "").casefold()]
        warnings.append("search_current_page_only")
    now, token = store.clock(), str(uuid.uuid4())
    page = {"items": filtered, "page_token": token, "fetched_at": _iso(now), "expires_at": _iso(now + PAGE_TTL_SECONDS),
            "last_id": last_id, "next_cursor": next_cursor, "has_more": has_more, "total": total, "total_source": total_source,
            "filtered_count": len(rows) - len(filtered), "page_item_count": len(rows), "warning_codes": warnings,
            "search_scope": "current_page", "query": query, "visibility": visibility, "limit": limit, "seen_cursors": trace,
            "api_writes_performed": False}
    with store._db() as db:
        db.execute("DELETE FROM catalog_pages WHERE expires_at<=?", (now,))
        db.execute("INSERT INTO catalog_pages VALUES(?,?,?,?)", (token, shop, now + PAGE_TTL_SECONDS, _json(page)))
        # At most 400 live page receipts per shop. Expired/evicted receipts must
        # be re-read; no monitoring records or source data are deleted here.
        db.execute("DELETE FROM catalog_pages WHERE shop=? AND token NOT IN (SELECT token FROM catalog_pages WHERE shop=? ORDER BY expires_at DESC,rowid DESC LIMIT 400)", (shop, shop))
    return page


def _read_page_in(db, shop, page_token, now):
    if not isinstance(page_token, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", page_token):
        raise CatalogError("page_not_found", http_status=404)
    row = db.execute("SELECT expires_at,payload FROM catalog_pages WHERE token=? AND shop=?", (page_token, shop)).fetchone()
    if row is None:
        raise CatalogError("page_not_found", http_status=404)
    if row["expires_at"] <= now:
        raise CatalogError("page_expired", http_status=409)
    return json.loads(row["payload"])


def read_page(store, shop, page_token):
    shop = _key(shop, "店铺", shop=True)
    with store._db() as db:
        return _read_page_in(db, shop, page_token, store.clock())


def import_selected(store, shop, page_token, offer_ids):
    shop = _key(shop, "店铺", shop=True)
    if not isinstance(offer_ids, (list, tuple)) or not 1 <= len(offer_ids) <= MAX_SELECTED or not all(isinstance(offer, str) for offer in offer_ids):
        raise CatalogError("invalid_selection", http_status=422)
    unique = list(dict.fromkeys(offer_ids))
    now, imported, existing, products = store.clock(), 0, 0, []
    with store._db() as db:
        db.execute("BEGIN IMMEDIATE")
        page = _read_page_in(db, shop, page_token, now)
        by_offer = {row["offer_id"]: row for row in page["items"]}
        if any(offer not in by_offer for offer in unique):
            raise CatalogError("invalid_selection", http_status=422)
        for offer in unique:
            source = by_offer[offer]
            if source["shop"] != shop or not _identifier(source["ozon_product_id"]):
                raise CatalogError("identity_conflict", http_status=409)
            saved = db.execute("SELECT payload FROM products WHERE shop=? AND offer_id=?", (shop, offer)).fetchone()
            old = json.loads(saved[0]) if saved else {}
            if old.get("ozon_product_id") and old["ozon_product_id"] != source["ozon_product_id"]:
                raise CatalogError("identity_conflict", http_status=409)
            if old:
                # Local source/warehouse and API observations have higher
                # ownership: browsing the catalogue never overwrites them.
                product = dict(old)
                for field in ("ozon_product_id", "ozon_sku", "name", "thumbnail"):
                    if product.get(field) is None and source.get(field) is not None:
                        product[field] = source[field]
                product["catalog_seen_at"] = _iso(now)
                existing += 1
            else:
                product = {**source, "discovered_at": _iso(now), "catalog_imported_at": _iso(now),
                           "catalog_seen_at": _iso(now), "monitoring": "read_only"}
                imported += 1
            db.execute("INSERT INTO products VALUES(?,?,?,?) ON CONFLICT(shop,offer_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (shop, offer, _json(product), now))
            products.append(product)
    return {"imported": imported, "existing": existing, "items": products, "api_writes_performed": False, "jobs_enqueued": 0}
