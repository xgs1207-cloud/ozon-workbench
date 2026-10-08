"""A read-only product document shared by summary, copy, and listing-card UI."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .listing_form import read_json, _as_values, _saved_provenance, _missing
from .listing_offer_ids import read_offer_ids

_MISSING_COLOUR = {"", "unknown", "未指定", "未确认", "待确认", "无", "не указан", "не указана", "не указано", "not specified", "none", "-", "—"}
_COLOR_NAMES = {"цвет", "цвет товара", "название цвета", "颜色", "商品颜色", "颜色名称", "color", "colour", "color_name"}
_MISSING_SUFFIX = re.compile(r"\s+(?:—|–|-)\s*(?:未指定|未确认|не указан(?:а|о)?|not specified|unknown)\s*$", re.IGNORECASE)


def color_is_real(value: Any) -> bool:
    return value is not None and str(value).strip().casefold() not in _MISSING_COLOUR


def variant_color(sku: Mapping[str, Any], attributes: Sequence[Mapping[str, Any]] = ()) -> str | None:
    for item in attributes:
        if (int(item.get("attribute_id") or 0) in {10096, 10097}
                or str(item.get("attribute_name") or item.get("name") or "").strip().casefold() in _COLOR_NAMES):
            if color_is_real(item.get("value")):
                return str(item["value"]).strip()
    for key in ("color_ru", "color", "color_cn", "color_zh"):
        if color_is_real(sku.get(key)):
            return str(sku[key]).strip()
    for item in sku.get("option_values") or []:
        if isinstance(item, Mapping) and str(item.get("name_cn") or item.get("name") or "").strip().casefold() in _COLOR_NAMES:
            value = item.get("value_cn", item.get("value"))
            if color_is_real(value):
                return str(value).strip()
    return None


def clean_missing_color_suffix(title: Any, *, has_real_color: bool = False) -> str:
    """Remove only the generated missing-colour suffix, never a title word."""
    text = str(title or "").strip()
    # A stale placeholder is not made meaningful by subsequently discovering
    # a colour. Keep the keyword argument for existing callers, but always
    # remove this exact generated suffix before adding any real colour.
    return _MISSING_SUFFIX.sub("", text).strip()


def variant_title(title: str, sku: Mapping[str, Any], attributes: Sequence[Mapping[str, Any]] = (), *,
                  core_keyword: str | None = None, confirmed_title_ru: str | None = None) -> str:
    """Publish approved copy unchanged, never a raw supplier name/color suffix."""
    from rules.validate import keyword_phrase_present, validate_title_ru
    base = clean_missing_color_suffix(title)
    # Raw capture keys are not human confirmation. A trusted editor caller may
    # explicitly supply an independently confirmed title, never a supplier flag.
    if confirmed_title_ru is not None:
        candidate = clean_missing_color_suffix(confirmed_title_ru)
        if validate_title_ru(candidate, max_length=200):
            raise ValueError("人工确认的逐规格标题不符合文案要求")
        if core_keyword and not keyword_phrase_present(candidate, core_keyword, at_start=True):
            raise ValueError("逐规格标题必须以主关键词完整短语开头")
        return candidate
    if core_keyword and base and not keyword_phrase_present(base, core_keyword, at_start=True):
        raise ValueError("上架标题必须以主关键词完整短语开头")
    return base


def read_listing_document(directory: Path | str, *, shop: str | None = None,
                          cache_root: Path | str | None = None,
                          offer_db_path: Path | str | None = None) -> dict[str, Any]:
    """Local projections only; no Ozon metadata refresh, AI calls, or file writes."""
    from .guided_workflow import workflow_status
    from .copy_evidence import verified_copy_facts
    from .image_insights import read_image_insights
    from .listing_defaults import field_display_metadata
    from .sku_selection import active_skus
    from .selected_source import selected_source
    from .summary_display import read_summary_display

    directory = Path(directory)
    source = read_json(directory / "input/source.json")
    skus = active_skus(directory, source.get("skus") or [])
    workflow = workflow_status(directory)
    analysis = workflow.get("analysis") or {}
    copy = workflow.get("copy") or {}
    selection = read_json(directory / "input/category-selection.json")
    bound_shop = selection.get("shop_id") or selection.get("shop")
    if shop and bound_shop and shop != bound_shop:
        raise ValueError("商品资料目标店铺与已确认的官方类目不一致")
    shop = shop or bound_shop
    form = read_json(directory / "input/category-form.json")
    valid_form = bool(form.get("source") == "ozon_seller_api" and form.get("category_id") == selection.get("category_id")
                      and form.get("type_id") == selection.get("type_id") and form.get("shop_id") == shop)
    fields = form.get("fields") or [] if valid_form else []
    confirmations = read_json(directory / "input/human-confirmations.json")
    common = _as_values(confirmations.get("attributes"))
    per_sku = {key: _as_values(values) for key, values in (confirmations.get("sku_attributes") or {}).items()}
    missing, missing_by_sku = _missing({"fields": fields}, common, per_sku, [{"sku_id": str(sku["sku_id"])} for sku in skus])
    labels = {int(field["attribute_id"]): field["name"] for field in fields}
    validation_errors = []
    if cache_root is not None and valid_form:
        from .category_form import validate_attributes
        for label, values in [("商品共用", common), *[(str(sku["sku_id"]), per_sku.get(str(sku["sku_id"]), {})) for sku in skus]]:
            try:
                validate_attributes(form, values, cache_root=cache_root)
            except ValueError as error:
                validation_errors.append(f"{label}：{error}")
    if validation_errors:
        missing = [int(field["attribute_id"]) for field in fields if field.get("required")]
    copy_payload = copy.get("payload") or {}
    scoped = selected_source(directory, source, require_selection=False)
    facts = verified_copy_facts(analysis.get("payload") or {}, scoped) if analysis.get("confirmed") else []
    insights = read_image_insights(directory)
    from .product_editor import read_listing_details
    details_report = read_listing_details(directory)
    details = details_report["details"]
    prices = read_json(directory / "input/manual-prices.json").get("prices") or {}
    offers = read_offer_ids(directory, shop, db_path=offer_db_path) if shop else {
        "items": [], "offers": {}, "complete": False, "readonly": True}
    selected_rows = []
    for sku in skus:
        sku_id = str(sku["sku_id"])
        sku_attributes = []
        for key, values in {**common, **per_sku.get(sku_id, {})}.items():
            for value in values:
                sku_attributes.append({"attribute_id": int(key), "attribute_name": labels.get(int(key), ""), **value})
        selected_rows.append({"source_sku_id": sku_id, "sku_id": sku_id,
                              "name": sku.get("name_zh") or sku.get("sku_name") or sku.get("name") or sku.get("spec_text") or sku_id,
                              "offer_id": offers["offers"].get(sku_id), "color": variant_color(sku, sku_attributes),
                              "title_ru": variant_title(str(copy_payload.get("title_ru") or ""), sku, sku_attributes,
                                                        core_keyword=copy_payload.get("core_keyword") if copy.get("selected") else None),
                              "manual_price": prices.get(sku_id), "purchase_price_cny": sku.get("purchase_price_cny"),
                              "option_values": [{"name": item.get("name_cn") or item.get("name"),
                                                 "value": item.get("value_cn", item.get("value"))}
                                                for item in sku.get("option_values") or [] if isinstance(item, Mapping)]})
    title = clean_missing_color_suffix(copy_payload.get("title_ru"), has_real_color=any(row["color"] for row in selected_rows))
    package = {axis: details.get(f"package_{axis}") for axis in ("length_mm", "width_mm", "height_mm", "weight_g")}
    product = {axis: details.get(f"product_{axis}") for axis in ("length_mm", "width_mm", "height_mm", "weight_g")}
    operational_missing = [{"key": f"package_{axis}", "name": label} for axis, label in
                           (("length_mm", "包装长度，毫米"), ("width_mm", "包装宽度，毫米"), ("height_mm", "包装高度，毫米"), ("weight_g", "含包装重量，克"))
                           if not isinstance(package[axis], int) or package[axis] <= 0]
    for row in selected_rows:
        price = row["manual_price"] or {}
        try:
            positive = float(price.get("price") or 0) > 0
        except (TypeError, ValueError):
            positive = False
        if not positive:
            operational_missing.append({"key": "manual_price", "source_sku_id": row["source_sku_id"], "name": "逐规格售价"})
    # The model name is only edited in the official attributes, not the
    # operational panel: it is not a duplicate and must stay visible there.
    duplicates = {85: "brand", 4191: "description_ru", 23171: "hashtags"}
    operational_names = {
        "包装长度，毫米": "package_length_mm", "包装宽度，毫米": "package_width_mm", "包装高度，毫米": "package_height_mm",
        "含包装重量，克": "package_weight_g", "包装重量，克": "package_weight_g", "简介": "description_ru", "商品简介": "description_ru",
        "货号": "offer_id", "非促销最高价格": "manual_price", "划线价": "old_price",
    }
    for field in fields:
        name = str(field.get("name") or "").strip().replace(",", "，")
        if name in operational_names:
            duplicates[int(field["attribute_id"])] = operational_names[name]
    excluded = [{"attribute_id": key, "name": labels[key], "operational_key": value}
                for key, value in duplicates.items() if key in labels]
    return {"schema_version": "1.0.0", "product_id": directory.name, "shop": shop,
            "source_title": str(source.get("title_zh") or ""), "selected_skus": selected_rows,
            "summary": {"status": analysis.get("status", "missing"), "confirmed": bool(analysis.get("confirmed")),
                        "input_fingerprint": analysis.get("fingerprint"), "payload": analysis.get("payload") or {},
                        "display_zh": read_summary_display(directory, payload=analysis.get("payload") or {})},
            "facts": facts, "image_suggestions": {"status": insights["status"], "payload": insights.get("payload"),
                                                    "advisory_only": True, "automatic_fact_updates": False,
                                                    "warning_zh": insights["warning_zh"]},
            "copy": {"status": copy.get("status", "missing"), "selected": bool(copy.get("selected")),
                     "confirmed": bool(copy.get("confirmed")), "input_fingerprint": copy.get("fingerprint"),
                     "title_ru": title, "description_ru": str(copy_payload.get("description_ru") or ""),
                     "hashtags": list(copy_payload.get("hashtags") or []), "candidate_id": copy_payload.get("candidate_id")},
            "card": {"category": selection, "form": form if valid_form else None, "attributes": common,
                     "per_sku_attributes": per_sku, "details": details, "manual_prices": prices,
                     "title_ru": title, "description_ru": str(copy_payload.get("description_ru") or ""),
                     "hashtags": list(copy_payload.get("hashtags") or []),
                     "field_display": field_display_metadata(form, directory=directory, attributes=common, per_sku_attributes=per_sku) if valid_form else {},
                     "provenance": _saved_provenance(confirmations, common, per_sku)},
            "operational_fields": {"offer_ids": offers["offers"], "manual_prices": prices, "package": package, "product": product,
                                   "title_ru": title, "description_ru": str(copy_payload.get("description_ru") or ""),
                                   "hashtags": list(copy_payload.get("hashtags") or []), "provenance": details_report["provenance"],
                                   "required_package_keys": ["length_mm", "width_mm", "height_mm", "weight_g"], "product_optional": True},
            "official_excluded_attribute_ids": [row["attribute_id"] for row in excluded],
            "official_excluded_attributes": excluded,
            "offer_ids": offers,
            "missing": {"official_form_missing": not valid_form,
                        "required_attribute_ids": missing, "required_attributes": [{"attribute_id": key, "name": labels.get(key, str(key))} for key in missing],
                        "by_sku": missing_by_sku, "validation_errors": validation_errors,
                        "operational": operational_missing,
                        "offer_ids": [row["source_sku_id"] for row in selected_rows if not row["offer_id"]]},
            "preparation_blockers": workflow.get("preparation_blockers") or [],
            "publication_blockers": workflow.get("publication_blockers") or [],
            "readonly": True, "model_calls": 0, "api_calls": 0, "api_writes_performed": False}
