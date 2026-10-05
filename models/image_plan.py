"""图片规划（M3）：产出符合上游 ``image-plan`` 契约的槽位计划 + 人类可读的规划说明。

规则来源：``rules/image-slot-and-qc-rules.md``（从原项目 skill 提炼）。关键点：

- 主图 = 每个已选 SKU 恰好 1 张（顺序必须与选中 SKU 顺序一致），详情图 = 整套恰好 8 张，合计 N+8；
- ``detail_images`` 在契约里是 ``minItems=maxItems=8``，少一张多一张都会校验失败；
- 图片只允许引用 ``input/{main,sku,detail}-images`` 里的真实文件（不引用 output/ 或历史商品）；
- **不编造尺寸/材质/认证**：没有结构化尺寸就不给 ``measurement_annotation``，确认不了的槽位标 ``needs_human_input``；
- 禁止后置叠字（``overlay_strategy=single_pass_model_native_typography``），文字只来自本计划；
- 中文只出现在内部规划字段里，``russian_text`` 与提示词里的可见文字必须是俄语。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"
ASPECT_RATIO = "3:4"
SHARED_DETAIL_COUNT = 8
MAX_MAIN_IMAGES = 10

REF_SOURCE = "input/source.json"

#: 8 张共享详情图的购买决策顺序（认知 → 用途 → 结构 → 差异 → 使用 → 场景 → 细节 → 提醒）
DETAIL_PLAN: tuple[dict[str, str], ...] = (
    {
        "image_type": "benefit",
        "layout_type": "core_benefit",
        "purpose": "让买家一眼明白这是什么、解决什么问题",
        "buyer_question": "Что это и зачем мне это нужно?",
        "visual_goal": "Показать товар крупно и объяснить основную пользу",
    },
    {
        "image_type": "usage",
        "layout_type": "usage_scene",
        "purpose": "展示典型使用场景",
        "buyer_question": "Где и как я буду это использовать?",
        "visual_goal": "Показать реальную ситуацию использования",
    },
    {
        "image_type": "feature",
        "layout_type": "structure_callout",
        "purpose": "拆解结构与关键部件",
        "buyer_question": "Из чего это сделано и как устроено?",
        "visual_goal": "Показать устройство и основные элементы",
    },
    {
        "image_type": "comparison",
        "layout_type": "sku_comparison",
        "purpose": "帮助买家在已选 SKU 之间做选择",
        "buyer_question": "Какой вариант выбрать?",
        "visual_goal": "Сравнить варианты по подтверждённым различиям",
    },
    {
        "image_type": "usage",
        "layout_type": "usage_scene",
        "purpose": "演示使用步骤与注意事项",
        "buyer_question": "Как этим пользоваться?",
        "visual_goal": "Показать порядок использования",
    },
    {
        "image_type": "scene",
        "layout_type": "usage_scene",
        "purpose": "真实生活场景，增强代入感",
        "buyer_question": "Подойдёт ли это мне в жизни?",
        "visual_goal": "Показать товар в бытовой обстановке",
    },
    {
        "image_type": "detail",
        "layout_type": "structure_callout",
        "purpose": "近摄细节与做工",
        "buyer_question": "Как выглядит вблизи, аккуратно ли сделано?",
        "visual_goal": "Показать фактуру и качество сборки крупным планом",
    },
    {
        "image_type": "detail",
        "layout_type": "purchase_notice",
        "purpose": "购买提醒与已知限制（不编造、不吓人）",
        "buyer_question": "Что важно знать перед покупкой?",
        "visual_goal": "Спокойно перечислить подтверждённые ограничения",
    },
)

FORBIDDEN_CONTENT: tuple[str, ...] = (
    "中文文字或拼音（buyer 可见区域）",
    "供应商水印、店铺 URL、二维码",
    "未采集到的品牌名、认证标志、检测报告",
    "未采集到的参数（承重、容量、材质、配件数量）",
    "与本次采集不一致的 SKU 颜色或数量",
)

MUST_NOT_CHANGE: tuple[str, ...] = (
    "商品本体结构与比例",
    "SKU 颜色与规格差异",
    "真实拍摄的配件数量",
)

MUST_PRESERVE: tuple[str, ...] = (
    "商品主体外观与颜色",
    "真实材质纹理",
    "3:4 画幅与商品为最大视觉区",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _capacity_of(sku: Mapping[str, Any]) -> str | None:
    from rules.validate import normalize_capacity_text

    for key in ("capacity", "capacity_text", "volume", "spec_zh"):
        value = normalize_capacity_text(sku.get(key))
        if value:
            return value
    return None


def _russian_color(sku: Mapping[str, Any]) -> str | None:
    """取该 SKU 的**俄语**颜色词：优先已是俄语，其次把中文/英文颜色名映射过去。

    buyer 可见的叠字与生图提示词里不允许出现中文，所以这里只回俄语；映射不了就返回 None。
    """
    from rules.validate import normalize_russian_color_name

    for key in ("color_ru", "color"):
        value = sku.get(key)
        if value:
            mapped = normalize_russian_color_name(value)
            if mapped:
                return mapped
    return normalize_russian_color_name(sku.get("color_zh"))


def _variant_kind(sku: Mapping[str, Any]) -> str:
    color = _russian_color(sku)
    capacity = _capacity_of(sku)
    if color and capacity:
        return "mixed_supported"
    if capacity:
        return "size_or_measurement"
    if color:
        return "color"
    return "not_applicable"


def _variant_value(sku: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    color = _russian_color(sku)
    if color:
        parts.append(color)
    capacity = _capacity_of(sku)
    if capacity:
        parts.append(capacity)
    return " / ".join(dict.fromkeys(parts)) or None


def _list_reference_images(product_dir: Path) -> list[dict[str, Any]]:
    """只登记 input/ 下真实存在的图片（output/、历史商品一律不算）。"""
    plan = (
        ("input/main-images", "main", "main"),
        ("input/sku-images", "sku", "sku"),
        ("input/detail-images", "detail", "detail"),
    )
    images: list[dict[str, Any]] = []
    for relative, role, prefix in plan:
        directory = product_dir / relative
        if not directory.is_dir():
            continue
        files = sorted(path for path in directory.iterdir() if path.is_file())
        for index, path in enumerate(files, start=1):
            images.append(
                {
                    "id": f"{prefix}-{index:03d}",
                    "path": f"{relative}/{path.name}",
                    "role": role,
                    "usable": True,
                    "notes": None,
                }
            )
    return images


def _art_direction(*, kind: str, index: int, differentiation: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "product_scale_percent": 60 if kind == "main" else 45,
        "product_position": "center",
        "palette": ["#F5F3EF", "#D9D4CC", "#2B2B2B"],
        "lighting": "Мягкий рассеянный свет, естественные тени",
        "typography": "Крупный читаемый шрифт без засечек, не более двух строк",
        "iconography": "Минимальные линейные иконки",
        "information_hierarchy": ["Продукт", "Одна ключевая выгода"],
        "negative_space": "Не менее 25% свободного пространства вокруг товара",
        "value_signal": "Фотореалистичная предметная съёмка без 3D и иллюстраций",
        "slot_differentiation": differentiation,
    }
    if kind == "main":
        base.update(
            {
                "concept": "Чистая предметная съёмка: товар крупно, ровный свет, минимум лишних деталей",
                "scene": "Светлый нейтральный фон студии или домашней кухни без отвлекающих предметов",
                "composition": "Товар по центру, занимает 45–65% ширины кадра, вокруг свободное место",
                "background": "Светлый однотонный фон с мягкой естественной тенью",
            }
        )
    else:
        base.update(
            {
                "concept": "Съёмка в реальной обстановке: товар в использовании, живой свет, без студийной стерильности",
                "scene": "Бытовая обстановка, соответствующая сценарию использования товара",
                "composition": "Товар в естественном ракурсе, вторичные объекты не перекрывают его",
                "background": "Реалистичный фон с умеренной глубиной резкости",
            }
        )
    return base


def _overlay(
    *,
    role: str,
    text: str,
    priority: int,
    vertical_align: str = "bottom",
    accent_style: str = "top_line",
) -> dict[str, Any]:
    return {
        "role": role,
        "text": text,
        "box": [0.06, 0.72 if vertical_align == "bottom" else 0.06, 0.88, 0.16],
        "font_size_ratio": 0.04,
        "font_weight": "bold",
        "text_color": "#2B2B2B",
        "accent_color": "#C8102E",
        "background_style": "translucent",
        "background_color": "#FFFFFF",
        "accent_style": accent_style,
        "align": "left",
        "vertical_align": vertical_align,
        "priority": priority,
    }


def _planned_image(
    *,
    slot: str,
    image_type: str,
    layout_type: str,
    purpose: str,
    buyer_question: str,
    visual_goal: str,
    scene: str,
    scene_description: str,
    purchase_reason: str,
    russian_text: Sequence[str],
    reference_ids: Sequence[str],
    reference_paths: Sequence[str],
    operation: str,
    output_path: str,
    status: str,
    design_rationale: str,
    art_direction: dict[str, Any],
    overlay_plan: list[dict[str, Any]],
    prompt: str,
    prompt_brief: str,
    variant_scope: str,
    shared_across_variants: bool,
    source_sku_id: str | None = None,
    variant_kind: str = "not_applicable",
    variant_value: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": image_type,
        "slot": slot,
        "image_type": image_type,
        "layout_type": layout_type,
        "purpose": purpose,
        "buyer_question": buyer_question,
        "visual_goal": visual_goal,
        "selling_goal": visual_goal,
        "scene": scene,
        "scene_description": scene_description,
        "style_direction": "Фотореализм: как у продавца, без 3D, CGI и векторной графики",
        "visual_direction": "Крупный план товара, естественный свет, честная фактура",
        "purchase_reason": purchase_reason,
        "russian_text": list(russian_text),
        "reference_product_images": list(reference_paths),
        "reference_image_ids": list(reference_ids),
        "operation": operation,
        "prompt": prompt,
        "prompt_brief": prompt_brief,
        "output_path": output_path,
        "status": status,
        "failure_reason": failure_reason,
        "design_rationale": design_rationale,
        "art_direction": art_direction,
        "overlay_plan": overlay_plan,
        "variant_scope": variant_scope,
        "shared_across_variants": shared_across_variants,
        "variant_kind": variant_kind,
        "requested_image_type": image_type,
        "generation_attempts": 0,
    }
    if source_sku_id:
        payload["source_sku_id"] = source_sku_id
        payload["sku_identity"] = source_sku_id
    if variant_value:
        payload["variant_value"] = variant_value
    return payload


def _main_prompt(core: str, variant_value: str | None, colors: Sequence[str]) -> str:
    subject = core or "the product"
    parts = [f"Photo-realistic seller photo of {subject}"]
    if variant_value:
        parts.append(f"({variant_value})")
    if colors:
        parts.append("color: " + ", ".join(colors[:2]))
    parts.append(
        "centered on a light neutral background, soft diffused daylight, subtle natural shadow, "
        "product occupies 45-65% of frame width, no text overlays, no watermarks, no Chinese characters"
    )
    return ", ".join(parts)


def build_image_plan(
    *,
    product_dir: Path,
    source: Mapping[str, Any],
    source_refs: Sequence[str],
    copy_bundle: Mapping[str, Any] | None = None,
    analysis: Mapping[str, Any] | None = None,
    generated_by: str = "fake",
) -> dict[str, Any]:
    """构建完整的 image-plan（N 张 SKU 主图 + 恰好 8 张共享详情图）。"""
    product_id = str(source.get("product_id") or product_dir.name)
    # 只为"要上架"的 SKU 规划主图：没选的规格不该花豆包生图的钱
    from pipeline.sku_selection import active_skus

    skus = active_skus(product_dir, source.get("skus") or [])
    if not skus:
        raise ValueError("图片规划至少需要 1 个已选 SKU（检查 input/selected-skus.json）")
    if len(skus) > MAX_MAIN_IMAGES:
        raise ValueError(f"已选 SKU 超过 {MAX_MAIN_IMAGES} 个，无法规划主图")

    collection_id = str(source.get("collection_id") or "")
    if not collection_id:
        raise ValueError("source.json 里没有 collection_id（先走采集入库）")

    copy_bundle = dict(copy_bundle or {})
    analysis = dict(analysis or {})
    core = str(copy_bundle.get("core_keyword") or "").strip()
    title_ru = str(copy_bundle.get("title_ru") or "").strip()
    # 图片上的文字只能来自文案（原项目规则），所以文案是图片规划的硬前置；
    # 契约也要求 overlay_plan 至少 1 条，拿不到俄文文字就没法给合规的计划。
    visible_text = core or title_ru
    if not visible_text:
        raise ValueError(
            "图片规划需要文案提供俄文文字（core_keyword / title_ru）：先生成 output/copy-ru.json"
        )

    references = _list_reference_images(product_dir)
    main_refs = [item for item in references if item["role"] == "main"]
    sku_refs = [item for item in references if item["role"] == "sku"]
    detail_refs = [item for item in references if item["role"] == "detail"]

    risks: list[dict[str, Any]] = []
    if not sku_refs:
        risks.append(
            {
                "area": "reference",
                "level": "high",
                "message": "SKU 参考图为空：主图缺少身份锁，需要人工补图或改用同品主图",
            }
        )
    if not detail_refs:
        risks.append(
            {
                "area": "reference",
                "level": "medium",
                "message": "详情原图为空：详情图只能依赖主图素材",
            }
        )

    main_images: list[dict[str, Any]] = []
    for index, sku in enumerate(skus, start=1):
        sku_id = str(sku.get("sku_id") or f"S{index}")
        variant_value = _variant_value(sku)
        colors = [color for color in (_russian_color(sku),) if color]
        # 身份锁优先用同一 SKU 的参考图；没有就退回主图素材
        bound_refs = sku_refs or main_refs
        operation = "generate_from_reference" if bound_refs else "needs_human_input"
        status = "planned" if bound_refs else "needs_review"
        russian_text = [core] if core else []
        if variant_value:
            russian_text.append(variant_value)
        slot = f"main-{sku_id}" if sku_id else f"main-{index:03d}"
        main_images.append(
            _planned_image(
                slot=slot,
                image_type="main",
                layout_type="sku_main",
                purpose=f"SKU {sku_id} 的独立主图：3 秒内说明是什么、哪个规格、为什么值得买",
                buyer_question="Что это, для чего и чем отличается этот вариант?",
                visual_goal="Показать товар и отличия этого SKU крупным планом",
                scene="sku_main",
                scene_description="Нейтральный светлый фон, товар крупно, один ракурс, без лишних предметов",
                purchase_reason="Основная причина покупки: понятная польза товара",
                russian_text=[item for item in russian_text if item],
                reference_ids=[item["id"] for item in bound_refs[:3]],
                reference_paths=[item["path"] for item in bound_refs[:3]],
                operation=operation,
                output_path=f"output/generated-images/variant-main/{slot}.png",
                status=status,
                failure_reason=None if bound_refs else "缺少该 SKU 的参考图",
                design_rationale=(
                    "Главное изображение должно за три секунды ответить, что это, для чего нужно и чем "
                    "отличается именно этот вариант, и дать одну подтверждённую причину покупки."
                ),
                art_direction=_art_direction(
                    kind="main",
                    index=index,
                    differentiation=f"Отличие варианта: {variant_value or 'без подтверждённых отличий'}",
                ),
                overlay_plan=[_overlay(role="benefit", text=visible_text, priority=1)],
                prompt=_main_prompt(core, variant_value, colors),
                prompt_brief=(
                    f"主图 {index}：主体为商品本体（{variant_value or '无规格差异'}），单品居中占 45–65% 画幅，"
                    "浅色中性背景 + 柔和日光与自然阴影，照片级实拍，不叠字、无水印、无中文。"
                ),
                variant_scope="sku",
                shared_across_variants=False,
                source_sku_id=sku_id,
                variant_kind=_variant_kind(sku),
                variant_value=variant_value,
            )
        )

    detail_images: list[dict[str, Any]] = []
    single_sku = len(skus) == 1
    for index, plan in enumerate(DETAIL_PLAN, start=1):
        slot = f"detail-{index:03d}"
        # 单 SKU 时按规则不做 SKU 对比图，改用卖点图
        layout_type = plan["layout_type"]
        image_type = plan["image_type"]
        if single_sku and layout_type == "sku_comparison":
            layout_type, image_type = "core_benefit", "benefit"
        russian_text = [core] if core and index == 1 else []
        operation = "compose_from_real_images" if image_type == "comparison" else "generate_from_reference"
        if not detail_refs and not main_refs:
            operation, status = "needs_human_input", "needs_review"
            failure_reason = "没有可用的参考原图"
        else:
            status, failure_reason = "planned", None
        detail_images.append(
            _planned_image(
                slot=slot,
                image_type=image_type,
                layout_type=layout_type,
                purpose=plan["purpose"],
                buyer_question=plan["buyer_question"],
                visual_goal=plan["visual_goal"],
                scene=layout_type,
                scene_description=f"Слот {slot}: {plan['visual_goal']}",
                purchase_reason=plan["purpose"],
                russian_text=russian_text,
                reference_ids=[item["id"] for item in (detail_refs or main_refs)[:4]],
                reference_paths=[item["path"] for item in (detail_refs or main_refs)[:4]],
                operation=operation,
                output_path=f"output/generated-images/detail/{slot}.png",
                status=status,
                failure_reason=failure_reason,
                design_rationale=(
                    f"Восьмая часть общей истории: {plan['purpose']}. Каждый слот отвечает на свой вопрос "
                    "покупателя и не повторяет предыдущий."
                ),
                art_direction=_art_direction(
                    kind="detail",
                    index=index,
                    differentiation=f"Слот {index} из {SHARED_DETAIL_COUNT}: {plan['buyer_question']}",
                ),
                overlay_plan=[_overlay(role="callout", text=plan["buyer_question"], priority=1)],
                prompt=(
                    f"Photo-realistic seller photo illustrating: {plan['visual_goal'].lower()}, "
                    f"{'based on the real product photos' if (detail_refs or main_refs) else 'product reference required'}, "
                    "natural light, no text overlays, no watermarks, no Chinese characters, 3:4"
                ),
                prompt_brief=(
                    f"详情图 {index}/8：{plan['purpose']}；要回答买家问题「{plan['buyer_question']}」；"
                    "照片级实拍、3:4、商品为主视觉，不叠中文、不用 3D/插画。"
                ),
                variant_scope="shared",
                shared_across_variants=True,
            )
        )

    generator_contract = {
        "must_follow_ecommerce_design": True,
        "ecommerce_design_ref": "output/ozon-ecommerce-design.json",
        "allowed_structure": ["main_images", "detail_images", "disclaimer_images"],
        "aspect_ratio": ASPECT_RATIO,
        "deviation_requires_review": True,
        "advisory_scope": "none",
        "image_slot_concurrency": 3,
        "image_qc_same_execution": True,
        "product_pixel_lock_required": True,
        "composition_tool": "reference_locked_compositor",
        "source_preflight_ref": "output/image-source-preflight.json",
        "generation_strategy": "product_specific_visual_story",
        "deterministic_image_types": ["comparison", "size_spec"],
        "ai_reference_edit_image_types": ["benefit", "feature", "scene", "usage", "detail"],
        "raw_1688_image_direct_upload_forbidden": True,
        "final_chinese_text_forbidden": True,
        "plain_white_background_forbidden": False,
        "main_images_first": True,
        "target_total_seconds": 1800,
        "quality_gate": "image_qc_hard_gate",
        "typography_strategy": "single_pass_model_native_typography",
        "prompt_first_required": True,
        "empty_placeholder_panels_forbidden": True,
        "exact_shared_detail_count": SHARED_DETAIL_COUNT,
        "ecommerce_design_required": True,
        "overlay_strategy": "single_pass_model_native_typography",
        "true_parallel_slot_executor": True,
        "brand_watermark_required": "none",
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "collection_id": collection_id,
        "source_kind": str(source.get("source_kind") or "workbench_collection"),
        "source_refs": list(source_refs) or [REF_SOURCE],
        "ecommerce_design_ref": "output/ozon-ecommerce-design.json",
        "visual_contract_ref": "output/ozon-ecommerce-design.json#visual_system",
        "visual_family": "product_specific_visual_story",
        "image_positioning": "Предметная фотореалистичная съёмка без студийной стерильности",
        "main_image_goal": "За три секунды объяснить, что это, для чего и чем отличается вариант",
        "visual_style": "Фотореализм, естественный свет, честные материалы",
        "need_model": True,
        "avoid_style": "3D-рендер, CGI, векторные иллюстрации, коллажи, стоковые шаблоны",
        "image_sequence_ref": f"image-plan/{product_id}",
        "image_set_structure": [
            f"{len(skus)} 张 SKU 主图（按选中 SKU 顺序）",
            f"{SHARED_DETAIL_COUNT} 张共享详情图",
            "0 张独立免责图（无证据时不单列）",
        ],
        "variant_image_strategy": {
            "mode": "sku_specific_main_shared_details",
            "variant_kinds": sorted({_variant_kind(sku) for sku in skus} - {"not_applicable", "mixed_supported"})
            or ["seller_specification"],
            "variant_main_count": len(main_images),
            "shared_detail_count": SHARED_DETAIL_COUNT,
            "shared_disclaimer_count": 0,
        },
        "creative_direction": {
            "generated_by": generated_by,
            "core_keyword": core or None,
            "visual_family": "product_specific_visual_story",
        },
        "listing_context": {
            "title_ru": title_ru or None,
            "sku_count": len(skus),
            "hashtag_count": len(list(copy_bundle.get("hashtags") or [])),
            "analysis_decision": ((analysis.get("recommendation") or {}) or {}).get("decision"),
        },
        "buyer_objections": [
            "Подойдёт ли размер и формат использования",
            "Насколько качественные материалы",
            "Чем отличается выбранный вариант от других",
        ],
        "generator_contract": generator_contract,
        "reference_images": references,
        "main_images": main_images,
        "detail_images": detail_images,
        "disclaimer_images": [],
        "must_preserve": list(MUST_PRESERVE),
        "must_not_change": list(MUST_NOT_CHANGE),
        "forbidden_content": list(FORBIDDEN_CONTENT),
        "risks": risks,
        "processing": {
            "step": "image_plan",
            "status": "completed",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "error": None,
        },
    }


def render_plan_brief(plan: Mapping[str, Any]) -> str:
    """把图片计划渲染成给运营看的 Markdown（"AI 告诉我图要怎么规划、提示词大概是什么"）。"""
    lines: list[str] = []
    lines.append(f"# 图片计划 · {plan.get('product_id')}")
    lines.append("")
    lines.append(f"- 结构：{' / '.join(plan.get('image_set_structure') or [])}")
    lines.append(f"- 画幅：{ASPECT_RATIO}（契约硬要求），商品必须是最大视觉区")
    lines.append(f"- 变体策略：{(plan.get('variant_image_strategy') or {}).get('mode')}")
    lines.append(f"- 叠字策略：{((plan.get('generator_contract') or {}).get('overlay_strategy'))}")
    lines.append("")
    risks = plan.get("risks") or []
    if risks:
        lines.append("## 风险提示")
        for risk in risks:
            lines.append(f"- [{risk.get('level')}] {risk.get('area')}: {risk.get('message')}")
        lines.append("")

    for group, title in (("main_images", "SKU 主图"), ("detail_images", "共享详情图")):
        lines.append(f"## {title}（{len(plan.get(group) or [])} 张）")
        lines.append("")
        for item in plan.get(group) or []:
            lines.append(f"### {item.get('slot')} · {item.get('purpose')}")
            lines.append(f"- 买家问题：{item.get('buyer_question')}")
            lines.append(f"- 要体现：{item.get('visual_goal')}")
            lines.append(f"- 画面：{item.get('scene_description')}")
            lines.append(f"- 叠字（俄文）：{'；'.join(item.get('russian_text') or []) or '无'}")
            lines.append(f"- 操作：`{item.get('operation')}` / 状态：`{item.get('status')}`")
            lines.append(f"- 参考图：{', '.join(item.get('reference_image_ids') or []) or '无'}")
            lines.append(f"- 输出：`{item.get('output_path')}`")
            lines.append(f"- 提示词建议：{item.get('prompt_brief')}")
            lines.append(f"- 提示词（英文，可直接喂生图）：`{item.get('prompt')}`")
            lines.append("")
    lines.append("## 禁止出现")
    for item in plan.get("forbidden_content") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)
