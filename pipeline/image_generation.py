"""``image_generation`` handler：调用可插拔的生图后端逐图位出图。

没有配置生图后端时**不会**注册这个 handler（runner 会如实报 handler_not_implemented）。
如果配置的是占位图生成器，产物会明确标注"非最终图片"，并在 QC 与上传载荷里继续提示。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from models import ImageRequest, ModelError

from .context import PipelineGateError, StepContext
from .image_probe import probe_image


def handle_image_generation(ctx: StepContext) -> dict[str, Any]:
    generator = getattr(ctx, "image_generator", None)
    if generator is None:
        raise PipelineGateError(ctx.step, "未配置生图后端（run_product(image_generator=...)）")

    source = ctx.require_json("input/source.json")
    plan = ctx.require_json("output/image-plan.json")
    if plan.get("studio_mode"):
        raise PipelineGateError(ctx.step, "自主生图工作区请逐张创建任务，不通过旧流水线批量调用付费模型")
    planned = list(plan.get("main_images") or []) + list(plan.get("detail_images") or [])

    try:
        result = generator.generate(
            ImageRequest(
                product_id=ctx.product_dir.name,
                product_dir=ctx.product_dir,
                source=source,
                extra={"planned_slots": len(planned)},
            )
        )
    except (ModelError, ValueError, FileNotFoundError) as error:
        raise PipelineGateError(ctx.step, f"生图后端失败：{error}") from error

    generated = [item for item in (result.get("generated") or []) if isinstance(item, Mapping)]
    if not generated:
        raise PipelineGateError(ctx.step, "生图后端没有产出任何图片")

    # 立刻做一次尺寸自检：生成器说"写好了"不等于文件真的合规
    bad: list[str] = []
    for item in generated:
        relative = str(item.get("path") or "")
        probe = probe_image(ctx.path(relative)) if relative else {"ok": False}
        if not probe.get("ok"):
            bad.append(f"{item.get('slot')}: {relative or '无路径'}")
    if bad:
        raise PipelineGateError(ctx.step, "生图产物不可读：" + "、".join(bad[:5]))

    report = {
        "schema_version": "1.0.0",
        "product_id": ctx.product_dir.name,
        "generator": str(result.get("generator") or getattr(generator, "name", "unknown")),
        "final_images": bool(result.get("final_images")),
        "planned_slots": len(planned),
        "generated_slots": len(generated),
        "note": result.get("note"),
        "files": [
            {"slot": item.get("slot"), "path": item.get("path"), "bytes": ctx.path(str(item.get("path"))).stat().st_size}
            for item in generated
            if item.get("path") and ctx.path(str(item.get("path"))).is_file()
        ],
    }
    ctx.write_json("output/image-generation-report.json", report)

    warnings: list[str] = []
    if not report["final_images"]:
        warnings.append(
            f"当前生图后端是 `{report['generator']}`（非最终图片）：正式上架前必须换成真实生图后端重跑"
        )
    if report["generated_slots"] < report["planned_slots"]:
        warnings.append(
            f"只生成 {report['generated_slots']}/{report['planned_slots']} 个图位"
        )
    return {
        "warnings": warnings,
        "artifacts": ["output/image-generation-report.json"],
        "generated_slots": report["generated_slots"],
        "generator": report["generator"],
    }


IMAGE_GENERATION_HANDLERS = {"image_generation": handle_image_generation}


def image_generation_handlers(generator: Any | None = None) -> dict[str, Any]:
    return dict(IMAGE_GENERATION_HANDLERS) if generator is not None else {}
