"""把 1688 商品页"抓取脚本"导出的 JSON 变成可入库的素材文件夹。

流程（零手工，三步）：

1. 在 1688 商品页的控制台/书签脚本里跑 ``collector/capture_1688.js`` →
   它会下载一个 ``capture-<offer_id>.json``（标题、SKU、主图/详情图/SKU 图 URL）；
2. ``python -m collector.fetch_images --json capture-xxx.json --out D:\\capture\\p1``
   → 把图片下载到 ``main-images/`` ``sku-images/`` ``detail-images/``，并写出 ``product.json``；
3. ``python -m collector.push_capture --folder D:\\capture\\p1 --keyword "..." --category-id ... --type-id ...``
   → 推到服务器入库（自动带关键词与类目）。

为什么分成"浏览器抓 URL"+"本机下载"：浏览器不能任意写磁盘，而 1688 的图片 CDN 会校验 Referer，
用本机 Python 带正确的 Referer 头去下最稳（也便于失败重试与去重）。**不经过任何第三方**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

DEFAULT_REFERER = "https://detail.1688.com/"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ROLE_DIRS = {"main": "main-images", "sku": "sku-images", "detail": "detail-images"}
MAX_IMAGES_PER_ROLE = 30
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),  # RIFF....WEBP
)


class FetchError(RuntimeError):
    """抓取 JSON 不可用，或下载失败得无法继续。"""


def detect_extension(data: bytes, url: str = "") -> str | None:
    """按内容魔数判断图片类型（不信任 URL 后缀）。"""
    for magic, suffix in MAGIC:
        if data.startswith(magic):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                continue
            return suffix
    return None


def load_capture(path: Path | str) -> dict[str, Any]:
    """读浏览器脚本导出的 JSON（容忍直接是 URL 列表的简写形式）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FetchError(f"读不了抓取 JSON：{error}") from error
    if not isinstance(payload, Mapping):
        raise FetchError("抓取 JSON 顶层必须是对象")
    images = payload.get("images")
    if isinstance(images, list):
        payload = {**payload, "images": {"main": images}}
        images = payload["images"]
    elif not isinstance(images, Mapping):
        raise FetchError("抓取 JSON 里没有 images（应为 {main: [...], sku: [...], detail: [...]}）")
    if not payload.get("source_url"):
        raise FetchError("抓取 JSON 里没有 source_url")
    cleaned: dict[str, list[str]] = {}
    for role in ROLE_DIRS:
        entries = images.get(role) or []
        urls: list[str] = []
        for item in entries:
            text = str(item.get("url") if isinstance(item, Mapping) else item or "").strip()
            if text.startswith(("http://", "https://")) and text not in urls:
                urls.append(text)
        if urls:
            cleaned[role] = urls[:MAX_IMAGES_PER_ROLE]
    if not cleaned:
        raise FetchError("抓取 JSON 里没有任何图片 URL")
    return {**dict(payload), "images": cleaned}


def _headers(referer: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return {
        "User-Agent": DEFAULT_UA,
        "Referer": referer or DEFAULT_REFERER,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
        **(dict(extra or {})),
    }


def fetch_folder(
    capture: Mapping[str, Any],
    out_dir: Path | str,
    *,
    opener: Callable[..., Any] | None = None,
    referer: str = DEFAULT_REFERER,
    timeout: int = 30,
    dry_run: bool = False,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """下载图片并写出 product.json；返回摘要。"""
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    send = opener or urllib.request.urlopen
    hashes: dict[str, str] = {}
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    plan: dict[str, list[str]] = {}

    for role, urls in (capture.get("images") or {}).items():
        directory_name = ROLE_DIRS.get(str(role))
        if not directory_name:
            skipped.append({"url": str(role), "reason": f"未知图片角色：{role}"})
            continue
        directory = base / directory_name
        plan[role] = urls
        if dry_run:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        index = 1
        for url in urls:
            request = urllib.request.Request(url, headers=_headers(referer, headers))
            try:
                with send(request, timeout=timeout) as response:
                    data = response.read()
            except urllib.error.HTTPError as error:
                skipped.append({"url": url, "reason": f"HTTP {error.code}"})
                continue
            except Exception as error:  # noqa: BLE001 - 网络问题很多种，统一记为跳过
                skipped.append({"url": url, "reason": str(error)})
                continue
            if len(data) > max_image_bytes:
                skipped.append({"url": url, "reason": f"超过单张上限 {max_image_bytes // 1024 // 1024}MB"})
                continue
            suffix = detect_extension(data, url)
            if not suffix:
                skipped.append({"url": url, "reason": "响应不是图片（看魔数判定）"})
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in hashes:
                skipped.append({"url": url, "reason": f"与已下载的 {hashes[digest]} 内容相同"})
                continue
            hashes[digest] = url
            target = directory / f"{index:02d}{suffix}"
            target.write_bytes(data)
            written.append({"role": role, "path": str(target.relative_to(base)).replace("\\", "/"), "bytes": len(data), "url": url})
            index += 1

    descriptor: dict[str, Any] = {"source_url": capture.get("source_url")}
    for key in ("title_zh", "skus", "category", "keywords", "keyword_source", "keyword_category", "attributes_zh"):
        if capture.get(key):
            descriptor[key] = capture[key]
    if not dry_run:
        (base / "product.json").write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return {
        "ok": bool(written) or bool(dry_run),
        "out_dir": str(base),
        "dry_run": bool(dry_run),
        "planned": {role: len(urls) for role, urls in plan.items()},
        "downloaded": len(written),
        "files": written,
        "skipped": skipped,
        "product_json": None if dry_run else str(base / "product.json"),
        "next": (
            f'python -m collector.push_capture --folder "{base}"'
            + (f' --keyword "{capture.get("keywords")[0]}"' if capture.get("keywords") else "")
        ),
    }


def fetch_file(path: Path | str, out_dir: Path | str, **kwargs: Any) -> dict[str, Any]:
    return fetch_folder(load_capture(path), out_dir, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 1688 抓取 JSON 变成可入库的素材文件夹")
    parser.add_argument("--json", required=True, help="capture_1688.js 导出的 JSON")
    parser.add_argument("--out", required=True, help="输出文件夹（如 D:\\capture\\p1）")
    parser.add_argument("--referer", default=DEFAULT_REFERER, help="下载时带的 Referer（1688 CDN 会校验）")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="只列出计划，不下载")
    parser.add_argument("--json-out", action="store_true", help="打印机器可读结果")
    args = parser.parse_args(argv)

    try:
        summary = fetch_file(args.json, args.out, referer=args.referer, timeout=args.timeout, dry_run=args.dry_run)
    except FetchError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json_out:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        mode = "（dry-run）" if summary["dry_run"] else ""
        print(f"输出目录：{summary['out_dir']}{mode}")
        print("计划下载：" + json.dumps(summary["planned"], ensure_ascii=False))
        print(f"已下载：{summary['downloaded']} 张")
        for item in summary["skipped"][:8]:
            print(f"  ⚠️ 跳过 {item['reason']}：{item['url'][:90]}")
        if not summary["dry_run"]:
            print(f"已写：{summary['product_json']}")
            print("下一步：" + summary["next"])
    return 0 if summary["downloaded"] or summary["dry_run"] else 1


if __name__ == "__main__":
    sys.exit(main())
