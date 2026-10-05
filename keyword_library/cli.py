"""命令行入口（不启服务也能入库 / 重算 / 查询）。

在 ``ozon-workbench`` 目录下运行：

    python -m keyword_library.cli ingest seerfar.json --source seerfar
    python -m keyword_library.cli rescore --lam 0.8 --min-heat 0.7
    python -m keyword_library.cli query --category-id 100 --type-id 200 --limit 20
    python -m keyword_library.cli stats
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import store
from .scoring import ScoreConfig


def _load_ingest_rows(path: Path, fallback_source: str) -> tuple[list[dict[str, Any]], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = fallback_source
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        source = str(payload.get("source") or fallback_source)
        category = payload.get("category") or {}
        for item in payload.get("keywords") or []:
            row = dict(item)
            row.setdefault("category_id", category.get("category_id"))
            row.setdefault("type_id", category.get("type_id"))
            row.setdefault("category_path_zh", category.get("category_path_zh"))
            rows.append(row)
    elif isinstance(payload, list):
        rows = [dict(item) for item in payload]
    else:
        raise SystemExit("输入 JSON 必须是对象或数组")
    return rows, source


def _score_config(args: argparse.Namespace) -> ScoreConfig:
    base = ScoreConfig()
    return ScoreConfig(
        lam=args.lam if args.lam is not None else base.lam,
        min_heat_percentile=(
            args.min_heat if args.min_heat is not None else base.min_heat_percentile
        ),
        max_competition_percentile=(
            args.max_competition if args.max_competition is not None else base.max_competition_percentile
        ),
        min_search_volume=(
            args.min_search_volume if args.min_search_volume is not None else base.min_search_volume
        ),
        max_competitor_count=(
            args.max_competitors if args.max_competitors is not None else base.max_competitor_count
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keyword_library", description="Ozon 关键词库")
    parser.add_argument("--root", default=str(store.DEFAULT_ROOT), help="关键词库目录")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="导入关键词 JSON 并打分")
    ingest.add_argument("file")
    ingest.add_argument("--source", default="seerfar")

    rescore = sub.add_parser("rescore", help="重算分数")
    rescore.add_argument("--lam", type=float)
    rescore.add_argument("--min-heat", type=float)
    rescore.add_argument("--max-competition", type=float)
    rescore.add_argument("--min-search-volume", type=float)
    rescore.add_argument("--max-competitors", type=float)
    rescore.add_argument("--category-id")
    rescore.add_argument("--type-id")

    query = sub.add_parser("query", help="查询关键词")
    query.add_argument("--category-id")
    query.add_argument("--type-id")
    query.add_argument("--status")
    query.add_argument("--min-score", type=float)
    query.add_argument("--text")
    query.add_argument("--order", default="score", choices=["score", "heat", "competition", "keyword"])
    query.add_argument("--limit", type=int, default=50)

    sub.add_parser("stats", help="按类目统计")
    sub.add_parser("index", help="重建 index.json")

    args = parser.parse_args(argv)
    root = Path(args.root)

    if args.command == "ingest":
        rows, source = _load_ingest_rows(Path(args.file), args.source)
        summary = store.upsert(root, rows, source=source)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "rescore":
        summary = store.rescore(
            root,
            _score_config(args),
            category_id=args.category_id,
            type_id=args.type_id,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "query":
        items = store.query(
            root,
            category_id=args.category_id,
            type_id=args.type_id,
            status=args.status,
            min_score=args.min_score,
            text=args.text,
            order=args.order,
            limit=args.limit,
        )
        for item in items:
            print(
                f"{item.get('score')}\t{item.get('status')}\t{item.get('keyword')}\t"
                f"vol={item.get('search_volume')}\tcomp={item.get('competitor_count')}"
            )
        print(f"# {len(items)} 条", file=sys.stderr)
        return 0

    if args.command == "stats":
        payload = store.stats(root)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    payload = store.index(root)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
