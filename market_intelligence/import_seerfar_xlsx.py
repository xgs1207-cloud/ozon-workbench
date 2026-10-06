"""Import the user's exported Seerfar category and market reports as raw snapshots.

No paid Seerfar API call is made. Source columns and values are kept verbatim.
Use the report's actual statistical month, not the export date if they differ.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import ingest_snapshot


def rows(path: Path) -> list[dict[str, Any]]:
    try:
        import openpyxl
    except ImportError as error:
        raise RuntimeError("读取 Seerfar XLSX 需要 openpyxl") from error
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        iterator = sheet.values
        headers = [str(item or "").strip() for item in next(iterator)]
        if not headers or any(not item for item in headers) or len(set(headers)) != len(headers):
            raise ValueError(f"{path.name} 的表头为空或重复")
        return [dict(zip(headers, values)) for values in iterator if any(value is not None for value in values)]
    finally:
        workbook.close()


def import_report(db: Path, path: Path, *, dataset: str, period: str) -> dict[str, Any]:
    records = rows(path)
    if dataset == "categories":
        required = {"类目", "销售方式", "销售额", "销量", "竞对数", "竞品数", "退货取消率"}
    elif dataset == "keywords":
        required = {"关键词", "类目", "销售方式", "月搜热度", "竞对数", "竞品数", "转化率"}
    else:
        raise ValueError("仅支持类目表和市场关键词表")
    if not records or not required.issubset(records[0]):
        raise ValueError(f"{path.name} 缺少关键列：{sorted(required - set(records[0] if records else {}))}")
    results = []
    for start in range(0, len(records), 200):
        results.append(ingest_snapshot(
            db, source="seerfar", dataset=dataset, capture_method="file_import",
            period=period, page_url="", captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            records=records[start:start + 200],
        ))
    return {"dataset": dataset, "rows": len(records), "batches": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="将 Seerfar XLSX 导出表导入本地市场数据库，不调用付费 API")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--period", required=True, help="报表的统计月 YYYY-MM，不是导出日期")
    parser.add_argument("--categories", type=Path, required=True)
    parser.add_argument("--keywords", type=Path, required=True)
    args = parser.parse_args()
    if len(args.period) != 7 or args.period[4] != "-" or not args.period[:4].isdigit() or not args.period[5:].isdigit():
        parser.error("--period 须为 YYYY-MM")
    print(import_report(args.db, args.categories, dataset="categories", period=args.period))
    print(import_report(args.db, args.keywords, dataset="keywords", period=args.period))


if __name__ == "__main__":
    main()
