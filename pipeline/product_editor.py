"""Partial, evidence-labelled local listing details; never changes a live listing."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .listing_form import read_json, write_json
from .product_edit_lock import product_file_transaction, serialized_product_edit

FACT_KEYS = {"material", "package_quantity"}
DIMENSION_KEYS = tuple(f"{kind}_{key}" for kind in ("product", "package")
                       for key in ("length_mm", "width_mm", "height_mm", "weight_g"))
DETAIL_KEYS = FACT_KEYS | set(DIMENSION_KEYS)
DETAIL_FILE = "input/listing-details.json"


def read_listing_details(directory: Path) -> dict[str, Any]:
    saved = read_json(directory / DETAIL_FILE)
    details: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    human = read_json(directory / "input/human-confirmations.json")
    overrides = read_json(directory / "input/workbench-sku-overrides.json")
    for key in FACT_KEYS:
        if key in human:
            details[key] = human[key]
    if "material" not in details and "material_zh" in human:
        details["material"] = human["material_zh"]
    if "product_weight_g" in human:
        details["product_weight_g"] = human["product_weight_g"]
    legacy_dimensions = human.get("product_dimensions_mm")
    if isinstance(legacy_dimensions, Mapping):
        for axis in ("length", "width", "height"):
            if axis in legacy_dimensions:
                details[f"product_{axis}_mm"] = legacy_dimensions[axis]
    block = overrides.get("product") or {}
    for key in DIMENSION_KEYS:
        if key in block:
            details[key] = block[key]
    for key in details:
        provenance[key] = {"source": "manual", "evidence": "已保存的人工确认资料",
                           "status": "confirmed"}
    # Presence matters: null is an explicit clear, not permission to refill it.
    details.update({key: value for key, value in (saved.get("details") or {}).items() if key in DETAIL_KEYS})
    provenance.update({key: value for key, value in (saved.get("provenance") or {}).items() if key in details})
    return {"details": details, "provenance": provenance, "saved": bool(saved),
            "api_writes_performed": False}


def _normalize(key: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if key == "material":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
            raise ValueError("材质必须是 1–500 字的明确资料，无法确认时请留空")
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 100_000_000:
        raise ValueError(f"{key} 必须是正整数，单位为毫米、克或件；不能估算缺失值")
    return value


@serialized_product_edit
def save_listing_details(directory: Path, details: Mapping[str, Any]) -> dict[str, Any]:
    from .guided_review import invalidate_from
    from .listing_autofill import build_basic_fields

    if not isinstance(details, Mapping) or set(details) - DETAIL_KEYS:
        raise ValueError("商品资料含不支持的字段；售价、条码和官方属性请在对应区域填写")
    current = read_listing_details(directory)
    merged = {**current["details"], **{key: _normalize(key, value) for key, value in details.items()}}
    # Validate all supplied facts before touching files. Compare only known pairs.
    for axis in ("length_mm", "width_mm", "height_mm", "weight_g"):
        product, package = merged.get(f"product_{axis}"), merged.get(f"package_{axis}")
        if product is not None and package is not None and package < product:
            raise ValueError("包装尺寸和重量不能小于商品本体；请核对供应商资料或实测值")
    if merged == current["details"]:
        return current
    sources = build_basic_fields(directory)
    provenance = dict(current["provenance"])
    for key in details:
        source = sources.get(key) or {}
        if merged[key] is not None and source.get("value") == merged[key]:
            provenance[key] = {name: source.get(name) for name in ("source", "evidence", "status")}
        else:
            provenance[key] = {"source": "manual", "evidence": "人工填写" if merged[key] is not None else "人工清空",
                               "status": "confirmed" if merged[key] is not None else "cleared"}
    payload = {"details": merged, "provenance": provenance,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    human_path = directory / "input/human-confirmations.json"
    human = read_json(human_path)
    for key in FACT_KEYS:
        if key in merged:
            if merged[key] is None:
                human.pop(key, None)
            else:
                human[key] = merged[key]
    if "material" in merged:
        human.pop("material_zh", None)
    if any(key.startswith("product_") for key in details):
        human.pop("product_dimensions_mm", None)
        human.pop("product_weight_g", None)
    # Bind existing approval digests to partial details too. Partial drafts must
    # not leave older confirmed dimensions approved behind the displayed form.
    human["listing_details"] = merged
    overrides_path = directory / "input/workbench-sku-overrides.json"
    overrides = read_json(overrides_path)
    block = dict(overrides.get("product") or {})
    for key in DIMENSION_KEYS:
        if key in merged:
            block.pop(key, None)
    # Preserve each confirmed axis independently. A complete shipping package
    # must not be discarded just because optional item measurements are unknown.
    block.update({key: merged[key] for key in DIMENSION_KEYS if merged.get(key) is not None})
    if block:
        overrides["product"] = block
    else:
        overrides.pop("product", None)
    with product_file_transaction(directory, ("status.json", DETAIL_FILE,
                                              "input/human-confirmations.json",
                                              "input/workbench-sku-overrides.json")):
        # Check the no-return gate before persisting any input changes.
        invalidate_from(directory, "product_analysis" if any(key in details for key in FACT_KEYS) else "measurements")
        write_json(directory / DETAIL_FILE, payload)
        write_json(human_path, human)
        if overrides or overrides_path.exists():
            write_json(overrides_path, overrides)
    return read_listing_details(directory)
