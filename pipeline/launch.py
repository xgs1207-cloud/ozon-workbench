"""一条命令跑完一个商品（或一批商品）：生图 → 发布图片 → 质检 → 载荷 → 提交。

为什么需要它：流水线里 ``image_qc`` 之后、``ozon_upload`` 之前，必须先把图片发布到对象存储
（COS / 自建 nginx）并写出 ``output/image-public-urls.json`` —— 上传门禁**只接受 https 公网地址**，
否则会被 ``production_blockers`` 拦住。这一步原先要手工敲两条命令，容易漏、也容易顺序搞错。

设计取舍：

- **不改 15 步状态机**：发布是"商品目录上的一次外部动作"，不是流水线步骤，所以由本编排器在
  两次 ``run_product`` 之间执行，而不是塞进状态机；
- **阶段清晰**：① 内容与图片（跑到 image_qc）→ ② 发布图片（COS/本地目录）→ ③ 提交（干跑给回执/真提交）；
- **干跑语义与 runner 一致**：不传 ``--execute-upload`` 时，第 ③ 阶段走 ``upload_product(upload_mode="dry-run")``
  产生回执与阻断项，**绝不写任何请求、绝不产生 task_id**；
- 单店失败不影响其它店（沿用 upload 层语义），批量模式里单个商品失败也不影响其它商品。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .batch import create_batch
from .runner import run_product
from .upload import UPLOAD_MODE_DRY_RUN, UPLOAD_MODE_PRODUCTION, upload_product

#: 阶段名（报告里用）
PHASE_CONTENT = "content_and_images"
PHASE_PUBLISH = "publish_images"
PHASE_UPLOAD = "upload"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _runner_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    stop_reason = report.get("stop_reason")
    stopped_at = report.get("stopped_at")
    hints = {
        "handler_not_implemented": {
            "image_generation": "没有配置生图后端：加 --image-generator placeholder（本地占位图）或 doubao（需 ARK_API_KEY）",
            "category_match": "没有 Ozon 只读客户端：加 --ozon-fixture contracts/fixtures（离线）或配置 Ozon 凭据",
            "ozon_upload": "没有配置 uploader：加 --uploader dry-run|simulated|ozon-api",
        },
        "missing_inputs": {"measurements": "缺人工确认的尺寸重量（input/workbench-sku-overrides.json）"},
        "gate_failed": {"ecommerce_design": "文案/设计没过校验，看 output/run-report.json 里的 problems"},
    }
    hint = (hints.get(str(stop_reason)) or {}).get(str(stopped_at))
    return {
        "stop_reason": stop_reason,
        "stopped_at": stopped_at,
        "hint": hint,
        "completed_steps": list(report.get("completed_steps") or []),
        "product_status": report.get("product_status"),
        "progress": report.get("progress"),
        "api_write_count": report.get("api_write_count"),
        "warnings": [
            f"{item.get('step')}: {item.get('reason')}"
            for item in (report.get("executed") or [])
            if item.get("status") in {"gate_failed", "missing_inputs", "handler_not_implemented"}
        ][:5],
    }


def _collect_evidence(product_dir: Path) -> dict[str, Any]:
    """把关键产物摘要出来（预检、质检、载荷阻断、店铺回执）。"""
    qc = _read_json(product_dir / "output" / "image-qc-report.json")
    urls = _read_json(product_dir / "output" / "image-public-urls.json")
    receipts: dict[str, Any] = {}
    run_dir = product_dir / "output" / "store-runs"
    if run_dir.is_dir():
        for store_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
            result = _read_json(store_dir / "ozon-result.json")
            if result:
                receipts[store_dir.name] = {
                    "status": result.get("status"),
                    "task_id": result.get("task_id"),
                    "items": len(result.get("items") or []),
                }
    return {
        "image_qc": {"decision": qc.get("decision"), "score": qc.get("score"), "critical": qc.get("critical_failures")},
        "image_public_urls": {
            "count": len(urls.get("urls") or {}),
            "storage": urls.get("storage") or ("local-static" if urls.get("base_url") else None),
            "https_only": all(str(u).startswith("https://") for u in (urls.get("urls") or {}).values())
            if urls.get("urls")
            else False,
        },
        "receipts": receipts,
    }


def ensure_authorized(
    directory: Path,
    *,
    store_ids: Sequence[str],
    batches_root: Path | str | None = None,
    products_root: Path | str | None = None,
) -> dict[str, Any]:
    """没授权就先建批次（runner 拒绝未授权的商品，这是刻意的门禁）。"""
    from .status import load_status

    status = load_status(directory)
    if status.get("task_authorized"):
        return {"authorized": True, "created": False, "batch_id": status.get("batch_id")}
    targets = [str(item) for item in store_ids] or [str(item) for item in (status.get("target_store_ids") or [])]
    if not targets:
        return {
            "authorized": False,
            "created": False,
            "reason": "商品未授权且没有目标店铺：用 --store 指定，或先 create_batch()",
        }
    root = Path(products_root) if products_root else directory.parent
    batch = create_batch(
        root,
        batches_root=batches_root,
        product_ids=[directory.name],
        target_store_ids=targets,
    )
    return {"authorized": True, "created": True, "batch_id": batch.get("batch_id"), "stores": targets}


def launch_product(
    product_dir: Path | str,
    *,
    provider: Any | None = None,
    image_generator: Any | None = None,
    uploader: Any | None = None,
    ozon_client: Any | None = None,
    publisher: Any | None = None,
    store_ids: Sequence[str] | None = None,
    execute_upload: bool = False,
    app_mode: str | None = None,
    step_budget: int = 30,
    handlers: Mapping[str, Any] | None = None,
    enabled_store_ids: Sequence[str] | None = None,
    batches_root: Path | str | None = None,
    products_root: Path | str | None = None,
    auto_authorize: bool = True,
) -> dict[str, Any]:
    """跑完一个商品：授权 → 内容与图片 → 发布图片 → 提交（或干跑回执）。"""
    directory = Path(product_dir)
    mode = app_mode or ("production" if execute_upload else "development")
    dry = not execute_upload
    phases: list[dict[str, Any]] = []

    # ⓪ 授权（runner 对未授权商品会直接拒绝）
    if auto_authorize:
        authorization = ensure_authorized(
            directory, store_ids=list(store_ids or []), batches_root=batches_root, products_root=products_root
        )
        phases.append({"phase": "authorize", **authorization})
        if not authorization.get("authorized"):
            return {
                "ok": False,
                "product_id": directory.name,
                "stopped_phase": "authorize",
                "reason": authorization.get("reason"),
                "phases": phases,
                "evidence": _collect_evidence(directory),
            }

    # ① 内容与图片（跑到 ozon_upload 之前）
    first = run_product(
        directory,
        until="ozon_upload",
        dry_run=dry,
        app_mode=mode,
        step_budget=step_budget,
        provider=provider,
        uploader=uploader,
        image_generator=image_generator,
        ozon_client=ozon_client,
        handlers=dict(handlers or {}),
    )
    phases.append({"phase": PHASE_CONTENT, **_runner_summary(first)})
    if first.get("stop_reason") not in {"until_reached", "all_available_steps_done"}:
        return {
            "ok": False,
            "product_id": directory.name,
            "stopped_phase": PHASE_CONTENT,
            "reason": first.get("stop_reason"),
            "phases": phases,
            "evidence": _collect_evidence(directory),
        }

    # ② 发布图片（COS 或自建目录）——上传门禁只认 https 公网地址
    if publisher is not None:
        try:
            published = publisher.publish_product(directory)
        except Exception as error:  # noqa: BLE001 - 发布失败要如实报，不要假装继续
            phases.append({"phase": PHASE_PUBLISH, "status": "failed", "error": str(error)})
            return {
                "ok": False,
                "product_id": directory.name,
                "stopped_phase": PHASE_PUBLISH,
                "reason": f"发布图片失败：{error}",
                "phases": phases,
                "evidence": _collect_evidence(directory),
            }
        phases.append(
            {
                "phase": PHASE_PUBLISH,
                "status": "ok",
                "storage": published.get("storage"),
                # 自建目录叫 published，COS 叫 uploaded；两个都报出来，别让人以为"没传"
                "uploaded": published.get("uploaded", published.get("published")),
                "unchanged": published.get("unchanged"),
                "missing": [item.get("slot") for item in (published.get("missing") or [])],
                "urls": len(published.get("urls") or {}),
                "https_ok": published.get("https_ok"),
            }
        )
        if published.get("missing"):
            return {
                "ok": False,
                "product_id": directory.name,
                "stopped_phase": PHASE_PUBLISH,
                "reason": "有图位没有发布成功（上传门禁会拦住）",
                "phases": phases,
                "evidence": _collect_evidence(directory),
            }

    # ③ 提交（真提交走 runner；干跑直接拿回执，避免 runner 的干跑闸门把它拦下）
    if dry or uploader is None:
        target_stores = list(store_ids or [])
        if not target_stores:
            from .status import load_status

            target_stores = [str(item) for item in (load_status(directory).get("target_store_ids") or [])]
        if uploader is None or not target_stores:
            phases.append(
                {
                    "phase": PHASE_UPLOAD,
                    "status": "skipped",
                    "reason": "没有配置 uploader 或没有目标店铺" if not target_stores else "没有配置 uploader",
                }
            )
            return {
                "ok": False,
                "product_id": directory.name,
                "stopped_phase": PHASE_UPLOAD,
                "reason": "缺少 uploader 或目标店铺",
                "phases": phases,
                "evidence": _collect_evidence(directory),
            }
        summary = upload_product(
            directory,
            target_stores,
            uploader,
            upload_mode=UPLOAD_MODE_DRY_RUN,
            enabled_store_ids=enabled_store_ids,
        )
        phases.append({"phase": PHASE_UPLOAD, "mode": "dry_run_receipt", **{
            key: summary.get(key) for key in ("submitted", "skipped", "failed", "api_writes")
        }})
        evidence = _collect_evidence(directory)
        return {
            "ok": summary.get("failed", 0) == 0,
            "product_id": directory.name,
            "phases": phases,
            "evidence": evidence,
            "dry_run": True,
        }

    second = run_product(
        directory,
        dry_run=False,
        app_mode=mode,
        step_budget=step_budget,
        provider=provider,
        uploader=uploader,
        image_generator=image_generator,
        ozon_client=ozon_client,
        handlers=dict(handlers or {}),
    )
    phases.append({"phase": PHASE_UPLOAD, "mode": "runner", **_runner_summary(second)})
    evidence = _collect_evidence(directory)
    return {
        "ok": second.get("stop_reason") == "all_available_steps_done",
        "product_id": directory.name,
        "phases": phases,
        "evidence": evidence,
        "dry_run": False,
    }


def select_products(
    products_root: Path | str,
    *,
    keywords: Sequence[str] | None = None,
    status: str | None = "COLLECTED",
) -> list[Path]:
    """挑出要跑的商品：默认所有 COLLECTED；给了关键词就只挑挂了这些词的。"""
    from collector.collection_plan import _product_keywords

    root = Path(products_root)
    if not root.is_dir():
        return []
    wanted = {str(item).strip().casefold() for item in (keywords or []) if str(item).strip()}
    selected: list[Path] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if not (directory / "status.json").is_file():
            continue
        current = str(_read_json(directory / "status.json").get("status") or "")
        if status and current != status:
            continue
        if wanted:
            linked = {text.casefold() for text in _product_keywords(directory)}
            if not linked & wanted:
                continue
        selected.append(directory)
    return selected


def launch_batch(
    products_root: Path | str,
    store_ids: Sequence[str],
    *,
    batches_root: Path | str | None = None,
    keywords: Sequence[str] | None = None,
    status: str | None = "COLLECTED",
    **launch_kwargs: Any,
) -> dict[str, Any]:
    """给一批商品建批次并逐个跑（单个失败不影响其它）。"""
    products = select_products(products_root, keywords=keywords, status=status)
    if not products:
        return {"ok": False, "reason": "没有符合条件的商品（默认只挑 COLLECTED）", "products": []}

    batch = create_batch(
        Path(products_root),
        batches_root=batches_root,
        product_ids=[item.name for item in products],
        target_store_ids=list(store_ids),
    )
    results: list[dict[str, Any]] = []
    for directory in products:
        results.append(
            launch_product(
                directory,
                store_ids=store_ids,
                batches_root=batches_root,
                products_root=products_root,
                auto_authorize=False,  # 批次已建好并授权
                **launch_kwargs,
            )
        )
    failed = [item["product_id"] for item in results if not item.get("ok")]
    return {
        "ok": not failed,
        "batch_id": batch.get("batch_id"),
        "products": results,
        "summary": {
            "products": len(results),
            "ok": len(results) - len(failed),
            "failed": failed,
            "stopped_phases": sorted({item.get("stopped_phase") for item in results if item.get("stopped_phase")}),
        },
    }


def render_report(report: Mapping[str, Any]) -> str:
    lines = [f"# 一键跑批次 · {'成功' if report.get('ok') else '有阻断'}", ""]
    summary = report.get("summary") or {}
    if summary:
        lines.append(
            f"批次 {report.get('batch_id')}：商品 {summary.get('products')} 个，"
            f"成功 {summary.get('ok')}，失败 {len(summary.get('failed') or [])}"
        )
        lines.append("")
    for item in report.get("products") or [report]:
        lines.append(f"## {item.get('product_id')}")
        for phase in item.get("phases") or []:
            detail = {k: v for k, v in phase.items() if k not in {"phase", "completed_steps"}}
            lines.append(f"- **{phase.get('phase')}**：{json.dumps(detail, ensure_ascii=False)}")
        evidence = item.get("evidence") or {}
        if evidence:
            lines.append(f"- 质检：{json.dumps(evidence.get('image_qc'), ensure_ascii=False)}")
            lines.append(f"- 图片地址：{json.dumps(evidence.get('image_public_urls'), ensure_ascii=False)}")
            if evidence.get("receipts"):
                lines.append(f"- 店铺回执：{json.dumps(evidence['receipts'], ensure_ascii=False)}")
        if item.get("reason"):
            lines.append(f"- ⛔ 停在 {item.get('stopped_phase')}：{item.get('reason')}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一条命令跑完商品：生图 → 发布图片 → 质检 → 载荷 → 提交")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--product-dir", help="单个商品目录")
    target.add_argument("--products-root", help="批量：商品根目录（默认只挑 COLLECTED）")
    parser.add_argument("--batch-from-collection-plan", action="store_true",
                        help="批量时只用采集清单里已采集的关键词（等价于 --keyword 批量传）")
    parser.add_argument("--keyword", action="append", dest="keywords", help="只跑挂了这些关键词的商品（可重复）")
    parser.add_argument("--status", default="COLLECTED", help="批量时筛选的商品状态（空字符串=不筛）")
    parser.add_argument("--batches-root", default=None)
    parser.add_argument("--store", action="append", dest="stores", help="目标店铺（可重复；批量时必填）")
    parser.add_argument("--provider", default="fake", help="模型层：fake / ark / http / none")
    parser.add_argument("--image-generator", default=None, help="生图后端：placeholder / doubao / none")
    parser.add_argument("--uploader", default="dry-run", help="上传器：dry-run / simulated / ozon-api / none")
    parser.add_argument("--execute-upload", action="store_true", help="真提交（需 --store 与生产模式确认）")
    parser.add_argument("--i-understand-this-hits-ozon", action="store_true")
    parser.add_argument("--oss", default="none", help="图片发布：cos / local / none")
    parser.add_argument("--oss-root", default=None, help="local 模式的静态目录")
    parser.add_argument("--oss-base-url", default=None, help="local 模式的公网前缀（https://...）")
    parser.add_argument("--key-prefix", default=None, help="cos 模式的对象键前缀覆盖")
    parser.add_argument(
        "--ozon-fixture",
        default=None,
        help="用录制夹具做 Ozon 只读调用（离线演练，如 contracts/fixtures）；不给则需真实凭据",
    )
    parser.add_argument("--step-budget", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from models import ModelError, load_image_generator, load_provider

    provider = None
    if str(args.provider or "none").lower() not in {"none", "off"}:
        try:
            provider = load_provider(args.provider)
        except ModelError as error:
            print(json.dumps({"ok": False, "error": f"模型层不可用：{error}"}, ensure_ascii=False, indent=2))
            return 1

    image_generator = None
    if args.image_generator and args.image_generator.lower() not in {"none", "off"}:
        try:
            image_generator = load_image_generator(args.image_generator)
        except ModelError as error:
            print(json.dumps({"ok": False, "error": f"生图后端不可用：{error}"}, ensure_ascii=False, indent=2))
            return 1

    from .upload import DryRunUploader, SimulatedUploader

    uploader = None
    if str(args.uploader or "none").lower() not in {"none", "off"}:
        resolved = str(args.uploader).lower()
        if resolved in {"ozon", "ozon-api", "real"}:
            if not (args.execute_upload and args.i_understand_this_hits_ozon):
                raise SystemExit("拒绝使用真实提交器：需要 --execute-upload 与 --i-understand-this-hits-ozon")
            from .ozon_write import OzonWriteUploader

            uploader = OzonWriteUploader()
        else:
            uploader = SimulatedUploader() if resolved in {"simulated", "sim"} else DryRunUploader()

    publisher = None
    publish_kind = str(args.oss or "none").lower()
    if publish_kind == "cos":
        from .oss_cos import CosError, _storage_from_env

        try:
            publisher = _storage_from_env(dry_run=False)
            if args.key_prefix:
                publisher.key_prefix = str(args.key_prefix).strip("/")
        except CosError as error:
            print(json.dumps({"ok": False, "error": f"COS 不可用：{error}"}, ensure_ascii=False, indent=2))
            return 1
    elif publish_kind == "local":
        if not (args.oss_root and args.oss_base_url):
            parser.error("--oss local 需要 --oss-root 与 --oss-base-url")
        from .oss_local import LocalObjectStorage

        publisher = LocalObjectStorage(args.oss_root, args.oss_base_url)

    common: dict[str, Any] = {
        "provider": provider,
        "image_generator": image_generator,
        "uploader": uploader,
        "publisher": publisher,
        "execute_upload": bool(args.execute_upload),
        "step_budget": args.step_budget,
    }

    if args.ozon_fixture:
        from .ozon_http import FixtureTransport, OzonClient

        common["ozon_client"] = OzonClient(FixtureTransport(directory=Path(args.ozon_fixture)))
        common["batches_root"] = args.batches_root

    if args.product_dir:
        report = launch_product(args.product_dir, store_ids=args.stores or [], **common)
    else:
        if not args.stores:
            parser.error("批量模式需要 --store")
        keywords = args.keywords
        if args.batch_from_collection_plan and not keywords:
            plan = _read_json(Path("output") / "collection-plan.json")
            keywords = [
                str(item.get("keyword"))
                for item in (plan.get("tasks") or [])
                if item.get("status") == "collected" and item.get("keyword")
            ]
        report = launch_batch(
            args.products_root, args.stores, batches_root=args.batches_root, keywords=keywords, **common
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_report(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
