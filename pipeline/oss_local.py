"""对象存储适配器（自建服务器 / 本地目录）：把生成的图片同步出去并写出公网地址。

用户的选择是「对象存储在**自己的腾讯云服务器**上」，所以这里实现的是**自建静态站点**方案：

1. 把 ``output/generated-images/**`` 按 ``<root>/<product_id>/<slot>.png`` 复制到服务器目录
   （通常是 nginx 的站点根，例如 ``/var/www/ozon-images``）；
2. 写出 ``output/image-public-urls.json``（slot → https URL），上传载荷就读它 ——
   这就是替换原项目"24 小时 Cloudflare 隧道"的落地点；
3. **增量同步**：按 sha256 比对，内容没变就不重复复制；
4. **不猜域名**：``base_url`` 必须由你给（``https://img.example.com``）。

⚠️ 我们的上传门禁**只接受 https 地址**（Ozon 服务器要能抓取到图片）：
腾讯云服务器需要域名 + 证书（Let's Encrypt 免费或腾讯云免费证书），
或者改用腾讯云 COS 的 https 域名。**没有 https 时门禁会拦住**，这是刻意的。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"
URLS_FILE = "output/image-public-urls.json"
PLAN_FILE = "output/image-plan.json"
DEFAULT_LAYOUT = "{product_id}/{slot}.png"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def planned_slots(product_dir: Path | str) -> list[dict[str, Any]]:
    """读图片计划里的槽位（主图 + 详情图），只保留有实际文件路径的。"""
    directory = Path(product_dir)
    plan = _read_json(directory / PLAN_FILE)
    from .media_selection import selected_image_specs
    rows: list[dict[str, Any]] = []
    for item in selected_image_specs(plan):
        if not isinstance(item, Mapping):
            continue
        slot = str(item.get("slot") or "").strip()
        relative = str(item.get("output_path") or "").strip()
        if slot and relative:
            rows.append({**item, "studio_mode": plan.get("studio_mode") is True})
    return rows


class LocalObjectStorage:
    """把商品图片同步到本地/自建静态目录，并返回公网 URL 映射。"""

    name = "local-static"

    def __init__(
        self,
        root: Path | str,
        base_url: str,
        *,
        layout: str = DEFAULT_LAYOUT,
        dry_run: bool = False,
        url_base_path: str = "",
    ) -> None:
        cleaned = str(base_url).strip().rstrip("/")
        if not cleaned.startswith(("https://", "http://")):
            raise ValueError(f"base_url 必须是 http(s) 地址：{base_url!r}")
        self.root = Path(root)
        self.base_url = cleaned
        self.layout = layout or DEFAULT_LAYOUT
        self.dry_run = dry_run
        self.url_base_path = str(url_base_path or "").strip("/")

    def url_for(self, product_id: str, slot: str) -> str:
        relative = self.layout.format(product_id=product_id, slot=slot).lstrip("/")
        prefix = f"{self.url_base_path}/" if self.url_base_path else ""
        return f"{self.base_url}/{prefix}{relative}"

    def _target_and_url(self, product_id: str, row: Mapping[str, Any], source: Path, digest: str) -> tuple[Path, str]:
        relative = self.layout.format(product_id=product_id, slot=str(row["slot"])).lstrip("/")
        if row.get("studio_mode") or row.get("origin") == "captured":
            # Exact source extension and immutable content-addressed key: a JPEG
            # must not be labelled PNG, and a redo cannot overwrite an old URL.
            relative = str(Path(relative).with_suffix("") / (digest + source.suffix.lower())).replace("\\", "/")
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError("静态图片目标路径必须位于配置的存储目录内")
        prefix = f"{self.url_base_path}/" if self.url_base_path else ""
        return target, f"{self.base_url}/{prefix}{relative}"

    def publish_slot(self, product_dir: Path, row: Mapping[str, Any]) -> dict[str, Any]:
        product_id = product_dir.name
        slot = str(row["slot"])
        source = (product_dir / str(row["output_path"])).resolve()
        if not source.is_relative_to(product_dir.resolve()):
            raise ValueError("图片来源必须位于当前商品目录内")
        if not source.is_file():
            return {"slot": slot, "status": "missing", "reason": f"本地文件不存在：{row['output_path']}"}
        if row.get("origin") == "captured":
            from .captured_images import validate_captured_image
            receipt = next((entry for entry in _read_json(product_dir / "output/image-generation-report.json").get("files") or []
                            if entry.get("slot") == slot), {})
            errors = validate_captured_image(product_dir, row, receipt)
            if errors:
                raise ValueError("；".join(errors))

        source_hash = sha256_file(source)
        target, url = self._target_and_url(product_id, row, source, source_hash)
        unchanged = target.is_file() and target.stat().st_size == source.stat().st_size and sha256_file(target) == source_hash
        if unchanged:
            return {"slot": slot, "status": "unchanged", "path": str(target), "url": url,
                    "sha256": source_hash, "bytes": target.stat().st_size}
        if self.dry_run:
            return {"slot": slot, "status": "would_copy", "path": str(target), "url": url}

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return {
            "slot": slot,
            "status": "published",
            "path": str(target),
            "bytes": target.stat().st_size,
            "sha256": source_hash,
            "url": url,
        }

    def publish_product(
        self,
        product_dir: Path | str,
        *,
        slots: Sequence[str] | None = None,
        write_urls: bool = True,
    ) -> dict[str, Any]:
        """同步一个商品的所有槽位，并写出 ``output/image-public-urls.json``。"""
        directory = Path(product_dir)
        rows = planned_slots(directory)
        if not rows:
            raise ValueError(f"缺少图片计划或计划里没有槽位：{directory / PLAN_FILE}")
        wanted = {str(item) for item in slots} if slots else None
        if wanted:
            rows = [row for row in rows if row["slot"] in wanted]

        from .oss_cos import image_publication_version, IMAGE_MANIFEST_VERSION
        version = image_publication_version(directory)
        results = [self.publish_slot(directory, row) for row in rows]
        published = [item for item in results if item["status"] in {"published", "unchanged"}]
        urls = {item["slot"]: item["url"] for item in published if item.get("url")}

        written = None
        if write_urls and urls and not self.dry_run:
            from .guided_review import slot_fingerprint
            if image_publication_version(directory) != version:
                raise ValueError("图片或审核内容在公开期间变化，请重新审核并公开")
            by_slot = {row["slot"]: row for row in rows}
            bindings = {}
            for result in published:
                spec = by_slot[result["slot"]]
                source = directory / spec["output_path"]
                if sha256_file(source) != result["sha256"] or sha256_file(Path(result["path"])) != result["sha256"]:
                    raise ValueError("公开副本与当前已审核图片不一致")
                bindings[result["slot"]] = {"output_path": spec["output_path"], "slot_fingerprint": slot_fingerprint(spec),
                    "sha256": result["sha256"], "size_bytes": result["bytes"], "url": result["url"],
                    **({"origin": "captured", "capture_receipt": spec.get("capture_receipt")}
                       if spec.get("origin") == "captured" else {})}
            payload = {
                "schema_version": SCHEMA_VERSION,
                "product_id": directory.name,
                "base_url": self.base_url,
                "layout": self.layout,
                "storage": self.name,
                "published_at": now_iso(),
                "note": "自建静态站点（nginx 目录）；上传载荷只接受 https 地址",
                "urls": urls,
                "image_manifest_version": IMAGE_MANIFEST_VERSION, **version, "files": bindings,
                "skipped_slots": [item["slot"] for item in results if item["status"] not in {"published", "unchanged"}],
            }
            path = directory / URLS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written = str(path)

        return {
            "product_id": directory.name,
            "storage": self.name,
            "root": str(self.root),
            "base_url": self.base_url,
            "dry_run": self.dry_run,
            "slots": len(rows),
            "published": len([item for item in results if item["status"] == "published"]),
            "unchanged": len([item for item in results if item["status"] == "unchanged"]),
            "missing": [item for item in results if item["status"] == "missing"],
            "urls": urls,
            "urls_file": written,
            "results": results,
            "https_ok": all(str(url).startswith("https://") for url in urls.values()) if urls else False,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把商品图片同步到自建静态目录并写出公网地址")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--root", required=True, help="服务器上的静态目录（如 /var/www/ozon-images）")
    parser.add_argument("--base-url", required=True, help="公网前缀（https://img.example.com）")
    parser.add_argument("--layout", default=DEFAULT_LAYOUT, help="相对路径模板，默认 {product_id}/{slot}.png")
    parser.add_argument(
        "--url-base-path",
        default="",
        help="若 URL 与目录不完全同名（如 CDN 前缀 /ozon），在这里补",
    )
    parser.add_argument("--slot", action="append", dest="slots")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要做什么，不复制、不写映射")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        storage = LocalObjectStorage(
            args.root,
            args.base_url,
            layout=args.layout,
            dry_run=args.dry_run,
            url_base_path=args.url_base_path,
        )
        summary = storage.publish_product(args.product_dir, slots=args.slots)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
    else:
        mode = "（dry-run）" if summary["dry_run"] else ""
        print(f"{summary['product_id']} → {summary['root']}{mode}")
        print(f"  槽位 {summary['slots']}：新上传 {summary['published']}，未变化 {summary['unchanged']}")
        for item in summary["missing"]:
            print(f"  ⚠️ {item['slot']}: {item['reason']}")
        if summary["urls_file"]:
            print(f"  已写出：{summary['urls_file']}")
        if summary["urls"] and not summary["https_ok"]:
            print("  ⚠️ 有非 https 地址：上传门禁只接受 https（Ozon 要能抓取图片）")
    return 0 if not summary["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())
