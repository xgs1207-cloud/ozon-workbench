"""采集数据体检：在跑流水线之前，先把"真实 1688 采集常见的坑"挑出来。

为什么单独一层：``validate_source`` 只管硬门禁（1688 来源、SKU 数、采购价），但真实采集里经常有：

- 同一链接抓到**重复 sku_id** → 后面同一 offer 会被提交两次（Ozon 报重复）；
- sku_id 含中文/空格，**清洗+截断后撞车**（``红 500`` 与 ``红-500`` 都会变成 ``-500``）→ 也是重复 offer；
- sku_id 太长 → offer_id 被截断到 50 字符后撞车；
- **一张图都没有** → 图片规划必然 needs_review，流程走到一半才炸；
- 变体值（颜色/容量）全空 → Ozon 变体属性没法填；
- 采购价明显异常（0.5 元 / 99999 元）→ 多半抓错了。

体检结果分 **阻断** 与 **提醒**：能确定的错（重复、无图）阻断；可疑的（价格离群、缺变体值）只提醒，
并把数量写进 ``output/source-quality.json``，供档案与预检复用。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

QUALITY_FILE = "output/source-quality.json"
SCHEMA_VERSION = "1.0.0"
IMAGE_DIRS = {"main": "input/main-images", "sku": "input/sku-images", "detail": "input/detail-images"}
SKU_ID_MAX = 40
OFFER_ID_MAX = 50
PRICE_FLOOR_CNY = 1.0
PRICE_CEILING_CNY = 20000.0
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sanitize_sku_id(value: Any, index: int) -> str:
    """与 upload._offer_id 完全一致的清洗规则（撞车判断必须用同一套）。"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or f"S{index}"))


def _parse_number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _count_images(product_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for role, relative in IMAGE_DIRS.items():
        directory = product_dir / relative
        if not directory.is_dir():
            counts[role] = 0
            continue
        counts[role] = len(
            [item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES]
        )
    return counts


def check_capture(
    source: Mapping[str, Any],
    *,
    product_dir: Path | str | None = None,
    active_sku_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """体检采集数据；返回 ``{"blocking": [...], "warnings": [...], "stats": {...}}``。"""
    blocking: list[str] = []
    warnings: list[str] = []

    raw_skus = [item for item in (source.get("skus") or []) if isinstance(item, Mapping)]
    wanted = {str(item) for item in (active_sku_ids or [])}
    skus = [
        sku
        for index, sku in enumerate(raw_skus, start=1)
        if not wanted or str(sku.get("sku_id") or f"S{index}") in wanted
    ]

    # ① 重复 sku_id
    seen: dict[str, int] = {}
    for index, sku in enumerate(skus, start=1):
        key = str(sku.get("sku_id") or f"S{index}").strip()
        seen[key] = seen.get(key, 0) + 1
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    if duplicates:
        blocking.append(f"重复 sku_id：{duplicates}（同一 offer 会被提交两次）")

    # ② 清洗+截断后撞车（offer_id 用 product_id-sku_id 截到 50 字符）
    product_id = str(source.get("product_id") or (Path(product_dir).name if product_dir else "P000000"))
    offers: dict[str, list[str]] = {}
    for index, sku in enumerate(skus, start=1):
        original = str(sku.get("sku_id") or f"S{index}").strip()
        offer = f"{product_id}-{sanitize_sku_id(original, index)}"[:OFFER_ID_MAX]
        offers.setdefault(offer, []).append(original)
    collisions = {offer: names for offer, names in offers.items() if len(names) > 1}
    if collisions:
        detail = "；".join(f"{offer} ← {names}" for offer, names in list(collisions.items())[:3])
        blocking.append(f"清洗/截断后 offer_id 撞车：{detail}（Ozon 会当成同一个 offer）")

    # ③ 图片（**只提醒不阻断**：先跑文案后补图是合法流程；图片步骤自己也有 needs_review 门禁）
    counts = _count_images(Path(product_dir)) if product_dir else {}
    if product_dir is not None:
        total_images = sum(counts.values())
        if total_images == 0:
            warnings.append("一张图都没有（input/main-images 等为空）：图片规划/质检做不了，补图后再跑")
        elif counts.get("main", 0) == 0:
            warnings.append("没有主图素材（input/main-images 为空）：主图只能靠详情图凑，建议补主图")
        if counts and counts.get("main", 0) < len(skus):
            warnings.append(
                f"主图素材 {counts.get('main', 0)} 张 < 上架 SKU {len(skus)} 个：多个 SKU 会共用参考图"
            )

    # ④ SKU 字段
    long_ids = [key for key in seen if len(key) > SKU_ID_MAX]
    if long_ids:
        warnings.append(f"sku_id 超过 {SKU_ID_MAX} 字符：{long_ids[:3]}（offer_id 会被截断）")
    missing_variant: list[str] = []
    odd_price: list[str] = []
    for index, sku in enumerate(skus, start=1):
        key = str(sku.get("sku_id") or f"S{index}")
        if not any(str(sku.get(field) or "").strip() for field in ("color_ru", "color_zh", "capacity", "spec_zh", "name_zh")):
            missing_variant.append(key)
        price = None
        for field in ("purchase_price_cny", "cost_cny", "purchase_price", "price_cny"):
            price = _parse_number(sku.get(field))
            if price is not None:
                break
        if price is not None and (price < PRICE_FLOOR_CNY or price > PRICE_CEILING_CNY):
            odd_price.append(f"{key}={price}")
    if missing_variant:
        warnings.append(f"这些 SKU 没有颜色/规格值：{missing_variant[:5]}（Ozon 变体属性会缺值）")
    if odd_price:
        warnings.append(f"采购价异常（疑似抓错）：{odd_price[:5]}")

    # ⑤ 标题
    title = str(source.get("title_zh") or "").strip()
    if not title:
        warnings.append("缺中文标题（title_zh）：AI 总结会少一个重要输入")
    elif len(title) < 6:
        warnings.append(f"中文标题过短（{len(title)} 字符）：{title!r}")

    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now_iso(),
        "blocking": blocking,
        "warnings": warnings,
        "stats": {
            "skus_total": len(raw_skus),
            "skus_active": len(skus),
            "images": counts,
            "duplicate_sku_ids": duplicates,
            "offer_collisions": sorted(collisions),
        },
    }


def write_quality(product_dir: Path | str, *, active_sku_ids: Sequence[str] | None = None) -> dict[str, Any]:
    directory = Path(product_dir)
    source = json.loads((directory / "input" / "source.json").read_text(encoding="utf-8"))
    report = check_capture(source, product_dir=directory, active_sku_ids=active_sku_ids)
    path = directory / QUALITY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["file"] = QUALITY_FILE
    return report


def render_report(report: Mapping[str, Any]) -> str:
    lines = [f"# 采集体检 · {'有问题' if report.get('blocking') else '可以开工'}", ""]
    stats = report.get("stats") or {}
    lines.append(f"- SKU：{stats.get('skus_active')} / 采集 {stats.get('skus_total')} 个｜图片：{stats.get('images')}")
    for item in report.get("blocking") or []:
        lines.append(f"- ⛔ {item}")
    for item in report.get("warnings") or []:
        lines.append(f"- ⚠️ {item}")
    if not report.get("blocking") and not report.get("warnings"):
        lines.append("- ✅ 没发现问题")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集数据体检（跑流水线之前先看一眼）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = write_quality(args.product_dir)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 1 if report.get("blocking") else 0


if __name__ == "__main__":
    sys.exit(main())
