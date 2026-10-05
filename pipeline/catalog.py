"""类目/属性相关的**本地** handler：变体规则判定与属性编译（不联网、不调模型）。

放在这里而不是 runner 里，是为了让 runner 保持"调度 + 门禁"的单一职责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from contracts import format_problems, validate_contract

from .attributes import build_attribute_fill_input, compile_attributes, evaluate_variant_rules, sha256_of
from .context import PipelineGateError, StepContext

CATEGORY_SNAPSHOT = "output/ozon-category-attributes.json"
#: 旁挂的变体属性文件（Ozon 的 is_aspect；上游快照契约里没有这个字段）
ASPECT_FILE = "output/ozon-aspect-attributes.json"
#: 旁挂的字典精确查值结果（品牌等大字典属性）
LOOKUP_FILE = "output/ozon-dictionary-lookups.json"
CATEGORY_FILE = "output/ozon-category.json"
FILL_INPUT = "output/attribute-fill-input.json"
ATTRIBUTES_FINAL = "output/ozon-attributes-final.json"
DESIGN_FILE = "output/ozon-ecommerce-design.json"
COPY_FILE = "output/copy-ru.json"
ANALYSIS_FILE = "output/product-analysis.json"


def handle_variant_rules(ctx: StepContext) -> dict[str, Any]:
    """SKU 变体规则：只在类目能承载该差异时才允许合并（拿不准就拆卡 / 转人工）。"""
    source = ctx.require_json("input/source.json")
    snapshot = ctx.require_json(CATEGORY_SNAPSHOT)
    skus = [item for item in (source.get("skus") or []) if isinstance(item, dict)]
    if not skus:
        raise PipelineGateError(ctx.step, "没有已选 SKU，无法判定变体规则")

    # 变体属性以旁挂文件（Ozon 的 is_aspect）为准；没有就退回按名字匹配（老行为）
    aspect_file = ctx.read_json(ASPECT_FILE) if ctx.path(ASPECT_FILE).is_file() else {}
    aspect_attributes = aspect_file.get("aspect_attributes") if isinstance(aspect_file, Mapping) else None
    result = evaluate_variant_rules(
        skus=skus,
        category_attributes=snapshot.get("attributes") or [],
        aspect_attributes=aspect_attributes or None,
    )
    problems = validate_contract("platform-grouping-result", result)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "变体规则结果不符合 platform-grouping-result 契约",
            {"problems": problems[:8], "summary": format_problems(problems)},
        )
    ctx.write_json("output/platform-grouping-result.json", result)

    warnings: list[str] = []
    if result["upload_strategy"] == "rule_required":
        warnings.append(result["reason"])
    return {
        "warnings": warnings,
        "artifacts": ["output/platform-grouping-result.json"],
        "upload_strategy": result["upload_strategy"],
        "platform_can_merge": result["platform_can_merge"],
    }


def handle_field_completion(ctx: StepContext) -> dict[str, Any]:
    """属性编译：把类目快照 + 有证据的事实编译成 ozon-attributes-final。

    缺的属性**如实报进 required_summary.missing**，由 upload_feasibility 去拦 ——
    这里不编造，也不因为缺属性就报错转人工（那样会把"可补资料"和"真错误"混为一谈）。
    """
    source = ctx.require_json("input/source.json")
    snapshot = ctx.require_json(CATEGORY_SNAPSHOT)
    category = ctx.read_json(CATEGORY_FILE)
    if not isinstance(snapshot.get("category_id"), int) or int(snapshot.get("category_id") or 0) < 1:
        raise PipelineGateError(ctx.step, "类目属性快照里没有有效的 category_id（先跑 category_match 拉实时类目）")
    if not isinstance(snapshot.get("type_id"), int) or int(snapshot.get("type_id") or 0) < 1:
        raise PipelineGateError(ctx.step, "类目属性快照里没有有效的 type_id（先跑 category_match 拉实时类目）")
    if str(category.get("metadata_source") or "") != "ozon_seller_api":
        raise PipelineGateError(
            ctx.step,
            "类目不是来自 Ozon Seller API（metadata_source != ozon_seller_api），拒绝编译属性",
        )
    if str(category.get("match_status") or "") not in {"api_confirmed", "api_match_needs_review"}:
        raise PipelineGateError(ctx.step, f"类目匹配状态不可用：{category.get('match_status')!r}")

    copy_bundle = ctx.read_json(COPY_FILE)
    analysis = ctx.read_json(ANALYSIS_FILE)
    warnings: list[str] = []

    from .sku_selection import active_skus

    active = active_skus(ctx.product_dir, source.get("skus") or [])
    fill_input_path = ctx.path(FILL_INPUT)
    if fill_input_path.is_file():
        fill_input = ctx.read_json(FILL_INPUT)
    else:
        # 只把"要上架"的 SKU 交给属性填值：否则会把没上架规格的颜色/容量当变体属性提交
        fill_input = build_attribute_fill_input(
            source={**dict(source), "skus": active}, copy_bundle=copy_bundle, analysis=analysis
        )
        ctx.write_json(FILL_INPUT, fill_input)
        warnings.append("本地生成了 output/attribute-fill-input.json（设计步骤尚未实现）")

    # 安全网：已有文件可能含没上架的 SKU → 过滤**并回写**，让磁盘状态与编译结果一致
    if isinstance(fill_input.get("skus"), list):
        allowed = {str(item.get("sku_id")) for item in active}
        kept = [item for item in fill_input["skus"] if str(item.get("sku_id")) in allowed]
        if len(kept) != len(fill_input["skus"]):
            warnings.append(f"按上架 SKU 选择过滤了属性变体：{len(fill_input['skus'])} → {len(kept)}")
            fill_input = {**fill_input, "skus": kept}
            ctx.write_json(FILL_INPUT, fill_input)

    # 大字典属性（品牌）用 category_match 时旁挂的"精确查值"结果填，保持编译步骤不发 API 调用
    lookup_file = ctx.read_json(LOOKUP_FILE) if ctx.path(LOOKUP_FILE).is_file() else {}
    dictionary_lookups = lookup_file.get("lookups") if isinstance(lookup_file, Mapping) else None

    compiled = compile_attributes(
        product_id=ctx.product_dir.name,
        category_snapshot=snapshot,
        fill_input=fill_input,
        design_hash=sha256_of(ctx.path(DESIGN_FILE)),
        fill_input_hash=sha256_of(fill_input_path),
        dictionary_lookups=dictionary_lookups,
    )
    problems = validate_contract("ozon-attributes-final", compiled)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "属性编译结果不符合 ozon-attributes-final 契约",
            {"problems": problems[:8], "summary": format_problems(problems)},
        )
    ctx.write_json(ATTRIBUTES_FINAL, compiled)

    summary = compiled["required_summary"]
    if summary["missing"]:
        warnings.append(
            f"必需属性仍缺 {summary['missing']} 个：{summary['missing_attribute_ids']}"
            "（需要人工补资料或模型翻译后重编译；upload_feasibility 会拦住）"
        )
    warnings.extend(compiled.get("warnings") or [])
    return {
        "warnings": warnings,
        "artifacts": [ATTRIBUTES_FINAL],
        "required_total": summary["total"],
        "required_filled": summary["filled"],
        "required_missing": summary["missing"],
    }


CATALOG_HANDLERS = {
    "variant_rules": handle_variant_rules,
    "field_completion": handle_field_completion,
}
