"""把图位映射成公网 URL（对象存储 adapter 的落地点）。

原项目用 24 小时 Cloudflare 隧道让本地图片对 Ozon 可见；我们用**你自己的对象存储**：
你（或你的 adapter）把 ``output/generated-images/**`` 上传到对象存储后，用这个命令/函数写出
``output/image-public-urls.json``（slot → https URL），上传载荷就会用它。

    python -m pipeline.publish_urls --product-dir products/P000001 --base-url https://cdn.example.com

默认模板 ``{base}/{product_id}/{slot}.png``，可用 ``--template`` 覆盖。
**不传文件、不联网**：只写映射文件（上传动作由你的对象存储工具完成）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"
DEFAULT_TEMPLATE = "{base}/{product_id}/{slot}.png"
URLS_FILE = "output/image-public-urls.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_url_map(
    product_dir: Path | str,
    base_url: str,
    *,
    template: str = DEFAULT_TEMPLATE,
    require_files: bool = True,
) -> dict[str, Any]:
    """按图片计划生成 slot → URL 映射；``require_files`` 时跳过还没有文件的图位。"""
    directory = Path(product_dir)
    plan = _read_json(directory / "image-plan.json")
    if not plan:
        plan = _read_json(directory / "output" / "image-plan.json")
    from .media_selection import selected_image_specs
    slots = selected_image_specs(plan)
    if not slots:
        raise ValueError("缺少图片计划（output/image-plan.json）：先生成或导入计划")

    cleaned = str(base_url).strip().rstrip("/")
    if not cleaned.startswith(("https://", "http://")):
        raise ValueError(f"base_url 必须是 http(s) 地址：{base_url!r}")

    urls: dict[str, str] = {}
    skipped: list[str] = []
    for item in slots:
        slot = str(item.get("slot") or "")
        relative = str(item.get("output_path") or "")
        if not slot:
            continue
        source = (directory / relative).resolve()
        if relative and not source.is_relative_to(directory.resolve()):
            raise ValueError("图片路径必须位于当前商品目录内")
        if require_files and (not relative or not (directory / relative).is_file()):
            skipped.append(slot)
            continue
        if template == DEFAULT_TEMPLATE and item.get("origin") == "captured":
            # Mapping only: this does not claim a verified public upload.
            from .oss_local import sha256_file
            if not source.is_file():
                skipped.append(slot)
                continue
            urls[slot] = f"{cleaned}/{directory.name}/{slot}/{sha256_file(source)}{source.suffix.lower()}"
        else:
            urls[slot] = template.format(base=cleaned, product_id=directory.name, slot=slot, role=item.get("role") or "")
    return {"urls": urls, "skipped": skipped, "base_url": cleaned, "template": template,
            "verified_upload": False, "origins": {row["slot"]: row.get("origin", "ai") for row in slots}}


def write_url_map(
    product_dir: Path | str,
    base_url: str,
    *,
    template: str = DEFAULT_TEMPLATE,
    require_files: bool = True,
) -> dict[str, Any]:
    directory = Path(product_dir)
    built = build_url_map(directory, base_url, template=template, require_files=require_files)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "base_url": built["base_url"],
        "template": built["template"],
        "generated_at": now_iso(),
        "generated_by": "pipeline.publish_urls",
        "note": "对象存储上传完成后写出；上传载荷只接受 https 地址",
        "urls": built["urls"],
        "verified_upload": False, "origins": built["origins"],
        "skipped_slots": built["skipped"],
    }
    path = directory / URLS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写出图位 → 公网 URL 映射（不传文件、不联网）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--base-url", required=True, help="例如 https://cdn.example.com")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--allow-missing", action="store_true", help="文件不存在也写映射（默认跳过）")
    args = parser.parse_args(argv)

    try:
        payload = write_url_map(
            args.product_dir,
            args.base_url,
            template=args.template,
            require_files=not args.allow_missing,
        )
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "written": URLS_FILE,
                "urls": len(payload["urls"]),
                "skipped_slots": payload["skipped_slots"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
