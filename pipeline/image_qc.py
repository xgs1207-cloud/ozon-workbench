"""图片技术质检（本地、真实现）+ ``image_qc`` handler。

**本地能做的（真做）**：文件可读、真实格式和宽高、原图封存凭据。
AI 产物保持项目 3:4 / 900×1200 标准；直接采用的原图遵守 Seller API
明确支持的 PNG/JPEG 格式，比例和小尺寸仅作画质提醒，不冒充官方最低像素规则。

**本地做不了的**：商品一致性、颜色、结构、配件、合规文案 —— 需要视觉模型。
这些维度会明确标注"未配置视觉模型"：整体 ``decision`` 记 ``revise``，
但 ``regenerate_needed=false``（不谎报 pass，也不做无意义的重生成）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import format_problems, validate_contract

from .context import PipelineGateError, StepContext
from .image_probe import aspect_ratio_text, probe_image
from .media_selection import selected_image_specs

SCHEMA_VERSION = "1.0.0"
ASPECT_TOLERANCE = 0.002
MIN_WIDTH = 900
MIN_HEIGHT = 1200
TARGET_ASPECT = 3 / 4

DIMENSION_WEIGHTS: tuple[tuple[str, int, str], ...] = (
    ("product_consistency", 30, "商品与参考图一致性（需要视觉模型）"),
    ("conversion_logic", 25, "转化逻辑与排序（需要视觉模型）"),
    ("style_match", 20, "风格一致性（需要视觉模型）"),
    ("visual_quality", 15, "技术画质（本地可判定）"),
    ("compliance", 10, "合规（中文/水印/虚假参数，需要 OCR/视觉模型）"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _source_refs(directory: Path) -> list[str]:
    candidates = [
        "output/image-plan.json",
        "input/source.json",
        "output/copy-ru.json",
        "output/ozon-attributes-final.json",
        "output/generated-images",
    ]
    refs = [item for item in candidates if (directory / item).exists()]
    for filler in ("output/image-plan.json", "output/generated-images", "input/source.json"):
        if len(refs) >= 3:
            break
        if filler not in refs:
            refs.append(filler)
    return refs[:6]


def _technical_status(*, ok: bool, format_ok: bool, aspect_ok: bool, resolution_ok: bool) -> tuple[str, str, list[str]]:
    failures: list[str] = []
    if not ok:
        return "reject", "文件不可读或格式无法识别", ["image_file_unreadable"]
    if not format_ok:
        return "reject", "格式必须是 png（项目规则）", ["unsupported_format"]
    if not aspect_ok:
        return "reject", f"比例必须是 3:4（容差 {ASPECT_TOLERANCE}）", ["aspect_ratio_mismatch"]
    if not resolution_ok:
        return "revise", f"分辨率低于 {MIN_WIDTH}×{MIN_HEIGHT}", []
    return "pass", "技术检查通过", []


def run_image_qc(
    product_dir: Path | str,
    *,
    semantic_provider: Any | None = None,
    generator_name: str | None = None,
    produces_final_images: bool = False,
) -> dict[str, Any]:
    """跑一次技术质检，返回对齐 ``image-qc-report`` 契约的报告。"""
    directory = Path(product_dir)
    plan = _read_json(directory / "output" / "image-plan.json")
    slots = selected_image_specs(plan)
    if not slots:
        raise ValueError("缺少图片计划（output/image-plan.json），无法质检")

    images_checked: list[dict[str, Any]] = []
    technical: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    critical: list[str] = []
    failing_slots: list[str] = []
    revise_slots: list[str] = []
    original_warnings: list[str] = []
    receipts = {row.get("slot"): row for row in _read_json(directory / "output/image-generation-report.json").get("files") or []
                if isinstance(row, Mapping)}

    for item in slots:
        slot = str(item.get("slot") or "unknown")
        relative = str(item.get("output_path") or "")
        references = [str(value) for value in (item.get("reference_image_ids") or [])]
        if not references:
            references = ["unknown"]
            issues.append(
                {
                    "code": "missing_reference",
                    "severity": "high",
                    "image_slots": [slot],
                    "message": "该图位没有参考图（缺少身份锁），需要人工补图",
                }
            )
        images_checked.append(
            {
                "slot": slot,
                "image_type": str(item.get("image_type") or "unknown"),
                "path": relative,
                "reference_images": references,
            }
        )

        target = (directory / relative).resolve() if relative else directory.resolve()
        probe = probe_image(target) if relative and target.is_relative_to(directory.resolve()) else {"ok": False, "format": "missing", "width": 0, "height": 0, "error": "没有有效 output_path"}
        format_ok = str(probe.get("format")) == "png"
        width = int(probe.get("width") or 0)
        height = int(probe.get("height") or 0)
        aspect_ok = False
        if width and height:
            aspect_ok = abs((width / height) - TARGET_ASPECT) <= ASPECT_TOLERANCE
        resolution_ok = width >= MIN_WIDTH and height >= MIN_HEIGHT
        captured = item.get("origin") == "captured"
        if captured:
            from .captured_images import validate_captured_image
            provenance = validate_captured_image(directory, item, receipts.get(slot) or {})
            if provenance:
                status, message, codes = "reject", "；".join(provenance), ["capture_provenance_invalid"]
            elif not probe.get("ok"):
                status, message, codes = "reject", "原图无法读取", ["image_file_unreadable"]
            elif probe.get("format") not in {"png", "jpeg"}:
                status, message, codes = "reject", "Seller API 商品导入明确支持 JPG/PNG；此原图可入库，但须转换后单独审核再发布", ["unsupported_format"]
            elif not aspect_ok or not resolution_ok:
                status, message, codes = "revise", "原图可读且格式可用；非项目建议3:4或900×1200，仅提示画质，不强制付费重生成", []
                original_warnings.append(slot)
            else:
                status, message, codes = "pass", "原图格式、可读性和真实采集封存校验通过；内容仍须人工审核", []
        else:
            status, message, codes = _technical_status(
                ok=bool(probe.get("ok")), format_ok=format_ok, aspect_ok=aspect_ok, resolution_ok=resolution_ok
            )
        technical.append(
            {
                "slot": slot,
                "path": relative or "unknown",
                "format": str(probe.get("format") or "unknown"),
                "width": max(1, width),
                "height": max(1, height),
                "aspect_ratio": aspect_ratio_text(width, height),
                "status": status,
                "message": message if probe.get("ok") else f"{message}：{probe.get('error')}",
            }
        )
        for code in codes:
            if code not in critical:
                critical.append(code)
            issues.append(
                {
                    "code": code,
                    "severity": "critical",
                    "image_slots": [slot],
                    "message": message,
                }
            )
        if status == "reject":
            if slot not in failing_slots:
                failing_slots.append(slot)
        elif status == "revise" and not captured:
            if slot not in revise_slots:
                revise_slots.append(slot)

    technical_ok = all(item["status"] == "pass" for item in technical)
    semantic_available = semantic_provider is not None

    dimensions: dict[str, Any] = {}
    total = 0
    for name, weight, label in DIMENSION_WEIGHTS:
        if name == "visual_quality":
            score = weight if technical_ok else (0 if failing_slots else max(0, weight // 2))
            status = "pass" if technical_ok else ("reject" if failing_slots else "revise")
            check = {
                "criterion": "technical_file_quality",
                "max_score": 12,
                "deduction": 12 - min(12, score),
                "score": min(12, score),
                "status": status,
                "message": "文件可读，真实格式、来源与各自画质标准检查通过" if technical_ok else "技术检查存在问题或画质提醒",
                "evidence": [item["path"] for item in technical[:3]],
            }
        else:
            score = 0
            status = "revise"
            check = {
                "criterion": f"{name}_semantic",
                "max_score": 12,
                "deduction": 12,
                "score": 0,
                "status": "revise",
                "message": (
                    f"{label}：已配置视觉模型，待接入评分"
                    if semantic_available
                    else f"{label}：本地未配置视觉模型，未评分"
                ),
                "evidence": ["output/image-plan.json"],
            }
        dimensions[name] = {
            "max_score": weight,
            "score": int(score),
            "status": status,
            "checks": [check],
        }
        total += int(score)

    if critical:
        decision = "reject"
        recommendation = "存在技术致命问题，必须重做这些图位：" + ", ".join(failing_slots)
        regenerate = True
    elif not semantic_available:
        decision = "revise"
        recommendation = (
            "本地可读性/格式/来源检查已执行，详见逐图结果；商品一致性/合规等维度需要视觉模型，本地未评分。"
            "正式上架前建议接入视觉质检或人工过一遍。"
        )
        regenerate = bool(revise_slots)
    elif any(item["status"] != "pass" for item in dimensions.values()):
        decision = "revise"
        recommendation = "存在需要修改的维度，按建议重生成未通过图位"
        regenerate = True
    else:
        decision = "pass"
        recommendation = "全部维度通过"
        regenerate = False

    suggestions: list[str] = []
    if not produces_final_images and generator_name:
        suggestions.append(
            f"当前图片由 `{generator_name}` 生成（占位图）：正式上架前必须用真实生图后端重跑 image_generation"
        )
    if revise_slots:
        suggestions.append(f"分辨率未达标、建议重生成的图位：{', '.join(revise_slots)}")
    if original_warnings:
        suggestions.append(f"原图画质提醒（非强制重生成）：{', '.join(original_warnings)}；仍需人工检查文字、水印、规格一致性与合规内容")
    if semantic_available:
        suggestions.append("语义维度评分已启用（视觉模型）")
    else:
        suggestions.append("语义维度未评分：可接视觉模型，或人工确认后再提交")

    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "checked_at": now_iso(),
        "source_refs": _source_refs(directory),
        "images_checked": images_checked,
        "technical_checks": technical,
        "dimensions": dimensions,
        "score": min(100, total),
        "decision": decision,
        "recommendation": recommendation,
        "issues": issues,
        "suggestions": suggestions,
        "critical_failures": critical,
        "regenerate_needed": bool(regenerate),
    }


def handle_image_qc(ctx: StepContext) -> dict[str, Any]:
    """图片质检步骤：写报告；有致命问题就当门禁失败（转人工），否则带告警通过。"""
    generator_name = None
    produces_final = False
    report_path = ctx.path("output/image-generation-report.json")
    if report_path.is_file():
        generation = _read_json(report_path)
        generator_name = generation.get("generator")
        produces_final = bool(generation.get("final_images"))

    report = run_image_qc(
        ctx.product_dir,
        generator_name=generator_name,
        produces_final_images=produces_final,
    )
    problems = validate_contract("image-qc-report", report)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "图片质检报告不符合 image-qc-report 契约",
            {"problems": problems[:8], "summary": format_problems(problems)},
        )
    ctx.write_json("output/image-qc-report.json", report)

    if report["regenerate_needed"]:
        failed = [
            item["slot"] for item in report["technical_checks"] if item["status"] in {"reject", "revise"}
        ]
        ctx.write_json(
            "output/image-regeneration-request.json",
            {
                "schema_version": "1.0.0",
                "product_id": ctx.product_dir.name,
                "failed_slots": failed,
                "reason": report["recommendation"],
                "requested_at": now_iso(),
                "preserve_passed_images": True,
            },
        )

    if report["critical_failures"]:
        raise PipelineGateError(
            ctx.step,
            "图片质检出现致命问题：" + "、".join(report["critical_failures"]),
            {"report": report, "failed_slots": [i["slot"] for i in report["technical_checks"] if i["status"] == "reject"]},
        )

    warnings = list(report["suggestions"])
    return {
        "warnings": warnings,
        "artifacts": ["output/image-qc-report.json"],
        "decision": report["decision"],
        "score": report["score"],
        "critical_failures": report["critical_failures"],
    }


IMAGE_QC_HANDLERS = {"image_qc": handle_image_qc}
