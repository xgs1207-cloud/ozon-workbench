"""Opt-in, single-page Ozon Premium Pro market-data importer.

Credentials are read only from environment variables. No request is sent
without --execute; this avoids unexpected API usage during setup/tests.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import ingest_snapshot

API_BASE = "https://api-seller.ozon.ru"
REQUESTS = {
    "categories": ("/v1/analytics/category/comparison", {
        "period": "MONTH", "group": "CATEGORY_3", "metric": "GMV", "sort": "DESC", "limit": 100, "offset": 0,
    }, "items"),
    "keywords": ("/v1/search-queries/top", {"limit": 50, "offset": 0}, "search_queries"),
}


def fetch_page(kind: str, client_id: str, api_key: str) -> list[dict[str, Any]]:
    path, body, result_key = REQUESTS[kind]
    request = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise RuntimeError("Ozon 拒绝访问：请核对是否为 Premium Pro、API 密钥权限及店铺状态") from error
        raise RuntimeError(f"Ozon API 返回 HTTP {error.code}") from error
    records = payload.get(result_key)
    if not isinstance(records, list):
        raise RuntimeError(f"Ozon API 响应缺少 {result_key} 数组")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单页拉取 Ozon Premium Pro 类目或热门词快照")
    parser.add_argument("kind", choices=tuple(REQUESTS))
    parser.add_argument("--execute", action="store_true", help="确认发起一次真实的 Ozon API 请求")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("WORKBENCH_MARKET_DB_PATH") or "runtime/market-intelligence.sqlite3"))
    args = parser.parse_args(argv)
    path, body, _ = REQUESTS[args.kind]
    if not args.execute:
        print(json.dumps({"dry_run": True, "endpoint": path, "request": body}, ensure_ascii=False))
        return 0
    client_id = (os.environ.get("OZON_CLIENT_ID") or os.environ.get("OZON_DEFAULT_CLIENT_ID") or "").strip()
    api_key = (os.environ.get("OZON_API_KEY") or os.environ.get("OZON_DEFAULT_API_KEY") or "").strip()
    if not client_id or not api_key:
        parser.error("请先设置 OZON_CLIENT_ID/OZON_API_KEY 或现有店铺的 OZON_DEFAULT_CLIENT_ID/OZON_DEFAULT_API_KEY")
    records = fetch_page(args.kind, client_id, api_key)
    if not records:
        print(json.dumps({"received": 0, "message": "Ozon 返回空列表；未写入数据库"}, ensure_ascii=False))
        return 0
    result = ingest_snapshot(
        args.db, source="ozon_seller_api", dataset=args.kind, capture_method="official_api",
        period="", page_url=API_BASE + path,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), records=records,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
