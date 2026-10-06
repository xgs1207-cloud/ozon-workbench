"""广告真实词：Ozon Performance API 的 ``SEARCH_PHRASES`` 报表。

**这个模块解决什么问题**

第三方工具（Seerfar/Mpstats）给的关键词热度是**估算**；而广告报表里的搜索词组，是买家
在 Ozon 前台真实输入、并且真金白银点过的**一手词**。本模块把这些词拉回来：

1. **高转化出单词** → 写进标题/简介，吃免费自然流量，逐步降低广告预算；
2. **只点不买的词** → 识别为否定词（negative），停止为无效点击付费；
3. **没见过的别称/长尾/场景词** → 沉淀进关键词库，作为扩词与扩品方向；
4. 词里的尺寸/用途/材质修饰 → 看出买家最在意什么，反哺选品与变体。

**鉴权与流程（Performance API 2.0，主机 ``api-performance.ozon.ru``）**

1. ``POST /api/client/token``：client_id + client_secret + grant_type=client_credentials
   → Bearer access_token（约 30 分钟，过期重取）；
2. 建报表 ``POST /api/client/statistics/search-phrases/json``：campaigns + dateFrom/dateTo
   → ``{UUID}``；
3. 轮询 ``GET /api/client/statistics/{UUID}``：state ∈ NOT_STARTED/IN_PROGRESS/ERROR/OK，
   OK 后给相对 ``link``（CSV）；
4. 带 Bearer 下载 link → 解析 CSV。

⚠️ Performance API 需要**独立密钥**（广告后台「设置 → API-ключи」，client_id 形如
``xxx@advertising.performance.ozon.ru``），与 Seller API 的 Client-Id/Api-Key 不同。
报表创建路径以官方文档为准，可用环境变量 ``OZON_PERF_REPORT_PATH`` 覆盖。

设计：传输层可注入（离线测试零网络）；CSV 表头做别名兼容（俄/英），未知列保留进 extra。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

SCHEMA_VERSION = "1.0.0"

PERF_BASE_URL = "https://api-performance.ozon.ru"
PATH_TOKEN = "/api/client/token"
# 报表创建路径（若官方调整，用 OZON_PERF_REPORT_PATH 覆盖）
DEFAULT_REPORT_PATH = "/api/client/statistics/search-phrases/json"
PATH_STATUS = "/api/client/statistics/{uuid}"

# 报表限制：一次最多 10 个 campaign、最长 62 天、同时只能有 1 个报表
MAX_CAMPAIGNS = 10
MAX_RANGE_DAYS = 62

# 判定否定词的点击门槛：点击 ≥ 该值且 0 单，视为无效流量
NEGATIVE_CLICK_THRESHOLD = 5


class SearchPhrasesError(RuntimeError):
    pass


# ------------------------------------------------------------ 传输协议


class PerformanceTransport(Protocol):
    def token(self, client_id: str, client_secret: str) -> dict[str, Any]: ...
    def post_json(self, path: str, body: Mapping[str, Any], bearer: str) -> dict[str, Any]: ...
    def get_json(self, path: str, bearer: str) -> dict[str, Any]: ...
    def get_text(self, path: str, bearer: str) -> str: ...


# ---------------------------------------------------------------- 时间


def _date(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=int(days_ago))).strftime("%Y-%m-%d")


def default_dates(days: int = 14) -> tuple[str, str]:
    days = min(int(days), MAX_RANGE_DAYS)
    return _date(days), _date(1)


# ---------------------------------------------------------------- 鉴权


def obtain_token(
    transport: PerformanceTransport, *, client_id: str, client_secret: str
) -> str:
    if not client_id or not client_secret:
        raise SearchPhrasesError(
            "缺少 Performance API 凭据：在广告后台「设置→API-ключи」创建 client_id/client_secret"
        )
    response = transport.token(client_id, client_secret)
    token = str(response.get("access_token") or "").strip()
    if not token:
        raise SearchPhrasesError(f"未取到 access_token：{response}")
    return token


# ---------------------------------------------------------------- 报表


def create_report(
    transport: PerformanceTransport,
    bearer: str,
    *,
    campaigns: Sequence[Any],
    date_from: str,
    date_to: str,
    report_path: str = DEFAULT_REPORT_PATH,
    group_by: str = "NO_GROUP_BY",
) -> str:
    clean = [str(item).strip() for item in campaigns if str(item).strip()]
    if not clean:
        raise SearchPhrasesError("至少需要 1 个 campaign id")
    if len(clean) > MAX_CAMPAIGNS:
        raise SearchPhrasesError(f"一个报表最多 {MAX_CAMPAIGNS} 个 campaign")
    response = transport.post_json(
        report_path,
        {
            "campaigns": clean,
            "dateFrom": date_from,
            "dateTo": date_to,
            "groupBy": group_by,
        },
        bearer,
    )
    uuid = str(response.get("UUID") or response.get("uuid") or "").strip()
    if not uuid:
        raise SearchPhrasesError(f"未返回报表 UUID：{response}")
    return uuid


def wait_for_report(
    transport: PerformanceTransport,
    bearer: str,
    uuid: str,
    *,
    poll_interval: float = 3.0,
    timeout: float = 120.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """轮询到 state=OK，返回相对下载 link；ERROR/超时即报错。"""
    deadline = time.monotonic() + timeout
    while True:
        response = transport.get_json(PATH_STATUS.format(uuid=uuid), bearer)
        state = str(response.get("state") or "").upper()
        if state == "OK":
            link = str(response.get("link") or "").strip()
            if not link:
                raise SearchPhrasesError(f"报表 OK 但没有 link：{response}")
            return link
        if state == "ERROR":
            raise SearchPhrasesError(f"报表生成失败：{response.get('error') or response}")
        if time.monotonic() >= deadline:
            raise SearchPhrasesError(f"报表轮询超时（state={state}）")
        sleeper(poll_interval)


def download_csv(transport: PerformanceTransport, bearer: str, link: str) -> str:
    # link 可能是相对路径或完整 URL
    path = link if link.startswith("http") else (
        link if link.startswith("/api/") else f"/api{'' if link.startswith('/') else '/'}{link}"
    )
    return transport.get_text(path, bearer)


# ---------------------------------------------------------------- CSV


# 表头别名 → 规范字段（兼容俄/英、空格差异）
HEADER_ALIASES: dict[str, str] = {
    "поисковая фраза": "phrase",
    "фраза": "phrase",
    "search phrase": "phrase",
    "keyword": "phrase",
    "показы": "impressions",
    "impressions": "impressions",
    "клики": "clicks",
    "clicks": "clicks",
    "ctr": "ctr",
    "расход": "spend",
    "затраты": "spend",
    "spend": "spend",
    "cost": "spend",
    "заказы": "orders",
    "orders": "orders",
    "заказы в рублях": "orders_revenue",
    "выручка": "orders_revenue",
    "revenue": "orders_revenue",
    "корзины": "carts",
    "добавления в корзину": "carts",
}


def _canon_header(text: str) -> str:
    return " ".join(str(text or "").strip().casefold().split())


def parse_csv_text(text: str) -> list[dict[str, Any]]:
    """宽容解析：识别表头别名，数值转 number，未知列保留 extra。"""
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for raw in reader:
        row: dict[str, Any] = {"extra": {}}
        for original, value in (raw or {}).items():
            if original is None:
                continue
            field = HEADER_ALIASES.get(_canon_header(original))
            if field is None:
                if value not in (None, ""):
                    row["extra"][original.strip()] = value
                continue
            row[field] = _num(value)
        phrase = str(row.get("phrase") or "").strip()
        if not phrase:
            continue
        row["phrase"] = phrase
        for numeric in ("impressions", "clicks", "spend", "orders", "orders_revenue", "carts", "ctr"):
            row.setdefault(numeric, None)
        rows.append(row)
    return rows


def _num(value: Any) -> Any:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", ".").replace(" ", "")
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else round(number, 4)


# ------------------------------------------------------------ 分类/入库


def classify(
    rows: Sequence[Mapping[str, Any]],
    *,
    click_threshold: int = NEGATIVE_CLICK_THRESHOLD,
) -> dict[str, list[dict[str, Any]]]:
    """拆成 winners（出单词）/ negatives（只点不买）/ neutral。"""
    winners, negatives, neutral = [], [], []
    for row in rows:
        clicks = row.get("clicks") or 0
        orders = row.get("orders") or 0
        if orders and orders > 0:
            winners.append(dict(row))
        elif clicks and clicks >= click_threshold:
            negatives.append(dict(row))
        else:
            neutral.append(dict(row))
    return {"winners": winners, "negatives": negatives, "neutral": neutral}


def to_keyword_records(
    rows: Sequence[Mapping[str, Any]],
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    category_path_zh: str | None = None,
) -> list[dict[str, Any]]:
    """把词组行转成关键词库 ``upsert`` 记录（指标放 extra，来源 performance_ad）。"""
    records = []
    for row in rows:
        records.append(
            {
                "keyword": row["phrase"],
                "category_id": category_id or "",
                "type_id": type_id or "",
                "category_path_zh": category_path_zh,
                "source": "performance_ad",
                "extra": {
                    "impressions": row.get("impressions"),
                    "clicks": row.get("clicks"),
                    "spend": row.get("spend"),
                    "orders": row.get("orders"),
                    "orders_revenue": row.get("orders_revenue"),
                    "carts": row.get("carts"),
                },
            }
        )
    return records


def feed_keyword_library(
    library_root: Path | str,
    rows: Sequence[Mapping[str, Any]],
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    category_path_zh: str | None = None,
) -> dict[str, Any]:
    """把词组写进关键词库（去重合并、自动打分）。"""
    from keyword_library.store import upsert

    records = to_keyword_records(
        rows,
        category_id=category_id,
        type_id=type_id,
        category_path_zh=category_path_zh,
    )
    return upsert(library_root, records, source="performance_ad")


# ---------------------------------------------------------------- 编排


@dataclass
class PerformanceCredentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PerformanceCredentials":
        import os

        source = env if env is not None else os.environ
        return cls(
            client_id=str(source.get("OZON_PERFORMANCE_CLIENT_ID") or "").strip(),
            client_secret=str(source.get("OZON_PERFORMANCE_CLIENT_SECRET") or "").strip(),
        )


def run(
    transport: PerformanceTransport,
    *,
    campaigns: Sequence[Any],
    days: int = 14,
    category_id: str | None = None,
    type_id: str | None = None,
    category_path_zh: str | None = None,
    library_root: Path | str | None = None,
    credentials: PerformanceCredentials | None = None,
    report_path: str = DEFAULT_REPORT_PATH,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """完整跑一遍：换 token → 建报表 → 轮询 → 下载 → 解析 →（可选）入库。"""
    creds = credentials or PerformanceCredentials.from_env()
    bearer = obtain_token(
        transport, client_id=creds.client_id, client_secret=creds.client_secret
    )
    date_from, date_to = default_dates(days)
    uuid = create_report(
        transport,
        bearer,
        campaigns=campaigns,
        date_from=date_from,
        date_to=date_to,
        report_path=report_path,
    )
    link = wait_for_report(transport, bearer, uuid, timeout=timeout)
    text = download_csv(transport, bearer, link)
    rows = parse_csv_text(text)
    groups = classify(rows)
    result: dict[str, Any] = {
        "uuid": uuid,
        "period": {"dateFrom": date_from, "dateTo": date_to},
        "phrases": len(rows),
        "winners": [r["phrase"] for r in groups["winners"]],
        "negatives": [r["phrase"] for r in groups["negatives"]],
    }
    if library_root is not None and rows:
        result["library"] = feed_keyword_library(
            library_root,
            rows,
            category_id=category_id,
            type_id=type_id,
            category_path_zh=category_path_zh,
        )
    return result


# --------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="广告真实词 SEARCH_PHRASES 采集")
    parser.add_argument("--campaign", action="append", dest="campaigns")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--category-id")
    parser.add_argument("--type-id")
    parser.add_argument("--category-path")
    parser.add_argument("--library-root", help="词组写进该关键词库目录")
    parser.add_argument("--fixture-csv", help="离线：直接解析该 CSV（不联网）")
    args = parser.parse_args(argv)

    try:
        if args.fixture_csv:
            text = Path(args.fixture_csv).read_text(encoding="utf-8")
            rows = parse_csv_text(text)
            groups = classify(rows)
            output: dict[str, Any] = {
                "ok": True,
                "phrases": len(rows),
                "winners": [r["phrase"] for r in groups["winners"]],
                "negatives": [r["phrase"] for r in groups["negatives"]],
            }
            if args.library_root:
                output["library"] = feed_keyword_library(
                    args.library_root,
                    rows,
                    category_id=args.category_id,
                    type_id=args.type_id,
                    category_path_zh=args.category_path,
                )
        else:
            if not args.campaigns:
                raise SearchPhrasesError("请用 --campaign 指定广告活动 id（或用 --fixture-csv 离线测试）")
            from collector.performance_http import UrllibPerformanceTransport

            transport = UrllibPerformanceTransport(PERF_BASE_URL)
            result = run(
                transport,
                campaigns=args.campaigns,
                days=args.days,
                category_id=args.category_id,
                type_id=args.type_id,
                category_path_zh=args.category_path,
                library_root=args.library_root,
            )
            output = {"ok": True, **result}
    except SearchPhrasesError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
