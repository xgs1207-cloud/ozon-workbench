"""属性编译（M4，纯本地）与变体规则判定。

两个约束驱动了这里的实现：

1. ``ozon-category-attributes.schema.json`` 里**没有 ``is_aspect```**（只有 attribute_id / name /
   required / type / dictionary_id / allowed_values…），所以变体判定只能按**属性名 + 字典值**匹配；
   拿不准时**不允许合并**（宁可拆卡，也不把非 aspect 属性当变体维度）。
2. **不编造属性值**：只填有证据的（类目字典里的"Нет бренда"、SKU 自己的俄语颜色、归一后的容量），
   中文事实（材质/认证等）不直接进买家可见字段 —— 缺就是缺，交给 `required_summary.missing` 如实报出来。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from parsing import parse_number
from rules.validate import normalize_capacity_text, normalize_russian_color_name

COMPILER_VERSION = "1.0.0"

#: 颜色类属性（Ozon 侧 attr 10097「Название цвета」等）
COLOR_PATTERNS: tuple[str, ...] = ("название цвета", "цвет товара", "цвет")
#: 容量/体积类属性
CAPACITY_PATTERNS: tuple[str, ...] = ("объём", "объем", "объем товара", "ёмкость", "емкость", "capacity", "volume")
#: 品牌类属性
BRAND_PATTERNS: tuple[str, ...] = ("бренд",)
#: 无品牌时的默认字典值（原项目 AGENTS.md 的"无品牌"规则）
UNBRANDED_TEXT = "Нет бренда"

#: **已知的 Ozon 变体属性 id**（比名字可靠：中文/俄文/英文命名都可能变）
KNOWN_ASPECT_IDS: dict[int, str] = {
    10096: "color",   # 商品颜色
    10097: "color",   # 颜色名称（合并变体时通常用这个）
    6771: "size_or_measurement",   # 纸张尺寸
    6781: "configuration",         # 板材类型
}

#: 变体维度候选（按名称匹配；中文与俄文都要认，因为属性名语言取决于拉取时的 language）
ASPECT_HINT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("color", "название цвета"),
    ("color", "цвет"),
    ("color", "颜色"),
    ("color", "color"),
    ("size_or_measurement", "объём"),
    ("size_or_measurement", "объем"),
    ("size_or_measurement", "размер"),
    ("size_or_measurement", "尺寸"),
    ("size_or_measurement", "规格"),
    ("size_or_measurement", "size"),
    ("configuration", "комплектация"),
    ("configuration", "配置"),
    ("configuration", "套装"),
    ("seller_specification", "исполнение"),
    ("seller_specification", "样式"),
)


def aspect_kind_for(attribute: Mapping[str, Any]) -> str | None:
    """判断一个属性是不是变体维度、属于哪一类（先认 id，再认中俄文名）。"""
    attribute_id = attribute.get("attribute_id") or attribute.get("id")
    try:
        known = KNOWN_ASPECT_IDS.get(int(attribute_id))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        known = None
    if known:
        return known
    name = str(attribute.get("attribute_name") or attribute.get("name") or "").casefold()
    for kind, hint in ASPECT_HINT_PATTERNS:
        if hint in name:
            return kind
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _matches(name: str, patterns: Sequence[str]) -> bool:
    lowered = str(name or "").strip().casefold()
    return any(pattern in lowered for pattern in patterns)


# --------------------------------------------------------------------- 变体规则


def detect_sku_differences(skus: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """从已选 SKU 里找出真实差异（颜色 / 容量），每个差异带上涉及的 SKU。"""
    differences: list[dict[str, Any]] = []
    for kind, extractor in (
        ("color", lambda sku: normalize_russian_color_name(sku.get("color_ru") or sku.get("color") or sku.get("color_zh"))),
        ("size_or_measurement", lambda sku: normalize_capacity_text(sku.get("capacity") or sku.get("capacity_text") or sku.get("volume"))),
    ):
        buckets: dict[str, list[str]] = {}
        for index, sku in enumerate(skus, start=1):
            value = extractor(sku)
            if not value:
                continue
            buckets.setdefault(value, []).append(str(sku.get("sku_id") or f"S{index}"))
        if len(buckets) > 1:
            differences.append(
                {
                    "kind": kind,
                    "values": sorted(buckets),
                    "sku_ids": sorted({sku for ids in buckets.values() for sku in ids}),
                }
            )
    return differences


def evaluate_variant_rules(
    *,
    skus: Sequence[Mapping[str, Any]],
    category_attributes: Sequence[Mapping[str, Any]],
    aspect_attributes: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """产出 ``platform-grouping-result`` 契约形状的判定结果。

    ``aspect_attributes``（来自 ``output/ozon-aspect-attributes.json``，是 Ozon 用 ``is_aspect``
    亲自标记的变体属性）比"按名字猜"可靠得多：名字会随拉取语言变化（中文/俄文），
    而 ``is_aspect`` 是权威标记。给了就用它，没给再退回按名字匹配（老行为）。
    """
    differences = detect_sku_differences(skus)

    candidates: list[Mapping[str, Any]] = []
    aspect_source = "name_hint"
    if aspect_attributes:
        candidates = list(aspect_attributes)
        aspect_source = "ozon_is_aspect"
    else:
        candidates = list(category_attributes)

    allowed: list[dict[str, Any]] = []
    for attribute in candidates:
        name = str(attribute.get("attribute_name") or attribute.get("name") or "")
        kind = aspect_kind_for(attribute)
        if not kind:
            if aspect_attributes:
                # 是变体属性但认不出属于哪一类 → 仍然列出来（人工可见），但不参与自动映射
                allowed.append(
                    {
                        "attribute_id": attribute.get("attribute_id") or attribute.get("id"),
                        "attribute_name": name,
                        "kind": "unknown_aspect",
                        "dictionary_id": attribute.get("dictionary_id"),
                        "has_dictionary_values": bool(attribute.get("allowed_values")),
                    }
                )
            continue
        allowed.append(
            {
                "attribute_id": attribute.get("attribute_id") or attribute.get("id"),
                "attribute_name": name,
                "kind": kind,
                "dictionary_id": attribute.get("dictionary_id"),
                "has_dictionary_values": bool(attribute.get("allowed_values")),
            }
        )

    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for difference in differences:
        matches = [item for item in allowed if item["kind"] == difference["kind"]]
        if matches:
            mapped.append({**difference, "attribute_id": matches[0]["attribute_id"], "attribute_name": matches[0]["attribute_name"]})
        else:
            unmapped.append(difference)

    if len(skus) <= 1:
        strategy = "single_sku"
        can_merge = False
        reason = "只有一个 SKU，不需要变体合并"
    elif not differences:
        strategy = "separate_cards"
        can_merge = False
        reason = "已选 SKU 之间没有可核实的差异（颜色/容量），不按变体合并"
    elif unmapped:
        strategy = "rule_required"
        can_merge = False
        reason = (
            "存在无法映射到类目变体属性的差异："
            + "、".join(f"{item['kind']}={'/'.join(item['values'])}" for item in unmapped)
            + f"；需要人工确认后再决定是否合并（变体属性来源：{aspect_source}）"
        )
    else:
        strategy = "merged_variants"
        can_merge = True
        reason = f"所有已选 SKU 的差异都能映射到类目变体属性（来源：{aspect_source}）"

    return {
        "internal_group_count": 1,
        "platform_card_count": 1 if can_merge else max(1, len(skus)),
        "platform_can_merge": can_merge,
        "allowed_aspect_attributes": allowed,
        "detected_sku_differences": differences,
        "mapped_aspect_attributes": mapped,
        "upload_strategy": strategy,
        "reason": reason,
    }


# --------------------------------------------------------------------- 属性填值输入


def build_attribute_fill_input(
    *,
    source: Mapping[str, Any],
    copy_bundle: Mapping[str, Any] | None = None,
    analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把"我们确实知道的事实"整理成属性填值输入（不猜、不翻译中文事实）。"""
    skus = [item for item in (source.get("skus") or []) if isinstance(item, Mapping)]
    copy_bundle = dict(copy_bundle or {})
    analysis_body = dict(analysis or {})

    sku_rows: list[dict[str, Any]] = []
    for index, sku in enumerate(skus, start=1):
        color = normalize_russian_color_name(sku.get("color_ru") or sku.get("color") or sku.get("color_zh"))
        capacity = normalize_capacity_text(sku.get("capacity") or sku.get("capacity_text") or sku.get("volume"))
        row: dict[str, Any] = {
            "sku_id": str(sku.get("sku_id") or f"S{index}"),
            "offer_id": sku.get("offer_id"),
            "color_ru": color,
            "capacity_ru": capacity,
            "price_cny": parse_number(sku.get("purchase_price_cny")),
            "evidence": ["input/source.json"],
        }
        sku_rows.append(row)

    materials: list[str] = []
    extra = source.get("extra") if isinstance(source.get("extra"), Mapping) else {}
    for item in extra.get("attributes") or []:
        if isinstance(item, Mapping) and _matches(str(item.get("name") or ""), ("材质", "材料", "material")):
            value = str(item.get("value") or "").strip()
            if value and re.search(r"[\u4e00-\u9fff]", value):
                # 中文材质需要翻译才能进 Ozon 字段，这里如实标记为待处理
                materials.append(f"NEEDS_TRANSLATION:{value}")
            elif value:
                materials.append(value)

    return {
        "schema_version": "1.0.0",
        "product_id": str(source.get("product_id") or "unknown"),
        "generated_at": now_iso(),
        "title_ru": copy_bundle.get("title_ru"),
        "core_keyword": copy_bundle.get("core_keyword"),
        "analysis_decision": ((analysis_body.get("recommendation") or {}) or {}).get("decision"),
        "product_level": {
            "materials": materials,
            "materials_need_translation": [item for item in materials if item.startswith("NEEDS_TRANSLATION:")],
            "unknowns": [
                {"field": item.get("field"), "reason": item.get("reason")}
                for item in (analysis_body.get("unknowns") or [])
                if isinstance(item, Mapping)
            ],
        },
        "skus": sku_rows,
    }


# --------------------------------------------------------------------- 属性编译


def _attribute_entry(
    *,
    attribute_id: int,
    attribute_name: str,
    required: bool,
    value: Any,
    source: str,
    scope: str,
    confidence: float,
    evidence: Sequence[str],
    dictionary_value_id: int | None = None,
    sku_id: str | None = None,
    mapping_method: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "attribute_id": int(attribute_id),
        "attribute_name": attribute_name,
        "required": bool(required),
        "value": value,
        "source": source,
        "confidence": round(float(confidence), 3),
        "dictionary_value_id": dictionary_value_id,
        "evidence": list(evidence),
        "scope": scope,
    }
    if sku_id:
        entry["sku_id"] = sku_id
    if mapping_method:
        entry["mapping_method"] = mapping_method
    return entry


def _dictionary_match(allowed_values: Sequence[Mapping[str, Any]], wanted: str) -> dict[str, Any] | None:
    target = str(wanted or "").strip().casefold()
    for item in allowed_values:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("value") or "").strip().casefold() == target:
            return {"dictionary_value_id": item.get("id"), "value": item.get("value")}
    return None


def forced_dictionary_value(attribute: Mapping[str, Any]) -> dict[str, Any] | None:
    """字典里**只有一个合法值**时，那就是强制值（没有选择余地，不算编造）。

    真实案例：类目 17028731/92612 的「类型」(attr 8229) 字典只有 ``床单`` 一个值 —— 如实填它，
    比留空等人工强。但**值被截断时不能用**（`values_truncated`：快照只存了前 N 个，
    看起来只剩一个不代表真的只有一个）。
    """
    if attribute.get("values_truncated"):
        return None
    allowed_values = [item for item in (attribute.get("allowed_values") or []) if isinstance(item, Mapping)]
    if len(allowed_values) != 1:
        return None
    item = allowed_values[0]
    value = str(item.get("value") or "").strip()
    if not value:
        return None
    return {"value": value, "dictionary_value_id": item.get("id")}


def compile_attributes(
    *,
    product_id: str,
    category_snapshot: Mapping[str, Any],
    fill_input: Mapping[str, Any],
    design_hash: str | None = None,
    fill_input_hash: str | None = None,
) -> dict[str, Any]:
    """把类目属性快照 + 填值输入编译成 ``ozon-attributes-final``。

    **只为有证据的字段赋值**：类目字典里的"Нет бренда"、SKU 的俄语颜色、归一后的容量。
    其余必需属性一律计入 ``required_summary.missing``（由 upload_feasibility 去拦）。
    """
    attributes = [item for item in (category_snapshot.get("attributes") or []) if isinstance(item, Mapping)]
    skus = [item for item in (fill_input.get("skus") or []) if isinstance(item, Mapping)]

    common: list[dict[str, Any]] = []
    by_sku: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    missing_ids: list[Any] = []
    required_total = 0
    required_filled = 0

    for attribute in attributes:
        attribute_id = attribute.get("attribute_id")
        name = str(attribute.get("attribute_name") or "")
        required = bool(attribute.get("required"))
        allowed_values = attribute.get("allowed_values") or []
        if not isinstance(attribute_id, int):
            continue
        if required:
            required_total += 1

        entry: dict[str, Any] | None = None
        if _matches(name, BRAND_PATTERNS):
            match = _dictionary_match(allowed_values, UNBRANDED_TEXT)
            if match:
                entry = _attribute_entry(
                    attribute_id=attribute_id,
                    attribute_name=name,
                    required=required,
                    value=match["value"],
                    dictionary_value_id=match["dictionary_value_id"],
                    source="category_dictionary",
                    scope="common",
                    confidence=0.9,
                    evidence=["output/ozon-category-attributes.json"],
                    mapping_method="project_unbranded_rule",
                )
            else:
                warnings.append(f"类目字典里没有「{UNBRANDED_TEXT}」，品牌属性 {attribute_id} 留空")
        elif _matches(name, COLOR_PATTERNS):
            for sku in skus:
                color = sku.get("color_ru")
                if not color:
                    continue
                match = _dictionary_match(allowed_values, color)
                by_sku.setdefault(str(sku.get("sku_id")), []).append(
                    _attribute_entry(
                        attribute_id=attribute_id,
                        attribute_name=name,
                        required=required,
                        value=match["value"] if match else color,
                        dictionary_value_id=match["dictionary_value_id"] if match else None,
                        source="sku_fact",
                        scope="sku",
                        confidence=0.85,
                        evidence=["input/source.json"],
                        sku_id=str(sku.get("sku_id")),
                        mapping_method="color_name_normalized",
                    )
                )
            if not any(sku.get("color_ru") for sku in skus) and required:
                missing_ids.append(attribute_id)
            continue
        elif _matches(name, CAPACITY_PATTERNS):
            for sku in skus:
                capacity = sku.get("capacity_ru")
                if not capacity:
                    continue
                match = _dictionary_match(allowed_values, capacity)
                by_sku.setdefault(str(sku.get("sku_id")), []).append(
                    _attribute_entry(
                        attribute_id=attribute_id,
                        attribute_name=name,
                        required=required,
                        value=match["value"] if match else capacity,
                        dictionary_value_id=match["dictionary_value_id"] if match else None,
                        source="sku_fact",
                        scope="sku",
                        confidence=0.8,
                        evidence=["input/source.json"],
                        sku_id=str(sku.get("sku_id")),
                        mapping_method="capacity_normalized",
                    )
                )
            if not any(sku.get("capacity_ru") for sku in skus) and required:
                missing_ids.append(attribute_id)
            continue

        if entry is None:
            forced = forced_dictionary_value(attribute)
            if forced:
                entry = _attribute_entry(
                    attribute_id=attribute_id,
                    attribute_name=name,
                    required=required,
                    value=forced["value"],
                    dictionary_value_id=forced["dictionary_value_id"],
                    source="category_dictionary",
                    scope="common",
                    confidence=0.95,
                    evidence=["output/ozon-category-attributes.json"],
                    mapping_method="single_dictionary_value_forced",
                )
                warnings.append(
                    f"属性 {attribute_id}「{name}」字典只有一个合法值，按强制值填入：{forced['value']}"
                )
            else:
                if required:
                    missing_ids.append(attribute_id)
                continue

        common.append(entry)

    filled_common_ids = {item["attribute_id"] for item in common}
    filled_sku_ids = {
        attribute["attribute_id"]
        for entries in by_sku.values()
        for attribute in entries
    }
    filled_ids = filled_common_ids | filled_sku_ids
    required_attributes = [
        item for item in attributes if isinstance(item, Mapping) and item.get("required")
    ]
    required_filled = len([item for item in required_attributes if item.get("attribute_id") in filled_ids])
    missing_ids = [item.get("attribute_id") for item in required_attributes if item.get("attribute_id") not in filled_ids]

    flattened = list(common) + [attribute for entries in by_sku.values() for attribute in entries]

    return {
        "schema_version": "1.0.0",
        "product_id": product_id,
        "category_id": int(category_snapshot.get("category_id") or 0),
        "type_id": int(category_snapshot.get("type_id") or 0),
        "schema_source": "ozon_seller_api",
        "compiler": {
            "attribute_fill_input_hash": fill_input_hash or "unknown",
            "ecommerce_design_hash": design_hash or "unknown",
            "compiler_version": COMPILER_VERSION,
        },
        "common_attributes": common,
        "attributes_by_sku": by_sku,
        "attributes": flattened,
        "required_summary": {
            "total": len(required_attributes),
            "filled": required_filled,
            "missing": len(missing_ids),
            "missing_attribute_ids": missing_ids,
        },
        "warnings": warnings,
    }
