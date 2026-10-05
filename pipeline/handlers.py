"""模型驱动的步骤 handler（M2）。

铁律：模型输出必须先过**契约校验**（``contracts/original/*.schema.json``，用我们自己的轻量校验器）
与**规则校验**（``rules/``，来自原项目 skill 的硬规则）。任一不过就抛 :class:`PipelineGateError`，
商品转 ``NEEDS_ATTENTION`` —— **绝不把不合格文案写进产物**。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import format_problems, validate_contract
from models import AnalysisRequest, CopyRequest, ModelError, existing_source_refs, load_provider
from rules.validate import official_copy_checks, validate_copy_bundle

from .context import PipelineGateError, StepContext
from .selection import load_selected_keywords

SOURCE_REF_CANDIDATES: tuple[str, ...] = (
    "input/source.json",
    "input/raw-snapshot.json",
    "input/category-selection.json",
    "input/selected-keywords.json",
)

#: (契约名, 模型返回包里的键, 落盘路径)
CONTRACT_FILES: tuple[tuple[str, str, str], ...] = (
    ("title-ru", "title_ru", "output/title-ru.json"),
    ("description-ru", "description_ru", "output/description-ru.json"),
    ("keywords-ru", "keywords_ru", "output/keywords-ru.json"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _refs(ctx: StepContext) -> list[str]:
    refs = existing_source_refs(ctx.product_dir, SOURCE_REF_CANDIDATES)
    return refs or ["input/source.json"]


def _source_with_category(ctx: StepContext, source: Mapping[str, Any]) -> dict[str, Any]:
    """把"后来补的类目"合进 source（采集时没选类目、之后补选的场景）。

    采集入库时没选类目 → source.json 里 ``selected_category`` 为空；
    用户之后在 ``input/category-selection.json`` 里补选（或由 ``pipeline.category --set-product`` 写入）
    时，分析/设计等步骤应该能看见它，而不是永远卡在"缺少类目"。**只是合并已有值，绝不猜。**
    """
    merged = dict(source)
    current = merged.get("selected_category")
    if isinstance(current, Mapping) and current.get("category_id") and current.get("type_id"):
        return merged
    selection = ctx.read_json("input/category-selection.json")
    if isinstance(selection, Mapping) and selection.get("category_id") and selection.get("type_id"):
        merged["selected_category"] = dict(selection)
        merged.setdefault("category_source", "input/category-selection.json")
    return merged


def handle_product_analysis(ctx: StepContext) -> dict[str, Any]:
    """商品信息总结：调模型 → 契约校验 → 落盘。阻断性风险与"需人工确认"都会转人工。"""
    provider = ctx.require_provider()
    source = _source_with_category(ctx, ctx.require_json("input/source.json"))
    try:
        payload = provider.analyze_product(
            AnalysisRequest(
                product_id=ctx.product_dir.name,
                product_dir=ctx.product_dir,
                source=source,
                source_refs=_refs(ctx),
            )
        )
    except ModelError as error:
        raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error

    problems = validate_contract("product-analysis", payload)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "商品分析不符合 product-analysis 契约",
            {"problems": problems[:10], "summary": format_problems(problems)},
        )

    ctx.write_json("output/product-analysis.json", payload)

    recommendation = payload.get("recommendation") or {}
    decision = str(recommendation.get("decision") or "unknown")
    if decision == "reject":
        raise PipelineGateError(ctx.step, f"模型建议拒绝上架：{recommendation.get('reason') or '未说明'}")
    if decision == "needs_human_input":
        raise PipelineGateError(ctx.step, f"模型要求人工确认：{recommendation.get('reason') or '未说明'}")

    blocking = [item for item in (payload.get("risks") or []) if item.get("blocking")]
    if blocking:
        raise PipelineGateError(
            ctx.step,
            "存在阻断性风险：" + "；".join(str(item.get("message") or item.get("area")) for item in blocking),
            {"risks": blocking},
        )
    return {
        "warnings": [],
        "artifacts": ["output/product-analysis.json"],
        "decision": decision,
        "sku_count": len((payload.get("facts") or {}).get("skus") or []),
    }


def handle_russian_copy(ctx: StepContext) -> dict[str, Any]:
    """标题/简介/标签。

    上游语义：``russian_copy`` 是**纯投影**（文案由设计步骤产出）。所以这里优先从
    ``output/ozon-ecommerce-design.json`` 投影；设计还不存在时才退回调模型生成。
    """
    provider = ctx.require_provider()
    source = ctx.require_json("input/source.json")
    analysis = ctx.read_json("output/product-analysis.json")

    design = ctx.read_json("output/ozon-ecommerce-design.json")
    warnings: list[str] = []
    if design:
        from models.design import project_copy_from_design

        bundle = project_copy_from_design(
            design=design, product_id=ctx.product_dir.name, source_refs=_refs(ctx)
        )
        warnings.append("文案来自设计文档的投影（russian_copy 不调模型）")
    else:
        selection = load_selected_keywords(ctx.product_dir)
        if not selection or not selection.get("keywords"):
            raise PipelineGateError(
                ctx.step,
                "还没有选词：先在关键词库选词并写入 input/selected-keywords.json（见 pipeline.selection）",
            )
        try:
            bundle = provider.write_copy_ru(
                CopyRequest(
                    product_id=ctx.product_dir.name,
                    product_dir=ctx.product_dir,
                    source=source,
                    source_refs=_refs(ctx),
                    analysis=analysis,
                    selected_keywords=list(selection.get("keywords") or []),
                )
            )
        except ModelError as error:
            raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error

    documents: dict[str, Mapping[str, Any]] = {}
    problems: list[str] = []
    for contract_name, bundle_key, _ in CONTRACT_FILES:
        document = bundle.get(bundle_key)
        if not isinstance(document, Mapping):
            problems.append(f"{contract_name}：模型没有返回该文档（缺 {bundle_key}）")
            continue
        documents[contract_name] = document
        problems.extend(f"{contract_name}: {item}" for item in validate_contract(contract_name, document))

    copy_bundle = bundle.get("copy_bundle")
    if isinstance(copy_bundle, Mapping):
        problems.extend(validate_copy_bundle(copy_bundle))
    else:
        problems.append("copy_bundle：模型没有返回文案包")

    if problems:
        raise PipelineGateError(
            ctx.step,
            "文案未通过契约/规则校验",
            {"problems": problems[:12]},
        )

    # Ozon 官方规则的"建议项"：不阻断，但要让人看到（例如标题超推荐长度、绝对化用语）
    if isinstance(copy_bundle, Mapping):
        warnings.extend(official_copy_checks(copy_bundle)["advisory"])

    artifacts: list[str] = []
    for contract_name, _, relative in CONTRACT_FILES:
        ctx.write_json(relative, documents[contract_name])
        artifacts.append(relative)

    assert isinstance(copy_bundle, Mapping)
    ctx.write_json(
        "output/copy-ru.json",
        {
            "schema_version": "1.0.0",
            "product_id": ctx.product_dir.name,
            "generated_by": getattr(provider, "name", "unknown"),
            "generated_at": now_iso(),
            **dict(copy_bundle),
        },
    )
    artifacts.append("output/copy-ru.json")

    return {
        "warnings": warnings,
        "artifacts": artifacts,
        "title_ru": copy_bundle.get("title_ru"),
        "hashtags": len(copy_bundle.get("hashtags") or []),
        "primary_keywords": len(copy_bundle.get("primary_keywords") or []),
        "projected": bool(design),
    }


def handle_image_plan(ctx: StepContext) -> dict[str, Any]:
    """图片规划（M3）：N 张 SKU 主图 + 恰好 8 张共享详情图，并产出给运营看的规划说明。

    比契约多一条硬检查：主图数量必须等于已选 SKU 数量（契约只约束 1–10）。
    """
    from models import ImagePlanRequest
    from models.image_plan import render_plan_brief

    provider = ctx.require_provider()
    source = ctx.require_json("input/source.json")
    analysis = ctx.read_json("output/product-analysis.json")
    copy_bundle = ctx.read_json("output/copy-ru.json")
    warnings: list[str] = []
    if not copy_bundle:
        raise PipelineGateError(
            ctx.step,
            "还没有 output/copy-ru.json：图片上的文字必须来自文案，请先生成文案再规划图片",
        )

    existing = ctx.read_json("output/image-plan.json")
    if existing:
        # 上游语义：图片计划是设计文档的物化投影（设计步骤已经算过），这里只做校验
        plan = existing
        warnings.append("图片计划来自设计步骤的物化产物（image_plan 不重复调模型）")
    else:
        try:
            plan = provider.plan_images(
                ImagePlanRequest(
                    product_id=ctx.product_dir.name,
                    product_dir=ctx.product_dir,
                    source=source,
                    source_refs=_refs(ctx),
                    analysis=analysis,
                    copy_bundle=copy_bundle,
                )
            )
        except (ModelError, ValueError) as error:
            raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error

    problems = validate_contract("image-plan", plan)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "图片计划不符合 image-plan 契约",
            {"problems": problems[:12], "summary": format_problems(problems)},
        )

    from .sku_selection import active_skus

    # 数量按"要上架的 SKU"算：选择文件排除掉的规格不该有主图，也不该被要求有
    sku_count = len(active_skus(ctx.product_dir, source.get("skus") or []))
    main_images = list(plan.get("main_images") or [])
    detail_images = list(plan.get("detail_images") or [])
    if len(main_images) != sku_count:
        raise PipelineGateError(
            ctx.step,
            f"主图数量 {len(main_images)} 与要上架的 SKU 数 {sku_count} 不一致（每个上架 SKU 必须恰好 1 张主图）",
            {"sku_count": sku_count, "main_images": len(main_images)},
        )
    if len(detail_images) != 8:
        raise PipelineGateError(
            ctx.step,
            f"共享详情图必须是 8 张，实际 {len(detail_images)}",
            {"detail_images": len(detail_images)},
        )

    ctx.write_json("output/image-plan.json", plan)
    brief_path = ctx.path("output/image-plan-brief.md")
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(render_plan_brief(plan), encoding="utf-8")

    for item in main_images + detail_images:
        if item.get("status") == "needs_review":
            warnings.append(f"{item.get('slot')}: {item.get('failure_reason') or '需要人工确认'}")

    return {
        "warnings": warnings,
        "artifacts": ["output/image-plan.json", "output/image-plan-brief.md"],
        "main_images": len(main_images),
        "detail_images": len(detail_images),
        "needs_review": sum(1 for item in main_images + detail_images if item.get("status") == "needs_review"),
    }


def handle_product_positioning(ctx: StepContext) -> dict[str, Any]:
    """商品定位：调模型 → 过 product-positioning 契约 → 落盘。"""
    from models import PositionRequest

    provider = ctx.require_provider()
    source = ctx.require_json("input/source.json")
    analysis = ctx.read_json("output/product-analysis.json")
    copy_bundle = ctx.read_json("output/copy-ru.json")
    pricing = ctx.read_json("output/pricing-result.json")
    try:
        payload = provider.position_product(
            PositionRequest(
                product_id=ctx.product_dir.name,
                product_dir=ctx.product_dir,
                source=source,
                source_refs=_refs(ctx),
                analysis=analysis,
                copy_bundle=copy_bundle,
                pricing=pricing,
            )
        )
    except (ModelError, ValueError) as error:
        raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error

    problems = validate_contract("product-positioning", payload)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "商品定位不符合 product-positioning 契约",
            {"problems": problems[:10], "summary": format_problems(problems)},
        )
    ctx.write_json("output/product-positioning.json", payload)
    return {
        "warnings": [],
        "artifacts": ["output/product-positioning.json"],
        "price_position": payload.get("recommended_price_position"),
        "unknowns": len(payload.get("unknowns") or []),
    }


def handle_ecommerce_design(ctx: StepContext) -> dict[str, Any]:
    """电商设计：组装完整设计文档（过 ozon-ecommerce-design 契约）。

    上游语义是"设计步骤产出文案与图片方案"，所以我们在这里按需生成文案
    （``output/copy-ru.json`` 不存在时调模型的 ``write_copy_ru``），
    紧随其后的 ``russian_copy`` 则退化为**纯投影**。
    """
    from models import CopyRequest, DesignRequest, ImagePlanRequest
    from models.image_plan import render_plan_brief

    provider = ctx.require_provider()
    source = ctx.require_json("input/source.json")
    analysis = ctx.read_json("output/product-analysis.json")
    image_plan = ctx.read_json("output/image-plan.json")
    attributes_final = ctx.read_json("output/ozon-attributes-final.json")
    positioning = ctx.read_json("output/product-positioning.json")

    selection = load_selected_keywords(ctx.product_dir)
    copy_bundle = ctx.read_json("output/copy-ru.json")
    warnings: list[str] = []
    if not copy_bundle:
        if not selection or not selection.get("keywords"):
            raise PipelineGateError(
                ctx.step,
                "还没有选词：先在关键词库选词（input/selected-keywords.json）再生成设计与文案",
            )
        try:
            bundle = provider.write_copy_ru(
                CopyRequest(
                    product_id=ctx.product_dir.name,
                    product_dir=ctx.product_dir,
                    source=source,
                    source_refs=_refs(ctx),
                    analysis=analysis,
                    selected_keywords=list(selection.get("keywords") or []),
                    positioning=positioning,
                )
            )
        except ModelError as error:
            raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error
        copy_bundle = dict(bundle.get("copy_bundle") or {})
        warnings.append("设计步骤内联生成了文案（output/copy-ru.json 之前不存在）")

    problems = [f"copy: {item}" for item in validate_copy_bundle(copy_bundle)]
    if problems:
        raise PipelineGateError(ctx.step, "文案未通过规则校验，无法生成设计", {"problems": problems[:10]})

    if not image_plan:
        # 上游语义：设计步骤**产出**图片计划（image_plan 步骤只是物化/校验）
        try:
            image_plan = provider.plan_images(
                ImagePlanRequest(
                    product_id=ctx.product_dir.name,
                    product_dir=ctx.product_dir,
                    source=source,
                    source_refs=_refs(ctx),
                    analysis=analysis,
                    copy_bundle=copy_bundle,
                )
            )
        except (ModelError, ValueError) as error:
            raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error
        plan_problems = validate_contract("image-plan", image_plan)
        if plan_problems:
            raise PipelineGateError(
                ctx.step,
                "由设计步骤算出的图片计划不符合 image-plan 契约",
                {"problems": plan_problems[:10], "summary": format_problems(plan_problems)},
            )
        ctx.write_json("output/image-plan.json", image_plan)
        brief_path = ctx.path("output/image-plan-brief.md")
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(render_plan_brief(image_plan), encoding="utf-8")
        warnings.append("设计步骤物化了图片计划（output/image-plan.json）")

    try:
        design = provider.design_listing(
            DesignRequest(
                product_id=ctx.product_dir.name,
                product_dir=ctx.product_dir,
                source=source,
                source_refs=_refs(ctx),
                analysis=analysis,
                copy_bundle=copy_bundle,
                image_plan=image_plan,
                attributes_final=attributes_final,
                positioning=positioning,
            )
        )
    except (ModelError, ValueError) as error:
        raise PipelineGateError(ctx.step, f"模型层失败：{error}") from error

    problems = validate_contract("ozon-ecommerce-design", design)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "设计文档不符合 ozon-ecommerce-design 契约",
            {"problems": problems[:12], "summary": format_problems(problems)},
        )
    ctx.write_json("output/ozon-ecommerce-design.json", design)
    ctx.write_json(
        "output/design-provenance.json",
        {
            "schema_version": "1.0.0",
            "product_id": ctx.product_dir.name,
            "generated_by": getattr(provider, "name", "unknown"),
            "generated_at": design.get("processing", {}).get("generated_at"),
            "contract_note": "processing.model_mode 是上游契约常量 connected_codex，不代表真实生成方",
            "main_images": len(design.get("main_images") or []),
            "detail_images": len(design.get("detail_images") or []),
            "sku_count": len(design.get("sku_plan") or []),
        },
    )
    return {
        "warnings": warnings,
        "artifacts": ["output/ozon-ecommerce-design.json", "output/design-provenance.json"],
        "main_images": len(design.get("main_images") or []),
        "detail_images": len(design.get("detail_images") or []),
        "selling_points": len((design.get("listing") or {}).get("selling_points") or []),
    }


MODEL_HANDLERS = {
    "product_analysis": handle_product_analysis,
    "product_positioning": handle_product_positioning,
    "ecommerce_design": handle_ecommerce_design,
    "russian_copy": handle_russian_copy,
    "image_plan": handle_image_plan,
}


def model_handlers(provider: Any | None = None) -> dict[str, Any]:
    """只有传了 provider 才注册模型步骤 —— 否则 runner 会如实报 handler_not_implemented。"""
    return dict(MODEL_HANDLERS) if provider is not None else {}


def run_single_step(
    product_dir: Path | str,
    step: str,
    *,
    provider: Any | None = None,
    app_mode: str = "development",
) -> dict[str, Any]:
    """单独跑某一个模型步骤（不经过流水线，便于 M2 调试）。"""
    from .status import load_status, normalize

    if step not in MODEL_HANDLERS:
        raise ValueError(f"不是模型步骤：{step}（可选：{', '.join(MODEL_HANDLERS)}）")
    resolved = provider if provider is not None else load_provider()
    directory = Path(product_dir)
    status = normalize(load_status(directory))
    context = StepContext(directory, step, True, app_mode, status, resolved)
    return MODEL_HANDLERS[step](context)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单独运行模型步骤（product_analysis / russian_copy）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--step", required=True, choices=sorted(MODEL_HANDLERS))
    parser.add_argument("--provider", default=None, help="默认读 MODEL_PROVIDER，未设置则用 fake")
    args = parser.parse_args(argv)

    try:
        result = run_single_step(args.product_dir, args.step, provider=load_provider(args.provider))
    except PipelineGateError as error:
        print(
            json.dumps(
                {"ok": False, "step": error.step, "reason": error.reason, "details": error.details},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps({"ok": True, "step": args.step, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
