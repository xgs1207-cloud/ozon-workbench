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
    if resolved in {"ark", "doubao", "volcengine", "volcano"}:
        # 火山方舟文本端点（与生图共用 ARK_API_KEY）
        from .http_provider import build_provider_from_env

        return build_provider_from_env(ark=True)
    if resolved in {"none", "off", ""}:
        raise ModelError("未启用模型层（provider=none）")
    raise ModelError(
        f"未知模型层 {resolved!r}：可用 fake（确定性自检）、http（OpenAI 兼容端点，见 models/http_provider.py）；"
        "Codex CLI adapter 按同一接口加一个分支即可（见 HANDOFF §5.1）"
    )


def load_image_generator(name: str | None = None, env: Mapping[str, str] | None = None,
                         **overrides: Any) -> Any:
    """CLI 默认占位；真实后端显式配置，图位过滤传给实际生成器。"""
    source = env if env is not None else os.environ
    resolved = str(name or source.get("IMAGE_GENERATOR") or "placeholder").strip().lower()
    if resolved in {"placeholder", "local", "local-placeholder"}:
        from .local_image import LocalPlaceholderGenerator

        return LocalPlaceholderGenerator()
    if resolved in {"doubao", "ark", "volcano", "seedream", "seededit"}:
        from .doubao_image import DoubaoImageGenerator

        return DoubaoImageGenerator.from_env(env, **overrides)
    if resolved in {"rightapi", "right-api"}:
        from .rightapi_image import RightApiImageGenerator

        return RightApiImageGenerator.from_env(env, **overrides)
    if resolved in {"none", "off", ""}:
        return None
    raise ModelError(
        f"未知生图后端 {resolved!r}：可用 placeholder（占位图）、doubao（豆包/火山方舟）、rightapi；"
        "其它后端按 models/local_image.py 的协议实现 generate(request) 即可"
    )


def image_backend_settings(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """工作台用的安全配置摘要；不回传密钥、远端 URL 或环境内容。"""
    source = env if env is not None else os.environ
    resolved = str(source.get("WORKBENCH_WEB_IMAGE_GENERATOR")
                   or source.get("IMAGE_GENERATOR") or "doubao").strip().lower()
    if resolved in {"doubao", "ark", "volcano", "seedream", "seededit"}:
        from .doubao_image import DEFAULT_MODEL, DEFAULT_SIZE

        return {"name": "doubao", "label": "豆包 / 火山方舟",
                "model": str(source.get("ARK_IMAGE_MODEL") or DEFAULT_MODEL),
                "configured": bool(str(source.get("ARK_API_KEY") or "").strip()),
                "aspect_ratio": "3:4", "image_size": str(source.get("ARK_IMAGE_SIZE") or DEFAULT_SIZE),
                "normalized_size": "900×1200", "produces_final_images": True}
    if resolved in {"rightapi", "right-api"}:
        return {"name": "rightapi", "label": "RightAPI",
                "model": str(source.get("RIGHTAPI_IMAGE_MODEL") or "gpt-image-2.5"),
                "configured": bool(str(source.get("RIGHTAPI_API_KEY") or "").strip()),
                "aspect_ratio": str(source.get("RIGHTAPI_ASPECT_RATIO") or "3:4"),
                "image_size": str(source.get("RIGHTAPI_IMAGE_SIZE") or "2k"),
                "normalized_size": "900×1200", "produces_final_images": True}
    if resolved in {"placeholder", "local", "local-placeholder"}:
        return {"name": "placeholder", "label": "本地占位图", "model": None,
                "configured": True, "aspect_ratio": "3:4", "image_size": "900×1200",
                "normalized_size": "900×1200", "produces_final_images": False}
    return {"name": None, "label": "未配置生图后端", "model": None,
            "configured": False, "aspect_ratio": None, "image_size": None,
            "normalized_size": None, "produces_final_images": False}


def load_web_image_generator(env: Mapping[str, str] | None = None, **overrides: Any) -> Any:
    settings = image_backend_settings(env)
    if not settings["configured"] or not settings["produces_final_images"]:
        raise ModelError("工作台未配置真实生图后端，请配置 RIGHTAPI_API_KEY 或 ARK_API_KEY 及对应后端")
    if settings["name"] == "doubao":
        overrides["max_attempts"] = 1
    return load_image_generator(settings["name"], env, **overrides)


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
    "image_backend_settings",
    "load_web_image_generator",
    "load_provider",
]
