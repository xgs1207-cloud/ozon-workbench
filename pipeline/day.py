"""一天的工作：一条命令把「选词 → 选品 → 采集清单 → 跑商品 → 预检」串起来并给出报告。

设计取舍：

- **只做编排，不重造逻辑**：每一步都调已有模块（``collector.seerfar_xlsx`` / ``collector.sourcing`` /
  ``collector.collection_plan`` / ``pipeline.launch`` / ``pipeline.doctor``），失败点与门禁语义完全一致；
- **不做危险默认**：默认干跑（``--provider fake --image-generator placeholder``），
  真提交必须显式给 ``--uploader ozon-api --execute-upload --i-understand-this-hits-ozon``；
- **如实报告跳过**：没有 xlsx 就跳过导入（不会假装导过）、没有已采集商品就只出清单、
  缺凭据就把"需要人工/需要密钥"列出来，绝不吞掉阻断项；
- 报告里给出**下一步该敲的命令**，让"工作台"真的能被一步步用起来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .doctor import run_doctor

DEFAULT_LIBRARY = "keyword-library"
DEFAULT_PRODUCTS = "products"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def count_brand_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """疑似品牌词（拉丁字母/含 ®™/带品牌提示）——这些要让 1688 按**品类**找货。"""
    return [
        row
        for row in rows
        if str(row.get("keyword_kind") or row.get("kind") or row.get("type") or "") in {"brand_or_latin"}
        or "品牌" in str(row.get("keyword_note") or "")
    ]


def _step(name: str, status: str, **detail: Any) -> dict[str, Any]:
    return {"step": name, "status": status, **detail}


def run_day(
    *,
    xlsx: Path | str | None = None,
    library_root: Path | str = DEFAULT_LIBRARY,
    products_root: Path | str = DEFAULT_PRODUCTS,
    base: Path | str | None = None,
    stores: Sequence[str] = (),
    top: int = 5,
    collection_top: int = 10,
    bindings_path: Path | str | None = None,
    category_id: str | None = None,
    type_id: str | None = None,
    provider: Any | None = None,
    image_generator: Any | None = None,
    uploader: Any | None = None,
    publisher: Any | None = None,
    ozon_client: Any | None = None,
    execute_upload: bool = False,
    status: str = "COLLECTED",
    step_budget: int = 30,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """跑一遍日常流程，返回结构化报告（每一步的成败与下一步建议）。"""
    base_dir = Path(base) if base else Path(".")
    library = Path(library_root)
    products = Path(products_root)
    steps: list[dict[str, Any]] = []
    todos: list[str] = []

    # ① 关键词入库 + 打分
    if xlsx:
        from collector.seerfar_xlsx import import_xlsx

        try:
            summary = import_xlsx(
                xlsx,
                library,
                category_id=category_id,
                type_id=type_id,
                bindings_path=bindings_path,
            )
        except Exception as error:  # noqa: BLE001 - 表格格式问题很多种，如实记下
            steps.append(_step("import_keywords", "failed", error=str(error)))
            return _finish(steps, todos, base_dir, ok=False)
        steps.append(
            _step(
                "import_keywords",
                "ok",
                rows_parsed=summary.get("rows_parsed"),
                keywords=summary.get("keywords_imported"),
                created=summary.get("created"),
                updated=summary.get("updated"),
                bound=summary.get("bound_records"),
                categories=(summary.get("categories") or {}) if isinstance(summary.get("categories"), Mapping) else None,
                warnings=len(summary.get("warnings") or []),
            )
        )
    else:
        steps.append(_step("import_keywords", "skipped", reason="没给 --xlsx（用关键词库现有数据继续）"))

    # ② 选品清单
    from collector.sourcing import build_sourcing_plan, render_markdown, write_plan

    try:
        plan = build_sourcing_plan(library, top_n=top)
    except Exception as error:  # noqa: BLE001
        steps.append(_step("sourcing_plan", "failed", error=str(error)))
        return _finish(steps, todos, base_dir, ok=False)
    written = write_plan(base_dir, plan)
    render_markdown(plan)  # 渲染一次，确保报告里能用的字段都在
    rows = list(plan.get("rows") or plan.get("items") or [])
    brand_rows = count_brand_rows(rows)
    steps.append(
        _step(
            "sourcing_plan",
            "ok" if rows else "empty",
            candidates=len(rows),
            brand_warnings=len(brand_rows),
            files=written,
        )
    )
    if not rows:
        todos.append("关键词库里没有达标词：先导入 Seerfar 表或放宽打分门槛（POST /api/keywords/score）")
        return _finish(steps, todos, base_dir, ok=False)
    if brand_rows:
        todos.append(f"{len(brand_rows)} 个候选疑似品牌词：1688 按品类找货，标题里不要照抄品牌")

    # ③ 采集清单（哪些词还没采）
    from collector.collection_plan import build_collection_plan, render_collection_markdown, write_collection_plan

    collection = build_collection_plan(rows, products_root=products, top=collection_top)
    collection_files = write_collection_plan(base_dir, collection)
    tasks = list(collection.get("tasks") or [])
    pending = [task for task in tasks if task.get("status") != "collected"]
    collected = [task for task in tasks if task.get("status") == "collected"]
    steps.append(
        _step(
            "collection_plan",
            "ok",
            tasks=len(tasks),
            pending=len(pending),
            collected=len(collected),
            files=collection_files,
        )
    )

    # ④ 跑已采集的商品（默认干跑）
    if not collected:
        steps.append(_step("launch", "skipped", reason="还没有采集到商品：先按采集清单去 1688 采集"))
        todos.append("采集清单里都是待采集：用采集脚本抓 1688 后 push_capture 入库")
    elif not stores:
        steps.append(_step("launch", "skipped", reason="没给 --store"))
        todos.append("要跑商品请加 --store <店铺id>（可多店铺）")
    else:
        from .launch import launch_batch

        report = launch_batch(
            products,
            list(stores),
            batches_root=str(base_dir / "batches"),
            status=status,
            provider=provider,
            image_generator=image_generator,
            uploader=uploader,
            publisher=publisher,
            ozon_client=ozon_client,
            execute_upload=execute_upload,
            app_mode="production" if execute_upload else "development",
            step_budget=step_budget,
        )
        steps.append(
            _step(
                "launch",
                "ok" if report.get("ok") else "attention",
                batch_id=report.get("batch_id"),
                summary=report.get("summary"),
                dry_run=not execute_upload,
            )
        )
        for item in report.get("products") or []:
            if not item.get("ok"):
                todos.append(
                    f"{item.get('product_id')} 停在 {item.get('stopped_phase')}："
                    f"{item.get('reason') or '（看 run-report.json）'}"
                )

    # ⑤ 预检
    doctor = run_doctor(products, env=env)
    environment = doctor.get("environment") or {}
    summary = doctor.get("summary") or {}
    steps.append(
        _step(
            "doctor",
            "ok",
            contracts=environment.get("contracts_available"),
            shops_total=environment.get("shops_total"),
            shops_enabled=environment.get("shops_enabled"),
            shops_ready=environment.get("shops_with_credentials"),
            products=summary.get("products"),
            ready_to_submit=summary.get("ready_to_submit"),
        )
    )
    if not environment.get("shops_with_credentials"):
        todos.append("还没有可用凭据：填 /etc/ozon-workbench.env（或用 config/shops.json 指向的环境变量）")
    for product in doctor.get("products") or []:
        if product.get("blockers"):
            todos.append(f"{product.get('product_id')}：{product['blockers'][0]}")

    return _finish(steps, todos, base_dir, ok=all(item["status"] in {"ok", "skipped", "empty"} for item in steps))


def _finish(steps: Sequence[Mapping[str, Any]], todos: Sequence[str], base: Path, *, ok: bool) -> dict[str, Any]:
    pending = [item for item in todos if item]
    return {
        # 有需要人工处理的待办时就不算"顺利"——否则报告会给人"今天全好了"的错觉
        "ok": bool(ok) and not pending,
        "steps": list(steps),
        "todos": pending,
        "next_commands": _next_commands(steps, base),
    }


def _next_commands(steps: Sequence[Mapping[str, Any]], base: Path) -> list[str]:
    by_name = {str(item["step"]): item for item in steps}
    commands: list[str] = []
    collection = by_name.get("collection_plan") or {}
    if int(collection.get("pending") or 0) > 0:
        commands.append("# 采集清单里还有待采集的词 → 打开 output/collection-plan.md 里的 1688 搜索链接")
        commands.append("python -m collector.fetch_images --json capture-xxx.json --out D:\\capture\\p1")
        commands.append("python -m collector.push_capture --folder D:\\capture\\p1 --keyword \"...\" --category-id <id> --type-id <id>")
    launch = by_name.get("launch") or {}
    if launch.get("status") == "skipped":
        commands.append("python -m pipeline.day --products products --store <店铺id>   # 补上店铺再跑")
    if launch.get("status") == "attention":
        commands.append("python -m pipeline.doctor --products-root products   # 看每个商品卡在哪一步")
        commands.append("python -m pipeline.sku_selection --product-dir products/<P######> --list")
    if not commands:
        commands.append("python -m pipeline.launch --products-root products --store <店铺id> --provider ark --image-generator doubao --oss cos")
    return commands


def render_report(report: Mapping[str, Any]) -> str:
    lines = [f"# 今日流程报告 · {'顺利' if report.get('ok') else '有待办'}", ""]
    lines.append("| 步骤 | 结果 | 关键数字 |")
    lines.append("|---|---|---|")
    labels = {
        "import_keywords": "① 关键词入库打分",
        "sourcing_plan": "② 选品清单",
        "collection_plan": "③ 采集清单",
        "launch": "④ 跑商品",
        "doctor": "⑤ 上线前预检",
    }
    for item in report.get("steps") or []:
        detail = {
            key: value
            for key, value in item.items()
            if key not in {"step", "status"} and not isinstance(value, (dict, list))
        }
        nested = {
            key: value for key, value in item.items() if key not in {"step", "status"} and isinstance(value, (dict, list))
        }
        numbers = ", ".join(f"{key}={value}" for key, value in detail.items())
        if nested:
            numbers += ("；" if numbers else "") + json.dumps(nested, ensure_ascii=False)[:160]
        reason = item.get("reason") or item.get("error")
        lines.append(f"| {labels.get(str(item['step']), item['step'])} | {item['status']}{'（' + str(reason) + '）' if reason else ''} | {numbers} |")
    todos = report.get("todos") or []
    if todos:
        lines.append("")
        lines.append("## 需要人工处理")
        lines.extend(f"- {item}" for item in todos)
    commands = report.get("next_commands") or []
    if commands:
        lines.append("")
        lines.append("## 下一步命令")
        lines.append("```bash")
        lines.extend(commands)
        lines.append("```")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一条命令跑完一天：选词 → 选品 → 采集清单 → 跑商品 → 预检")
    parser.add_argument("--xlsx", default=None, help="Seerfar 导出表（可选；不给就用关键词库现有数据）")
    parser.add_argument("--library", default=DEFAULT_LIBRARY)
    parser.add_argument("--products", default=DEFAULT_PRODUCTS)
    parser.add_argument("--base", default=None, help="产物输出目录（默认当前目录，产物落在 <base>/output/）")
    parser.add_argument("--store", action="append", dest="stores", help="目标店铺（可重复）")
    parser.add_argument("--top", type=int, default=5, help="选品清单取前几个词")
    parser.add_argument("--collection-top", type=int, default=10)
    parser.add_argument("--status", default="COLLECTED", help="跑商品时筛选的状态")
    parser.add_argument("--bindings", default=None, help="config/category-bindings.json（把类目名换成真实 Ozon id）")
    parser.add_argument("--category-id", default=None)
    parser.add_argument("--type-id", default=None)
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--image-generator", default="placeholder")
    parser.add_argument("--uploader", default="dry-run")
    parser.add_argument("--oss", default="none", help="图片发布：cos / local / none")
    parser.add_argument("--oss-root", default=None)
    parser.add_argument("--oss-base-url", default=None)
    parser.add_argument("--ozon-fixture", default=None, help="离线演练：用录制夹具做 Ozon 只读调用")
    parser.add_argument("--execute-upload", action="store_true")
    parser.add_argument("--i-understand-this-hits-ozon", action="store_true")
    parser.add_argument("--step-budget", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from models import ModelError, load_image_generator, load_provider
    from .upload import DryRunUploader, SimulatedUploader

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

    uploader = None
    if str(args.uploader or "none").lower() not in {"none", "off"}:
        kind = str(args.uploader).lower()
        if kind in {"ozon", "ozon-api", "real"}:
            if not (args.execute_upload and args.i_understand_this_hits_ozon):
                raise SystemExit("拒绝使用真实提交器：需要 --execute-upload 与 --i-understand-this-hits-ozon")
            from .ozon_write import OzonWriteUploader

            uploader = OzonWriteUploader()
        else:
            uploader = SimulatedUploader() if kind in {"simulated", "sim"} else DryRunUploader()

    publisher = None
    if str(args.oss).lower() == "cos":
        from .oss_cos import CosError, _storage_from_env

        try:
            publisher = _storage_from_env()
        except CosError as error:
            print(json.dumps({"ok": False, "error": f"COS 不可用：{error}"}, ensure_ascii=False, indent=2))
            return 1
    elif str(args.oss).lower() == "local":
        if not (args.oss_root and args.oss_base_url):
            parser.error("--oss local 需要 --oss-root 与 --oss-base-url")
        from .oss_local import LocalObjectStorage

        publisher = LocalObjectStorage(args.oss_root, args.oss_base_url)

    ozon_client = None
    if args.ozon_fixture:
        from .ozon_http import FixtureTransport, OzonClient

        ozon_client = OzonClient(FixtureTransport(directory=Path(args.ozon_fixture)))

    report = run_day(
        xlsx=args.xlsx,
        library_root=args.library,
        products_root=args.products,
        base=args.base,
        stores=args.stores or [],
        top=args.top,
        collection_top=args.collection_top,
        bindings_path=args.bindings,
        category_id=args.category_id,
        type_id=args.type_id,
        provider=provider,
        image_generator=image_generator,
        uploader=uploader,
        publisher=publisher,
        ozon_client=ozon_client,
        execute_upload=bool(args.execute_upload),
        status=args.status,
        step_budget=args.step_budget,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_report(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
