"""采集清单：把选品清单里选中的关键词变成"待采集任务"，并跟踪每个词是否已经采集。

与上游的关系：这是**我们自己加的桥**（原项目从 1688 页面直接采集，没有"关键词→采集任务"这一环）。
它解决的问题是：从 Seerfar 选出 20 个词之后，逐个去 1688 找货时不知道该采哪个词、也不记得哪个词已经采过。

产物：

- ``output/collection-plan.json``：每个词的 {关键词, 中文找货词, 目标 Ozon 类目, 两个搜索入口, 状态, 已采集商品}
- ``output/collection-plan.md``：运营照着做的勾选清单

状态判定只看**商品目录里真实存在的关键词**（``input/source.json`` 的 ``keywords``
或 ``input/selected-keywords.json``），**不靠人工打勾**，所以不会出现"以为采过其实没有"。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"
PLAN_JSON = "output/collection-plan.json"
PLAN_MD = "output/collection-plan.md"

STATUS_PENDING = "pending"
STATUS_COLLECTED = "collected"
STATUS_SKIPPED = "skipped"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _product_keywords(product_dir: Path) -> list[str]:
    """商品上真实记录的关键词（source.json 的 keywords / selected-keywords.json）。

    两个文件都可能记录同一个词（采集时同时写入），这里按 casefold 去重，
    否则"这个词下有几个商品"会重复计数。
    """
    texts: list[str] = []
    seen: set[str] = set()
    for relative in ("input/source.json", "input/selected-keywords.json"):
        payload = _read_json(product_dir / relative)
        for item in payload.get("keywords") or []:
            text = str(item.get("keyword") if isinstance(item, Mapping) else item or "").strip()
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                texts.append(text)
    return texts


def scan_products(products_root: Path | str | None) -> dict[str, list[dict[str, Any]]]:
    """扫商品目录：关键词（casefold）→ [{product_id, status, current_step}]。"""
    root = Path(products_root) if products_root else None
    if not root or not root.is_dir():
        return {}
    index: dict[str, list[dict[str, Any]]] = {}
    for product_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        status = _read_json(product_dir / "status.json")
        entry = {
            "product_id": product_dir.name,
            "status": status.get("status"),
            "current_step": status.get("current_step"),
        }
        for text in _product_keywords(product_dir):
            index.setdefault(text.casefold(), []).append(entry)
    return index


def build_collection_plan(
    rows: Sequence[Mapping[str, Any]],
    *,
    products_root: Path | str | None = None,
    top: int | None = None,
    only: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把选品清单的行整理成采集清单（带状态与已采集商品）。"""
    index = scan_products(products_root)
    wanted = {str(item).strip().casefold() for item in (only or []) if str(item).strip()}

    tasks: list[dict[str, Any]] = []
    for row in rows:
        keyword = str(row.get("keyword") or "").strip()
        if not keyword:
            continue
        if wanted and keyword.casefold() not in wanted:
            continue
        products = index.get(keyword.casefold(), [])
        tasks.append(
            {
                "keyword": keyword,
                "keyword_kind": row.get("keyword_kind") or "generic",
                "chinese_term": row.get("chinese_term") or "",
                "category_id": row.get("category_id") or "",
                "type_id": row.get("type_id") or "",
                "category_name_ru": row.get("category_name_ru") or "",
                "score": row.get("score"),
                "search_volume": row.get("search_volume"),
                "competitor_count": row.get("competitor_count"),
                "ozon_search_url": row.get("ozon_search_url"),
                "alibaba_search_url": row.get("alibaba_search_url"),
                "status": STATUS_COLLECTED if products else STATUS_PENDING,
                "products": products,
            }
        )
    if top:
        tasks = tasks[: max(0, int(top))]
    pending = [item for item in tasks if item["status"] == STATUS_PENDING]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "products_root": str(products_root) if products_root else None,
        "total": len(tasks),
        "pending": len(pending),
        "collected": len([item for item in tasks if item["status"] == STATUS_COLLECTED]),
        "tasks": tasks,
        "note": "状态来自商品目录里真实记录的关键词；采集入库时带上关键词即自动标记为已采集",
    }


def render_collection_markdown(plan: Mapping[str, Any]) -> str:
    tasks = list(plan.get("tasks") or [])
    lines = [
        f"# 采集清单 · {plan.get('generated_at')}",
        "",
        f"共 {plan.get('total')} 个词：待采集 {plan.get('pending')} 个，已采集 {plan.get('collected')} 个。",
        "",
        "| # | 状态 | score | 关键词 | 类型 | 中文找货词 | 目标类目 | 1688 找货 | 已采集商品 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for index, task in enumerate(tasks, start=1):
        def number(value: Any, digits: int = 3) -> str:
            return "—" if value is None else f"{float(value):.{digits}f}"

        status = "✅ 已采集" if task["status"] == STATUS_COLLECTED else "⬜ 待采集"
        category = (
            f"{task.get('category_name_ru') or '—'} ({task.get('category_id')}/{task.get('type_id')})"
            if task.get("category_id")
            else "**未绑定**"
        )
        shop = (
            f"[搜 {task['chinese_term']}]({task['alibaba_search_url']})"
            if task.get("alibaba_search_url")
            else "—"
        )
        products = ", ".join(
            f"{item['product_id']}({item.get('status')})" for item in (task.get("products") or [])
        ) or "—"
        lines.append(
            f"| {index} | {status} | {number(task.get('score'))} | {task.get('keyword')} | "
            f"{'品牌?' if task.get('keyword_kind') == 'brand_or_latin' else '品类'} | "
            f"{task.get('chinese_term') or '—'} | {category} | {shop} | {products} |"
        )
    lines.extend(
        [
            "",
            "## 采集时怎么带上关键词",
            "",
            "采集入库时把关键词一起写进载荷（采集器/导入都支持），商品就会自动：",
            "",
            "1. 在 `input/source.json` 里留下 `keywords` 与 `keyword_source`；",
            "2. 直接写好 `input/selected-keywords.json`，**可以跳过手工选词**去跑文案；",
            "3. 若没给类目但给了 `keyword_category`，类目也会跟着带上（不猜，只是沿用绑定值）。",
            "",
            "```powershell",
            "python -m collector.ingest --folder D:\\capture\\p1 --keyword \"простынь на резинке 160х200\"",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def write_collection_plan(base: Path | str | None, plan: Mapping[str, Any]) -> dict[str, str]:
    directory = Path(base) if base else Path(".")
    output = directory / "output"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "collection-plan.json"
    md_path = output / "collection-plan.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_collection_markdown(plan), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def mark_keyword(product_dir: Path | str, keyword: str, *, source: str = "manual_mark") -> dict[str, Any]:
    """把关键词补记到已有商品上（此前采集时没带关键词的情况）。"""
    from pipeline.selection import set_selected_keywords

    directory = Path(product_dir)
    if not directory.is_dir():
        raise ValueError(f"商品目录不存在：{directory}")
    text = str(keyword or "").strip()
    if len(text) < 2:
        raise ValueError("关键词太短")
    result = set_selected_keywords(directory, [text], source=source, note="采集清单回填")
    source_path = directory / "input" / "source.json"
    source_doc = _read_json(source_path)
    if source_doc:
        existing = [
            str(item.get("keyword") if isinstance(item, Mapping) else item or "").strip()
            for item in (source_doc.get("keywords") or [])
        ]
        if text not in existing:
            source_doc.setdefault("keywords", []).append({"keyword": text})
            source_doc.setdefault("keyword_source", source)
            source_path.write_text(
                json.dumps(source_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    return {"ok": True, "product_id": directory.name, "keyword": text, "selection": result}


# --------------------------------------------------------------------- CLI


def _load_sourcing_rows(source_plan: Path | str | None, library: str | None) -> list[dict[str, Any]]:
    if source_plan:
        payload = _read_json(Path(source_plan))
        rows = payload.get("rows")
        if not rows:
            raise ValueError(f"选品清单里没有 rows：{source_plan}（先跑 collector.sourcing）")
        return [dict(item) for item in rows]
    if library:
        from collector.sourcing import build_sourcing_plan

        return list(build_sourcing_plan(library, top_n=0).get("rows") or [])
    raise ValueError("需要 --plan（选品清单）或 --library（直接从关键词库重建）")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="选品清单 → 采集清单（带状态跟踪）")
    parser.add_argument("--plan", default=None, help="output/sourcing-plan.json")
    parser.add_argument("--library", default=None, help="或直接从关键词库重建（keyword-library）")
    parser.add_argument("--products", default="products", help="商品目录（用于判断哪些词已采集）")
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--only", action="append", dest="only", help="只看这些关键词（可重复）")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--mark", default=None, help="把关键词补记到商品：--mark products/P000001")
    parser.add_argument("--keyword", default=None, help="配合 --mark 使用的关键词")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.mark:
        if not args.keyword:
            parser.error("--mark 需要同时给 --keyword")
        try:
            result = mark_keyword(args.mark, args.keyword)
        except (OSError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    try:
        rows = _load_sourcing_rows(args.plan, args.library)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    plan = build_collection_plan(rows, products_root=args.products, top=args.top, only=args.only)
    written = write_collection_plan(args.out_dir, plan)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "total": plan["total"],
                    "pending": plan["pending"],
                    "collected": plan["collected"],
                    "written": written,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_collection_markdown(plan))
        print(f"已写出：{written['json']}、{written['markdown']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
