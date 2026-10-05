"""模型层：把"用哪个模型"与流水线解耦。

流水线只认这里的接口；具体接 Codex CLI 还是 API，是 ``models/`` 下的一个 adapter。
所有方法都返回**可直接落盘的契约形状对象**（不会在 handler 里再拼一次）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class ModelError(RuntimeError):
    """模型层不可用或返回不可用结果。"""


@dataclass
class RequestBase:
    product_id: str
    product_dir: Path
    source: Mapping[str, Any]
    source_refs: list[str] = field(default_factory=list)
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisRequest(RequestBase):
    """商品信息总结（对应 product-analysis 契约）。"""

    #: 已选关键词（可选）：真实模型会用它判断商品定位与卖点顺序
    selected_keywords: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CopyRequest(RequestBase):
    """标题/简介/标签生成（对应 title-ru / description-ru / keywords-ru 契约）。"""

    analysis: Mapping[str, Any] = field(default_factory=dict)
    selected_keywords: list[dict[str, Any]] = field(default_factory=list)
    positioning: Mapping[str, Any] | None = None


@dataclass
class PositionRequest(RequestBase):
    """商品定位（对应 product-positioning 契约）。"""

    analysis: Mapping[str, Any] = field(default_factory=dict)
    copy_bundle: Mapping[str, Any] = field(default_factory=dict)
    pricing: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class DesignRequest(RequestBase):
    """电商设计（对应 ozon-ecommerce-design 契约）。"""

    analysis: Mapping[str, Any] = field(default_factory=dict)
    copy_bundle: Mapping[str, Any] = field(default_factory=dict)
    image_plan: Mapping[str, Any] = field(default_factory=dict)
    attributes_final: Mapping[str, Any] = field(default_factory=dict)
    positioning: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ImagePlanRequest(RequestBase):
    """图片规划（M3）：输出槽位与每张图的提示词要点。"""

    analysis: Mapping[str, Any] = field(default_factory=dict)
    copy_bundle: Mapping[str, Any] = field(default_factory=dict)
    max_slots: int = 18


@dataclass
class ImageRequest(RequestBase):
    """单张图生成（M3）：返回本地文件路径与提示词回执。"""

    slot: str = ""
    prompt: str = ""
    reference_images: list[str] = field(default_factory=list)


class ModelProvider(Protocol):
    """模型层接口。实现方必须保持**确定性可复现**或显式记录随机性来源。"""

    name: str

    def analyze_product(self, request: AnalysisRequest) -> dict[str, Any]: ...

    def position_product(self, request: PositionRequest) -> dict[str, Any]: ...

    def write_copy_ru(self, request: CopyRequest) -> dict[str, Any]: ...

    def design_listing(self, request: DesignRequest) -> dict[str, Any]: ...

    def plan_images(self, request: ImagePlanRequest) -> dict[str, Any]: ...

    def generate_image(self, request: ImageRequest) -> dict[str, Any]: ...


def existing_source_refs(product_dir: Path, candidates: Sequence[str]) -> list[str]:
    """只把真实存在的文件列为证据（不编造 source_refs）。"""
    refs: list[str] = []
    for relative in candidates:
        if (product_dir / relative).is_file() and relative not in refs:
            refs.append(relative)
    return refs
