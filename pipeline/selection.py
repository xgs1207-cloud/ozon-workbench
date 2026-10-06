"""商品级"已选关键词"：把 M0 关键词库接到 M2 文案生成的桥。

落点 ``products/<id>/input/selected-keywords.json``。两种选法：

1. **人工/界面挑**：:func:`set_selected_keywords` 直接写入选中的词；
2. **从库里按分数取**：:func:`select_from_library` 用关键词库的筛选结果自动取 topN
   （默认只取 ``in_library`` 与 ``qualified``，分数降序）。

不改写 ``input/source.json`` —— 与"采集输入不可被后续步骤改写"的约定一致。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def selection_path(product_dir: Path | str) -> Path:
    return Path(product_dir) / "input" / "selected-keywords.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_selected_keywords(product_dir: Path | str) -> dict[str, Any] | None:
    path = selection_path(product_dir)
    payload = _read_json(path)
    return payload or None


def set_selected_keywords(
    product_dir: Path | str,
    keywords: Sequence[Any],
    *,
    category: Mapping[str, Any] | None = None,
    source: str = "manual",
    note: str | None = None,
) -> dict[str, Any]:
    """写入选中的关键词。``keywords`` 可以是字符串列表，也可以是库里的记录字典。"""
    directory = Path(product_dir)
    if not directory.is_dir():
        raise ValueError(f"商品目录不存在：{directory}")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in keywords:
        if isinstance(item, Mapping):
            text = str(item.get("keyword") or item.get("text") or "").strip()
            record = {
                "keyword": text,
                "role": item.get("role"),
                "source_key": item.get("key"),
                "score": item.get("score"),
                "status": item.get("status"),
                "search_volume": item.get("search_volume"),
                "competitor_count": item.get("competitor_count"),
            }
        else:
            text = str(item or "").strip()
            record = {"keyword": text, "source_key": None, "score": None, "status": None}
        if len(text) < 2 or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        normalized.append(record)

    if not normalized:
        raise ValueError("至少要选一个长度 ≥2 的关键词")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "selected_at": now_iso(),
        "source": source,
        "category": dict(category) if category else None,
        "note": note,
        "keywords": normalized,
    }
    path = selection_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def select_from_library(
    product_dir: Path | str,
    library_root: Path | str,
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    limit: int = 10,
    include_status: Iterable[str] = ("in_library", "qualified"),
) -> dict[str, Any]:
    """从关键词库按分数取词并写入选词文件（库里没有就报错，不造词）。"""
    from keyword_library import store  # 延迟导入，避免关键词库成为硬依赖

    directory = Path(product_dir)
    if category_id is None or type_id is None:
        source = _read_json(directory / "input" / "source.json")
        category = source.get("selected_category") or {}
        category_id = category_id or category.get("category_id")
        type_id = type_id or category.get("type_id")

    picked: list[dict[str, Any]] = []
    wanted = list(include_status)
    for status in wanted:
        rows = store.query(
            library_root,
            category_id=category_id,
            type_id=type_id,
            status=status,
            order="score",
            limit=limit,
        )
        for row in rows:
            if row["key"] not in {item.get("source_key") for item in picked}:
                picked.append(row)
        if len(picked) >= limit:
            break

    if not picked:
        raise ValueError(
            f"关键词库里没有可用词（category_id={category_id}, type_id={type_id}, 状态={wanted}）"
        )

    return set_selected_keywords(
        directory,
        picked[:limit],
        category={"category_id": category_id, "type_id": type_id},
        source=f"library:{','.join(wanted)}",
    )


def selected_keyword_texts(product_dir: Path | str) -> list[str]:
    payload = load_selected_keywords(product_dir) or {}
    return [str(item.get("keyword")) for item in (payload.get("keywords") or []) if item.get("keyword")]
