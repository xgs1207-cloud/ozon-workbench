"""模型层入口：按名字/环境变量取一个 provider。

    MODEL_PROVIDER=fake python -m pipeline.runner --product-dir products/P000001
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from .base import (
    AnalysisRequest,
    CopyRequest,
    DesignRequest,
    ImagePlanRequest,
    ImageRequest,
    ModelError,
    ModelProvider,
    PositionRequest,
    RequestBase,
    existing_source_refs,
)

DEFAULT_PROVIDER = "fake"


def load_provider(name: str | None = None) -> ModelProvider:
    resolved = str(name or os.environ.get("MODEL_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if resolved in {"fake", "deterministic"}:
        from .fake import FakeProvider

        return FakeProvider()
    if resolved in {"http", "openai", "openai-compatible", "deepseek", "api", "llm"}:
        from .http_provider import build_provider_from_env

        return build_provider_from_env()
    if resolved in {"none", "off", ""}:
        raise ModelError("未启用模型层（provider=none）")
    raise ModelError(
        f"未知模型层 {resolved!r}：可用 fake（确定性自检）、http（OpenAI 兼容端点，见 models/http_provider.py）；"
        "Codex CLI adapter 按同一接口加一个分支即可（见 HANDOFF §5.1）"
    )


def load_image_generator(name: str | None = None, env: Mapping[str, str] | None = None) -> Any:
    """按名字取生图后端：``placeholder``（本地占位图）/ ``doubao``（火山方舟）。"""
    resolved = str(name or os.environ.get("IMAGE_GENERATOR") or "placeholder").strip().lower()
    if resolved in {"placeholder", "local", "local-placeholder"}:
        from .local_image import LocalPlaceholderGenerator

        return LocalPlaceholderGenerator()
    if resolved in {"doubao", "ark", "volcano", "seedream", "seededit"}:
        from .doubao_image import DoubaoImageGenerator

        return DoubaoImageGenerator.from_env(env)
    if resolved in {"none", "off", ""}:
        return None
    raise ModelError(
        f"未知生图后端 {resolved!r}：可用 placeholder（占位图）、doubao（豆包/火山方舟）；"
        "其它后端按 models/local_image.py 的协议实现 generate(request) 即可"
    )


__all__ = [
    "AnalysisRequest",
    "CopyRequest",
    "DesignRequest",
    "ImagePlanRequest",
    "ImageRequest",
    "ModelError",
    "ModelProvider",
    "PositionRequest",
    "RequestBase",
    "existing_source_refs",
    "load_image_generator",
    "load_provider",
]
