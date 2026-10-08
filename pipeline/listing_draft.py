"""Compile an already reviewed draft. This stage never generates text or images."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .context import PipelineGateError, StepContext, read_json, write_json
from .product_edit_lock import product_edit_lock

CARD_FILE = "output/listing-card-prepared.json"
CARD_INPUTS = ("input/selected-skus.json", "input/category-selection.json",
               "input/human-confirmations.json", "input/workbench-sku-overrides.json",
               "input/manual-prices.json", "output/copy-ru.json",
               "output/ozon-category-attributes.json", "input/listing-grouping-choice.json",
               "input/listing-offer-ids.json")


def grouping_scope(directory: Path) -> str:
    from .sku_selection import active_skus
    source = read_json(directory / "input/source.json")
    selection = read_json(directory / "input/category-selection.json")
    return hashlib.sha256(json.dumps({"skus": active_skus(directory, source.get("skus") or []),
        "category": {key: selection.get(key) for key in ("shop_id", "category_id", "type_id")}},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def card_fingerprint(directory: Path) -> str:
    items = {name: read_json(directory / name) for name in CARD_INPUTS}
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _modern_ready(directory: Path, *, plan: bool = False):
    from .guided_workflow import workflow_status
    state = workflow_status(directory)
    for name in ("analysis", "copy"):
        if not state.get(name, {}).get("confirmed"):
            raise ValueError(f"请先确认最新的{'商品摘要' if name == 'analysis' else '文案'}")
    if plan and state.get("plan", {}).get("status") != "ready":
        raise ValueError("请先完成图片规划与图片审核")
    return state


def prepare_listing_card(directory: Path, *, shop: str) -> dict[str, Any]:
    from .runner import DEFAULT_HANDLERS
    from .listing_form import product_form, persist_category_form
    from .status import complete_step
    import api

    with product_edit_lock(directory):
        from .listing_form import _require_editable
        _require_editable(directory)
        _modern_ready(directory)
        selection = read_json(directory / "input/category-selection.json")
        if selection.get("shop_id") != shop:
            raise ValueError("目标店铺与已确认类目不一致，请重新确认店铺类目")
        from .listing_defaults import persist_user_defaults
        persist_user_defaults(directory, api.MARKET_DB_PATH.parent, shop_id=shop)
        form = product_form(directory, api.MARKET_DB_PATH.parent, shop_id=shop)
        persist_category_form(directory, form["form"])
        write_json(directory / "input/manual-pricing-required.json", {"required": True,
                    "reason": "分步上架必须使用逐规格人工确认售价"})
        # Never reuse a stale fill input derived from old specifications or copy.
        fill = directory / "output/attribute-fill-input.json"
        fill.unlink(missing_ok=True)
        executed, blockers = [], []
        for step in ("validate_source", "variant_rules", "measurements", "field_completion"):
            try:
                result = DEFAULT_HANDLERS[step](StepContext(directory, step))
                if step == "variant_rules":
                    choice = read_json(directory / "input/listing-grouping-choice.json")
                    grouped = read_json(directory / "output/platform-grouping-result.json")
                    if choice.get("scope") == grouping_scope(directory) and choice.get("strategy") == "separate_cards":
                        from .sku_selection import active_skus
                        count = len(active_skus(directory, read_json(directory / "input/source.json").get("skus") or []))
                        grouped.update(platform_card_count=count, platform_can_merge=False,
                            upload_strategy="separate_cards", reason="运营确认逐 SKU 拆卡，不使用变体合并")
                        write_json(directory / "output/platform-grouping-result.json", grouped)
                complete_step(directory, step)
                executed.append({"step": step, "result": result})
            except (PipelineGateError, ValueError) as error:
                blockers.append(f"{step}: {error}")
        from .guided_review import manual_prices_complete, problems
        from .measurements import load_measurements
        blockers.extend(problems(directory, "fields"))
        blockers.extend(problems(directory, "grouping"))
        if not manual_prices_complete(directory):
            blockers.append("请逐规格填写人工确认的正数售价")
        surface = load_measurements(directory)
        attributes = read_json(directory / "output/ozon-attributes-final.json")
        missing = (attributes.get("required_summary") or {}).get("missing_attribute_ids") or []
        if missing:
            labels = {row["attribute_id"]: row["name"] for row in form["form"]["fields"]}
            blockers.append("官方必填未填写：" + "、".join(labels.get(key, str(key)) for key in missing))
        for name, label in (("package", "含包装"),):
            dimensions = surface.get(name) or {}
            if any(not isinstance(dimensions.get(key), int) or dimensions.get(key, 0) <= 0
                   for key in ("length_mm", "width_mm", "height_mm", "weight_g")):
                blockers.append(f"{label}尺寸重量尚未完整确认")
        if not surface.get("hierarchy_ok", True):
            blockers.append("包装尺寸或重量不能小于商品本体")
        blockers = list(dict.fromkeys(blockers))
        if not blockers:
            complete_step(directory, "category_match")
        result = {"ok": not blockers, "shop": shop, "input_fingerprint": card_fingerprint(directory),
                  "executed": executed, "blockers": blockers, "api_writes_performed": False,
                  "model_calls": 0}
        write_json(directory / CARD_FILE, result)
        return result


def card_ready(directory: Path) -> bool:
    result = read_json(directory / CARD_FILE)
    return bool(result.get("ok") and result.get("input_fingerprint") == card_fingerprint(directory))


def canonical_listing(directory: Path, *, shop: str) -> dict[str, Any]:
    from .stores import ensure_registry, list_shops
    from .upload import build_upload_payload, payload_problems
    from .ozon_write import build_import_request

    workflow = _modern_ready(directory, plan=True)
    if workflow.get("publication_blockers"):
        raise ValueError("发布前须解决商品合规风险：" + "；".join(workflow["publication_blockers"][:4]))
    if not card_ready(directory):
        raise ValueError("卡片资料已变化或尚未编译，请重新填充并检查卡片")
    from .guided_review import status as review_status
    review = review_status(directory)
    if not review["ready_to_preflight"]:
        raise ValueError("；".join(review["blockers"][:8]))
    shops = {str(row["id"]): row for row in list_shops(ensure_registry(None))}
    selected = shops.get(shop)
    if not selected or not selected.get("enabled"):
        raise ValueError("请选择已授权并启用的店铺")
    if read_json(directory / "input/category-selection.json").get("shop_id") != shop:
        raise ValueError("发布店铺与已确认类目店铺不一致")
    payload = build_upload_payload(directory, shop_name=shop, upload_mode="production",
                                   currency_code=selected.get("default_currency_code"))
    issues = payload_problems(payload, upload_mode="production")
    if issues:
        raise ValueError("；".join(issues[:8]))
    # The exact same draft must pass the official API compiler before XLS export.
    build_import_request(payload)
    return payload


def submission_editable(directory: Path | str) -> bool:
    """Only an explicitly rejected, never-created submission may be edited.

    Keep the attempt receipt: permission is derived from its structured outcome,
    never from deleting a lock file or treating a timeout as rejection.
    """
    from .publications import load_publications
    from .status import load_status
    directory = Path(directory)
    attempt_path = directory / "runtime/listing-submit-attempt.json"
    if not attempt_path.is_file():
        return int(load_status(directory).get("api_write_count") or 0) == 0
    attempt = read_json(attempt_path)
    if attempt.get("state") != "rejected" or attempt.get("safe_to_retry") is not True:
        return False
    entries = load_publications(directory).get("stores") or {}
    return not any(entry.get("task_id") or any(row.get("task_id") or row.get("ozon_product_id")
        for row in entry.get("sku_publications") or []) for entry in entries.values())


def submit_listing(directory: Path, *, shop: str, retry_rejected: bool = False,
                   publication_config_required: bool = False,
                   publication_db_path: Path | str | None = None) -> dict[str, Any]:
    from .guided_review import status as review_status
    from .preflight import preflight
    from .upload import upload_product
    from .ozon_write import OzonWriteUploader
    from .publications import load_publications
    from .ozon_status import confirm_product

    with product_edit_lock(directory):
        previous = load_publications(directory).get("stores", {}).get(shop, {})
        receipt_dir = directory / "output/store-runs" / shop
        receipt_task = (read_json(receipt_dir / "submission-receipt.json").get("task_id")
                        or read_json(receipt_dir / "ozon-result.json").get("task_id"))
        known_task = previous.get("task_id") or next((row.get("task_id")
            for row in previous.get("sku_publications", []) if row.get("task_id")), None) or receipt_task
        if str(known_task or "").isdigit() and int(known_task) > 0:
            # A repeated click reconciles the existing task; it never writes a
            # second import merely because polling was slow or failed earlier.
            confirmation = confirm_product(directory, shop, max_attempts=1, interval_seconds=0)
            return {"ok": bool(confirmation.get("ok")), "status": "already_submitted",
                "publication": load_publications(directory).get("stores", {}).get(shop, {}),
                "reconciliation": confirmation, "api_writes": 0}
        # An ambiguous result is never retried automatically.
        attempt = directory / "runtime/listing-submit-attempt.json"
        previous_attempt = read_json(attempt) if attempt.is_file() else {}
        if previous_attempt and not (retry_rejected and previous_attempt.get("shop") == shop
                                     and submission_editable(directory)):
            raise ValueError("此商品已有提交尝试，请先回读 Ozon 结果；明确拒绝后须修正资料并确认重试")
        payload = canonical_listing(directory, shop=shop)
        fingerprint = hashlib.sha256(json.dumps({key: value for key, value in payload.items()
            if key != "generated_at"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if previous_attempt and previous_attempt.get("request_sha256") == fingerprint:
            raise ValueError("拒绝后的资料尚未改变，请先修正错误，不可原样重复提交")
        review = review_status(directory)
        if not review["ready_to_preflight"]:
            raise ValueError("；".join(review["blockers"][:8]))
        checked = preflight(directory, shop=shop)
        if not checked.get("ok"):
            raise ValueError("提交前预检未通过：" + "；".join([
                *checked.get("problems", []), *checked.get("production_blockers", [])][:8]))
        if previous_attempt:
            old_hash = hashlib.sha256(json.dumps(previous_attempt, sort_keys=True,
                ensure_ascii=False).encode()).hexdigest()
            write_json(directory / "runtime/listing-submit-history" / f"{old_hash}.json", previous_attempt)
        if publication_config_required:
            from .listing_publications import seal_config
            seal_config(directory, shop=shop, payload=payload, db_path=publication_db_path)
        write_json(attempt, {"shop": shop, "state": "started", "request_sha256": fingerprint,
                             "safe_to_retry": False, "no_automatic_retry": True})
        try:
            result = upload_product(directory, [shop], OzonWriteUploader(), upload_mode="production",
                                    enabled_store_ids=[shop], publication_db_path=publication_db_path)
        except Exception:
            write_json(attempt, {"shop": shop, "state": "unknown_requires_readback",
                "request_sha256": fingerprint, "safe_to_retry": False, "no_automatic_retry": True})
            raise ValueError("提交结果未确定，已暂停重试；请回读 Ozon 导入结果") from None
        from .status import load_status, save_status
        current = load_status(directory)
        current["api_write_count"] = int(current.get("api_write_count") or 0) + int(result.get("api_writes") or 0)
        store_result = (result.get("stores") or {}).get(shop, {})
        outcome = store_result.get("outcome") or (
            "processing" if result.get("submitted") else "unknown_requires_readback")
        confirmation_status = (store_result.get("confirmation") or {}).get("status")
        if confirmation_status == "created":
            outcome = "imported"
        safe_to_retry = bool(outcome == "rejected" and store_result.get("safe_to_retry") is True
                             and not store_result.get("task_id"))
        result["ok"] = bool(result.get("ok", result.get("submitted") and not result.get("failed")))
        result["state"] = outcome
        result["safe_to_retry"] = safe_to_retry
        current["guided_submit_state"] = outcome
        save_status(directory, current)
        write_json(directory / "output/upload-summary.json", result)
        write_json(attempt, {"shop": shop, "state": outcome, "request_sha256": fingerprint,
            "safe_to_retry": safe_to_retry, "report": result, "no_automatic_retry": True})
        if publication_config_required and store_result.get("task_id"):
            # The SUBMIT confirmation covers this one bounded stock action.
            # Polling GETs and legacy import workflows never enter this branch.
            from .listing_publications import continue_stocks
            try:
                stock_update = continue_stocks(directory, shop=shop, confirm="UPDATE_STOCK",
                                               db_path=publication_db_path)
            except Exception:
                stock_update = {"ok": False, "status": "stock_pending", "pending": True,
                    "api_writes": 0, "warning": "导入已记录，库存尚未完成；请查看台账并明确继续库存流程",
                    "automatic_retry": False}
            result["stock_update"] = stock_update
            result["api_writes"] = int(result.get("api_writes") or 0) + int(stock_update.get("api_writes") or 0)
            result["ok"] = result["ok"] and stock_update.get("ok", False)
            write_json(directory / "output/upload-summary.json", result)
            write_json(attempt, {"shop": shop, "state": outcome, "request_sha256": fingerprint,
                "safe_to_retry": safe_to_retry, "report": result, "no_automatic_retry": True})
        return result
