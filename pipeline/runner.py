"""干跑型流水线执行器：在**不碰 Ozon、不联网**的前提下把能做的步骤真跑一遍。

设计目标（也是安全边界）：

- ``dry_run=True``（默认）时**永不执行 ozon_upload**，直接停在闸门前并记录原因；
- 即使 ``dry_run=False``，只要 ``app_mode != "production"`` 也拒绝上传；
- 没实现的步骤**不会**被标记完成，而是停下并报 ``handler_not_implemented``
  —— 宁可停在半路，也不假装成功；
- 声明的前置产物不存在 → ``missing_inputs`` 停下（不猜、不造）；
- 跑完强制校验 ``api_write_count == 0``，一旦有 handler 偷偷发了 Ozon 写请求就抛错；
- 每次运行写 ``output/run-report.json``，便于人工复盘。

已实现的 handler 都是**纯本地**的：``validate_source``、``offer_exists_check``、``upload_feasibility``。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from parsing import is_positive_int, parse_number

from .catalog import CATALOG_HANDLERS
from .category import category_handlers
from .context import PipelineGateError, StepContext, read_json as _read_json, write_json as _write_json
from .handlers import model_handlers
from .image_generation import image_generation_handlers
from .image_qc import IMAGE_QC_HANDLERS
from .measurements import MEASUREMENT_HANDLERS
from .status import (
    MAX_SELECTED_SKUS,
    complete_step,
    load_status,
    mark_needs_attention,
    normalize,
    now_iso,
)
from .steps import PIPELINE_STEPS, step_definition
from .upload import upload_handlers

APP_MODE_PRODUCTION = "production"
_FILE_SUFFIXES = (".json", ".jsonl", ".json5", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp", ".csv")

# PipelineGateError 与 StepContext 定义在 pipeline/context.py（runner 与 handlers 共用），
# 这里重新导出以保持 `from pipeline.runner import PipelineGateError` 这类旧写法可用。
__all__ = ["PipelineGateError", "StepContext", "DEFAULT_HANDLERS", "run_product", "main"]


# --------------------------------------------------------------------- handlers


def _positive_int(value: Any) -> bool:
    return is_positive_int(value)


def _number(value: Any) -> float | None:
    return parse_number(value)


def handler_validate_source(ctx: StepContext) -> dict[str, Any]:
    """校验采集输入：硬条件是 1688 来源 + 1–10 个合法 SKU；缺 raw-snapshot/category-selection 只告警。"""
    source = ctx.require_json("input/source.json")
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    source_url = str(source.get("source_url") or "")
    checks["source_url_is_1688"] = "1688.com/offer/" in source_url
    if not checks["source_url_is_1688"]:
        raise PipelineGateError(ctx.step, f"source_url 不是 1688 商品页：{source_url or '空'}")

    from .sku_selection import active_skus

    raw_skus = source.get("skus")
    # 只校验"要上架"的 SKU：选择文件里被排除的规格不该卡住整单
    skus = active_skus(ctx.product_dir, raw_skus if isinstance(raw_skus, list) else [])
    if not 1 <= len(skus) <= MAX_SELECTED_SKUS:
        raise PipelineGateError(
            ctx.step,
            f"已选 SKU 必须在 1–{MAX_SELECTED_SKUS} 之间，实际 "
            f"{len(skus) if skus else '空'}（源数据 {len(raw_skus) if isinstance(raw_skus, list) else 0} 个，"
            "可用 pipeline.sku_selection 调整上架范围）",
        )
    checks["sku_count"] = len(skus)

    missing_sku_fields: list[str] = []
    for index, sku in enumerate(skus):
        if not isinstance(sku, Mapping):
            missing_sku_fields.append(f"#{index} 不是对象")
            continue
        if not str(sku.get("sku_id") or "").strip():
            missing_sku_fields.append(f"#{index} 缺 sku_id")
        price = None
        for key in ("purchase_price_cny", "cost_cny", "purchase_price", "price_cny"):
            price = _number(sku.get(key))
            if price is not None:
                break
        if price is None or price <= 0:
            missing_sku_fields.append(f"#{index} 缺有效采购价")
    if missing_sku_fields:
        raise PipelineGateError(ctx.step, "SKU 资料不完整", {"skus": missing_sku_fields})
    checks["sku_fields_ok"] = True

    # 采集体检：重复 SKU、offer_id 撞车、一张图都没有 → 阻断；价格离群/缺变体值 → 提醒
    from .source_quality import check_capture, render_report as render_quality

    quality = check_capture(
        {**dict(source), "product_id": ctx.product_dir.name},
        product_dir=ctx.product_dir,
        active_sku_ids=[str(item.get("sku_id") or "") for item in skus],
    )
    ctx.write_json(
        "output/source-quality.json",
        {**quality, "artifacts_note": "由 validate_source 生成；upload_feasibility 与 dossier 会读它"},
    )
    checks["quality_blocking"] = len(quality["blocking"])
    checks["quality_warnings"] = len(quality["warnings"])
    if quality["blocking"]:
        raise PipelineGateError(
            ctx.step,
            "采集体检不通过：" + "；".join(quality["blocking"][:3]),
            {"blocking": quality["blocking"], "summary": render_quality(quality)},
        )
    warnings.extend(quality["warnings"])

    for optional, label in (
        ("input/raw-snapshot.json", "原始快照 raw-snapshot.json"),
        ("input/category-selection.json", "类目选择 category-selection.json"),
    ):
        if not ctx.path(optional).exists():
            warnings.append(f"缺少可选输入：{label}（后续步骤可能需要）")

    result = {"checked_at": now_iso(), "checks": checks, "warnings": warnings, "api_calls": 0}
    ctx.write_json("output/source-validation.json", result)
    return {
        "warnings": warnings,
        "artifacts": ["output/source-validation.json", "output/source-quality.json"],
        "checks": checks,
    }


def handler_offer_exists_check(ctx: StepContext) -> dict[str, Any]:
    """原项目这一步不查 API：只把店铺级重复检查推迟到提交阶段。"""
    source = ctx.read_json("input/source.json")
    skus = source.get("skus") if isinstance(source.get("skus"), list) else []
    payload = {
        "checked_at": now_iso(),
        "status": "deferred_store_specific",
        "reason": "重复检查按店铺在提交前进行；本步骤不做任何 Ozon 只读/写调用。",
        "candidate_offer_ids": [
            str(sku.get("offer_id") or sku.get("sku_id") or "") for sku in skus if isinstance(sku, Mapping)
        ],
        "api_calls": 0,
    }
    ctx.write_json("output/offer-id-precheck.json", payload)
    return {"warnings": [], "artifacts": ["output/offer-id-precheck.json"]}


def handler_upload_feasibility(ctx: StepContext) -> dict[str, Any]:
    """本地可判定的上架可行性子集。缺数据的检查给 WARN 而不是假装 PASS。

    硬门禁（FAIL）：类目必须来自 Ozon API 且匹配状态合规；必填属性 missing 必须为 0。
    软检查（WARN，M3/M4 接入后升级为硬门禁）：定价、包装>商品、图片齐全。
    """
    checks: dict[str, Any] = {}

    category = ctx.require_json("output/ozon-category.json")
    metadata_source = str(category.get("metadata_source") or "")
    match_status = str(category.get("match_status") or "")
    category_ok = (
        metadata_source == "ozon_seller_api"
        and _positive_int(category.get("category_id"))
        and _positive_int(category.get("type_id"))
        and match_status in {"api_confirmed", "api_match_needs_review"}
    )
    checks["category"] = {
        "status": "PASS" if category_ok else "FAIL",
        "metadata_source": metadata_source,
        "match_status": match_status,
        "category_id": category.get("category_id"),
        "type_id": category.get("type_id"),
        "note": "必须是 Ozon Seller API 的实时类目，且 category_id/type_id 为正整数",
    }

    attributes = ctx.read_json("output/ozon-attributes-final.json")
    attributes_source = "output/ozon-attributes-final.json"
    if not attributes:
        # 阶段 A 的早期预检：最终属性还没编译时回退读预览属性（上游同语义）
        attributes = ctx.read_json("output/ozon-attributes.json")
        attributes_source = "output/ozon-attributes.json"
    missing_required = None
    summary = attributes.get("required_summary")
    if isinstance(summary, Mapping):
        missing_required = summary.get("missing")
    if missing_required is None and isinstance(attributes.get("missing_required_attributes"), list):
        missing_required = len(attributes["missing_required_attributes"])
    if not attributes:
        checks["required_attributes"] = {
            "status": "WARN",
            "note": "阶段 A 还没有属性文件（field_completion 之后会变成硬门禁），"
            "提交前的必填属性门禁在 upload payload / ozon_upload 步骤",
        }
    else:
        attributes_ok = isinstance(missing_required, int) and missing_required == 0
        checks["required_attributes"] = {
            "status": "PASS" if attributes_ok else "FAIL",
            "missing": missing_required,
            "source": attributes_source,
            "note": "required_summary.missing 必须为 0",
        }

    pricing = ctx.read_json("output/pricing-result.json")
    prices = pricing.get("skus") if isinstance(pricing.get("skus"), list) else []
    priced = [
        item for item in prices
        if isinstance(item, Mapping) and (_number(item.get("selling_price_rub")) or 0) > 0
    ]
    if prices:
        checks["pricing"] = {
            "status": "PASS" if len(priced) == len(prices) else "FAIL",
            "priced_skus": len(priced),
            "total_skus": len(prices),
        }
    else:
        checks["pricing"] = {
            "status": "WARN",
            "note": "还没有 output/pricing-result.json；M4 前必须补上（每个 SKU 需 selling_price_rub）",
        }

    cost = ctx.read_json("output/measurements.json")
    product_dims = cost.get("product") if isinstance(cost.get("product"), Mapping) else None
    package_dims = cost.get("package") if isinstance(cost.get("package"), Mapping) else None
    if product_dims and package_dims:
        axis_ok = all(
            (_number(package_dims.get(axis)) or 0) >= (_number(product_dims.get(axis)) or 0)
            for axis in ("length_mm", "width_mm", "height_mm")
        )
        weight_ok = (_number(package_dims.get("weight_g")) or 0) >= (_number(product_dims.get("weight_g")) or 0)
        checks["measurement_hierarchy"] = {
            "status": "PASS" if axis_ok and weight_ok else "FAIL",
            "note": "包装尺寸与重量必须大于等于商品本体",
        }
    else:
        checks["measurement_hierarchy"] = {
            "status": "WARN",
            "note": "缺少 output/measurements.json 的商品/包装尺寸重量（先跑 measurements 并人工确认）",
        }

    image_dirs = {
        name: len([p for p in (ctx.path(rel)).glob("*") if p.is_file()])
        for name, rel in (
            ("main", "input/main-images"),
            ("sku", "input/sku-images"),
            ("detail", "input/detail-images"),
        )
    }
    has_images = image_dirs["main"] > 0 and image_dirs["sku"] > 0
    checks["images"] = {
        "status": "PASS" if has_images else "WARN",
        "counts": image_dirs,
        "note": "M3 接入图片计划/QC 后，这里升级为硬门禁（N 张 SKU 主图 + 8 张详情图）",
    }

    blocking = [name for name, item in checks.items() if item.get("status") == "FAIL"]
    warnings = [f"{name}: {item.get('note', '')}" for name, item in checks.items() if item.get("status") == "WARN"]
    payload = {
        "checked_at": now_iso(),
        "status": "FAIL" if blocking else "PASS",
        "blocking_checks": blocking,
        "warnings": warnings,
        "checks": checks,
        "api_calls": 0,
    }
    ctx.write_json("output/upload-feasibility.json", payload)
    if blocking:
        raise PipelineGateError(
            ctx.step,
            "上架可行性未通过：" + "、".join(blocking),
            {"blocking_checks": blocking, "checks": checks},
        )
    return {"warnings": warnings, "artifacts": ["output/upload-feasibility.json"], "checks": checks}


DEFAULT_HANDLERS: dict[str, Callable[[StepContext], dict[str, Any]]] = {
    "validate_source": handler_validate_source,
    "offer_exists_check": handler_offer_exists_check,
    "upload_feasibility": handler_upload_feasibility,
}
# 类目/属性相关的本地 handler（变体规则、属性编译）也在默认集合里
DEFAULT_HANDLERS.update(CATALOG_HANDLERS)
# 图片质检是纯本地检查（尺寸/格式/比例），永远可用
DEFAULT_HANDLERS.update(IMAGE_QC_HANDLERS)
# 定价与尺寸重量也是纯本地（不联网、不编造尺寸）
DEFAULT_HANDLERS.update(MEASUREMENT_HANDLERS)


# --------------------------------------------------------------------- runner


def _input_present(product_dir: Path, relative: str) -> bool:
    path = product_dir / relative
    if relative.endswith(_FILE_SUFFIXES):
        return path.is_file()
    return path.exists() and (path.is_dir() and any(path.iterdir()) or path.is_file())


def run_product(
    product_dir: Path | str,
    *,
    until: str | None = None,
    dry_run: bool = True,
    app_mode: str | None = None,
    handlers: Mapping[str, Callable[[StepContext], dict[str, Any]]] | None = None,
    step_budget: int | None = None,
    provider: Any | None = None,
    uploader: Any | None = None,
    image_generator: Any | None = None,
    ozon_client: Any | None = None,
) -> dict[str, Any]:
    """按状态机跑当前待处理步骤，直到：到达 ``until``、缺少 handler/输入、门禁失败或跑完。

    - ``provider`` 模型层 → 注册 product_analysis / russian_copy / image_plan
    - ``image_generator`` 生图后端 → 注册 image_generation
    - ``uploader`` 上传器 → 注册 ozon_upload（仍需 ``dry_run=False`` + ``app_mode=production``）
    - ``ozon_client`` Ozon 只读客户端 → 注册 category_match

    没传的步骤会如实停在 ``handler_not_implemented``（不会假装完成）。

    返回 run report（同时写入 ``output/run-report.json``）。
    """
    directory = Path(product_dir)
    mode = str(app_mode or os.environ.get("APP_MODE") or "development")
    available: dict[str, Callable[[StepContext], dict[str, Any]]] = dict(DEFAULT_HANDLERS)
    available.update(model_handlers(provider))
    available.update(image_generation_handlers(image_generator))
    available.update(upload_handlers(uploader))
    available.update(category_handlers(ozon_client))
    available.update(handlers or {})

    status = normalize(load_status(directory))
    if status.get("task_authorized") is not True:
        raise ValueError("商品未授权运行：先调用 create_batch() 或 queue_product()")
    if until is not None and until not in PIPELINE_STEPS:
        raise ValueError(f"--until 必须是流水线步骤：{until}")

    #: 本次运行开始时的累计写请求数（用于"本次干跑有没有写"的增量判断）
    api_write_count_before = int(status.get("api_write_count") or 0)

    started_at = now_iso()
    executed: list[dict[str, Any]] = []
    stop_reason: str | None = None
    stopped_at: str | None = None
    steps_run = 0

    for step in list(status.get("pending_steps") or PIPELINE_STEPS):
        if until is not None and step == until:
            stop_reason, stopped_at = "until_reached", step
            break
        if step_budget is not None and steps_run >= step_budget:
            stop_reason, stopped_at = "step_budget_exhausted", step
            break

        if step == "ozon_upload":
            if dry_run:
                stop_reason, stopped_at = "upload_gate_refused_dry_run", step
                break
            if mode != APP_MODE_PRODUCTION:
                stop_reason, stopped_at = "upload_gate_refused_app_mode", step
                break

        handler = available.get(step)
        if handler is None:
            stop_reason, stopped_at = "handler_not_implemented", step
            break

        definition = step_definition(step)
        optional = set(definition.get("optional_inputs") or ())
        missing = [
            rel
            for rel in definition.get("inputs", [])
            if rel not in optional and not _input_present(directory, rel)
        ]
        if missing:
            executed.append({"step": step, "status": "blocked", "missing_inputs": missing})
            stop_reason, stopped_at = "missing_inputs", step
            break

        began = time.monotonic()
        context = StepContext(directory, step, dry_run, mode, status, provider, uploader, image_generator, ozon_client)
        try:
            result = handler(context)
        except PipelineGateError as error:
            mark_needs_attention(directory, step, error.reason)
            executed.append(
                {
                    "step": step,
                    "status": "gate_failed",
                    "reason": error.reason,
                    "details": error.details,
                }
            )
            stop_reason, stopped_at = "gate_failed", step
            status = normalize(load_status(directory))
            break

        duration = int(time.monotonic() - began)
        status = complete_step(directory, step, duration_seconds=duration)
        executed.append(
            {
                "step": step,
                "status": "completed",
                "duration_seconds": duration,
                "warnings": list(result.get("warnings") or []),
                "artifacts": list(result.get("artifacts") or []),
            }
        )
        steps_run += 1

    final = normalize(load_status(directory))
    api_write_count = int(final.get("api_write_count") or 0)
    # 不变量：**本次**干跑不能产生 Ozon 写请求。
    # 注意要比较"增量"而不是累计值 —— 商品可能在之前的 production 运行里真提交过，
    # 那时累计值本来就 >0；拿累计值判断会让"对已提交商品再干跑一次"直接崩（真实踩过）。
    writes_this_run = api_write_count - api_write_count_before
    if dry_run and writes_this_run != 0:
        raise RuntimeError(
            f"不变量被破坏：本次干跑出现 Ozon 写请求（+{writes_this_run}，累计 {api_write_count}）"
        )

    report = {
        "schema_version": "1.0.0",
        "product_id": directory.name,
        "batch_id": final.get("batch_id"),
        "dry_run": bool(dry_run),
        "app_mode": mode,
        "started_at": started_at,
        "finished_at": now_iso(),
        "until": until,
        "stop_reason": stop_reason or "all_available_steps_done",
        "stopped_at": stopped_at,
        "executed": executed,
        "completed_steps": final.get("completed_steps"),
        "pending_steps": final.get("pending_steps"),
        "product_status": final.get("status"),
        "progress": final.get("progress"),
        "api_write_count": api_write_count,
    }
    _write_json(directory / "output" / "run-report.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="干跑型流水线执行器（不碰 Ozon）")
    parser.add_argument("--product-dir", required=True, help="例如 products/P000001")
    parser.add_argument("--until", default=None, help="跑到该步骤之前停下（默认跑完所有可执行步骤）")
    parser.add_argument(
        "--execute-upload",
        action="store_true",
        help="允许执行 ozon_upload（仍需 APP_MODE=production，且必须已注册上传 handler）",
    )
    parser.add_argument("--step-budget", type=int, default=None, help="本次最多执行多少步")
    parser.add_argument(
        "--provider",
        default=None,
        help="模型层：fake 或 none（默认 none，只跑本地步骤）",
    )
    parser.add_argument(
        "--uploader",
        default=None,
        help="上传器：dry-run / simulated / ozon-api（ozon-api 会真的写 Ozon，需要 --i-understand-this-hits-ozon）",
    )
    parser.add_argument(
        "--i-understand-this-hits-ozon",
        action="store_true",
        help="使用 --uploader ozon-api 且 --execute-upload 时的安全确认",
    )
    parser.add_argument(
        "--image-generator",
        default=None,
        help="生图后端：placeholder / doubao（需 ARK_API_KEY）/ rightapi（需 RIGHTAPI_API_KEY）/ none",
    )
    parser.add_argument(
        "--ozon-fixture",
        default=None,
        help="用录制夹具代替真实 Ozon 只读接口（离线自检），值为夹具目录",
    )
    parser.add_argument(
        "--ozon-real",
        action="store_true",
        help="使用真实 Ozon 只读接口（需要 config/shops.json + 凭据环境变量）",
    )
    args = parser.parse_args(argv)

    provider = None
    if args.provider and args.provider.lower() not in {"none", "off"}:
        from models import load_provider

        provider = load_provider(args.provider)

    uploader = None
    if args.uploader and args.uploader.lower() not in {"none", "off"}:
        from .upload import DryRunUploader, SimulatedUploader

        resolved = args.uploader.lower()
        if resolved in {"ozon", "ozon-api", "real"}:
            if not (args.execute_upload and args.i_understand_this_hits_ozon):
                raise SystemExit(
                    "拒绝使用真实提交器：需要同时给出 --execute-upload 与 --i-understand-this-hits-ozon"
                    "（并且 app_mode 必须是 production）"
                )
            from .ozon_write import OzonWriteUploader

            uploader = OzonWriteUploader()
        else:
            uploader = SimulatedUploader() if resolved in {"simulated", "sim"} else DryRunUploader()

    image_generator = None
    if args.image_generator and args.image_generator.lower() not in {"none", "off"}:
        from models import load_image_generator

        try:
            image_generator = load_image_generator(args.image_generator)
        except Exception as error:  # 生图后端配置问题要给出清晰提示，而不是堆栈
            raise SystemExit(f"生图后端不可用：{error}") from error

    ozon_client = None
    if args.ozon_fixture:
        from .ozon_http import FixtureTransport, OzonClient

        ozon_client = OzonClient(FixtureTransport(directory=Path(args.ozon_fixture)))
    elif args.ozon_real:
        from .ozon_http import OzonClient, OzonCredentials, UrllibTransport
        from .stores import list_shops, load_registry

        registry = load_registry()
        shops = list_shops(registry)
        if not shops:
            raise SystemExit("没有可用店铺：先配置 config/shops.json")
        credentials = OzonCredentials.from_shop(shops[0])
        ozon_client = OzonClient(UrllibTransport(credentials))

    report = run_product(
        args.product_dir,
        until=args.until,
        dry_run=not args.execute_upload,
        step_budget=args.step_budget,
        provider=provider,
        uploader=uploader,
        image_generator=image_generator,
        ozon_client=ozon_client,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
