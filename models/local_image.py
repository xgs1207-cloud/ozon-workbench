"""本地生图实现（占位图）：接口与真实生图后端一致，用于在没接后端时打通整条流水线。

**它不是最终图片**：只产出尺寸合规（3:4、≥900×1200）的纯色占位 PNG，
所以 QC 报告里会明确记录生成器名字并提示"正式上架前必须用真实生图后端重跑"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .base import ImageRequest

PLACEHOLDER_WIDTH = 900
PLACEHOLDER_HEIGHT = 1200


class LocalPlaceholderGenerator:
    """按 ``image-plan`` 的 ``output_path`` 逐图位写占位 PNG。"""

    name = "local-placeholder"
    produces_final_images = False

    def generate(self, request: ImageRequest) -> dict[str, Any]:
        from pipeline.image_probe import write_solid_png

        plan_path = request.product_dir / "output" / "image-plan.json"
        if not plan_path.is_file():
            raise FileNotFoundError("缺少 output/image-plan.json：先生成图片计划")
        import json

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        slots = list(plan.get("main_images") or []) + list(plan.get("detail_images") or [])
        if request.slot:
            slots = [item for item in slots if str(item.get("slot")) == str(request.slot)]
            if not slots:
                raise ValueError(f"图片计划里没有该槽位：{request.slot}")

        written: list[dict[str, Any]] = []
        for item in slots:
            relative = str(item.get("output_path") or "")
            if not relative:
                continue
            role = "main" if str(item.get("slot", "")).startswith("main-") else "detail"
            rgb = (250, 250, 250) if role == "main" else (238, 240, 244)
            target = write_solid_png(
                request.product_dir / relative,
                PLACEHOLDER_WIDTH,
                PLACEHOLDER_HEIGHT,
                rgb=rgb,
                accent_rgb=(198, 62, 74) if role == "main" else (70, 110, 160),
            )
            written.append({"slot": item.get("slot"), "path": relative, "source_path": str(target)})
        if not written:
            raise ValueError("没有可生成图位（计划的 output_path 为空）")
        return {
            "generated": written,
            "generator": self.name,
            "final_images": False,
            "note": "占位图：仅尺寸合规，用于打通流水线；正式上架前请用真实生图后端重跑",
        }


def generator_report(generator: Any) -> dict[str, Any]:
    if generator is None:
        return {"name": None, "configured": False}
    return {
        "name": getattr(generator, "name", "unknown"),
        "configured": True,
        "produces_final_images": bool(getattr(generator, "produces_final_images", False)),
    }


__all__ = ["LocalPlaceholderGenerator", "generator_report", "PLACEHOLDER_WIDTH", "PLACEHOLDER_HEIGHT"]
