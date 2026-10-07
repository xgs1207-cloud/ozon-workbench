"""确定性 fake 模型层：不联网、不随机，用于在没有真实模型时打通 M2。

用途：

1. 开发与测试流水线（``MODEL_PROVIDER=fake``）；
2. 作为**契约样例生成器**：它产出的对象必须能通过 ``contracts.validate_contract``，
   所以任何真实 adapter 都可以拿它的输出对照。

它**不会**做的事：不编造材质/认证/承重；缺证据的字段一律进 ``unknowns``。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from rules.validate import WEAK_SINGLE_TAGS, canonical_hashtag, normalize_capacity_text

from .base import (
    AnalysisRequest,
    CopyRequest,
    DesignRequest,
    ImagePlanRequest,
    ImageRequest,
    ModelError,
    PositionRequest,
)

SCHEMA_VERSION = "1.0.0"
REF_SOURCE = "input/source.json"
REF_ANALYSIS = "output/product-analysis.json"
REF_KEYWORDS = "input/selected-keywords.json"

_GENERIC_TITLE_TAIL = "для дома, офиса и путешествий"
_SECTION_TEXTS: dict[str, str] = {
    "product_value": "Практичное решение для повседневного использования и поездок.",
    "usage_scenarios": "Подходит для дома, офиса, дачи и прогулок на природе.",
    "core_advantages": "Продуманные детали и простой уход делают модель удобной каждый день.",
    "usage_method": "Перед первым использованием промойте изделие тёплой водой с мягким средством.",
    "notices": "Не применяйте абразивные средства и храните изделие в сухом месте.",
}


def _sku_text(source: Mapping[str, Any]) -> str:
    skus = source.get("skus") if isinstance(source.get("skus"), list) else []
    names = [str(item.get("name_zh") or item.get("spec_zh") or "").strip() for item in skus if isinstance(item, Mapping)]
    return " / ".join(item for item in names if item)


def _capacity(source: Mapping[str, Any]) -> str | None:
    skus = source.get("skus") if isinstance(source.get("skus"), list) else []
    for item in skus:
        if not isinstance(item, Mapping):
            continue
        for key in ("capacity", "capacity_text", "volume", "spec_zh"):
            normalized = normalize_capacity_text(item.get(key))
            if normalized:
                return normalized
    return None


def _colors(source: Mapping[str, Any]) -> list[str]:
    skus = source.get("skus") if isinstance(source.get("skus"), list) else []
    colors: list[str] = []
    for item in skus:
        if not isinstance(item, Mapping):
            continue
        value = item.get("color_ru") or item.get("color")
        if value and str(value) not in colors:
            colors.append(str(value))
    return colors


class FakeProvider:
    """确定性实现；``name`` 会写进产物，便于分辨是谁生成的。"""

    name = "fake"

    # ------------------------------------------------------------- 商品分析

    def analyze_product(self, request: AnalysisRequest) -> dict[str, Any]:
        source = dict(request.source)
        skus = [item for item in (source.get("skus") or []) if isinstance(item, Mapping)]
        product_id = str(source.get("product_id") or request.product_id)
        title_cn = source.get("title_zh")
        category = source.get("selected_category") or {}

        analyzed_skus = [
            {
                "sku_id": str(item.get("sku_id") or ""),
                "name_cn": item.get("name_zh") or item.get("spec_zh") or str(item.get("sku_id") or ""),
                "properties": {
                    key: str(item[key])
                    for key in ("color_zh", "capacity", "spec_zh", "pack_quantity")
                    if item.get(key) is not None
                },
                "price_cny": float(item.get("purchase_price_cny") or 0) or None,
                "image_refs": [str(item.get("image_path"))] if item.get("image_path") else [],
            }
            for item in skus
        ]

        unknowns: list[dict[str, Any]] = []
        for field, reason in (
            ("materials", "采集资料里没有材质字段"),
            ("dimensions", "采集资料里没有结构化尺寸"),
            ("weight", "采集资料里没有结构化重量"),
            ("certifications", "采集资料里没有认证信息"),
        ):
            unknowns.append({"field": field, "reason": reason, "needed_from_human": False})

        selling_points = [
            {
                "text": f"已选 {len(skus)} 个 SKU，可按颜色/规格分开展示",
                "evidence": [REF_SOURCE],
            }
        ]
        capacity = _capacity(source)
        if capacity:
            selling_points.append({"text": f"容量规格：{capacity}", "evidence": [REF_SOURCE]})

        risks: list[dict[str, Any]] = []
        if not (category.get("category_id") and category.get("type_id")):
            risks.append(
                {
                    "area": "category",
                    "level": "high",
                    "message": "采集时没有选择 Ozon 类目，无法进入属性匹配",
                    "blocking": True,
                }
            )
        if not capacity:
            risks.append(
                {
                    "area": "capacity",
                    "level": "low",
                    "message": "没有结构化容量数据，标题里不会声明容量",
                    "blocking": False,
                }
            )

        decision = "needs_human_input" if any(item["blocking"] for item in risks) else "continue"
        return {
            "schema_version": SCHEMA_VERSION,
            "product_id": product_id,
            "source_refs": request.source_refs or [REF_SOURCE],
            "product_type": title_cn or "unknown",
            "category": category.get("category_path_zh") or "unknown",
            "target_customer": [],
            "usage_scenarios": ["дом", "офис", "поездки"],
            "competitive_advantages": [],
            "missing_information": [],
            "recommended": "continue" if decision == "continue" else "needs_human_input",
            "score": 70 if decision == "continue" else 40,
            "facts": {
                "title_cn": title_cn,
                "category_cn": category.get("category_path_zh"),
                "brand": None,
                "materials": [],
                "dimensions": "unknown",
                "weight": "unknown",
                "load_capacity": "unknown",
                "certifications": [],
                "functions": [],
                "package_quantity": "unknown",
                "accessories": [],
                "skus": analyzed_skus,
            },
            "selling_points": selling_points,
            "inferences": [],
            "unknowns": unknowns,
            "risks": risks,
            "recommendation": {
                "decision": decision,
                "reason": "证据齐全，可继续" if decision == "continue" else "缺少类目，需人工确认",
            },
            "processing": {
                "step": "product_analysis",
                "status": "completed",
                "started_at": _now(),
                "finished_at": _now(),
                "error": None,
            },
        }

    # ------------------------------------------------------------- 定位与设计（M2）

    def position_product(self, request: PositionRequest) -> dict[str, Any]:
        from .design import build_positioning_document

        return build_positioning_document(
            product_id=request.product_id,
            source=request.source,
            analysis=request.analysis,
            copy_bundle=request.copy_bundle,
            pricing=request.pricing,
            source_refs=request.source_refs,
            generated_by=self.name,
        )

    def design_listing(self, request: DesignRequest) -> dict[str, Any]:
        from .design import build_design_document

        return build_design_document(
            product_id=request.product_id,
            source=request.source,
            analysis=request.analysis,
            copy_bundle=request.copy_bundle,
            image_plan=request.image_plan,
            attributes_final=request.attributes_final,
            positioning=request.positioning,
            source_refs=request.source_refs,
            generated_by=self.name,
        )

    def translate_terms(self, keywords: Sequence[str], context: Mapping[str, Any] | None = None) -> dict[str, str]:
        """确定性 fake **不做翻译**：返回空表，让调用方用 Seerfar 中文类目名兜底（不猜）。"""
        return {}

    # ------------------------------------------------------------- 文案

    def write_copy_candidates_ru(self, request: CopyRequest) -> dict[str, Any]:
        """One batch interface; fake's three local projections incur no API fee."""
        from dataclasses import replace
        return {"candidates": [
            {"mode": mode, "documents": self.write_copy_ru(replace(request, extra={**request.extra, "candidate_mode": mode}))}
            for mode in ("search_first", "conversion_first", "differentiation_first")
        ]}

    def write_copy_ru(self, request: CopyRequest) -> dict[str, Any]:
        keywords = [str(item.get("keyword") or "").strip() for item in request.selected_keywords]
        keywords = [item for item in keywords if len(item) >= 2]
        if not keywords:
            raise ModelError("没有已选关键词：请先在关键词库里选词（input/selected-keywords.json）")

        source = dict(request.source)
        product_id = str(source.get("product_id") or request.product_id)
        core = keywords[0]
        capacity = _capacity(source)
        colors = _colors(source)

        title = self._build_title(core, capacity, colors)
        short_title = self._fit(f"{core}", low=2, high=80)

        sections = {name: text for name, text in _SECTION_TEXTS.items()}
        sections["product_value"] = f"{core.capitalize()}: {sections['product_value']}"
        description = " ".join(sections[name] for name in ("product_value", "usage_scenarios", "core_advantages", "usage_method", "notices"))

        hashtags: list[str] = []
        for keyword in keywords:
            tag = canonical_hashtag(keyword)
            if not tag:
                continue
            if tag.lstrip("#") in WEAK_SINGLE_TAGS:
                continue
            if tag not in hashtags:
                hashtags.append(tag)
        hashtags = hashtags[:30]

        primary = keywords[: min(8, len(keywords))]
        secondary = keywords[8:28]
        keyword_basis = [
            {
                "keyword": keyword,
                "source": "product_type" if index == 0 else "usage_scene",
                "evidence": [REF_KEYWORDS],
            }
            for index, keyword in enumerate(primary)
        ]

        refs = list(request.source_refs)
        for candidate in (REF_SOURCE, REF_ANALYSIS, REF_KEYWORDS):
            if candidate not in refs:
                refs.append(candidate)

        return {
            "title_ru": {
                "schema_version": SCHEMA_VERSION,
                "product_id": product_id,
                "source_refs": refs[:4],
                "title_ru": title,
                "short_title_ru": short_title,
                "core_keyword": core,
                "evidence": [REF_KEYWORDS, REF_ANALYSIS],
                "excluded_claims": [],
                "warnings": [],
            },
            "description_ru": {
                "schema_version": SCHEMA_VERSION,
                "product_id": product_id,
                "source_refs": refs[:4],
                "description_ru": description,
                "sections": sections,
                "section_evidence": [
                    {"section": name, "source_refs": [REF_ANALYSIS, REF_KEYWORDS]} for name in sections
                ],
                "unknown_fields": ["materials", "dimensions", "weight"],
                "warnings": [],
            },
            "keywords_ru": {
                "schema_version": SCHEMA_VERSION,
                "product_id": product_id,
                "source_refs": refs[:4],
                "primary_keywords": primary,
                "secondary_keywords": secondary,
                "keyword_basis": keyword_basis,
                "excluded_keywords": [],
                "warnings": [],
            },
            "copy_bundle": {
                "title_ru": title,
                "short_title_ru": short_title,
                "description_ru": description,
                "description_sections": sections,
                "hashtags": hashtags,
                "core_keyword": core,
                "primary_keywords": primary,
                "secondary_keywords": secondary,
            },
        }

    # ------------------------------------------------------------- 图片规划（M3）

    def plan_images(self, request: ImagePlanRequest) -> dict[str, Any]:
        """确定性的槽位规划：N 张 SKU 主图 + 恰好 8 张共享详情图（过 image-plan 契约）。

        真正的生图留给后面的 adapter；这里只产出**计划与提示词**，不编造尺寸/材质/认证。
        """
        from .image_plan import build_image_plan

        return build_image_plan(
            product_dir=request.product_dir,
            source=request.source,
            source_refs=request.source_refs,
            copy_bundle=request.copy_bundle,
            analysis=request.analysis,
            generated_by=self.name,
        )

    def generate_image(self, request: ImageRequest) -> dict[str, Any]:
        raise ModelError("图片生成尚未实现：等选定生图后端与对象存储（M3 后半）")

    # ------------------------------------------------------------- 内部工具

    @staticmethod
    def _build_title(core: str, capacity: str | None, colors: Sequence[str]) -> str:
        parts = [core.capitalize()]
        if capacity:
            parts.append(capacity)
        if colors:
            parts.append(f"({', '.join(str(item) for item in colors[:2])})")
        if len(" ".join(parts)) < 25:
            parts.append(_GENERIC_TITLE_TAIL)
        title = " ".join(parts)
        return FakeProvider._fit(title, low=25, high=120)

    @staticmethod
    def _fit(text: str, *, low: int, high: int) -> str:
        value = re.sub(r"\s+", " ", str(text)).strip()
        if len(value) > high:
            cut = value[:high]
            if " " in cut:
                cut = cut[: cut.rfind(" ")]
            value = cut.strip()
        while len(value) < low:
            value = f"{value} {_GENERIC_TITLE_TAIL}".strip()
            if len(value) >= high:
                break
        return value[:high]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
