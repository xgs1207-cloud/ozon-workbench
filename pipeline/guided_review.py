"""Human approvals for the guided research-to-listing flow.

Approvals are tied to exact artifact bytes. Editing an upstream artifact or
changing a selected SKU silently invalidates downstream approvals; no stale
approval can authorize a live Ozon write.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import validate_contract
from rules.validate import validate_copy_bundle


REVIEW_FILE = "input/guided-review.json"
DEPENDENCIES = {
    "grouping": ("input/selected-skus.json", "input/category-selection.json", "output/platform-grouping-result.json"),
    "copy": ("input/source.json", "input/selected-skus.json", "input/selected-keywords.json",
             "input/category-selection.json", "input/human-confirmations.json",
             "output/product-analysis.json", "output/copy-ru.json"),
    "image_plan": ("input/selected-skus.json", "output/copy-ru.json", "output/image-plan.json"),
    "images": ("output/image-plan.json", "output/image-generation-report.json", "output/image-qc-report.json"),
    "fields": ("input/category-selection.json", "input/human-confirmations.json",
               "input/manual-prices.json", "output/pricing-result.json",
               "output/ozon-attributes-final.json", "output/platform-grouping-result.json",
               "input/workbench-sku-overrides.json", "input/listing-media.json"),
}


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, path)


def _image_paths(directory: Path) -> list[str]:
    plan = _read(directory / "output" / "image-plan.json")
    return [str(item.get("output_path")) for item in
            [*(plan.get("main_images") or []), *(plan.get("detail_images") or [])]
            if isinstance(item, Mapping) and item.get("output_path")]


def slot_fingerprint(slot: Mapping[str, Any]) -> str:
    """Only the inputs that affect this paid slot; a different slot can be redone alone."""
    fields = {key: slot.get(key) for key in ("slot", "prompt", "reference_image_ids",
                                             "reference_product_images", "output_path", "russian_text")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def digest(directory: Path | str, section: str) -> str | None:
    directory = Path(directory).resolve()
    if section not in DEPENDENCIES:
        raise ValueError("未知审核环节")
    if section == "copy" and (directory / "input/guided-workflow.json").is_file():
        from .guided_workflow import workflow_status
        copy = workflow_status(directory)["copy"]
        if not copy.get("confirmed"):
            return None
        return hashlib.sha256(json.dumps({"fingerprint": copy["fingerprint"],
            "payload": copy["payload"]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    paths = [*DEPENDENCIES[section], *(_image_paths(directory) if section == "images" else [])]
    digest_value = hashlib.sha256()
    for relative in paths:
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory):
            return None
        if not path.is_file():
            # Human confirmation is optional when nothing was missing.
            if relative not in {"input/human-confirmations.json", "input/workbench-sku-overrides.json", "input/listing-media.json"}:
                return None
            continue
        digest_value.update(relative.encode("utf-8"))
        digest_value.update(path.read_bytes())
    return digest_value.hexdigest()


def problems(directory: Path | str, section: str) -> list[str]:
    directory = Path(directory)
    if section not in DEPENDENCIES:
        return ["未知审核环节"]
    if digest(directory, section) is None:
        return ["前置资料或产物尚未齐全"]
    if section == "copy":
        if (directory / "input/guided-workflow.json").is_file():
            from .guided_workflow import workflow_status
            if not workflow_status(directory)["copy"].get("confirmed"):
                return ["请先选择并确认与最新规格、关键词一致的文案"]
            return validate_copy_bundle(_read(directory / "output/copy-ru.json"))
        copy_path = directory / "output" / "copy-ru.json"
        for relative in ("input/selected-skus.json", "input/selected-keywords.json",
                         "input/category-selection.json", "input/human-confirmations.json",
                         "output/product-analysis.json"):
            upstream = directory / relative
            if upstream.is_file() and upstream.stat().st_mtime_ns > copy_path.stat().st_mtime_ns:
                return [f"{relative} 比文案更新，请重新生成或人工完整复核文案"]
        return validate_copy_bundle(_read(copy_path))
    if section == "image_plan":
        if (directory / "input/guided-workflow.json").is_file():
            from .guided_workflow import workflow_status
            if workflow_status(directory)["plan"]["status"] != "ready":
                return ["图片规划已过期，请按最新规格和文案重新规划"]
        plan = _read(directory / "output" / "image-plan.json")
        from .sku_selection import active_skus

        source = _read(directory / "input" / "source.json")
        if len(plan.get("main_images") or []) != len(active_skus(directory, source.get("skus") or [])):
            return ["主图数量与当前上架 SKU 不一致，请重新生成图片计划"]
        return validate_contract("image-plan", plan)
    if section == "images":
        generation = _read(directory / "output" / "image-generation-report.json")
        if generation.get("final_images") is not True or generation.get("generator") != "doubao":
            return ["当前不是豆包生成的正式图片；占位图不能用于批次自动上架"]
        if generation.get("generated_slots") != generation.get("planned_slots"):
            return ["图片尚未覆盖整套规划图位"]
        plan = _read(directory / "output" / "image-plan.json")
        expected = {str(item.get("slot")): item for item in
                    [*(plan.get("main_images") or []), *(plan.get("detail_images") or [])]
                    if isinstance(item, Mapping)}
        produced = {str(item.get("slot")): item for item in generation.get("files") or []
                    if isinstance(item, Mapping)}
        if set(produced) != set(expected) or any(
            produced[slot].get("generator") != "doubao"
            or produced[slot].get("path") != spec.get("output_path")
            or produced[slot].get("slot_fingerprint") != slot_fingerprint(spec)
            for slot, spec in expected.items()
        ):
            return ["图片与当前图位提示词/参考图不一致，须重做变更的图位"]
        qc = _read(directory / "output" / "image-qc-report.json")
        if qc.get("critical_failures") or qc.get("decision") == "reject":
            return ["图片质检未通过"]
        if not _image_paths(directory):
            return ["图片计划里没有图位"]
    if section == "fields":
        attrs = _read(directory / "output" / "ozon-attributes-final.json")
        if (attrs.get("required_summary") or {}).get("missing") != 0:
            return ["Ozon 类目必填属性尚未补齐"]
        category = _read(directory / "input" / "category-selection.json")
        if category.get("source") != "ozon_seller_api" or category.get("confirmed_by_user") is not True:
            return ["尚未人工确认 Ozon 官方真实类目"]
        snapshot = _read(directory / "output" / "ozon-category-attributes.json")
        if (snapshot.get("category_id"), snapshot.get("type_id")) != (category.get("category_id"), category.get("type_id")):
            return ["Ozon 类目已变更，须重新拉取属性并准备载荷"]
        pricing = _read(directory / "output" / "pricing-result.json")
        manual = _read(directory / "input" / "manual-prices.json").get("prices") or {}
        if pricing.get("pricing_source") != "user_manual":
            return ["尚未把逐 SKU 人工售价重新编译进上架载荷"]
        for row in pricing.get("skus") or []:
            if not isinstance(row, Mapping):
                continue
            choice = manual.get(str(row.get("sku_id"))) or {}
            field = "selling_price_cny" if choice.get("currency") == "CNY" else "selling_price_rub"
            if choice.get("price") is None or abs(float(row.get(field) or 0) - float(choice["price"])) > .011:
                return ["人工售价与定价结果不一致，请重新准备载荷"]
    if section == "grouping":
        result = _read(directory / "output" / "platform-grouping-result.json")
        grouping_file = directory / "output" / "platform-grouping-result.json"
        selected = directory / "input" / "selected-skus.json"
        if selected.is_file() and selected.stat().st_mtime_ns > grouping_file.stat().st_mtime_ns:
            return ["SKU 选择已变更，请重新计算分组建议"]
        if result.get("upload_strategy") == "rule_required":
            return ["SKU 合并方式尚未决定"]
        category = _read(directory / "input" / "category-selection.json")
        snapshot = _read(directory / "output" / "ozon-category-attributes.json")
        if (snapshot.get("category_id"), snapshot.get("type_id")) != (category.get("category_id"), category.get("type_id")):
            return ["SKU 分组所用类目快照与当前选择不一致"]
    return []


def invalidate_from(directory: Path | str, step: str) -> None:
    """Requeue a pre-submission pipeline suffix after user-supplied facts change."""
    from .status import load_status, normalize, save_status
    from .steps import PIPELINE_STEPS

    if step not in PIPELINE_STEPS:
        raise ValueError("未知流水线步骤")
    directory = Path(directory)
    current = normalize(load_status(directory))
    if int(current.get("api_write_count") or 0) > 0:
        raise ValueError("此商品已有 Ozon 写入，不能在原商品流程中回退重跑")
    index = PIPELINE_STEPS.index(step)
    current["completed_steps"] = [name for name in current["completed_steps"]
                                  if name in PIPELINE_STEPS and PIPELINE_STEPS.index(name) < index]
    current["next_action"] = step
    current["current_step"] = step
    current["status"] = "QUEUED" if current.get("task_authorized") else "COLLECTED"
    current["attention_required"] = False
    save_status(directory, normalize(current))


def approve(directory: Path | str, section: str) -> dict[str, Any]:
    directory = Path(directory)
    blockers = problems(directory, section)
    if blockers:
        raise ValueError("；".join(blockers))
    current = digest(directory, section)
    if current is None:
        raise ValueError("产物尚未齐全")
    path = directory / REVIEW_FILE
    review = _read(path)
    review.setdefault("approved", {})[section] = {
        "sha256": current,
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write(path, review)
    return status(directory)


def manual_prices_complete(directory: Path | str) -> bool:
    from .sku_selection import active_skus

    directory = Path(directory)
    source = _read(directory / "input" / "source.json")
    skus = active_skus(directory, source.get("skus") or [])
    prices = _read(directory / "input" / "manual-prices.json").get("prices") or {}
    return bool(skus) and all(
        isinstance(prices.get(str(sku.get("sku_id"))), Mapping)
        and isinstance(prices[str(sku.get("sku_id"))].get("price"), (int, float))
        and prices[str(sku.get("sku_id"))]["price"] > 0
        for sku in skus
    )


def status(directory: Path | str) -> dict[str, Any]:
    from .status import load_status, normalize
    from .steps import PIPELINE_STEPS

    directory = Path(directory)
    review = _read(directory / REVIEW_FILE).get("approved") or {}
    sections = {}
    for name in DEPENDENCIES:
        current = digest(directory, name)
        issues = problems(directory, name)
        sections[name] = {"approved": bool(current and not issues and (review.get(name) or {}).get("sha256") == current),
                          "problems": issues}
    source = _read(directory / "output" / "product-analysis.json")
    decision = (source.get("recommendation") or {}).get("decision")
    modern = (directory / "input/guided-workflow.json").is_file()
    facts_ok = decision == "continue"
    if modern:
        from .guided_workflow import workflow_status
        facts_ok = bool(workflow_status(directory)["analysis"].get("confirmed"))
    from .sku_selection import selection_state

    sku_state = selection_state(directory)
    sku_ok = sku_state["has_selection"] and 1 <= sku_state["active_count"] <= 10 and not sku_state["unknown_in_selection"]
    prices_ok = manual_prices_complete(directory)
    blockers = []
    if not sku_ok:
        blockers.append("尚未确认要上架的 SKU")
    if not facts_ok:
        blockers.append("商品事实仍有待人工补证或 AI 分析尚未完成")
    if not prices_ok:
        blockers.append("所选 SKU 的人工售价尚未填完")
    completed = set(normalize(load_status(directory)).get("completed_steps") or [])
    pending_preparation = [step for step in PIPELINE_STEPS[:12] if step not in completed]
    if modern:
        from .listing_draft import card_ready
        pending_preparation = [] if card_ready(directory) else ["卡片资料编译"]
    if pending_preparation:
        blockers.append("准备流程尚未完成：" + "、".join(pending_preparation[:4]))
    for name, item in sections.items():
        if not item["approved"]:
            blockers.append(f"{name} 尚未审核确认或原产物已变更")
    return {"sections": sections, "facts_ready": facts_ok, "sku_ready": sku_ok,
            "manual_prices_ready": prices_ok, "ready_to_preflight": not blockers, "blockers": blockers}


def update_copy(directory: Path | str, *, title_ru: str, description_ru: str) -> dict[str, Any]:
    directory = Path(directory)
    path = directory / "output" / "copy-ru.json"
    copy = _read(path)
    if not copy:
        raise ValueError("尚未生成俄文文案")
    copy.update(title_ru=title_ru.strip(), description_ru=description_ru.strip(), manually_edited=True)
    errors = validate_copy_bundle(copy)
    if errors:
        raise ValueError("；".join(errors[:5]))
    _write(path, copy)
    return copy


def update_plan_slot(directory: Path | str, *, slot: str, prompt: str,
                     reference_ids: Sequence[str]) -> dict[str, Any]:
    directory = Path(directory)
    path = directory / "output" / "image-plan.json"
    plan = _read(path)
    if not plan:
        raise ValueError("尚未生成图片计划")
    reference_index = {str(item.get("id")): str(item.get("path")) for item in plan.get("reference_images") or []
                       if isinstance(item, Mapping)}
    ids = list(dict.fromkeys(str(item) for item in reference_ids))
    if not ids or len(ids) > 3 or any(item not in reference_index for item in ids):
        raise ValueError("须选 1–3 张此商品真实采集的参考图")
    root = directory.resolve()
    if any(not (directory / reference_index[item]).resolve().is_relative_to(root / "input")
           or not (directory / reference_index[item]).is_file() for item in ids):
        raise ValueError("所选参考图文件不存在")
    found = None
    for image in [*(plan.get("main_images") or []), *(plan.get("detail_images") or [])]:
        if isinstance(image, dict) and str(image.get("slot")) == slot:
            found = image
            break
    if found is None:
        raise ValueError("图位不存在")
    if not prompt.strip() or len(prompt) > 4000:
        raise ValueError("提示词须为 1–4000 字")
    found["prompt"] = prompt.strip()
    found["reference_image_ids"] = ids
    found["reference_product_images"] = [reference_index[item] for item in ids]
    found["operation"] = "generate_from_reference"
    found["status"] = "planned"
    errors = validate_contract("image-plan", plan)
    if errors:
        raise ValueError("图片计划校验失败：" + "；".join(errors[:4]))
    _write(path, plan)
    return plan
