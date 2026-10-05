"""把本地采集素材推到远端工作台（服务器上的 API）。

为什么需要它：工作台跑在你的腾讯云服务器上，而素材（1688 图片）在你 Windows 本机。
服务器上的 ``import-folder`` 只能读**服务器自己的磁盘**，所以这里把文件夹打包成
base64 JSON 推到 ``POST /api/collector/products/capture``，由服务器解包入库。

用法（配合 SSH 隧道，API 看起来就在本机 8766）：

    python -m collector.push_capture --folder D:\\capture\\p1 `
        --category-id 17028922 --type-id 91875 --keyword "простынь на резинке 160х200"

文件夹约定与本地入库一致：``product.json``（可选）+ ``main-images/`` + ``sku-images/`` + ``detail-images/``。
**不发任何图片给第三方**：只发到你自己的服务器。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_API = "http://127.0.0.1:8766"
CAPTURE_PATH = "/api/collector/products/capture"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ROLE_DIRS = {"main": "main-images", "sku": "sku-images", "detail": "detail-images"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class PushError(RuntimeError):
    """推送前或推送过程中的确定性失败。"""


def build_capture_payload(
    folder: Path | str,
    *,
    category: Mapping[str, Any] | None = None,
    skus: Sequence[Mapping[str, Any]] | None = None,
    title_zh: str | None = None,
    source_url: str | None = None,
    keywords: Sequence[str] | None = None,
    keyword_source: str = "collection_plan",
    keyword_category: Mapping[str, Any] | None = None,
    allow_new_version: bool = False,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> dict[str, Any]:
    """读文件夹 → 组装 capture 载荷（图片 base64）。"""
    base = Path(folder)
    if not base.is_dir():
        raise PushError(f"文件夹不存在：{base}")
    descriptor: dict[str, Any] = {}
    descriptor_path = base / "product.json"
    if descriptor_path.is_file():
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PushError(f"product.json 不是合法 JSON：{error}") from error

    resolved_url = source_url or descriptor.get("source_url")
    if not resolved_url:
        raise PushError("缺少 source_url：给 --source-url 或在 product.json 里写 source_url")
    resolved_skus = list(skus or descriptor.get("skus") or [])
    if not resolved_skus:
        raise PushError("缺少 SKU：给 --sku <id>:<采购价> 或在 product.json 里写 skus")
    resolved_title = title_zh or descriptor.get("title_zh")
    resolved_category = dict(category or descriptor.get("category") or {}) or None
    resolved_keywords = [str(item) for item in (keywords or descriptor.get("keywords") or [])]
    resolved_keyword_category = dict(keyword_category or descriptor.get("keyword_category") or {}) or None

    images: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for role, directory_name in ROLE_DIRS.items():
        directory = base / directory_name
        if not directory.is_dir():
            continue
        entries: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            size = path.stat().st_size
            if size > max_image_bytes:
                raise PushError(f"{path.name} 太大（{size // 1024 // 1024}MB，单张上限 {max_image_bytes // 1024 // 1024}MB）")
            total += size
            entries.append(
                {
                    "name": path.name,
                    "data_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
        if entries:
            images[role] = entries
    if not images:
        raise PushError(f"文件夹里没有图片（找过 {list(ROLE_DIRS.values())}）")
    if total > max_image_bytes * 20:
        raise PushError(f"图片总量 {total // 1024 // 1024}MB 过大，请分批推送")

    payload: dict[str, Any] = {
        "source_url": str(resolved_url),
        "title_zh": resolved_title,
        "skus": resolved_skus,
        "images": images,
        "allow_new_version": bool(allow_new_version),
    }
    if resolved_category:
        payload["category"] = resolved_category
    if resolved_keywords:
        payload["keywords"] = resolved_keywords
        payload["keyword_source"] = keyword_source
    if resolved_keyword_category:
        payload["keyword_category"] = resolved_keyword_category
    payload["_stats"] = {
        "images": sum(len(items) for items in images.values()),
        "bytes": total,
        "roles": {role: len(items) for role, items in images.items()},
    }
    return payload


def push_capture(
    folder: Path | str,
    *,
    api: str = DEFAULT_API,
    timeout: int = 120,
    opener: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """组装并 POST；返回服务器响应（含 product_id）。"""
    payload = build_capture_payload(folder, **kwargs)
    stats = payload.pop("_stats")
    request = urllib.request.Request(
        f"{str(api).rstrip('/')}{CAPTURE_PATH}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8")[:400]
        except Exception:  # noqa: BLE001
            detail = ""
        raise PushError(f"服务器返回 HTTP {error.code}：{detail}") from error
    except urllib.error.URLError as error:
        raise PushError(f"连不上工作台 API（{api}）：{error.reason}；隧道开了吗？") from error
    body["_stats"] = stats
    return body


def _parse_sku_flags(values: Sequence[str] | None) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for item in values or []:
        if ":" not in item:
            raise PushError(f"--sku 需要 <sku_id>:<采购价> 形式，收到：{item}")
        sku_id, _, price = item.partition(":")
        try:
            skus.append({"sku_id": sku_id.strip(), "purchase_price_cny": float(price)})
        except ValueError as error:
            raise PushError(f"--sku 价格不是数字：{item}") from error
    return skus


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把本地采集素材推到远端工作台（服务器上的 API）")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--api", default=DEFAULT_API, help=f"工作台 API（默认 {DEFAULT_API}）")
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--title-zh", default=None)
    parser.add_argument("--category-id", default=None)
    parser.add_argument("--type-id", default=None)
    parser.add_argument("--sku", action="append", dest="skus", help="<sku_id>:<采购价>，可重复")
    parser.add_argument("--keyword", action="append", dest="keywords", help="来自选品清单的关键词，可重复")
    parser.add_argument("--keyword-source", default="collection_plan")
    parser.add_argument("--keyword-category-id", default=None)
    parser.add_argument("--keyword-type-id", default=None)
    parser.add_argument("--new-version", action="store_true", help="同一 offer 已存在时新建版本")
    parser.add_argument("--dry-run", action="store_true", help="只打包看统计，不发送")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    category = (
        {"category_id": args.category_id, "type_id": args.type_id}
        if args.category_id or args.type_id
        else None
    )
    keyword_category = (
        {"category_id": args.keyword_category_id, "type_id": args.keyword_type_id}
        if args.keyword_category_id or args.keyword_type_id
        else None
    )
    try:
        if args.dry_run:
            payload = build_capture_payload(
                args.folder,
                category=category,
                skus=_parse_sku_flags(args.skus) or None,
                title_zh=args.title_zh,
                source_url=args.source_url,
                keywords=args.keywords,
                keyword_source=args.keyword_source,
                keyword_category=keyword_category,
                allow_new_version=args.new_version,
            )
            stats = payload.pop("_stats")
            print(json.dumps({"ok": True, "dry_run": True, "stats": stats}, ensure_ascii=False, indent=2))
            return 0
        result = push_capture(
            args.folder,
            api=args.api,
            timeout=args.timeout,
            category=category,
            skus=_parse_sku_flags(args.skus) or None,
            title_zh=args.title_zh,
            source_url=args.source_url,
            keywords=args.keywords,
            keyword_source=args.keyword_source,
            keyword_category=keyword_category,
            allow_new_version=args.new_version,
        )
    except PushError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
