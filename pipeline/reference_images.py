"""Stable capture IDs, selected variant photos first, shared evidence after.

Unknown SKU photos are not paired by position. They remain in the immutable
capture, but are not offered to the listing editor until explicitly associated.
"""
from pathlib import Path
from typing import Any


def selected_reference_images(directory: Path | str, *, annotate: bool = False) -> list[dict[str, Any]]:
    from models.image_plan import _list_reference_images
    from .image_jobs import _reference_owners
    from .listing_form import read_json
    from .sku_selection import selection_state

    directory = Path(directory)
    selection = selection_state(directory)
    selected = set(selection.get("selected") or []) if selection.get("has_selection") else set()
    source = read_json(directory / "input/source.json")
    variants, shared = [], []
    for row in _list_reference_images(directory):
        owners = _reference_owners(source, row["path"])
        if owners and not owners.intersection(selected):
            continue
        if row["role"] == "sku" and not owners:
            continue
        item = dict(row)
        if annotate:
            item.update(source_sku_ids=sorted(owners), selected_sku_ids=sorted(owners.intersection(selected)))
        (variants if row["role"] == "sku" else shared).append(item)
    return variants + shared
