"""把 Seerfar（或任何来源）导出的关键词 CSV 导入关键词库。

默认直接写库（不需要起服务）；加 ``--api`` 则改为 POST 到本地服务。

    python collector/import_csv.py seerfar.csv --category-id 1001 --type-id 2001 ^
        --category-path-zh "家居/厨房" 
    python collector/import_csv.py seerfar.csv --category-id 1001 --type-id 2001 --api

列名通过别名自动识别（不区分大小写、支持包含匹配），识别不到就跳过该字段而不是猜。
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
import urllib.request
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from keyword_library import store  # noqa: E402
from parsing import parse_number  # noqa: E402

COLUMN_ALIASES: dict[str, list[str]] = {
    "keyword": ["keyword", "关键词", "ключев", "запрос", "phrase", "фраза", "词"],
    "search_volume": ["search volume", "searches", "搜索量", "показы", "частота", "volume", "热度"],
    "competitor_count": ["competition", "competitors", "竞争", "конкурент", "products", "竞品"],
    "ads_count": ["ads", "广告", "реклам"],
    "cpc": ["cpc", "bid", "点击", "ставка"],
    "trend": ["trend", "增长", "динамик"],
    "category_path_zh": ["category", "类目", "分类"],
}


def detect_columns(header: list[str]) -> dict[str, int]:
    lowered = [str(item or "").strip().lower() for item in header]
    mapping: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for index, name in enumerate(lowered):
            if name and any(alias.lower() in name for alias in aliases):
                mapping[field] = index
                break
    return mapping


def to_number(value: Any) -> float | None:
    """数字解析统一走 :func:`parsing.parse_number`（见那里的歧义规则）。"""
    return parse_number(value)


def load_rows(path: pathlib.Path) -> tuple[list[dict[str, Any]], list[str]]:
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".txt"} else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            return [], []
        columns = detect_columns(header)
        if "keyword" not in columns:
            raise SystemExit(
                "识别不到关键词列。请把表头改成 keyword/关键词 之一，或用 --keyword-column 指定列号。"
            )
        rows: list[dict[str, Any]] = []
        for raw in reader:
            keyword = (raw[columns["keyword"]] if columns["keyword"] < len(raw) else "").strip()
            if not keyword:
                continue
            row: dict[str, Any] = {"keyword": keyword}
            for field in ("search_volume", "competitor_count", "ads_count", "cpc", "trend"):
                index = columns.get(field)
                row[field] = to_number(raw[index]) if index is not None and index < len(raw) else None
            path_index = columns.get("category_path_zh")
            if path_index is not None and path_index < len(raw):
                row["category_path_zh"] = raw[path_index].strip() or None
            row["extra"] = {"source_file": path.name}
            rows.append(row)
    return rows, header


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CSV → 关键词库")
    parser.add_argument("csv_file")
    parser.add_argument("--category-id", required=True, help="Ozon category_id")
    parser.add_argument("--type-id", required=True, help="Ozon type_id")
    parser.add_argument("--category-path-zh", default=None)
    parser.add_argument("--source", default="seerfar-csv")
    parser.add_argument("--root", default=str(store.DEFAULT_ROOT))
    parser.add_argument("--api", action="store_true", help="改为 POST 到本地服务而不是直接写库")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8766/api/keywords/ingest")
    args = parser.parse_args(argv)

    path = pathlib.Path(args.csv_file)
    if not path.is_file():
        raise SystemExit(f"文件不存在：{path}")
    rows, header = load_rows(path)
    if not rows:
        raise SystemExit("没有解析到任何关键词行")
    for row in rows:
        row["category_id"] = str(args.category_id)
        row["type_id"] = str(args.type_id)
        row.setdefault("category_path_zh", args.category_path_zh)

    if args.api:
        payload = {
            "source": args.source,
            "category": {
                "category_id": str(args.category_id),
                "type_id": str(args.type_id),
                "category_path_zh": args.category_path_zh,
            },
            "keywords": rows,
        }
        request = urllib.request.Request(
            args.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            print(response.read().decode("utf-8"))
        return 0

    summary = store.upsert(args.root, rows, source=args.source)
    print(json.dumps({"header": header, **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
