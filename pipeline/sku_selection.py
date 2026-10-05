"""上架 SKU 选择：从采集到的规格里挑出**真正要上架**的那几个。

为什么需要：1688 一个链接常有十几种颜色/尺寸，全上架既费图片成本又难管理。
选择结果写在 ``input/selected-skus.json``，并被下列环节共同遵守（一处选择，处处生效）：

- 上传载荷：只提交选中的 SKU（未选中的不进 offer）；
- 定价与尺寸重量：只为选中的 SKU 计算/校验（避免"没选的 SKU 缺价格"把整单卡住）；
- 类目属性：只按选中的 SKU 值分组（否则会提交没上架规格的属性值）；
- 图片规划：只为选中的 SKU 生成主图（**省豆包生图成本**）；
- 批次快照与运行前校验：按选中的 SKU 数量做 1–10 的校验。

**默认行为**：没有选择文件时 = 全部 SKU 都上架（向后兼容，不改变已有商品的行为）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SELECTION_FILE = "input/selected-skus.json"
SOURCE_FILE = "input/source.json"
SCHEMA_VERSION = "1.0.0"
MAX_SELECTED = 10


class SkuSelectionError(ValueError):
    """选择结果不合法（未知 SKU、全部排除、数量超限等）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def source_skus(product_dir: Path | str) -> list[dict[str, Any]]:
    """采集到的全部 SKU（保持采集顺序）。"""
    source = _read_json(Path(product_dir) / SOURCE_FILE)
    return [dict(item) for item in (source.get("skus") or []) if isinstance(item, Mapping)]


def sku_key(sku: Mapping[str, Any], index: int) -> str:
    return str(sku.get("sku_id") or f"S{index}")


def load_selection(product_dir: Path | str) -> dict[str, Any]:
    return _read_json(Path(product_dir) / SELECTION_FILE)


def active_skus(product_dir: Path | str, skus: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按选择文件过滤（无文件 = 全部；文件里提到的未知 SKU 忽略但会被 state 报出来）。"""
    rows = [dict(item) for item in skus if isinstance(item, Mapping)]
    selection = load_selection(product_dir)
    selected = selection.get("selected")
    if not isinstance(selected, list) or not selected:
        return rows
    wanted = {str(item) for item in selected}
    return [row for index, row in enumerate(rows, start=1) if sku_key(row, index) in wanted]


def selection_state(product_dir: Path | str) -> dict[str, Any]:
    """给人看的完整状态：选中/排除/文件里写了但采集数据里没有的。"""
    rows = source_skus(product_dir)
    keys = [sku_key(row, index) for index, row in enumerate(rows, start=1)]
    selection = load_selection(product_dir)
    selected = [str(item) for item in (selection.get("selected") or [])]
    excluded = selection.get("excluded") or []
    if not selection:
        return {
            "has_selection": False,
            "total": len(keys),
            "selected": keys,
            "excluded": [],
            "unknown_in_selection": [],
            "active_count": len(keys),
            "note": "没有选择文件：默认全部 SKU 都上架",
        }
    return {
        "has_selection": True,
        "total": len(keys),
        "selected": [key for key in keys if key in set(selected)],
        "excluded": [dict(item) for item in excluded if isinstance(item, Mapping)],
        "unknown_in_selection": [key for key in selected if key not in set(keys)],
        "active_count": len([key for key in keys if key in set(selected)]),
        "updated_at": selection.get("updated_at"),
        "note": selection.get("note"),
    }


def set_selection(
    product_dir: Path | str,
    *,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    reason: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """设置上架 SKU：``include`` 优先（白名单）；否则从全部里去掉 ``exclude``。"""
    directory = Path(product_dir)
    rows = source_skus(directory)
    if not rows:
        raise SkuSelectionError(f"没有采集数据：{directory / SOURCE_FILE}")
    keys = [sku_key(row, index) for index, row in enumerate(rows, start=1)]
    include_list = [str(item).strip() for item in (include or []) if str(item).strip()]
    exclude_list = [str(item).strip() for item in (exclude or []) if str(item).strip()]

    unknown = [item for item in include_list + exclude_list if item not in set(keys)]
    if unknown:
        raise SkuSelectionError(f"这些 SKU 不在采集数据里：{unknown}；可选：{keys}")
    both = set(include_list) & set(exclude_list)
    if both:
        raise SkuSelectionError(f"同一个 SKU 不能同时选中与排除：{sorted(both)}")

    if include_list:
        selected = [key for key in keys if key in set(include_list)]
    elif exclude_list:
        selected = [key for key in keys if key not in set(exclude_list)]
    else:
        selected = list(keys)
    if not selected:
        raise SkuSelectionError("至少要保留 1 个上架 SKU")
    if len(selected) > MAX_SELECTED:
        raise SkuSelectionError(f"上架 SKU 最多 {MAX_SELECTED} 个，当前 {len(selected)} 个")

    excluded = [
        {"sku_id": key, "reason": reason or "人工排除（不在本次上架范围）"}
        for key in keys
        if key not in set(selected)
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "source": "manual_selection",
        "total": len(keys),
        "selected": selected,
        "excluded": excluded,
        "note": note,
    }
    path = directory / SELECTION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "product_id": directory.name, "file": SELECTION_FILE, **{
        key: payload[key] for key in ("total", "selected", "excluded")
    }}


def clear_selection(product_dir: Path | str) -> dict[str, Any]:
    """删掉选择文件 = 恢复"全部上架"。"""
    path = Path(product_dir) / SELECTION_FILE
    existed = path.is_file()
    if existed:
        path.unlink()
    return {"ok": True, "product_id": Path(product_dir).name, "removed": existed, "note": "已恢复为全部 SKU 上架"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="选择要上架的 SKU（未选中的不进 offer）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--list", action="store_true", help="只看当前状态")
    parser.add_argument("--include", action="append", dest="include", help="要上架的 SKU（可重复；给白名单）")
    parser.add_argument("--exclude", action="append", dest="exclude", help="不上架的 SKU（可重复；从全部里去掉）")
    parser.add_argument("--reason", default=None, help="排除原因（记进文件，便于回溯）")
    parser.add_argument("--note", default=None)
    parser.add_argument("--all", action="store_true", help="全部上架（等价于清除选择文件）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.all:
            result = clear_selection(args.product_dir)
        elif args.include or args.exclude:
            result = set_selection(
                args.product_dir, include=args.include, exclude=args.exclude, reason=args.reason, note=args.note
            )
        else:
            state = selection_state(args.product_dir)
            if args.json:
                print(json.dumps(state, ensure_ascii=False, indent=2))
            else:
                print(f"{args.product_dir}：采集 {state['total']} 个 SKU，上架 {state['active_count']} 个")
                print(f"  上架：{state['selected']}")
                for item in state["excluded"]:
                    print(f"  不上架：{item.get('sku_id')}（{item.get('reason')}）")
                if state["unknown_in_selection"]:
                    print(f"  ⚠️ 选择文件里有采集数据中没有的 SKU：{state['unknown_in_selection']}")
                print(f"  {state['note']}")
            return 0
    except SkuSelectionError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
