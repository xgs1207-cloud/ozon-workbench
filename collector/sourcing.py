"""选词 → 选品清单：把关键词库里的"高热度低竞争"词变成可执行动作。

每个词给出三件事：

1. **Ozon 市场复核链接**（用俄文关键词直接搜）：确认这个词下的真实竞争与价格带；
2. **1688 找货链接**（用**中文**词）：1688 上搜俄文词搜不到东西，所以中文词是必需的；
   中文词默认取 Seerfar 表里的中文类目名（``床单``），也可让模型翻译（``--translate``）；
3. **打分依据**：score / 热度分位 / 竞争分位 / 月搜热度 / 竞对数，便于人工判定优先级。

产物：``output/sourcing-plan.json``（机器读）+ ``output/sourcing-plan.md``（运营照着做）+ 可选 CSV。
**不发任何网络请求**：这里只生成链接与清单（市场数据由你去 Ozon 页面看，采集仍走采集器）。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote_plus

SCHEMA_VERSION = "1.0.0"
OZON_SEARCH = "https://www.ozon.ru/search/?text={query}"
ALIBABA_SEARCH = "https://s.1688.com/selloffer/offer_search.htm?keywords={query}"
PLAN_JSON = "output/sourcing-plan.json"
PLAN_MD = "output/sourcing-plan.md"
PLAN_CSV = "output/sourcing-plan.csv"

DEFAULT_STATUSES = ("qualified", "in_library")
PLAN_COLUMNS = (
    "keyword",
    "keyword_kind",
    "chinese_term",
    "chinese_term_source",
    "score",
    "heat_percentile",
    "competition_percentile",
    "search_volume",
    "competitor_count",
    "category_name_ru",
    "category_name_zh",
    "ozon_search_url",
    "alibaba_search_url",
)

#: 出现这些形态的关键词多半是**品牌词**（拉丁字母、含 ® / ™、或首字母大写的拉丁词）
LATIN_PATTERN = re.compile(r"[A-Za-z]{3,}")
BRAND_MARK_PATTERN = re.compile(r"[®™]|\b(?:brand|tm)\b", re.IGNORECASE)

#: 俄文品牌名（Cyrillic）不会被拉丁规则抓到 —— 真机实测踩到：`шуйские ситцы`、`озон хоум`
#: 都是品牌，却因为"没有拉丁字母"被当成品类词。用一份可维护的观察名单兜住这类。
BRAND_WATCHLIST_FILE = "brand-watchlist.txt"
BRAND_WATCHLIST_PATHS = (
    "config/brand-watchlist.txt",
    "deploy/brand-watchlist.example.txt",
)
#: 兜底清单（Cyrillic 品牌居多，供应商/市场自有品牌）。可用 config/brand-watchlist.txt 覆盖/追加。
DEFAULT_BRAND_WATCHLIST = (
    "шуйские ситцы",
    "трехгорная мануфактура",
    "трёхгорная мануфактура",
    "ившвейстандарт",
    "валетекс",
    "смоленские",
    "ozon home",
    "озон хоум",
    "clever",
    "sofi de marko",
    "homequeen",
    "тва",
    "монолит",
)


def load_brand_watchlist(root: Path | str | None = None) -> tuple[str, ...]:
    """读品牌观察名单：``config/brand-watchlist.txt``（一行一个）优先，缺省用内置兜底清单。"""
    base = Path(root) if root else Path.cwd()
    for relative in BRAND_WATCHLIST_PATHS:
        path = base / relative
        try:
            if path.is_file():
                entries = [
                    line.strip().casefold()
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                if entries:
                    return tuple(entries)
        except OSError:
            continue
    return DEFAULT_BRAND_WATCHLIST


def brand_in_text(text: str, watchlist: Sequence[str]) -> str | None:
    """文本里是否出现观察名单中的品牌（按词边界匹配，避免 "тва" 命中 "тварь" 这类误判）。"""
    lowered = str(text or "").casefold()
    for brand in watchlist:
        if not brand:
            continue
        if re.search(rf"(?<![a-zа-яё0-9]){re.escape(brand)}(?![a-zа-яё0-9])", lowered):
            return brand
    return None


def classify_keyword(keyword: str, watchlist: Sequence[str] | None = None) -> tuple[str, str | None]:
    """给关键词分类：``generic``（品类词）/ ``brand_or_latin``（疑似品牌词）。"""
    text = str(keyword or "")
    brands = tuple(watchlist) if watchlist is not None else DEFAULT_BRAND_WATCHLIST
    matched = brand_in_text(text, brands)
    if matched:
        return (
            "brand_or_latin",
            f"命中品牌观察名单「{matched}」：标题里不得使用他人品牌，请按**品类**找货并做无品牌包装",
        )
    if BRAND_MARK_PATTERN.search(text) or LATIN_PATTERN.search(text):
        has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", text))
        note = (
            "含拉丁字母（多为品牌名或型号）：1688 上请按**品类**找货，注意商标与侵权风险"
            if has_cyrillic
            else "整词为拉丁字母（多为品牌名）：1688 上请按**品类**找货，注意商标与侵权风险"
        )
        return "brand_or_latin", note
    return "generic", None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ozon_search_url(keyword: str) -> str:
    return OZON_SEARCH.format(query=quote_plus(str(keyword).strip()))


def alibaba_search_url(term: str) -> str:
    return ALIBABA_SEARCH.format(query=quote_plus(str(term).strip()))


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pick_keywords(
    library_root: Path | str,
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    top_n: int = 20,
    statuses: Sequence[str] = DEFAULT_STATUSES,
    min_score: float | None = None,
) -> list[dict[str, Any]]:
    """从关键词库选出候选词：默认只取达标/已入库，按分数降序。"""
    from keyword_library import store as keyword_store

    wanted = {str(item) for item in statuses}
    rows: list[dict[str, Any]] = []
    for record in keyword_store.load_all(library_root):
        if category_id is not None and str(record.get("category_id")) != str(category_id):
            continue
        if type_id is not None and str(record.get("type_id")) != str(type_id):
            continue
        if wanted and str(record.get("status")) not in wanted:
            continue
        score = _to_float(record.get("score"))
        if min_score is not None and (score is None or score < min_score):
            continue
        rows.append(record)
    rows.sort(key=lambda item: (_to_float(item.get("score")) is None, -(_to_float(item.get("score")) or 0.0)))
    return rows[: max(0, int(top_n))] if top_n else rows


def _chinese_terms_from_records(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """默认中文找货词：Seerfar 表里的中文类目名。"""
    terms: dict[str, str] = {}
    for record in records:
        extra = record.get("extra") if isinstance(record.get("extra"), Mapping) else {}
        candidate = str(extra.get("category_name_zh") or "").strip()
        if candidate:
            terms[str(record.get("keyword"))] = candidate
    return terms


def translate_terms(
    keywords: Sequence[str],
    *,
    provider: Any | None = None,
    context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """让模型给出中文找货词；返回 (关键词→中文词, 警告)。模型不可用时不猜。"""
    warnings: list[str] = []
    if provider is None:
        return {}, warnings
    translator = getattr(provider, "translate_terms", None)
    if not callable(translator):
        warnings.append(f"provider {getattr(provider, 'name', 'unknown')} 不支持翻译：用类目名兜底")
        return {}, warnings
    try:
        translated = translator(list(keywords), context=dict(context or {}))
    except Exception as error:  # noqa: BLE001 - 翻译失败不该阻断清单生成
        warnings.append(f"翻译失败（用类目名兜底）：{error}")
        return {}, warnings
    cleaned = {
        str(key): str(value).strip()
        for key, value in (translated or {}).items()
        if str(value or "").strip() and str(key) in {str(item) for item in keywords}
    }
    missing = [item for item in keywords if str(item) not in cleaned]
    if missing:
        warnings.append(f"{len(missing)} 个词没有翻译结果（用类目名兜底）")
    return cleaned, warnings


def build_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    chinese_terms: Mapping[str, str] | None = None,
    warnings: Sequence[str] = (),
    watchlist: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把关键词记录组装成选品清单（含两个链接）。"""
    brands = tuple(watchlist) if watchlist is not None else load_brand_watchlist()
    fallback = _chinese_terms_from_records(records)
    provided = {str(key): str(value) for key, value in (chinese_terms or {}).items()}
    rows: list[dict[str, Any]] = []
    for record in records:
        keyword = str(record.get("keyword") or "").strip()
        if not keyword:
            continue
        extra = record.get("extra") if isinstance(record.get("extra"), Mapping) else {}
        chinese = provided.get(keyword) or fallback.get(keyword) or ""
        source = "model" if keyword in provided else ("seerfar_category" if chinese else "missing")
        kind, kind_note = classify_keyword(keyword, brands)
        rows.append(
            {
                "keyword": keyword,
                "keyword_kind": kind,
                "keyword_note": kind_note,
                "chinese_term": chinese,
                "chinese_term_source": source,
                "score": _to_float(record.get("score")),
                "heat_percentile": _to_float(record.get("heat_percentile")),
                "competition_percentile": _to_float(record.get("competition_percentile")),
                "search_volume": _to_float(record.get("search_volume")),
                "competitor_count": _to_float(record.get("competitor_count")),
                "status": str(record.get("status") or ""),
                "category_id": str(record.get("category_id") or ""),
                "type_id": str(record.get("type_id") or ""),
                "category_name_ru": str(extra.get("category_name_ru") or ""),
                "category_name_zh": str(extra.get("category_name_zh") or ""),
                "ozon_search_url": ozon_search_url(keyword),
                "alibaba_search_url": alibaba_search_url(chinese) if chinese else None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "keywords": len(rows),
        "with_chinese_term": len([row for row in rows if row["chinese_term"]]),
        "brand_like": len([row for row in rows if row["keyword_kind"] == "brand_or_latin"]),
        "rows": rows,
        "warnings": list(warnings),
        "note": "本清单只生成链接与优先级；市场数据请到 Ozon 页面人工复核，采集仍走采集器",
    }


def render_markdown(plan: Mapping[str, Any], *, limit: int | None = None) -> str:
    rows = list(plan.get("rows") or [])
    if limit:
        rows = rows[:limit]
    lines = [
        f"# 选品清单 · {plan.get('generated_at')}",
        "",
        f"共 {plan.get('keywords')} 个候选词（其中 {plan.get('with_chinese_term')} 个有中文找货词，"
        f"{plan.get('brand_like', 0)} 个疑似品牌词）。",
        "",
        "| # | score | 热度分位 | 竞争分位 | 关键词 | 类型 | 中文找货词 | 月搜热度 | 竞对数 | Ozon 复核 | 1688 找货 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(rows, start=1):
        def number(value: Any, digits: int = 2) -> str:
            return "—" if value is None else f"{float(value):.{digits}f}"

        chinese = row.get("chinese_term") or "（缺，需人工补）"
        alibaba = (
            f"[搜 {chinese}]({row['alibaba_search_url']})" if row.get("alibaba_search_url") else "—"
        )
        kind = "品牌?" if row.get("keyword_kind") == "brand_or_latin" else "品类"
        lines.append(
            f"| {index} | {number(row.get('score'), 3)} | {number(row.get('heat_percentile'), 2)} | "
            f"{number(row.get('competition_percentile'), 2)} | {row.get('keyword')} | {kind} | {chinese} | "
            f"{number(row.get('search_volume'), 0)} | {number(row.get('competitor_count'), 0)} | "
            f"[搜]({row['ozon_search_url']}) | {alibaba} |"
        )
    brand_rows = [row for row in rows if row.get("keyword_kind") == "brand_or_latin"]
    if brand_rows:
        lines.extend(
            [
                "",
                "## ⚠️ 疑似品牌词（务必人工判断）",
                "",
                "这些词含拉丁字母，通常是品牌名或型号。**在 1688 按品类找货、不要照抄品牌**，"
                "否则可能侵权或被 Ozon 下架：",
                "",
                *[f"- `{row['keyword']}`（1688 建议按「{row.get('chinese_term') or '品类词'}」找）" for row in brand_rows[:10]],
            ]
        )
    if plan.get("warnings"):
        lines.extend(["", "## 提醒", *[f"- ⚠️ {item}" for item in plan["warnings"]]])
    lines.extend(
        [
            "",
            "## 怎么用",
            "",
            "1. 点 Ozon 复核链接，看这个词下的**首页竞品价格带与图片水平**（决定能不能打）；",
            "2. 点 1688 找货链接（中文词），确定供货价与起订量，把选中的商品用采集器入库；",
            "3. 采集入库时**绑定该词的 Ozon 真实类目**（category_id/type_id），关键词库与商品就对上了。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_plan(
    product_dir: Path | str | None,
    plan: Mapping[str, Any],
    *,
    limit: int | None = None,
    csv_path: Path | str | None = None,
) -> dict[str, str]:
    """写出 json/md（以及可选 csv）。

    始终写到 ``<base>/output/``：商品目录给商品目录，其它基目录就给 ``<base>/output/``，
    这样默认落在被 .gitignore 忽略的 ``output/`` 里，不会把业务数据提交进仓库。
    """
    base = Path(product_dir) if product_dir else Path(".")
    output = base / "output"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "sourcing-plan.json"
    md_path = output / "sourcing-plan.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(plan, limit=limit), encoding="utf-8")
    written = {"json": str(json_path), "markdown": str(md_path)}

    target_csv = Path(csv_path) if csv_path else None
    if target_csv:
        target_csv.parent.mkdir(parents=True, exist_ok=True)
        with target_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(PLAN_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for row in plan.get("rows") or []:
                writer.writerow(row)
        written["csv"] = str(target_csv)
    return written


def build_sourcing_plan(
    library_root: Path | str,
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    top_n: int = 20,
    statuses: Sequence[str] = DEFAULT_STATUSES,
    min_score: float | None = None,
    provider: Any | None = None,
    translate: bool = False,
) -> dict[str, Any]:
    # 品牌观察名单（俄文品牌名不会被拉丁规则抓到，真机踩过：шуйские ситцы / озон хоум）
    watchlist = load_brand_watchlist()
    records = pick_keywords(
        library_root,
        category_id=category_id,
        type_id=type_id,
        top_n=top_n,
        statuses=statuses,
        min_score=min_score,
    )
    warnings: list[str] = []
    if not records:
        warnings.append("没有符合条件的候选词：先导入 Seerfar 表并放宽门槛，或改 --statuses")
    chinese_terms: dict[str, str] = {}
    if translate and records:
        chinese_terms, translate_warnings = translate_terms(
            [str(item.get("keyword")) for item in records],
            provider=provider,
            context={"category": (records[0].get("extra") or {}).get("category_name_zh")},
        )
        warnings.extend(translate_warnings)
    plan = build_plan(records, chinese_terms=chinese_terms, warnings=warnings, watchlist=watchlist)
    plan["filters"] = {
        "category_id": category_id,
        "type_id": type_id,
        "top_n": top_n,
        "statuses": list(statuses),
        "min_score": min_score,
        "translated": bool(translate),
    }
    return plan


# --------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="关键词库 → 选品清单（Ozon 复核 + 1688 找货链接）")
    parser.add_argument("--library", default="keyword-library")
    parser.add_argument("--category-id", default=None)
    parser.add_argument("--type-id", default=None)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument(
        "--statuses",
        default="qualified,in_library",
        help="逗号分隔；空字符串表示不按状态过滤",
    )
    parser.add_argument("--translate", action="store_true", help="让模型给出中文找货词（否则用 Seerfar 中文类目名）")
    parser.add_argument("--provider", default=None, help="翻译用的模型层：fake / ark / http")
    parser.add_argument("--out-dir", default=None, help="清单输出目录（默认写到 --library 的同级 output/）")
    parser.add_argument("--csv", default=None, help="额外输出 CSV 路径")
    parser.add_argument("--md-limit", type=int, default=None, help="Markdown 里最多列几条（JSON 仍是全部）")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    args = parser.parse_args(argv)

    statuses = [item.strip() for item in str(args.statuses).split(",") if item.strip()]
    provider = None
    if args.translate:
        try:
            from models import load_provider

            provider = load_provider(args.provider)
        except Exception as error:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"模型层不可用：{error}"}, ensure_ascii=False, indent=2))
            return 1

    plan = build_sourcing_plan(
        args.library,
        category_id=args.category_id,
        type_id=args.type_id,
        top_n=args.top,
        statuses=statuses,
        min_score=args.min_score,
        provider=provider,
        translate=args.translate,
    )
    base = Path(args.out_dir) if args.out_dir else Path(args.library).parent
    written = write_plan(base, plan, limit=args.md_limit, csv_path=args.csv)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "keywords": plan["keywords"],
                    "with_chinese_term": plan["with_chinese_term"],
                    "written": written,
                    "warnings": plan["warnings"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_markdown(plan, limit=args.md_limit or 20))
        print(f"已写出：{written['json']}、{written['markdown']}" + (f"、{written['csv']}" if "csv" in written else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
