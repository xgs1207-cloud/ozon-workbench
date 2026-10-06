"""效果回流：Ozon Seller API ``POST /v1/analytics/product-queries``。

**这个模块解决什么问题**

上品流水线原本是"单向"的：采集 → 生成 → 上架就结束，运营只能凭感觉判断新品死活。
本接口按 SKU 拉回平台侧真实搜索表现，回答四个问题：

1. 有没有人搜（``unique_search_users``）——搜不到 = 标题没覆盖真实词；
2. 平均排名（``position``）——排在 3 页之后基本没自然流量；
3. 有没有人看（``unique_view_users``）——搜的人多、看的人少 = 主图/价格在列表里不吸引人；
4. 看了买不买（``view_conversion`` / ``gmv``）——转化低 = 详情图/简介/价格有短板。

设计要点：

- **复用 Seller 传输层**：``pipeline.ozon_http`` 的注入式 ``Transport``（POST JSON +
  Client-Id/Api-Key），离线测试用夹具，零网络；
- **只读**：本模块只发 GET 语义的读取请求，不实现任何写接口；
- **Premium 才有完整字段**：``position`` / ``unique_view_users`` / ``view_conversion``
  仅 Premium / Premium Plus 返回，非会员这些字段为 ``None``，解析时如实保留；
- **数据时效**：最近 1 个月可查但**不含最近 3 天**（仍在计算），更早历史按周查。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

SCHEMA_VERSION = "1.0.0"
PATH_PRODUCT_QUERIES = "/v1/analytics/product-queries"

DEFAULT_PAGE_SIZE = 100
DEFAULT_DAYS = 30
MAX_SKUS_PER_CALL = 1000

# 只有 Premium 订阅才返回的字段（非 Premium 为 None）
PREMIUM_ONLY_FIELDS = ("position", "unique_view_users", "view_conversion")


class ProductQueriesError(RuntimeError):
    pass


class Transport(Protocol):
    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------- 时间


def _iso(days_ago: int) -> str:
    """UTC 日期时间（ISO）。``days_ago`` 为距今天数；0 = 今天。"""
    target = datetime.now(timezone.utc) - timedelta(days=int(days_ago))
    return target.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_period(days: int = DEFAULT_DAYS) -> tuple[str, str]:
    """默认查询窗口：结束于 3 天前（最近 3 天数据未算完），向前 ``days`` 天。"""
    return _iso(int(days) + 3), _iso(3)


# --------------------------------------------------------------------- 取数


def fetch_page(
    transport: Transport,
    *,
    skus: Sequence[Any],
    date_from: str,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "BY_SEARCHES",
    sort_dir: str = "DESCENDING",
) -> dict[str, Any]:
    clean_skus = [str(item).strip() for item in skus if str(item).strip()]
    if not clean_skus:
        raise ProductQueriesError("至少需要 1 个 Ozon SKU")
    if len(clean_skus) > MAX_SKUS_PER_CALL:
        raise ProductQueriesError(f"一次最多 {MAX_SKUS_PER_CALL} 个 SKU")
    body: dict[str, Any] = {
        "date_from": date_from,
        "skus": clean_skus,
        "page": int(page),
        "page_size": int(page_size),
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }
    if date_to:
        body["date_to"] = date_to
    response = transport.post(PATH_PRODUCT_QUERIES, body)
    if not isinstance(response, dict):
        raise ProductQueriesError("响应不是 JSON 对象")
    return response


def fetch_all(
    transport: Transport,
    *,
    skus: Sequence[Any],
    date_from: str,
    date_to: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "BY_SEARCHES",
    sort_dir: str = "DESCENDING",
) -> dict[str, Any]:
    """分页拉完，返回 ``{items, total, page_count, period}``。"""
    items: list[dict[str, Any]] = []
    page = 1
    total = 0
    page_count = 1
    period: dict[str, Any] = {}
    while True:
        response = fetch_page(
            transport,
            skus=skus,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        period = response.get("analytics_period") or period
        chunk = [item for item in (response.get("items") or []) if isinstance(item, Mapping)]
        items.extend(normalize_item(item) for item in chunk)
        total = int(response.get("total") or total)
        page_count = int(response.get("page_count") or page_count)
        if page >= page_count or not chunk:
            break
        page += 1
    return {"items": items, "total": total, "page_count": page_count, "period": period}


# ----------------------------------------------------------------- 归一化


def _number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def normalize_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把一条商品级指标整理成稳定形状；Premium 字段缺失即 None。"""
    return {
        "sku": _number(raw.get("sku")),
        "offer_id": str(raw.get("offer_id") or "") or None,
        "name": str(raw.get("name") or "") or None,
        "category": str(raw.get("category") or "") or None,
        "unique_search_users": _number(raw.get("unique_search_users")),
        "unique_view_users": _number(raw.get("unique_view_users")),
        "position": _number(raw.get("position")),
        "view_conversion": _number(raw.get("view_conversion")),
        "gmv": _number(raw.get("gmv")),
        "currency": str(raw.get("currency") or "") or None,
    }


# ------------------------------------------------------- SKU 枚举（最佳努力）


def enumerate_ozon_skus(product_dir: Path | str) -> list[str]:
    """从商品落盘产物里找已上架的 Ozon SKU（去重、保序）。

    扫描常见产物文件里键名为 ``sku`` / ``ozon_sku`` 的整数，以及
    upload/publications 结果里的同名字段。找不到就返回空列表，由调用方显式传入。
    """
    root = Path(product_dir)
    candidates = [
        root / "output" / "upload-result.json",
        root / "output" / "publications.json",
        root / "output" / "ozon-status.json",
        root / "status.json",
    ]
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower() in {"sku", "ozon_sku"}:
                    try:
                        number = str(int(value)).strip()
                    except (TypeError, ValueError):
                        continue
                    if number and number not in found:
                        found.append(number)
                else:
                    visit(value)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    for path in candidates:
        if not path.is_file():
            continue
        try:
            visit(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


# ----------------------------------------------------------------- 落盘


def build_report(
    *,
    product_id: str,
    skus: Sequence[Any],
    result: Mapping[str, Any],
    date_from: str,
    date_to: str | None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "fetched_at": fetched_at or _iso(0),
        "api_endpoint": PATH_PRODUCT_QUERIES,
        "requested_skus": [str(item) for item in skus],
        "period": result.get("period") or {"date_from": date_from, "date_to": date_to},
        "total": result.get("total"),
        "items": list(result.get("items") or []),
        "premium_only_fields": list(PREMIUM_ONLY_FIELDS),
    }


def collect(
    product_dir: Path | str,
    transport: Transport,
    *,
    skus: Sequence[Any] | None = None,
    days: int = DEFAULT_DAYS,
) -> dict[str, Any]:
    """拉取一个商品的搜索表现并写入 ``output/product-queries.json``，返回报告。"""
    root = Path(product_dir)
    resolved = list(skus) if skus else enumerate_ozon_skus(root)
    if not resolved:
        raise ProductQueriesError(
            "未能从落盘产物找到 Ozon SKU：请显式传入 skus（商品需已上架）"
        )
    date_from, date_to = default_period(days)
    result = fetch_all(
        transport, skus=resolved, date_from=date_from, date_to=date_to
    )
    report = build_report(
        product_id=root.name,
        skus=resolved,
        result=result,
        date_from=date_from,
        date_to=date_to,
    )
    target = root / "output" / "product-queries.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def read_report(product_dir: Path | str) -> dict[str, Any] | None:
    target = Path(product_dir) / "output" / "product-queries.json"
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- CLI


def _fixture_transport(directory: Path) -> Transport:
    from pipeline.ozon_http import FixtureTransport

    transport = FixtureTransport(directory=directory)

    class _Wrapped:
        def post(self, path, body):
            if path == PATH_PRODUCT_QUERIES:
                name = "product-queries.json"
                target = directory / name
                if target.is_file():
                    return json.loads(target.read_text(encoding="utf-8"))
            return transport.post(path, body)

    return _Wrapped()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="效果回流：商品搜索表现 product-queries")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--sku", action="append", dest="skus", help="Ozon SKU（可重复）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--fixture-dir", help="用夹具代替真实网络（离线自检）")
    parser.add_argument("--shop", help="店铺 id（真实凭据，从 config/shops.json 读）")
    args = parser.parse_args(argv)

    try:
        if args.fixture_dir:
            transport = _fixture_transport(Path(args.fixture_dir))
        else:
            from pipeline.ozon_http import OzonCredentials, UrllibTransport
            from pipeline.stores import list_shops, load_registry

            shops = list_shops(load_registry(None))
            shop = (
                next((item for item in shops if str(item.get("id")) == str(args.shop)), None)
                if args.shop
                else (shops[0] if shops else None)
            )
            if not shop:
                raise ProductQueriesError("没有可用店铺：先配置 config/shops.json")
            credentials = OzonCredentials.from_shop(shop)
            transport = UrllibTransport(credentials)
        report = collect(
            args.product_dir, transport, skus=args.skus, days=args.days
        )
    except ProductQueriesError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {"ok": True, "product_id": report["product_id"], "items": len(report["items"])},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
