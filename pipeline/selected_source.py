"""One selected-SKU projection for every AI context; never edits source evidence."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .sku_selection import active_skus, selection_state


def _model_safe(value: Any) -> Any:
    """Models need product facts, not signed media URLs or browser credentials."""
    if isinstance(value, Mapping):
        excluded = {"videos", "video_urls", "source_videos", "cookies", "cookie", "authorization", "access_token", "api_key", "password"}
        return {key: _model_safe(child) for key, child in value.items() if str(key).casefold() not in excluded}
    if isinstance(value, (list, tuple)):
        return [_model_safe(child) for child in value]
    if isinstance(value, str):
        def clean(match):
            try:
                url = urlsplit(match.group(0))
                # Strip basic auth, query tokens and fragments from AI context.
                host = url.hostname or ""
                if url.port:
                    host += f":{url.port}"
                return urlunsplit((url.scheme, host, url.path, "", ""))
            except ValueError:
                return "[商品来源链接]"
        return re.sub(r"https?://[^\s<>\"']+", clean, value)
    return value


def selected_source(directory: Path | str, source: Mapping[str, Any], *, require_selection: bool = False,
                    merge_category: bool = True) -> dict[str, Any]:
    directory = Path(directory)
    state = selection_state(directory)
    rows = active_skus(directory, source.get("skus") or [])
    if require_selection and (not state["has_selection"] or state["unknown_in_selection"] or not 1 <= len(rows) <= 10):
        raise ValueError("请先确认 1–10 个有效的上架规格，再分析商品")
    # A raw snapshot/extra body can contain all variants; do not pass that second
    # unfiltered copy to the model after filtering the primary skus array.
    projected = {key: deepcopy(value) for key, value in source.items()
                 if key not in {"extra", "raw_snapshot", "raw", "keywords", "selected_keywords", "image_sources"}}
    for row in rows:
        row.setdefault("name_zh", row.get("sku_name") or row.get("name") or row.get("spec_zh") or row.get("spec_text") or row.get("sku_id"))
    projected["skus"] = rows
    projected["selected_sku_ids"] = [str(row.get("sku_id")) for row in rows]
    projected["sku_selection_explicit"] = bool(state["has_selection"])
    projected["sku_scope_note"] = "仅总结这些已选规格；原标题、详情中的其他规格不属于本次上架，不可混入买家文案。"
    selection = directory / "input/category-selection.json"
    if merge_category and selection.is_file():
        import json
        try:
            category = json.loads(selection.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            category = {}
        if isinstance(category, dict) and category.get("category_id") and category.get("type_id"):
            projected["selected_category"] = category
    return _model_safe(projected)
