"""Product-bound official forms; draft editing never writes to Ozon."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .category_form import load_form, snapshot_for_product, validate_attributes
from .product_edit_lock import product_file_transaction, serialized_product_edit

FORM_TRANSACTION_FILES = (
    "status.json",
    "input/category-form.json",
    "input/human-confirmations.json",
    "output/ozon-category-attributes.json",
    "output/ozon-category.json",
    "output/ozon-aspect-attributes.json",
    "output/attribute-fill-input.json",
    "output/ozon-attributes-final.json",
)


def _require_editable(directory: Path) -> None:
    from .status import load_status

    if int(load_status(directory).get("api_write_count") or 0) > 0 or (directory / "runtime/listing-submit-attempt.json").is_file():
        raise ValueError("此商品已有 Ozon 写入，不能在原商品流程中修改属性")


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


def _manual_meta(values: Any) -> dict[str, Any]:
    return {"source": "manual", "evidence": "人工填写或确认" if values else "人工清空，自动补全不会覆盖",
            "status": "manual"}


def _saved_provenance(confirmations: Mapping[str, Any], common: Mapping[str, Any],
                      by_sku: Mapping[str, Any]) -> dict[str, Any]:
    saved = confirmations.get("attribute_provenance") or {}
    common_meta = saved.get("attributes") or {}
    sku_meta = saved.get("per_sku_attributes") or {}
    return {
        "attributes": {key: common_meta.get(key) or _manual_meta(values) for key, values in common.items()},
        "per_sku_attributes": {
            sku_id: {key: (sku_meta.get(sku_id) or {}).get(key) or _manual_meta(values)
                     for key, values in attributes.items()}
            for sku_id, attributes in by_sku.items()
        },
    }


def _verified_provenance(directory: Path, cache_root: Path, form: Mapping[str, Any],
                         confirmations: Mapping[str, Any], common: Mapping[str, Any], by_sku: Mapping[str, Any],
                         submitted: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only the server's re-derived source candidate can acquire a source badge."""
    from .listing_autofill import source_attribute_candidates

    proposed = submitted or {}
    if not isinstance(proposed, Mapping) or set(proposed) - {"attributes", "per_sku_attributes"}:
        raise ValueError("属性来源格式不正确")
    for section in ("attributes", "per_sku_attributes"):
        if not isinstance(proposed.get(section, {}), Mapping):
            raise ValueError("属性来源必须按字段和 SKU 区分")
    if set(proposed.get("attributes", {})) - set(common):
        raise ValueError("来源标记不能包含未保存的属性")
    if set(proposed.get("per_sku_attributes", {})) - set(by_sku):
        raise ValueError("来源标记只能对应当前保存的真实 SKU")
    for sku_id, meta in proposed.get("per_sku_attributes", {}).items():
        if not isinstance(meta, Mapping) or set(meta) - set(by_sku[sku_id]):
            raise ValueError("SKU 来源标记包含未保存的属性")
    original = _saved_provenance(confirmations, _as_values(confirmations.get("attributes")),
                                 {key: _as_values(value) for key, value in (confirmations.get("sku_attributes") or {}).items()})
    candidates = None
    default_candidates = None

    def verify(key: str, values: Any, sku_id: str | None) -> dict[str, Any]:
        nonlocal candidates, default_candidates
        explicit = ((proposed.get("per_sku_attributes", {}).get(sku_id) or {}).get(key) if sku_id else
                    proposed.get("attributes", {}).get(key))
        old_meta = ((original["per_sku_attributes"].get(sku_id) or {}).get(key) if sku_id else original["attributes"].get(key))
        old_values = ((confirmations.get("sku_attributes", {}).get(sku_id) or {}).get(key) if sku_id else
                      confirmations.get("attributes", {}).get(key))
        metadata = explicit
        if metadata is None and old_values == values and confirmations.get("category_form_scope") == form["scope"]:
            metadata = old_meta
        if metadata is None:
            return _manual_meta(values)
        if not isinstance(metadata, Mapping):
            raise ValueError("属性来源必须为对象")
        if metadata.get("source") in (None, "manual"):
            return _manual_meta(values)
        if not values:
            raise ValueError("清空的属性不能标记为采集确认值")
        if metadata.get("source") in {"user_requested_default", "ai_generated_copy"}:
            if default_candidates is None:
                default_candidates = source_attribute_candidates(directory, cache_root, form["shop_id"], include_defaults=True)
            expected = default_candidates
        else:
            if candidates is None:
                candidates = source_attribute_candidates(directory, cache_root, form["shop_id"])
            expected = candidates
        expected_values = ((expected["per_sku_attributes"].get(sku_id) or {}).get(key) if sku_id else
                           expected["attributes"].get(key))
        expected_meta = ((expected["provenance"]["per_sku_attributes"].get(sku_id) or {}).get(key) if sku_id else
                         expected["provenance"]["attributes"].get(key))
        if sku_id and expected_values is None:
            # A common collected fact can be installed individually for the
            # unaffected SKUs when another SKU was deliberately cleared.
            expected_values = expected["attributes"].get(key)
            expected_meta = expected["provenance"]["attributes"].get(key)
        if expected_values != values or not expected_meta or metadata.get("source") != expected_meta["source"]:
            # A stale stored source badge can degrade to manual on an unchanged
            # value; new client assertions must not fabricate evidence.
            if explicit is None:
                return _manual_meta(values)
            raise ValueError(f"属性 {key} 的采集来源与当前真实采集数据不一致")
        return dict(expected_meta)

    return {"attributes": {key: verify(key, values, None) for key, values in common.items()},
            "per_sku_attributes": {sku_id: {key: verify(key, values, sku_id) for key, values in attributes.items()}
                                   for sku_id, attributes in by_sku.items()}}


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
    from .listing_defaults import field_display_metadata
    return {"form": form, "attributes": common, "per_sku_attributes": by_sku,
            "selected_skus": skus, "missing_required": missing, "missing_by_sku": missing_by_sku,
            "provenance": _saved_provenance(confirmations, common, by_sku),
            "field_display": field_display_metadata(form, directory=directory, attributes=common, per_sku_attributes=by_sku),
            "validation_errors": validation_errors,
            "api_writes_performed": False}


@serialized_product_edit
def save_product_form(directory: Path, cache_root: Path, *, shop_id: str | None,
                      category_id: int, type_id: int, attributes: Mapping[str, Any],
                      per_sku_attributes: Mapping[str, Any] | None = None,
                      provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    # The API-level gate is not enough: direct callers and in-flight submissions
    # must be checked after taking the product lock, before any product writes.
    _require_editable(directory)
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
    verified_provenance = _verified_provenance(directory, cache_root, form, confirmations, common, by_sku, provenance)
    confirmations.update(attributes=common, sku_attributes=by_sku,
                         category_form_scope=form["scope"], category_form_confirmed_at=form["fetched_at"],
                         attribute_provenance=verified_provenance)
    from .guided_review import invalidate_from
    from .catalog import handle_field_completion
    from .context import StepContext

    # Metadata loading can wait on Ozon; recheck the submission gate and category
    # binding immediately before committing the local draft.
    _require_editable(directory)
    latest_selection = _selection(directory, shop_id)
    if any(latest_selection.get(key) != selection.get(key)
           for key in ("category_id", "type_id", "shop_id")):
        raise ValueError("类目已变更，请重新读取表单，不能保存旧类目属性")
    with product_file_transaction(directory, FORM_TRANSACTION_FILES):
        # invalidate_from repeats the no-return gate before its first write.
        # Any later persistence/compilation error restores this entire group.
        invalidate_from(directory, "field_completion")
        persist_category_form(directory, form)
        write_json(target, confirmations)
        handle_field_completion(StepContext(directory, "field_completion"))
    skus = selected_skus(directory)
    missing, missing_by_sku = _missing(form, common, by_sku, skus)
    from .listing_defaults import field_display_metadata
    return {"form": form, "attributes": common, "per_sku_attributes": by_sku,
            "selected_skus": skus, "missing_required": missing, "missing_by_sku": missing_by_sku,
            "provenance": verified_provenance,
            "field_display": field_display_metadata(form, directory=directory, attributes=common, per_sku_attributes=by_sku),
            "warnings": common_report["warnings"], "compiled": read_json(directory / "output/ozon-attributes-final.json"),
            "api_writes_performed": False}
