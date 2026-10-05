"""Seerfar 导出表（xlsx）→ 关键词库。

**列名按用户真实导出表核对过**（2026-10-05 导出，24 列）：

```
排名 关键词 类目 关键词相关产品 销售方式 平均价格 销量 销售额 月搜热度 月搜增长
商品数 竞对数 竞品数 加购人数 加购率 转化率 评论数 评分 市场空间 商品可见度
转化集中度 重量 体积 退货取消率
```

映射规则：

- ``关键词``：去掉换行后的中文释义括注（``простынь на резинке 160х200\\n(橡胶枕160x200)`` → 俄文关键词）；
- ``月搜热度`` → ``search_volume``（热度）；
- 竞争指标默认取 **``竞对数``**（直接竞争对手），可切到 ``商品数`` 或 ``竞品数``；
  三者的原始值都留在 ``extra`` 里，方便换口径重算；
- ``月搜增长/加购率/转化率/转化集中度/退货取消率`` 解析成百分数；``平均价格/销售额`` 去掉 ₽；
  ``重量``(g) / ``体积``(L) 拆成数值 + 单位；
- 其它列原样保留在 ``extra``，**不做有损转换**。

⚠️ 这份表里**没有 Ozon 的 category_id / type_id**，只有类目名称（中文 + 俄文）。
所以要么用 ``--category-id/--type-id`` 显式指定，要么按类目名生成 ``seerfar-<hash>`` 合成主键
（仅用于关键词库分组，真正上架时类目来自 Ozon Seller API，见 pipeline/category.py）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from parsing import parse_number

#: 规范字段 → 表头候选（真实表头在前，兼容常见变体）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "rank": ("排名", "Rank"),
    "keyword": ("关键词", "Keyword"),
    "category": ("类目", "Category"),
    "related_product": ("关键词相关产品", "相关产品"),
    "sales_mode": ("销售方式", "Sales Mode"),
    "avg_price": ("平均价格", "Average Price"),
    "sales_volume": ("销量", "Sales Volume"),
    "revenue": ("销售额", "Revenue"),
    "search_heat": ("月搜热度", "搜索热度", "Search Heat"),
    "search_growth": ("月搜增长", "搜索增长"),
    "product_count": ("商品数", "Products"),
    "competitor_count": ("竞对数", "Competitors"),
    "rival_count": ("竞品数", "Rivals"),
    "add_to_cart": ("加购人数",),
    "add_to_cart_rate": ("加购率",),
    "conversion_rate": ("转化率", "Conversion Rate"),
    "review_count": ("评论数", "Reviews"),
    "rating": ("评分", "Rating"),
    "market_space": ("市场空间",),
    "visibility": ("商品可见度",),
    "conversion_focus": ("转化集中度",),
    "weight": ("重量", "Weight"),
    "volume": ("体积", "Volume"),
    "return_rate": ("退货取消率", "退货率"),
}

REQUIRED_COLUMNS = ("keyword", "search_heat")
COMPETITION_FIELDS = ("竞对数", "商品数", "竞品数")

PERCENT_FIELDS = (
    "search_growth",
    "add_to_cart_rate",
    "conversion_rate",
    "conversion_focus",
    "return_rate",
)
MONEY_FIELDS = ("avg_price", "revenue")


class SeerfarError(RuntimeError):
    """表格结构或内容不可用。"""


# --------------------------------------------------------------------- 值解析


def clean_keyword(value: Any) -> str:
    """去掉换行与中文释义括注，保留俄文关键词本体。"""
    text = str(value or "").replace("\u00a0", " ")
    text = text.split("\n")[0] if "\n" in text else text
    text = text.strip()
    # 行内括注（中文释义）去掉：термос (保温杯)
    while True:
        start = text.rfind("(")
        end = text.rfind(")")
        if start == -1 or end < start:
            break
        inner = text[start + 1 : end]
        if any("\u4e00" <= char <= "\u9fff" for char in inner):
            text = (text[:start] + text[end + 1 :]).strip()
            continue
        break
    return " ".join(text.split()).strip()


def parse_percent(value: Any) -> float | None:
    """``"6.54%"`` → 6.54（百分点）；``0.0654`` 原样返回。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", "")
    if not text:
        return None
    text = text.rstrip("%").strip()
    number = parse_number(text)
    return number


def parse_measure(value: Any) -> tuple[float | None, str | None]:
    """``"782 g"`` → (782.0, "g")；``"4.97 L"`` → (4.97, "L")。"""
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip().replace("\u00a0", " ")
    if not text:
        return None, None
    parts = text.split()
    number = parse_number(parts[0]) if parts else None
    unit = parts[1] if len(parts) > 1 else None
    return number, unit


def split_category(value: Any) -> tuple[str | None, str | None]:
    """``"床单\\nПростыня"`` → ("床单", "Простыня")。"""
    if value is None:
        return None, None
    text = str(value).replace("\u00a0", " ").strip()
    if not text:
        return None, None
    pieces = [piece.strip() for piece in text.split("\n") if piece.strip()]
    zh = next((piece for piece in pieces if any("\u4e00" <= char <= "\u9fff" for char in piece)), None)
    ru = next((piece for piece in pieces if not any("\u4e00" <= char <= "\u9fff" for char in piece)), None)
    if zh is None and ru is None:
        return text, None
    return zh, ru


def synthetic_category_key(*names: Any) -> str:
    """按类目名生成稳定的合成主键（仅用于关键词库分组）。"""
    basis = "|".join(str(name or "").strip() for name in names if str(name or "").strip())
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]
    return f"seerfar-{digest}"


# --------------------------------------------------------------------- 读取


def _resolve_columns(header: Sequence[Any]) -> tuple[dict[str, int], list[str]]:
    """把表头映射到规范字段；返回 (字段→列号, 未识别的表头)。"""
    normalized = [str(item or "").strip() for item in header]
    mapping: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized.index(alias)
                break
    missing = [field for field in REQUIRED_COLUMNS if field not in mapping]
    if missing:
        raise SeerfarError(
            "表头缺少必需列："
            + ", ".join(COLUMN_ALIASES[field][0] for field in missing)
            + f"（实际表头：{normalized}）"
        )
    used = set(mapping.values())
    unmapped = [name for index, name in enumerate(normalized) if name and index not in used]
    return mapping, unmapped


def load_rows(path: Path | str, *, sheet: str | None = None, max_rows: int | None = None) -> tuple[list[list[Any]], list[str]]:
    try:
        import openpyxl
    except ImportError as error:  # pragma: no cover - 环境里已装
        raise SeerfarError("读取 xlsx 需要 openpyxl（pip install openpyxl）") from error

    workbook = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    try:
        name = sheet or workbook.sheetnames[0]
        if name not in workbook.sheetnames:
            raise SeerfarError(f"工作表 {name!r} 不存在（可选：{workbook.sheetnames}）")
        worksheet = workbook[name]
        rows: list[list[Any]] = []
        for index, row in enumerate(worksheet.iter_rows(values_only=True)):
            if max_rows is not None and index > max_rows:
                break
            rows.append(list(row))
        return rows, list(workbook.sheetnames)
    finally:
        workbook.close()


def parse_row(
    row: Sequence[Any],
    mapping: Mapping[str, int],
    *,
    competition_field: str = "竞对数",
) -> dict[str, Any]:
    """一行 → 关键词库记录片段（不含 category_id/type_id）。"""

    def cell(field: str) -> Any:
        index = mapping.get(field)
        if index is None or index >= len(row):
            return None
        return row[index]

    keyword = clean_keyword(cell("keyword"))
    if not keyword:
        raise SeerfarError("行里没有可用关键词")

    category_zh, category_ru = split_category(cell("category"))
    competition_map = {
        "竞对数": cell("competitor_count"),
        "商品数": cell("product_count"),
        "竞品数": cell("rival_count"),
    }
    weight_value, weight_unit = parse_measure(cell("weight"))
    volume_value, volume_unit = parse_measure(cell("volume"))

    extra: dict[str, Any] = {
        "rank": parse_number(cell("rank")),
        "category_name_zh": category_zh,
        "category_name_ru": category_ru,
        "related_product_image": cell("related_product"),
        "sales_mode": cell("sales_mode"),
        "sales_volume": parse_number(cell("sales_volume")),
        "revenue_rub": parse_number(str(cell("revenue") or "").replace("₽", "")),
        "product_count": parse_number(cell("product_count")),
        "competitor_count_raw": parse_number(cell("competitor_count")),
        "rival_count": parse_number(cell("rival_count")),
        "add_to_cart": parse_number(cell("add_to_cart")),
        "add_to_cart_rate_percent": parse_percent(cell("add_to_cart_rate")),
        "conversion_rate_percent": parse_percent(cell("conversion_rate")),
        "review_count": parse_number(cell("review_count")),
        "rating": parse_number(cell("rating")),
        "market_space": parse_number(cell("market_space")),
        "visibility": parse_number(cell("visibility")),
        "conversion_focus_percent": parse_percent(cell("conversion_focus")),
        "return_rate_percent": parse_percent(cell("return_rate")),
        "weight_g": weight_value,
        "weight_unit": weight_unit,
        "volume_l": volume_value,
        "volume_unit": volume_unit,
        "search_growth_percent": parse_percent(cell("search_growth")),
        "search_heat": parse_number(cell("search_heat")),
        "source_table": "seerfar",
    }
    extra = {key: value for key, value in extra.items() if value not in (None, "")}

    return {
        "keyword": keyword,
        "search_volume": parse_number(cell("search_heat")),
        "competitor_count": parse_number(competition_map.get(competition_field)),
        "trend": parse_percent(cell("search_growth")),
        "extra": {**extra, "competition_field": competition_field},
    }


def read_seerfar_xlsx(
    path: Path | str,
    *,
    sheet: str | None = None,
    competition_field: str = "竞对数",
    max_rows: int | None = None,
) -> dict[str, Any]:
    """读整张表 → 记录列表 + 元信息（列映射、未识别列、跳过行）。"""
    if competition_field not in COMPETITION_FIELDS:
        raise SeerfarError(f"未知的竞争口径 {competition_field!r}（可选：{', '.join(COMPETITION_FIELDS)}）")
    rows, sheets = load_rows(path, sheet=sheet, max_rows=max_rows)
    if not rows:
        raise SeerfarError("表格是空的")
    mapping, unmapped = _resolve_columns(rows[0])

    records: list[dict[str, Any]] = []
    skipped: list[str] = []
    for index, row in enumerate(rows[1:], start=2):
        if not any(value not in (None, "") for value in row):
            continue
        try:
            records.append(parse_row(row, mapping, competition_field=competition_field))
        except SeerfarError as error:
            skipped.append(f"第 {index} 行：{error}")
    if not records:
        raise SeerfarError("没有解析出任何关键词行（检查表头是否被改动）")
    return {
        "records": records,
        "sheets": sheets,
        "columns": {field: index for field, index in sorted(mapping.items(), key=lambda item: item[1])},
        "unmapped_columns": unmapped,
        "skipped_rows": skipped,
        "competition_field": competition_field,
        "source_path": str(path),
    }


# --------------------------------------------------------------------- 入库


def to_ingest_payload(
    records: Iterable[Mapping[str, Any]],
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    source: str = "seerfar",
) -> dict[str, Any]:
    """整理成 ``keyword_store.upsert`` 需要的载荷；按类目名分组时用合成主键。"""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        extra = record.get("extra") if isinstance(record.get("extra"), Mapping) else {}
        resolved_category = str(category_id or "").strip()
        resolved_type = str(type_id or "").strip()
        if not resolved_category or not resolved_type:
            synthetic = synthetic_category_key(extra.get("category_name_ru"), extra.get("category_name_zh"))
            resolved_category = resolved_category or synthetic
            resolved_type = resolved_type or synthetic
        entry = {
            "keyword": record.get("keyword"),
            "category_id": resolved_category,
            "type_id": resolved_type,
            "search_volume": record.get("search_volume"),
            "competitor_count": record.get("competitor_count"),
            "trend": record.get("trend"),
            "extra": dict(extra or {}),
        }
        grouped.setdefault((resolved_category, resolved_type), []).append(entry)

    # 逐类目返回一个载荷（同一把钥匙只出现一次，避免覆盖）
    first_key = next(iter(grouped))
    return {
        "source": source,
        "category": {"category_id": first_key[0], "type_id": first_key[1]},
        "keywords": grouped[first_key],
        "_grouped": {f"{key[0]}:{key[1]}": value for key, value in grouped.items()},
    }


def import_xlsx(
    path: Path | str,
    library_root: Path | str,
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    sheet: str | None = None,
    competition_field: str = "竞对数",
    max_rows: int | None = None,
    rescore: bool = True,
) -> dict[str, Any]:
    """读表 → 入库（按类目分组）→ 重算分数 → 返回摘要。"""
    from keyword_library import store as keyword_store

    parsed = read_seerfar_xlsx(
        path, sheet=sheet, competition_field=competition_field, max_rows=max_rows
    )
    payload = to_ingest_payload(parsed["records"], category_id=category_id, type_id=type_id)
    grouped = payload.pop("_grouped")

    imported = 0
    created = 0
    updated = 0
    categories: dict[str, int] = {}
    for key, entries in grouped.items():
        result = keyword_store.upsert(library_root, entries, source=payload["source"])
        imported += len(entries)
        created += int(result.get("created") or 0)
        updated += int(result.get("updated") or 0)
        categories[key] = len(entries)

    rescored: dict[str, Any] = {}
    if rescore:
        for key in categories:
            catalog_category, _, catalog_type = key.partition(":")
            result = keyword_store.rescore(
                library_root, category_id=catalog_category, type_id=catalog_type
            )
            # upsert 内部已经重算过一轮，所以这里 promoted 常常是 0；直接数当前状态更诚实
            qualified = len(
                keyword_store.query(
                    library_root,
                    category_id=catalog_category,
                    type_id=catalog_type,
                    status=keyword_store.STATUS_QUALIFIED,
                )
            )
            rescored[key] = {**result, "qualified": qualified}

    warnings: list[str] = []
    if not (category_id and type_id):
        warnings.append(
            "未提供 --category-id/--type-id：按类目名生成了合成主键（seerfar-<hash>）。"
            "关键词库只用于选词；真正上架的类目来自 Ozon Seller API。"
        )
    if parsed["skipped_rows"]:
        warnings.append(f"跳过 {len(parsed['skipped_rows'])} 行（首行：" + parsed["skipped_rows"][0] + "）")
    if parsed["unmapped_columns"]:
        warnings.append("未识别的列（原样保留在 extra 之外）：" + ", ".join(parsed["unmapped_columns"]))

    return {
        "ok": True,
        "source_path": parsed["source_path"],
        "rows_parsed": len(parsed["records"]),
        "keywords_imported": imported,
        "created": created,
        "updated": updated,
        "categories": categories,
        "competition_field": parsed["competition_field"],
        "columns": parsed["columns"],
        "unmapped_columns": parsed["unmapped_columns"],
        "rescored": {
            key: {
                "scored": value.get("scored"),
                "qualified": value.get("qualified"),
                "promoted": value.get("promoted"),
                "demoted": value.get("demoted"),
            }
            for key, value in rescored.items()
        },
        "warnings": warnings,
    }


# --------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seerfar 导出表 → 关键词库")
    parser.add_argument("--xlsx", required=True, help="Seerfar 导出的 .xlsx 路径")
    parser.add_argument("--library", default="keyword-library", help="关键词库目录")
    parser.add_argument("--category-id", default=None, help="Ozon category_id（类目名无法直接对上时建议显式给）")
    parser.add_argument("--type-id", default=None)
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--competition-field", default="竞对数", choices=COMPETITION_FIELDS)
    parser.add_argument("--max-rows", type=int, default=None, help="只读前 N 行（试跑用）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = import_xlsx(
            args.xlsx,
            args.library,
            category_id=args.category_id,
            type_id=args.type_id,
            sheet=args.sheet,
            competition_field=args.competition_field,
            max_rows=args.max_rows,
        )
    except (SeerfarError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"导入 {summary['keywords_imported']} 条关键词（解析 {summary['rows_parsed']} 行）")
        print(f"竞争口径：{summary['competition_field']}；类目分组：{len(summary['categories'])} 个")
        for key, count in summary["categories"].items():
            scored = summary["rescored"].get(key, {})
            print(
                f"  {key}: {count} 条（已评分 {scored.get('scored')}，"
                f"**高热度低竞争达标 {scored.get('qualified')} 条**）"
            )
        print("提示：达标 = 热度分位 ≥ 0.6 且竞争分位 ≤ 0.6（可用 POST /api/keywords/score 调 λ 与门槛）")
        for item in summary["warnings"]:
            print(f"⚠️ {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
