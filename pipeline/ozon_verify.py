"""提交后核对：读回 Ozon 上真实存下来的商品，逐项对账（**只读**）。

真提交跑通后，光看"imported"不够 —— 要确认 Ozon 那边**真的存下了**我们填的东西。
实测中这一层抓到过的问题类型：属性值丢失、变体没合并、图片没被 Ozon 转存、SKU 没分配。

检查项：
1. 每个 offer 都能在 Ozon 读回来；
2. Ozon 分配了真实 SKU（``sku`` 不为 0）；
3. 我们填的属性在 Ozon 上有值（逐条对比 ``ozon-attributes-final.json``）；
4. 图片被 Ozon 转存（主图变成 Ozon 自己的地址）；
5. 变体合并：所有 offer 的 ``model_info.model_id`` 相同，且 ``count`` 等于 offer 数。

用法::

    python -m pipeline.ozon_verify --product-dir products/P000006 --store default
    python -m pipeline.ozon_verify --product-dir products/P000006 --store default --json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .ozon_status import _read_json, _transport_for_store

PATH_PRODUCT_ATTRIBUTES = "/v4/product/info/attributes"
PATH_PRODUCT_INFO = "/v3/product/info/list"
READBACK_FILE = "media-readback.json"


def _readback_scope(product: Path, store_id: str) -> tuple[list[str], str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(store_id)):
        raise ValueError("店铺标识无效")
    ledger = _read_json(product / "output/store-publications.json")
    if ledger.get("product_id") not in {None, product.name}:
        raise ValueError("发布台账不属于当前商品，禁止扩展回读范围")
    stores = ledger.get("stores") or {}
    entry = stores.get(store_id) if isinstance(stores, Mapping) else None
    entry = entry if isinstance(entry, Mapping) else {}
    rows = entry.get("sku_publications") or []
    offers = list(dict.fromkeys(str(row["offer_id"]) for row in rows
                                if isinstance(row, Mapping) and row.get("offer_id")))
    if len(offers) > 100:
        raise ValueError("单商品回读货号异常，已停止，不能扩展查询到其他商品")
    payload = _read_json(product / "output/store-runs" / store_id / "payload.json")
    if payload.get("product_id") not in {None, product.name}:
        raise ValueError("提交快照不属于当前商品，不能作为回读依据")
    expected_offers = [str(row.get("offer_id")) for row in payload.get("variants") or [] if isinstance(row, Mapping)]
    if payload and any(offer not in expected_offers for offer in offers):
        raise ValueError("发布台账与提交快照货号不一致，停止回读")
    scope = {"product_id": product.name, "store": store_id, "offers": offers, "payload": payload}
    fingerprint = hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
    return offers, fingerprint


def _result_items(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(response, Mapping):
        return []
    rows = response.get("result")
    if isinstance(rows, Mapping):
        rows = rows.get("items")
    if not isinstance(rows, list):
        rows = response.get("items")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _public_url(value: Any) -> str | None:
    """No credentials or signed query tokens in cached/browser readback reports."""
    if not isinstance(value, str) or len(value) > 12000:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return None


def _video_attributes(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Official v4 uses flat attributes; also accept documented import-style groups.

    Attribute ids pair by array position inside their complex group, not by a
    cross-group union: a video URL must never borrow another video's title.
    """
    raw = item.get("complex_attributes")
    if not isinstance(raw, list):
        return []
    groups: list[list[Mapping[str, Any]]] = []
    flat: list[Mapping[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        nested = row.get("attributes")
        if isinstance(nested, list):
            groups.append([attribute for attribute in nested if isinstance(attribute, Mapping)])
        else:
            flat.append(row)
    if flat:
        groups.append(flat)
    videos = []
    for attributes in groups:
        urls, titles = [], []
        for attribute in attributes:
            identity = attribute.get("id", attribute.get("attribute_id"))
            if str(attribute.get("complex_id", "100001")) != "100001":
                continue  # 100002 / 21845 is a short video cover, not ordinary video.
            if str(identity) not in {"21841", "21837"}:
                continue
            raw_values = attribute.get("values")
            values = [value.get("value") for value in raw_values
                      if isinstance(value, Mapping) and isinstance(value.get("value"), str)] if isinstance(raw_values, list) else []
            (urls if str(identity) == "21841" else titles).extend(values)
        for index, url in enumerate(urls):
            videos.append({"url": url, "title": titles[index].strip() if index < len(titles) else ""})
    return videos


def video_readback(payload: Mapping[str, Any], attributes: Mapping[str, Mapping[str, Any]],
                   product_info: Mapping[str, Mapping[str, Any]], offers: Sequence[str]) -> dict[str, Any]:
    """Readback evidence is not a media player/moderation success assertion."""
    from .ozon_write import media_for_variant

    variants = {str(row.get("offer_id")): row for row in payload.get("variants") or [] if isinstance(row, Mapping)}
    results = []
    for offer in offers:
        variant = variants.get(offer) or {}
        expected, _cover = media_for_variant(payload, variant)
        observed = _video_attributes(attributes.get(offer) or {})
        statuses = (product_info.get(offer) or {}).get("statuses") or {}
        statuses = statuses if isinstance(statuses, Mapping) else {}
        errors = (product_info.get(offer) or {}).get("errors") or []
        errors = errors if isinstance(errors, list) else []
        video_errors = [row for row in errors if isinstance(row, Mapping) and
                        re.search(r"video|21841|21837|100001|видео", json.dumps(row, ensure_ascii=False), re.I)]
        details, all_exact = [], True
        for index, wanted in enumerate(expected):
            actual = observed[index] if index < len(observed) else {}
            exact_url = bool(actual.get("url") and actual.get("url") == wanted.get("url"))
            try:
                host = (urlsplit(actual.get("url") or "").hostname or "").lower()
            except ValueError:
                host = ""
            # Re-hosting changes the URL and therefore cannot prove source byte
            # identity. A matching title/order/count is recorded only as evidence.
            rehosted = host == "ozone.ru" or host.endswith(".ozone.ru")
            title_match = bool(actual.get("title") and actual["title"] == wanted.get("title"))
            all_exact = all_exact and exact_url and title_match
            details.append({"video_id": wanted.get("video_id"), "expected_title": wanted.get("title"),
                            "observed_title": actual.get("title") or None,
                            "title_match": title_match, "url_exact_match": exact_url,
                            "url_evidence": "exact_source_url" if exact_url else "ozon_rehosted_candidate" if rehosted else "not_matched",
                            "observed_url": _public_url(actual.get("url")),
                            "source_byte_identity_verified": False,
                            "storefront_verified": False})
        ready = bool(expected and len(expected) == len(observed) and all_exact and not video_errors)
        candidate = bool(expected and len(expected) == len(observed) and all(
            row["title_match"] and row["url_evidence"] in {"exact_source_url", "ozon_rehosted_candidate"} for row in details))
        pending = not observed or str(statuses.get("status") or "").lower() in {
            "pending", "processing", "in_progress", "importing"}
        state = ("not_selected" if not expected else "failed" if video_errors else
                 "readable_in_api" if ready else "rehosted_in_api_unverified_identity" if candidate else
                 "processing_or_not_yet_readable" if pending else "readback_mismatch")
        results.append({"offer_id": offer, "source_sku_id": variant.get("source_sku_id"),
                        "expected_count": len(expected), "observed_count": len(observed), "status": state,
                        "videos": details, "pending": bool(expected and state == "processing_or_not_yet_readable"),
                        "failed": state in {"failed", "readback_mismatch"}, "storefront_verified": False,
                        "message": "接口能读到视频，仍未验证买家页播放" if ready else
                                   "Ozon 返回了视频相关错误，请核对；不会自动重新提交" if video_errors else
                                   "Ozon 已转存候选视频；仅标题/顺序匹配，尚不能证明是同一源视频" if candidate else
                                   "视频未读回或仍在处理；只读刷新，不会自动重新提交" if expected else "本次未选择普通商品视频"})
    selected = [row for row in results if row["expected_count"]]
    state = ("not_selected" if not selected else "failed" if any(row["failed"] for row in selected) else
             "readable_in_api" if all(row["status"] == "readable_in_api" for row in selected) else
             "rehosted_in_api_unverified_identity" if all(row["status"] in {"readable_in_api", "rehosted_in_api_unverified_identity"} for row in selected)
             else "processing_or_not_yet_readable")
    return {"status": state, "video_expected": sum(row["expected_count"] for row in results),
            "video_readable": sum(row["observed_count"] for row in results), "items": results,
            "buyer_playback_verified": False, "video_processing_api_available": False,
            "note": "导入成功不等于视频处理、审核或买家页播放成功；回读仅证明本次查询返回的复杂属性。"}


def _attribute_text(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else "" if value is None else str(value)


def _attributes_of(document: Mapping[str, Any], sku_id: str | None = None) -> dict[int, list[str]]:
    """Read only the current SKU's overrides, never another variant's values."""
    result: dict[int, list[str]] = {}
    for item in document.get("common_attributes") or []:
        if isinstance(item, Mapping) and item.get("attribute_id") is not None:
            value = _attribute_text(item.get("value")).strip()
            if value:
                result.setdefault(int(item["attribute_id"]), []).append(value)
    by_sku = document.get("attributes_by_sku") if isinstance(document.get("attributes_by_sku"), Mapping) else {}
    for rows in ([by_sku.get(sku_id) or []] if sku_id is not None else []):
        override_ids = {int(row["attribute_id"]) for row in rows if isinstance(row, Mapping)
                        and row.get("attribute_id") is not None}
        for attribute_id in override_ids:
            result.pop(attribute_id, None)
        for item in rows or []:
            if isinstance(item, Mapping) and item.get("attribute_id") is not None:
                value = _attribute_text(item.get("value")).strip()
                if value:
                    result.setdefault(int(item["attribute_id"]), []).append(value)
    return result


def _expected_for_variant(payload: Mapping[str, Any], variant: Mapping[str, Any]) -> dict[int, list[dict[str, Any]]]:
    common = [row for row in payload.get("attributes") or [] if isinstance(row, Mapping)]
    overrides = [row for row in variant.get("attributes") or [] if isinstance(row, Mapping)]
    ids = {row.get("attribute_id") for row in overrides}
    result: dict[int, list[dict[str, Any]]] = {}
    for row in [*(row for row in common if row.get("attribute_id") not in ids), *overrides]:
        if row.get("attribute_id") is None:
            continue
        result.setdefault(int(row["attribute_id"]), []).append({
            "value": _attribute_text(row.get("value")),
            "dictionary_value_id": row.get("dictionary_value_id"),
        })
    return result


def verify_submitted(
    product_dir: Path | str,
    *,
    store_id: str,
    registry_path: Path | str | None = None,
    transport: Any | None = None,
    env: Mapping[str, str] | None = None,
    attributes_response: Mapping[str, Any] | None = None,
    product_info_response: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    product = Path(product_dir)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(store_id)):
        raise ValueError("店铺标识无效")
    ledger = _read_json(product / "output" / "store-publications.json")
    store_entry = ((ledger.get("stores") or {}).get(store_id) or {}) if isinstance(ledger, Mapping) else {}
    offer_ids = [
        str(item.get("offer_id"))
        for item in (store_entry.get("sku_publications") or [])
        if isinstance(item, Mapping) and item.get("offer_id")
    ]
    if not offer_ids:
        return {
            "ok": False,
            "product_id": product.name,
            "store": store_id,
            "error": "台账里没有这个店铺的 offer_id（先提交并跑 pipeline.ozon_status 确认）",
            "checks": [],
            "api_writes_performed": False,
        }

    offer_ids = list(dict.fromkeys(offer_ids))
    if transport is None and attributes_response is None:
        transport = _transport_for_store(
            store_id, transport_factory=None, registry_path=registry_path, env=env
        )
    response = attributes_response if attributes_response is not None else transport.post(
        PATH_PRODUCT_ATTRIBUTES, {"filter": {"offer_id": offer_ids, "visibility": "ALL"}, "limit": 100})
    items = _result_items(response)
    by_offer = {str(item.get("offer_id")): item for item in items if str(item.get("offer_id")) in offer_ids}
    foreign = any(str(item.get("offer_id")) not in offer_ids for item in items)
    duplicate = len(by_offer) != sum(str(item.get("offer_id")) in offer_ids for item in items)

    compiled_attributes = _read_json(product / "output" / "ozon-attributes-final.json")
    payload = _read_json(product / "output/store-runs" / store_id / "payload.json")
    variants = {str(row.get("offer_id")): row for row in payload.get("variants") or []
                if isinstance(row, Mapping)}
    source_skus = {str(row.get("offer_id")): str(row.get("sku_id") or "")
                   for row in store_entry.get("sku_publications") or [] if isinstance(row, Mapping)}
    grouping = _read_json(product / "output/platform-grouping-result.json")
    must_merge = (payload.get("product_group") or {}).get("must_merge")
    if must_merge is None:
        strategy = grouping.get("upload_strategy")
        must_merge = strategy == "merged_variants" if strategy else len(offer_ids) > 1
    checks: list[dict[str, Any]] = []
    checks.append({"name": "response_scope_valid", "ok": not foreign and not duplicate,
                   "detail": "仅核对本店铺台账中的商品；其他货号或重复回读不能作为成功证据"})

    missing_offers = [offer for offer in offer_ids if offer not in by_offer]
    checks.append(
        {
            "name": "offers_readable",
            "ok": not missing_offers,
            "detail": f"读回 {len(by_offer)}/{len(offer_ids)}" + (f"；缺 {missing_offers}" if missing_offers else ""),
        }
    )

    model_ids: set[Any] = set()
    for offer in offer_ids:
        item = by_offer.get(offer)
        if not item:
            continue
        sku = item.get("sku")
        checks.append(
            {
                "name": f"sku_assigned[{offer}]",
                "ok": str(sku or "").isdigit() and int(sku) > 0,
                "detail": f"sku={sku}",
            }
        )
        stored = {
            int(attribute["id"]): [
                str(value.get("value") or "")
                for value in (attribute.get("values") or [])
                if isinstance(value, Mapping)
            ]
            for attribute in (item.get("attributes") or [])
            if isinstance(attribute, Mapping) and attribute.get("id") is not None
        }
        variant = variants.get(offer)
        expected = (_expected_for_variant(payload, variant) if variant is not None else {
            key: [{"value": value} for value in values] for key, values in
            _attributes_of(compiled_attributes, source_skus.get(offer)).items()})
        stored_ids = {int(attribute["id"]): {int(value.get("dictionary_value_id") or 0)
            for value in attribute.get("values") or [] if isinstance(value, Mapping)}
            for attribute in item.get("attributes") or [] if isinstance(attribute, Mapping)
            and attribute.get("id") is not None}
        for attribute_id, values in expected.items():
            # Dictionary IDs are authoritative; text attributes compare the
            # expected values for this exact offer, not the union of all SKUs.
            wanted = {value["value"] for value in values if value.get("value")}
            stored_values = set(stored.get(attribute_id) or [])
            ok = all((int(value.get("dictionary_value_id") or 0) in stored_ids.get(attribute_id, set()))
                if int(value.get("dictionary_value_id") or 0) > 0 else value.get("value") in stored_values
                for value in values)
            # 类型(8229) 等由类和型决定，Ozon 可能不放在 attributes 里 → 只提示不判失败
            soft = attribute_id in {8229}
            checks.append(
                {
                    "name": f"attribute[{offer}][{attribute_id}]",
                    "ok": ok or soft,
                    "soft": soft,
                    "detail": f"期望 {sorted(wanted)}｜Ozon 上 {sorted(stored_values) or '（无此属性）'}",
                }
            )
        primary = str(item.get("primary_image") or "")
        image_host = (urlsplit(primary).hostname or "").casefold()
        checks.append(
            {
                "name": f"image_rehosted[{offer}]",
                "ok": bool(primary) and (image_host == "ozone.ru" or image_host.endswith(".ozone.ru")),
                "detail": _public_url(primary) or "（没有可安全展示的主图地址）",
            }
        )
        model_info = item.get("model_info") or {}
        if isinstance(model_info, Mapping) and model_info.get("model_id"):
            model_ids.add(model_info.get("model_id"))
            count = int(model_info.get("count") or 0)
            if must_merge:
                checks.append(
                    {
                        "name": f"variant_merged[{offer}]",
                        "ok": count >= len(offer_ids),
                        "detail": f"model_id={model_info.get('model_id')}｜count={count}（offer 数 {len(offer_ids)}）",
                    }
                )

    if len(offer_ids) > 1 and must_merge:
        checks.append(
            {
                "name": "same_model_id",
                "ok": len(model_ids) == 1 and bool(model_ids),
                "detail": f"model_id 集合={sorted(str(item) for item in model_ids)}",
            }
        )

    infos = {str(row.get("offer_id")): row for row in _result_items(product_info_response or {})
             if str(row.get("offer_id")) in offer_ids}
    media = video_readback(payload, by_offer, infos, offer_ids)
    media["expectation_available"] = bool(payload.get("variants"))
    if not media["expectation_available"]:
        # A legacy/missing submitted snapshot is not evidence that no video
        # was selected. Keep the ordinary legacy attribute checks independent.
        media["status"] = "expectation_unknown"
        media["note"] = "缺少本店铺原提交快照，无法确认是否应有视频；不能据此声称未选择或视频已完成"
    for row in media["items"]:
        if row["expected_count"]:
            checks.append({"name": f"video_readback[{row['offer_id']}]", "ok": row["status"] == "readable_in_api",
                           "pending": row["pending"], "detail": row["message"]})
    failed = [check for check in checks if not check["ok"] and not check.get("soft")]
    return {
        "ok": not failed,
        "product_id": product.name,
        "store": store_id,
        "offers": offer_ids,
        "checks": checks,
        "failed": [check["name"] for check in failed],
        "soft_failures": [check["name"] for check in checks if not check["ok"] and check.get("soft")],
        "media_readback": media,
        "api_writes_performed": False,
        "note": "只读核对：读的是 Ozon 服务端上真实存下来的数据",
    }


def readback_submitted(product_dir: Path | str, *, store_id: str, transport: Any | None = None,
                       registry_path: Path | str | None = None,
                       env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Explicit read only: two allowlisted API reads, never import/status replay.

    The posted canonical payload, not today's editable form, is the expected
    video snapshot. Reports can be cached after submission without unlocking it.
    """
    product = Path(product_dir)
    offers, fingerprint = _readback_scope(product, store_id)
    if not offers:
        return {"ok": False, "product_id": product.name, "store": store_id, "status": "not_submitted",
                "checks": [], "media_readback": {"status": "awaiting_submission", "items": [],
                    "buyer_playback_verified": False}, "api_writes_performed": False,
                "message": "当前店铺没有已提交货号，未请求 Ozon；不会自动创建或重新提交"}
    # Both POSTs are official pure reads. Do not follow a transport failure with
    # an import, media update, automatic retry, inventory operation or a third call.
    from .ozon_http import OzonHttpError
    reads = 0
    try:
        if transport is None:
            transport = _transport_for_store(store_id, transport_factory=None, registry_path=registry_path, env=env)
        reads += 1
        attributes = transport.post(PATH_PRODUCT_ATTRIBUTES, {"filter": {"offer_id": offers, "visibility": "ALL"}, "limit": 100})
        reads += 1
        info = transport.post(PATH_PRODUCT_INFO, {"offer_id": offers})
    except (OzonHttpError, TimeoutError, OSError) as error:
        # Do not cache provider response/error bodies or echoed signed URLs.
        return {"ok": False, "product_id": product.name, "store": store_id, "status": "readback_failed",
                "offers": offers, "checks": [], "failed": ["readonly_transport"],
                "api_writes_performed": False, "api_read_count": reads, "automatic_resubmit": False,
                "buyer_playback_verified": False,
                "media_readback": {"status": "readback_failed", "items": [], "buyer_playback_verified": False},
                "error_code": "OZON_READBACK_HTTP_ERROR" if isinstance(error, OzonHttpError) else "OZON_READBACK_CONNECTION_ERROR",
                "http_status": getattr(error, "status", None),
                "message": "只读回读失败；未重试、未重新提交。请检查店铺授权或网络后手动刷新"}
    info_rows = _result_items(info)
    info_scope_ok = (all(str(row.get("offer_id")) in offers for row in info_rows)
                     and len({str(row.get("offer_id")) for row in info_rows}) == len(info_rows)
                     and {str(row.get("offer_id")) for row in info_rows} == set(offers))
    report = verify_submitted(product, store_id=store_id, attributes_response=attributes, product_info_response=info)
    if not report["media_readback"]["expectation_available"]:
        report["ok"] = False
        report["checks"].append({"name": "submitted_media_snapshot_available", "ok": False,
                                 "detail": "缺少原提交快照，视频闭环无法对账；不会自动创建或重新提交"})
        report["failed"].append("submitted_media_snapshot_available")
    if not info_scope_ok:
        report["ok"] = False
        report["checks"].append({"name": "product_info_scope_valid", "ok": False,
                                 "detail": "商品信息回读有缺失、非台账货号或重复条目，不能视为完成"})
        report["failed"].append("product_info_scope_valid")
    report.update(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  status="verified_in_api" if report["ok"] else "incomplete",
                  api_read_count=reads, automatic_resubmit=False, buyer_playback_verified=False,
                  input_fingerprint=fingerprint,
                  product_statuses=[{"offer_id": row.get("offer_id"), "id": row.get("id"),
                                     "sku": row.get("sku"), "statuses": row.get("statuses") or {}}
                                    for row in info_rows if str(row.get("offer_id")) in offers])
    from .listing_form import write_json
    from .product_edit_lock import product_edit_lock
    with product_edit_lock(product):
        # Published products are intentionally not editable. This only stores
        # observational evidence; it does not save a draft or confirmation.
        if _readback_scope(product, store_id)[1] != fingerprint:
            raise ValueError("回读期间提交快照发生变化，结果已丢弃；请重新只读刷新")
        write_json(product / "output/store-runs" / store_id / READBACK_FILE, report)
    return report


def cached_readback(product_dir: Path | str, *, store_id: str) -> dict[str, Any]:
    """No network and no draft writes: GET should use this cache only."""
    product = Path(product_dir)
    _offers, fingerprint = _readback_scope(product, store_id)
    report = _read_json(product / "output/store-runs" / store_id / READBACK_FILE)
    valid = (report.get("product_id") == product.name and report.get("store") == store_id
             and report.get("input_fingerprint") == fingerprint)
    return report if valid else {"status": "stale" if report else "not_checked", "ok": False,
                     "product_id": product.name, "store": store_id,
                     "media_readback": {"status": "awaiting_readback", "items": [], "buyer_playback_verified": False},
                     "api_writes_performed": False, "automatic_resubmit": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提交后核对：读回 Ozon 上的商品逐项对账（只读）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--registry", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_submitted(args.product_dir, store_id=args.store, registry_path=args.registry)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"商品 {report['product_id']} → 店铺 {report['store']}｜核对：{'✅ 全部通过' if report['ok'] else '❌ 有未通过项'}")
        if report.get("error"):
            print("  ", report["error"])
        for check in report["checks"]:
            mark = "✅" if check["ok"] else ("⚠️" if check.get("soft") else "❌")
            print(f"  {mark} {check['name']}：{check['detail']}")
        if report.get("soft_failures"):
            print("  （⚠️ 为提示项：这类属性由 Ozon 的类目/型决定，可能不出现在 attributes 里）")
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
