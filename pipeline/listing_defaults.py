"""User-selected listing defaults, kept separate from collected product facts.

Read helpers never create a model name or contact Ozon. The explicit persist
operation records one random merge-model per captured product, reads only a
small number of official dictionary options, and uses the normal form saver.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import category_form
from .listing_form import read_json, write_json, _require_editable
from .product_edit_lock import serialized_product_edit

DEFAULT_STATE_FILE = "input/listing-defaults.json"
_KNOWN_IDS = {85: "brand", 9048: "model", 23171: "hashtags"}


def _key(value: Any) -> str:
    return re.sub(r"[\s_,，:：()（）\[\]【】*＊#＃]+", "",
                  unicodedata.normalize("NFKC", str(value)).casefold().strip())


_ALIASES = {
    "brand": ("品牌", "商标", "brand", "бренд"),
    "model": ("型号名称", "型号名称（针对合并为一张商品卡片）", "型号", "model", "model_name", "название модели", "название модели (для объединения в одну карточку)"),
    "statistical_quantity": ("统一计量单位中的商品数量", "统一计量单位的商品数量", "количество товара в единице измерения", "количество товара в одной единице"),
    "adult": ("签名18+", "标记18+", "标签18+", "18+", "признак 18+", "признак18+", "adult", "is_adult"),
    "marking_code": ("标记代码", "需要标记代码", "маркировочный код", "код маркировки", "требуется маркировка"),
    "origin": ("原产国", "原产国家", "原产地", "生产国家", "страна-изготовитель", "страна изготовитель", "страна производства", "country_of_origin", "origin_country"),
    "warranty": ("保证", "保修", "保修期", "质保", "质保期", "гарантия", "гарантийный срок"),
    # This is an optional characteristic, NOT the required top-level offer_id.
    "seller_code": ("卖家代码", "код продавца", "seller_code"),
    "similar_products": ("组合成类似的产品", "组合类似商品", "合并相似商品", "объединить в похожие товары", "объединить в похожие продукты"),
    "hs_code": ("欧亚经济联盟的HS编码", "欧亚经济联盟HS编码", "код тн вэд еаэс", "тн вэд еаэс", "еаэс тн вэд"),
    "factory_pack_count": ("原厂包装数量", "原厂包装数量，个", "原始包装数量", "количество в заводской упаковке", "количество в заводской упаковке, шт"),
    "shelf_life": ("保质期", "保质期（天）", "保质期，天", "срок годности", "срок годности в днях", "срок годности, дни"),
    "hashtags": ("主题标签", "标签", "хештеги", "hashtags"),
}
_ALIAS_INDEX = {_key(alias): name for name, aliases in _ALIASES.items() for alias in aliases}
_BLANK = {"statistical_quantity", "marking_code", "warranty", "seller_code", "similar_products", "hs_code", "shelf_life"}
_NO_BRAND = {"无品牌", "无", "no brand", "нет бренда", "без бренда"}
_CHINA = {"中国", "中华人民共和国", "china", "китай", "китайская народная республика", "cn"}
_UNKNOWN = {"", "unknown", "未知", "不详", "待确认", "暂无", "null", "none", "n/a", "-", "--", "—", "/"}


def field_default_semantic(field: Mapping[str, Any]) -> str | None:
    known = _KNOWN_IDS.get(int(field.get("attribute_id") or 0))
    if known:
        return known
    return _ALIAS_INDEX.get(_key(field.get("name") or field.get("attribute_name") or ""))


@serialized_product_edit
def ensure_defaults_state(directory: Path | str) -> dict[str, Any]:
    """Generate a random, stable shared model only during an explicit mutation."""
    directory = Path(directory)
    _require_editable(directory)
    target = directory / DEFAULT_STATE_FILE
    state = read_json(target)
    if state.get("product_id") == directory.name and re.fullmatch(r"WB-[A-F0-9]{12}", str(state.get("model_name") or "")):
        return state
    state = {"schema_version": "1.0.0", "product_id": directory.name,
             "model_name": "WB-" + uuid.uuid4().hex[:12].upper(),
             "source": "user_requested_default", "created_at": datetime.now(timezone.utc).isoformat()}
    write_json(target, state)
    return state


def field_display_metadata(form: Mapping[str, Any], *, directory: Path | str | None = None,
                           attributes: Mapping[str, Any] | None = None,
                           per_sku_attributes: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """UI policy by real field ID; leave the official schema entirely unchanged."""
    result = {}
    facts = _source_facts(Path(directory)) if directory is not None else {}
    for field in form.get("fields") or []:
        semantic = field_default_semantic(field)
        if not semantic:
            continue
        display = "hidden" if semantic == "brand" else "advanced" if semantic in _BLANK else "standard"
        if semantic == "brand":
            key = str(field["attribute_id"])
            def records(value):
                if value in (None, ""):
                    return []
                return [row if isinstance(row, Mapping) else {"value": row}
                        for row in (value if isinstance(value, list) else [value])]
            saved_values = [*records((attributes or {}).get(key)),
                            *(row for values in (per_sku_attributes or {}).values() for row in records(values.get(key)))]
            if _conflicts("brand", facts) or any(
                str(row.get("value") or "").strip().casefold() not in _NO_BRAND
                for row in saved_values if isinstance(row, Mapping) and row.get("value") not in (None, "")
            ):
                display = "attention"
        result[str(field["attribute_id"])] = {
            "semantic": semantic, "display": display, "hide_when_default": semantic == "brand",
            "default_policy": "leave_blank" if semantic in _BLANK else "ai_generated" if semantic == "hashtags" else "user_requested_default",
            "required": bool(field.get("required")),
        }
    return result


def _source_facts(directory: Path) -> dict[str, list[Any]]:
    from .listing_autofill import _source_rows, _sku_rows
    source, rows = _source_rows(directory)
    for sku in source.get("skus") or []:
        if isinstance(sku, Mapping):
            rows.extend(_sku_rows(sku))
    # These fields are not in the older basic-facts allowlist.
    for record in (source, read_json(directory / "input/raw-snapshot.json").get("raw") or {}):
        if isinstance(record, Mapping):
            for name, value in record.items():
                if _ALIAS_INDEX.get(_key(name)) in {"brand", "origin", "adult", "factory_pack_count"}:
                    rows.append({"name": name, "value": value})
    result: dict[str, list[Any]] = {}
    for row in rows:
        semantic = _ALIAS_INDEX.get(_key(row["name"]))
        value = row["value"]
        if semantic and not isinstance(value, (Mapping, list, tuple)) and str(value).strip().casefold() not in _UNKNOWN:
            result.setdefault(semantic, []).append(value)
    return result


def _conflicts(semantic: str, facts: Mapping[str, list[Any]]) -> bool:
    for value in facts.get(semantic, []):
        normalized = str(value).strip().casefold()
        if semantic == "brand" and normalized not in _NO_BRAND:
            return True
        if semantic == "origin" and normalized not in _CHINA:
            return True
        if semantic == "adult" and normalized not in {"false", "否", "无", "нет", "no", "0"}:
            return True
        if semantic == "factory_pack_count" and not re.fullmatch(r"1\s*(?:个|件|шт\.?)?", normalized):
            return True
    return False


def _confirmed_tags(directory: Path) -> tuple[str, str] | None:
    from .guided_workflow import workflow_status
    from .ozon_write import normalize_hashtags
    try:
        copy = workflow_status(directory)["copy"]
        if not copy.get("confirmed"):
            return None
        tags = normalize_hashtags((copy.get("payload") or {}).get("hashtags"))
        return (tags, str(copy["fingerprint"])) if tags else None
    except (KeyError, ValueError, RuntimeError):
        return None


def _is_manual(saved: Mapping[str, Any], meta: Mapping[str, Any], key: str) -> bool:
    if key not in saved:
        return False
    return not saved[key] or (meta.get(key) or {}).get("source") not in {"user_requested_default", "ai_generated_copy"}


def apply_user_defaults(directory: Path | str, cache_root: Path | str, form: Mapping[str, Any],
                        autofill_result: Mapping[str, Any], *, resolve_dictionaries: bool = False,
                        ignore_saved: bool = False, max_dictionary_lookups: int = 6) -> dict[str, Any]:
    """Merge policy suggestions; cached-only on GET and never fabricate dictionary IDs."""
    from .listing_autofill import _receipt_options
    directory, cache_root = Path(directory), Path(cache_root)
    result = deepcopy(dict(autofill_result))
    result.setdefault("attributes", {})
    result.setdefault("per_sku_attributes", {})
    provenance = result.setdefault("provenance", {})
    provenance.setdefault("attributes", {})
    provenance.setdefault("per_sku_attributes", {})
    result.setdefault("unresolved", [])
    result.setdefault("lookup_count", 0)
    result["defaults_policy"] = "user_requested"
    state = read_json(directory / DEFAULT_STATE_FILE)
    model = state.get("model_name") if state.get("product_id") == directory.name else None
    if not re.fullmatch(r"WB-[A-F0-9]{12}", str(model or "")):
        model = None
    saved = {} if ignore_saved else read_json(directory / "input/human-confirmations.json")
    common_saved = saved.get("attributes") or {}
    sku_saved = saved.get("sku_attributes") or {}
    saved_meta = saved.get("attribute_provenance") or {}
    common_meta = saved_meta.get("attributes") or {}
    sku_meta = saved_meta.get("per_sku_attributes") or {}
    result["field_display"] = field_display_metadata(form, directory=directory, attributes=common_saved,
                                                     per_sku_attributes=sku_saved)
    facts = _source_facts(directory)
    tags = _confirmed_tags(directory)

    for field in form.get("fields") or []:
        key = str(field["attribute_id"])
        semantic = field_default_semantic(field)
        if not semantic:
            continue
        if semantic in _BLANK:
            # Default blank means no automatic proposal. An existing human
            # value is never erased and a currently required field stays required.
            result["attributes"].pop(key, None)
            provenance["attributes"].pop(key, None)
            for sku_id, values in result["per_sku_attributes"].items():
                values.pop(key, None)
                provenance["per_sku_attributes"].get(sku_id, {}).pop(key, None)
            continue
        if semantic not in {"brand", "model", "origin", "adult", "factory_pack_count", "hashtags"}:
            continue
        if _is_manual(common_saved, common_meta, key):
            continue
        if semantic != "model" and _conflicts(semantic, facts):
            result["unresolved"].append({"attribute_id": int(key), "reason": "采集资料与用户默认值存在冲突，已保留真实资料，请人工核对", "source": "user_requested_default"})
            result["field_display"][key]["display"] = "attention"
            continue
        if semantic == "model":
            # This is Ozon's merge/model-name field, not a claim that the
            # supplier manufactured a particular hardware model. The user
            # explicitly chose one generated grouping name for every variant.
            result["attributes"].pop(key, None)
            provenance["attributes"].pop(key, None)
            for sku_id, proposed in result["per_sku_attributes"].items():
                proposed.pop(key, None)
                provenance["per_sku_attributes"].get(sku_id, {}).pop(key, None)
        # Verified brand/origin/age/pack facts win over defaults. No automatic
        # proposal replaces an explicit human value or clear.
        if key in result["attributes"] or any(key in values for values in result["per_sku_attributes"].values()):
            continue
        if not field.get("editable", True) or field.get("complex_id") or field.get("attribute_complex_id"):
            continue
        value: Any = {"brand": "Нет бренда", "origin": "中国", "adult": False,
                      "factory_pack_count": 1, "model": model}.get(semantic)
        source = "user_requested_default"
        evidence = "用户指定默认值；不是 AI 推断或供应商确认资料"
        extra_meta = {}
        if semantic == "hashtags":
            if not tags:
                continue
            value, fingerprint = tags
            source = "ai_generated_copy"
            evidence = "已选择并确认的俄文文案标签"
            extra_meta["copy_fingerprint"] = fingerprint
        if value is None:
            if semantic == "model":
                result["unresolved"].append({"attribute_id": int(key), "reason": "确认类目或准备卡片时将生成并保存同一商品的共享型号", "source": source})
            continue
        candidate = [{"value": value}]
        if field.get("dictionary_id"):
            aliases = _NO_BRAND if semantic == "brand" else _CHINA if semantic == "origin" else {str(value).casefold()}

            def match():
                options = [row for row in _receipt_options(cache_root, form, field)
                           if str(row["value"]).strip().casefold() in aliases]
                return options[0] if len(options) == 1 else None

            option = match()
            if option is None and resolve_dictionaries and result["lookup_count"] < max_dictionary_lookups:
                result["lookup_count"] += 1
                try:
                    category_form.dictionary_values(cache_root, int(form["category_id"]), int(form["type_id"]), int(key),
                                                    shop_id=form["shop_id"], q=str(value), limit=50)
                except (ValueError, OSError, TimeoutError, RuntimeError):
                    pass
                option = match()
            if option is None:
                result["unresolved"].append({"attribute_id": int(key), "reason": "用户默认值尚未匹配当前类目的唯一官方选项，请核对并补齐官方选项", "source": source})
                continue
            candidate = [option]
        try:
            values = category_form.validate_attributes(form, {key: candidate}, cache_root=cache_root)["attributes"].get(key)
        except ValueError:
            result["unresolved"].append({"attribute_id": int(key), "reason": "用户默认值与当前类目的官方字段类型不一致，请人工核对", "source": source})
            continue
        if not values:
            continue
        meta = {"source": source, "evidence": evidence, "status": "default" if source == "user_requested_default" else "copy_confirmed", **extra_meta}
        blockers = {sku_id for sku_id, values_by_field in sku_saved.items()
                    if _is_manual(values_by_field, sku_meta.get(sku_id) or {}, key)}
        if blockers:
            for sku in result.get("seller_offer_ids") or {}:
                if sku not in blockers:
                    result["per_sku_attributes"].setdefault(sku, {})[key] = values
                    provenance["per_sku_attributes"].setdefault(sku, {})[key] = meta
        else:
            result["attributes"][key] = values
            provenance["attributes"][key] = meta
    return result


@serialized_product_edit
def persist_user_defaults(directory: Path | str, cache_root: Path | str, shop_id: str | None = None,
                          resolve_dictionaries: bool = True) -> dict[str, Any]:
    """Explicit safe local mutation: defaults + collected proposals through validator."""
    from .listing_autofill import build_autofill
    from .listing_form import product_form, save_product_form, _saved_provenance, _as_values
    directory, cache_root = Path(directory), Path(cache_root)
    current = product_form(directory, cache_root, shop_id=shop_id)
    ensure_defaults_state(directory)
    proposal = build_autofill(directory, cache_root, shop_id=shop_id,
                              resolve_dictionaries=resolve_dictionaries, include_defaults=True)
    common = {**current["attributes"], **proposal["attributes"]}
    per_sku = deepcopy(current["per_sku_attributes"])
    for sku_id, values in proposal["per_sku_attributes"].items():
        per_sku[sku_id] = {**per_sku.get(sku_id, {}), **values}
    saved = read_json(directory / "input/human-confirmations.json")
    meta = _saved_provenance(saved, _as_values(current["attributes"]), current["per_sku_attributes"])
    meta["attributes"].update(proposal["provenance"]["attributes"])
    for sku_id, values in proposal["provenance"]["per_sku_attributes"].items():
        meta["per_sku_attributes"].setdefault(sku_id, {}).update(values)
    if common == current["attributes"] and per_sku == current["per_sku_attributes"]:
        return {**current, "defaults": proposal, "field_display": proposal.get("field_display") or {}}
    form = current["form"]
    result = save_product_form(directory, cache_root, shop_id=form["shop_id"],
                               category_id=form["category_id"], type_id=form["type_id"],
                               attributes=common, per_sku_attributes=per_sku, provenance=meta)
    return {**result, "defaults": proposal, "field_display": proposal.get("field_display") or {}}
