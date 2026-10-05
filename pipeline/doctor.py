"""上线前预检（doctor）：一条命令回答"还差什么才能真正提交到 Ozon"。

设计原则：**不重复实现门禁**。每个商品的可提交性直接复用
:func:`pipeline.upload.build_upload_payload` 的 ``production_blockers``（单一事实来源），
这里只额外补充环境检查（契约是否拉齐、店铺注册表与凭据是否就绪）与结构检查。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import available_contracts

from . import stores as store_registry
from .publications import load_publications, plan_publications, store_has_task
from .status import load_status, normalize
from .upload import build_upload_payload

SCHEMA_VERSION = "1.0.0"

REQUIRED_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("input/source.json", "采集输入"),
    ("input/selected-keywords.json", "已选关键词"),
    ("output/product-analysis.json", "商品分析"),
    ("output/copy-ru.json", "俄文标题/简介"),
    ("output/image-plan.json", "图片计划"),
    ("output/image-qc-report.json", "图片质检报告"),
    ("output/ozon-category.json", "类目（Ozon API）"),
    ("output/ozon-category-attributes.json", "类目属性快照"),
    ("output/ozon-attributes-final.json", "最终属性"),
    ("output/pricing-result.json", "卢布定价"),
    ("output/cost-analysis.json", "尺寸重量"),
    ("output/image-public-urls.json", "图片公网地址"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def check_environment(
    *,
    registry_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    contracts = available_contracts()
    registry = store_registry.load_registry(registry_path)
    registry_problems = store_registry.validate_registry(registry) if registry.get("shops") else ["注册表不存在或为空"]
    credentials = store_registry.credential_report(registry, env)
    enabled = store_registry.enabled_shop_ids(registry)
    ready_shops = [item["shop_id"] for item in credentials if item["ready"] and item["shop_id"] in enabled]

    blockers: list[str] = []
    if not contracts:
        blockers.append("尚未拉取上游契约：运行 contracts/fetch_contracts.ps1")
    if registry_problems:
        blockers.append("店铺注册表不可用：" + "；".join(registry_problems[:3]))
    if not enabled:
        blockers.append("没有任何启用的店铺（config/shops.json 里 enabled=true）")
    if enabled and not ready_shops:
        blockers.append("已启用店铺都缺凭据：按 shops.json 里的 *_env 设置环境变量")

    return {
        "contracts_available": len(contracts),
        "registry_path": str(registry_path or store_registry.DEFAULT_PATH),
        "shops_total": len(store_registry.list_shops(registry)),
        "shops_enabled": enabled,
        "shops_with_credentials": ready_shops,
        "credential_details": credentials,
        "blockers": blockers,
    }


def diagnose_product(
    product_dir: Path | str,
    *,
    enabled_store_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    directory = Path(product_dir)
    status = normalize(load_status(directory))
    product_id = directory.name

    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    blockers: list[str] = []

    for relative, label in REQUIRED_ARTIFACTS:
        exists = (directory / relative).is_file()
        checks.append({"name": label, "path": relative, "status": "ok" if exists else "missing"})

    store_ids = [str(item) for item in (status.get("target_store_ids") or [])]
    if not store_ids:
        blockers.append("没有选择目标店铺")

    payload = None
    if store_ids:
        payload = build_upload_payload(directory, shop_name=store_ids[0])
        blockers.extend(str(item) for item in (payload.get("production_blockers") or []))

    plan = plan_publications(directory, store_ids, enabled_store_ids=enabled_store_ids) if store_ids else []
    already_submitted = [store for store in store_ids if store_has_task(directory, store)]
    if already_submitted:
        warnings.append("已拿到 task_id 的店铺（不会重复创建）：" + "、".join(already_submitted))

    qc = _read_json(directory / "output" / "image-qc-report.json")
    if qc.get("decision") == "revise":
        warnings.append("图片质检为 revise（语义维度需要视觉模型或人工确认）")

    publications = load_publications(directory)
    if not publications.get("stores"):
        checks.append({"name": "发布台账", "path": "output/store-publications.json", "status": "missing"})
    else:
        checks.append({"name": "发布台账", "path": "output/store-publications.json", "status": "ok"})

    submission: dict[str, Any] = {}
    for store, entry in (publications.get("stores") or {}).items():
        rows = [row for row in (entry.get("sku_publications") or []) if isinstance(row, Mapping)]
        statuses = [str(row.get("status") or "") for row in rows if row.get("status")]
        counts = entry.get("import_counts") or {}
        submission[str(store)] = {
            "status": str(entry.get("status") or "not_started"),
            "task_ids": sorted({str(row.get("task_id")) for row in rows if row.get("task_id")}),
            "imported": counts.get("imported", len([s for s in statuses if s == "imported"])),
            "failed": counts.get("failed", len([s for s in statuses if s in {"failed", "rejected", "not_created"}])),
            "pending": counts.get("pending", 0),
            "confirmed_at": entry.get("import_confirmed_at"),
        }
        if counts.get("pending"):
            warnings.append(f"{store}：{counts['pending']} 个 SKU 仍在 processing，稍后用 ozon_status 确认")
        if counts.get("failed"):
            warnings.append(f"{store}：{counts['failed']} 个 SKU 被 Ozon 拒绝，见 output/store-runs/{store}/import-info.json")

    next_action = "ozon_upload" if not blockers else _first_blocking_step(blockers)
    return {
        "product_id": product_id,
        "status": status.get("status"),
        "current_step": status.get("current_step"),
        # 界面要在"工序导轨"上画进度：已完成的工序必须给出来，否则只能显示当前一步
        "completed_steps": list(status.get("completed_steps") or []),
        "progress": status.get("progress"),
        "next_action": status.get("next_action"),
        "target_stores": store_ids,
        "store_plan": plan,
        "submission": submission,
        "requires_attention": bool(status.get("attention_required")),
        "ready_to_submit": not blockers,
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "checks": checks,
        "image_qc_decision": qc.get("decision"),
        "missing_attributes": ((_read_json(directory / "output" / "ozon-attributes-final.json").get("required_summary") or {}).get("missing")),
    }


def _first_blocking_step(blockers: Sequence[str]) -> str:
    text = " ".join(blockers)
    if "选词" in text or "关键词" in text:
        return "selection"
    if "标题" in text or "简介" in text or "文案" in text:
        return "russian_copy"
    if "图片" in text or "图位" in text:
        return "image_generation"
    if "属性" in text or "类目" in text:
        return "field_completion"
    if "售价" in text or "定价" in text:
        return "measurements"
    if "尺寸" in text or "重量" in text:
        return "measurements"
    if "店铺" in text:
        return "stores"
    return "unknown"


def _keywords_of_product(directory: Path) -> list[str]:
    """商品上真实记录的关键词（source.json 的 keywords / selected-keywords.json）。"""
    texts: list[str] = []
    for relative, key in (("input/source.json", "keywords"), ("input/selected-keywords.json", "keywords")):
        payload = _read_json(directory / relative)
        for item in payload.get(key) or []:
            text = str(item.get("keyword") if isinstance(item, Mapping) else item or "").strip()
            if text and text not in texts:
                texts.append(text)
    return texts


def product_keywords(directory: Path | str) -> list[str]:
    return _keywords_of_product(Path(directory))


def run_doctor(
    products_root: Path | str,
    *,
    product_ids: Sequence[str] | None = None,
    registry_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(products_root)
    environment = check_environment(registry_path=registry_path, env=env)
    enabled = environment["shops_enabled"]

    if product_ids is None:
        directories = sorted(
            path for path in root.iterdir() if path.is_dir() and path.name.startswith("P") and (path / "status.json").is_file()
        ) if root.is_dir() else []
    else:
        directories = [root / str(item) for item in product_ids]

    products = [diagnose_product(directory, enabled_store_ids=enabled) for directory in directories]
    ready = [item["product_id"] for item in products if item["ready_to_submit"]]
    blocked = [item["product_id"] for item in products if not item["ready_to_submit"]]

    # 关键词 → 商品（回答："这个词下有几个商品、走到哪一步了"）
    keyword_index: dict[str, list[dict[str, Any]]] = {}
    for item, directory in zip(products, directories):
        for text in _keywords_of_product(Path(directory)):
            keyword_index.setdefault(text, []).append(
                {
                    "product_id": item["product_id"],
                    "status": item.get("status"),
                    "ready_to_submit": item.get("ready_to_submit"),
                }
            )

    next_steps: list[str] = []
    if environment["blockers"]:
        next_steps.extend(environment["blockers"])
    if not products:
        next_steps.append("products/ 下还没有商品：先采集或 import_folder 导入")
    for item in products:
        if item["blockers"]:
            next_steps.append(f"{item['product_id']}：{item['blockers'][0]}")

    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now_iso(),
        "products_root": str(root),
        "environment": environment,
        "products": products,
        "keywords": keyword_index,
        "summary": {
            "products": len(products),
            "ready_to_submit": len(ready),
            "blocked": len(blocked),
            "ready_ids": ready,
            "blocked_ids": blocked,
            "keywords_with_products": len(keyword_index),
        },
        "next_steps": next_steps,
        "api_writes_performed": False,
    }


def render_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    env = report.get("environment") or {}
    lines.append(f"# 上线前预检 · {report.get('checked_at')}")
    lines.append("")
    lines.append("## 环境")
    lines.append(f"- 上游契约：{env.get('contracts_available')} 个")
    lines.append(f"- 店铺：共 {env.get('shops_total')} 家，启用 {len(env.get('shops_enabled') or [])} 家，凭据就绪 {len(env.get('shops_with_credentials') or [])} 家")
    for item in env.get("credential_details") or []:
        missing = item.get("missing_env") or []
        if missing:
            lines.append(f"  - {item.get('shop_id')}: 缺少环境变量 {', '.join(str(name) for name in missing)}")
        else:
            lines.append(f"  - {item.get('shop_id')}: 凭据就绪")
    for item in env.get("blockers") or []:
        lines.append(f"- ❌ {item}")
    lines.append("")

    summary = report.get("summary") or {}
    lines.append("## 商品")
    lines.append(f"共 {summary.get('products')} 个：可提交 {summary.get('ready_to_submit')}，待处理 {summary.get('blocked')}")
    lines.append("")
    lines.append("| 商品 | 状态 | 可提交 | 阻断项（首条） |")
    lines.append("|---|---|---|---|")
    for item in report.get("products") or []:
        first = (item.get("blockers") or ["—"])[0]
        lines.append(
            f"| {item.get('product_id')} | {item.get('status')} | {'✅' if item.get('ready_to_submit') else '❌'} | {first} |"
        )
    lines.append("")
    keywords = report.get("keywords") or {}
    if keywords:
        lines.append("## 关键词 → 商品")
        lines.append("")
        lines.append("| 关键词 | 商品数 | 商品（状态 / 可否提交） |")
        lines.append("|---|---|---|")
        for keyword, rows in sorted(keywords.items(), key=lambda item: -len(item[1]))[:30]:
            detail = "、".join(
                f"{row['product_id']}（{row.get('status') or '未知'}"
                f"{' / 可提交' if row.get('ready_to_submit') else ''}）"
                for row in rows
            )
            lines.append(f"| {keyword} | {len(rows)} | {detail} |")
        lines.append("")
    if report.get("next_steps"):
        lines.append("## 下一步")
        for step in report["next_steps"]:
            lines.append(f"- {step}")
        lines.append("")
    lines.append("（本预检不做任何 Ozon 调用）")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上线前预检：还差什么才能真正提交")
    parser.add_argument("--products-root", default="products")
    parser.add_argument("--product-id", action="append", dest="product_ids", help="只看某个商品，可重复")
    parser.add_argument("--registry", default=None, help="店铺注册表路径（默认 config/shops.json）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = run_doctor(
        args.products_root,
        product_ids=args.product_ids,
        registry_path=args.registry,
        env=os.environ,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 0 if report["summary"]["blocked"] == 0 and not report["environment"]["blockers"] else 1


if __name__ == "__main__":
    sys.exit(main())
