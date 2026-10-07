"""Product-bound official forms; draft editing never writes to Ozon."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .category_form import load_form, snapshot_for_product, validate_attributes


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(dict(value), handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, path)


def persist_category_form(directory: Path, form: Mapping[str, Any]) -> None:
    """Keep full UI metadata separately from the strict existing pipeline contract."""
    write_json(directory / "input/category-form.json", form)
    write_json(directory / "output/ozon-category-attributes.json", snapshot_for_product(form, directory.name))
    write_json(directory / "output/ozon-category.json", {
        "schema_version": "1.0.0", "product_id": directory.name, "metadata_source": "ozon_seller_api",
        "category_id": form["category_id"], "type_id": form["type_id"],
        "category_name": form["category_name"], "category_path": form["category_path"],
        "match_status": "api_confirmed", "fetched_at": form["fetched_at"], "api_endpoint": form["api_endpoint"],
    })
    aspects = [dict(row) for row in form["fields"] if row["is_aspect"]]
    write_json(directory / "output/ozon-aspect-attributes.json", {
        "schema_version": "1.0.0", "product_id": directory.name,
        "category_id": form["category_id"], "type_id": form["type_id"],
        "source": "ozon_seller_api", "api_endpoint": form["api_endpoint"], "fetched_at": form["fetched_at"],
        "aspect_attribute_ids": [row["attribute_id"] for row in aspects], "aspect_attributes": aspects,
    })


def _selection(directory: Path, shop_id: str | None) -> dict[str, Any]:
    selection = read_json(directory / "input/category-selection.json")
    if not selection.get("category_id") or not selection.get("type_id"):
        raise ValueError("请先选择并确认 Ozon 官方类目")
    bound_shop = selection.get("shop_id") or selection.get("shop")
    if shop_id and bound_shop and shop_id != bound_shop:
        raise ValueError("店铺与当前类目确认不一致，请为目标店铺重新选择类目")
    return {**selection, "shop_id": bound_shop or shop_id}


def _as_values(attributes: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    result = {}
    for key, value in (attributes or {}).items():
        rows = value if isinstance(value, list) else [{"value": value}]
        result[str(key)] = [dict(row) if isinstance(row, Mapping) else {"value": row} for row in rows]
    return result


def selected_skus(directory: Path) -> list[dict[str, Any]]:
    from .sku_selection import active_skus
    source = read_json(directory / "input/source.json")
    return [{"sku_id": str(row["sku_id"]), "name": row.get("name") or row.get("spec_text") or row["sku_id"]}
            for row in active_skus(directory, source.get("skus") or [])]


def _missing(form: Mapping[str, Any], common: Mapping[str, Any],
             by_sku: Mapping[str, Any], skus: list[dict[str, Any]]) -> tuple[list[int], dict[str, list[int]]]:
    required = [int(row["attribute_id"]) for row in form["fields"] if row["required"]]
    missing_by_sku = {row["sku_id"]: [key for key in required
                      if not {**common, **by_sku.get(row["sku_id"], {})}.get(str(key))] for row in skus}
    missing = sorted({key for values in missing_by_sku.values() for key in values}) if skus else [
        key for key in required if not common.get(str(key))]
    return missing, missing_by_sku


def product_form(directory: Path, cache_root: Path, *, shop_id: str | None = None,
                 refresh: bool = False) -> dict[str, Any]:
    selection = _selection(directory, shop_id)
    form = load_form(cache_root, selection["category_id"], selection["type_id"],
                     shop_id=selection["shop_id"], refresh=refresh)
    confirmations = read_json(directory / "input/human-confirmations.json")
    common = _as_values(confirmations.get("attributes"))
    by_sku = {key: _as_values(value) for key, value in (confirmations.get("sku_attributes") or {}).items()}
    skus = selected_skus(directory)
    validation_errors = []
    for label, values in [("商品共用", common), *[(row["sku_id"], by_sku.get(row["sku_id"], {})) for row in skus]]:
        try:
            validate_attributes(form, values, cache_root=cache_root)
        except ValueError as error:
            validation_errors.append(f"{label}：{error}")
    missing, missing_by_sku = _missing(form, common, by_sku, skus)
    if validation_errors:
        # Preserve saved values for repair, but never present an expired or changed
        # schema/dictionary draft as a validated complete card.
        missing = list(form["required_attribute_ids"])
        missing_by_sku = {row["sku_id"]: list(missing) for row in skus}
    return {"form": form, "attributes": common, "per_sku_attributes": by_sku,
            "selected_skus": skus, "missing_required": missing, "missing_by_sku": missing_by_sku,
            "validation_errors": validation_errors,
            "api_writes_performed": False}


def save_product_form(directory: Path, cache_root: Path, *, shop_id: str | None,
                      category_id: int, type_id: int, attributes: Mapping[str, Any],
                      per_sku_attributes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    selection = _selection(directory, shop_id)
    if (category_id, type_id) != (selection["category_id"], selection["type_id"]):
        raise ValueError("类目已变更，请重新读取表单，不能将旧属性写入新类目")
    form = load_form(cache_root, category_id, type_id, shop_id=selection["shop_id"])
    common_report = validate_attributes(form, attributes, cache_root=cache_root)
    common = common_report["attributes"]
    source = read_json(directory / "input/source.json")
    known = {str(row["sku_id"]) for row in source.get("skus") or []}
    submitted = per_sku_attributes or {}
    if not isinstance(submitted, Mapping) or set(submitted) - known:
        raise ValueError("规格属性只能填写该商品已采集的真实 SKU")
    by_sku = {key: validate_attributes(form, value, cache_root=cache_root)["attributes"]
              for key, value in submitted.items()}
    target = directory / "input/human-confirmations.json"
    confirmations = read_json(target)
    confirmations.update(attributes=common, sku_attributes=by_sku,
                         category_form_scope=form["scope"], category_form_confirmed_at=form["fetched_at"])
    persist_category_form(directory, form)
    write_json(target, confirmations)
    from .guided_review import invalidate_from
    from .catalog import handle_field_completion
    from .context import StepContext

    invalidate_from(directory, "field_completion")
    handle_field_completion(StepContext(directory, "field_completion"))
    skus = selected_skus(directory)
    missing, missing_by_sku = _missing(form, common, by_sku, skus)
    return {"form": form, "attributes": common, "per_sku_attributes": by_sku,
            "selected_skus": skus, "missing_required": missing, "missing_by_sku": missing_by_sku,
            "warnings": common_report["warnings"], "compiled": read_json(directory / "output/ozon-attributes-final.json"),
            "api_writes_performed": False}
