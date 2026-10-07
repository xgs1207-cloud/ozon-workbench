"""Evidence-only listing suggestions: no AI, product writes or guessed facts.

GET uses only fresh local official metadata and dictionary receipts. The optional
resolve pass reads at most six dictionary pages/searches, never whole dictionaries.
Returned values are drafts; the normal listing-form validator still guards saves.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from . import category_form
from .listing_form import read_json
from .sku_selection import active_skus
from .upload import offer_id_for

MAX_DICTIONARY_LOOKUPS = 6
_MISSING = object()


def _key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return re.sub(r"[\s_，,:：()（）\[\]【】*＊]+", "", text)


_ALIASES = {
    "material": ("材质", "材料", "面料", "材质成分", "material", "materials", "материал"),
    "brand": ("品牌", "商标", "brand", "бренд"),
    "model": ("型号", "型号名称", "model", "model_name", "название модели", "型号名称（针对合并为一张商品卡片）"),
    "color": ("颜色", "商品颜色", "color", "colour", "color_cn", "color_zh", "color_ru", "цвет товара", "цвет"),
    "color_name": ("颜色名称", "color_name", "название цвета"),
    "package_quantity": ("包装数量", "每包数量", "单包数量", "package_quantity", "pack_quantity", "统一计量单位中的商品数量", "количество в упаковке"),
    "product_weight_g": ("重量", "单品重量", "产品重量", "商品重量", "净重", "weight_g", "product_weight_g", "вес товара", "вес"),
    "package_weight_g": ("含包装重量", "包装重量", "单件含包装重量", "单件毛重", "package_weight_g", "вес с упаковкой"),
}
for _prefix, _cn in (("product", "产品"), ("package", "包装")):
    for _axis, _cn_axis, _ru in (("length", "长度", "длина"), ("width", "宽度", "ширина"), ("height", "高度", "высота")):
        _ALIASES[f"{_prefix}_{_axis}_mm"] = (
            f"{_prefix}_{_axis}_mm", f"{_prefix}_{_axis}", f"{_cn}{_cn_axis}",
            f"{_cn}{_cn_axis}mm", f"{_cn}{_cn_axis}毫米", f"{_cn}{_cn_axis}cm", f"{_cn}{_cn_axis}厘米",
            *(() if _prefix == "package" else (_cn_axis, f"{_cn_axis}mm", f"{_cn_axis}cm", _ru,
                f"商品{_cn_axis}", f"商品{_cn_axis}mm", f"商品{_cn_axis}cm")),
            *(() if _prefix == "product" else (f"{_ru} упаковки",)),
        )
_ALIAS_INDEX = {_key(alias): field for field, aliases in _ALIASES.items() for alias in aliases}
_UNKNOWN = {"", "unknown", "未知", "不详", "待确认", "暂无", "null", "none", "n/a", "-", "--", "—", "/"}
_COLOUR_GROUPS = (
    ("红", "红色", "red", "красный"), ("蓝", "蓝色", "blue", "синий"),
    ("绿", "绿色", "green", "зелёный", "зеленый"), ("白", "白色", "white", "белый"),
    ("黑", "黑色", "black", "чёрный", "черный"), ("黄", "黄色", "yellow", "жёлтый", "желтый"),
    ("粉", "粉色", "粉红色", "pink", "розовый"), ("紫", "紫色", "purple", "фиолетовый"),
    ("灰", "灰色", "gray", "grey", "серый"), ("橙", "橙色", "orange", "оранжевый"),
    ("透明", "transparent", "прозрачный"),
)
_MATERIAL_GROUPS = (
    ("棉", "cotton", "хлопок"),
    ("硅胶", "silicone", "силикон"), ("不锈钢", "stainless steel", "нержавеющая сталь"),
    ("塑料", "plastic", "пластик"), ("聚酯纤维", "涤纶", "polyester", "полиэстер"),
)


def _present(record: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return _MISSING


def _usable(value: Any) -> bool:
    if value is _MISSING or value is None or isinstance(value, Mapping):
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in _UNKNOWN and len(value) <= 2000
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_usable(item) and not isinstance(item, (list, tuple)) for item in value)
    return isinstance(value, (bool, int, float)) and (not isinstance(value, float) or math.isfinite(value))


def _fact(name: Any, value: Any, path: str) -> dict[str, Any] | None:
    if not str(name).strip() or not _usable(value):
        return None
    clean = value.strip() if isinstance(value, str) else value
    return {"name": str(name).strip(), "value": clean, "source": "collected_product",
            "evidence": f"{path}：{name} = {clean}", "status": "confirmed"}


def _rows(block: Any, path: str) -> list[dict[str, Any]]:
    result = []
    if isinstance(block, Mapping):
        for name, value in block.items():
            row = _fact(name, value, f"{path}.{name}")
            if row:
                result.append(row)
    elif isinstance(block, list):
        for index, item in enumerate(block):
            if not isinstance(item, Mapping):
                continue
            name = _present(item, ("name_cn", "name", "key", "label", "attribute_name"))
            value = _present(item, ("value_cn", "value", "values"))
            if name is _MISSING:
                continue
            row = _fact(name, value, f"{path}[{index}]")
            if row:
                result.append(row)
    return result


def _source_rows(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = read_json(directory / "input/source.json")
    attributes = source.get("attributes_zh") or {}
    result = _rows(attributes.get("raw") if isinstance(attributes, Mapping) else attributes,
                   "input/source.json.attributes_zh.raw")
    if isinstance(attributes, Mapping):
        # Prefer preserved originals over lossy aliases; in particular the old
        # ingest alias "装箱数量" must not become a single retail pack count.
        originals = attributes.get("raw") if isinstance(attributes.get("raw"), Mapping) else {}
        for name, value in attributes.items():
            if name == "raw":
                continue
            canonical = _semantic(name)
            if any(_semantic(key) == canonical for key in originals) and canonical:
                continue
            if name == "package_quantity" and any("箱" in str(key) for key in originals):
                continue
            if name == "weight_g" and any(_key(key) in {_key("克重"), _key("毛重")} for key in originals):
                continue
            row = _fact(name, value, f"input/source.json.attributes_zh.{name}")
            if row:
                result.append(row)
    result.extend(_rows(source.get("product_attributes"), "input/source.json.product_attributes"))
    raw = read_json(directory / "input/raw-snapshot.json").get("raw") or {}
    if isinstance(raw, Mapping):
        result.extend(_rows(raw.get("product_attributes"), "input/raw-snapshot.json.raw.product_attributes"))
        result.extend(_rows(raw.get("attributes_zh"), "input/raw-snapshot.json.raw.attributes_zh"))
        # Only explicitly named scalar measurements; never wholesale price or
        # generic quantity/weight/unitless dimensions.
        for name in ("material_zh", "package_quantity", *[key for key in _ALIASES if key.startswith(("product_", "package_"))]):
            if name not in raw or isinstance(raw[name], Mapping):
                continue
            row = _fact("material" if name == "material_zh" else name, raw[name], f"input/raw-snapshot.json.raw.{name}")
            if row:
                result.append(row)
        result.extend(_dimension_blocks(raw, "input/raw-snapshot.json.raw"))
    for name in ("material", "package_quantity", *[key for key in _ALIASES if key.startswith(("product_", "package_"))]):
        if name in source:
            row = _fact(name, source[name], f"input/source.json.{name}")
            if row:
                result.append(row)
    result.extend(_dimension_blocks(source, "input/source.json"))
    return source, result


def _semantic(name: Any) -> str | None:
    exact = _ALIAS_INDEX.get(_key(name))
    if exact:
        return exact
    # Recognize units attached to known measured fields, not arbitrary similar
    # names such as fabric 克重 (g/m²), carton 毛重 or promotional 型号 claims.
    base = re.sub(r"\s*[,，(（]\s*(?:mm|cm|g|kg|毫米|厘米|克|千克|м[м]?|с[м]|г|кг)\s*[)）]?\s*$", "", str(name), flags=re.I)
    return _ALIAS_INDEX.get(_key(base))


def _units(text: str, *, weight: bool = False) -> str | None:
    vocabulary = r"kg|кг|千克|公斤|g|г|克" if weight else r"mm|毫米|мм|cm|厘米|см"
    matches = re.findall(rf"(?<![a-zа-я])({vocabulary})(?![a-zа-я])", text.casefold())
    normalized = {({"kg": "kg", "кг": "kg", "千克": "kg", "公斤": "kg", "g": "g", "г": "g", "克": "g"} if weight else
                   {"mm": "mm", "毫米": "mm", "мм": "mm", "cm": "cm", "厘米": "cm", "см": "cm"})[unit] for unit in matches}
    return next(iter(normalized)) if len(normalized) == 1 else None


def _measurement(row: Mapping[str, Any], semantic: str) -> int | None:
    value = row["value"]
    if isinstance(value, bool) or isinstance(value, (list, tuple)):
        return None
    weight = semantic.endswith("_g")
    inferred_unit = "g" if _key(row["name"]).endswith("g") and weight else "mm" if _key(row["name"]).endswith("mm") and not weight else None
    value_unit, name_unit = _units(str(value), weight=weight), _units(str(row["name"]), weight=weight)
    if value_unit and name_unit and value_unit != name_unit:
        return None
    unit = value_unit or name_unit or inferred_unit
    if not unit:
        return None
    number = re.fullmatch(r"\s*([+]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:kg|кг|千克|公斤|g|г|克|mm|毫米|мм|cm|厘米|см)?\s*", str(value), flags=re.I)
    if not number:
        return None
    converted = float(number.group(1)) * (1000 if unit == "kg" else 10 if unit == "cm" else 1)
    # The upload contract stores exact millimetres/grams as positive integers;
    # do not silently round a fractional millimetre or gram.
    return int(converted) if math.isfinite(converted) and converted > 0 and converted.is_integer() else None


def _quantity(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    match = re.fullmatch(r"\s*(\d+)\s*(?:件|个|只|pcs?|шт\.?)?\s*", str(value), flags=re.I)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _combined_measurements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        name = _key(row["name"])
        if name not in {_key(key) for key in ("尺寸", "产品尺寸", "商品尺寸", "包装尺寸", "产品尺寸长宽高", "包装尺寸长宽高")}:
            continue
        text = str(row["value"])
        unit = _units(text)
        if not unit:
            continue
        prefix = "package" if "包装" in name else "product"
        found = re.findall(r"(长度|长|宽度|宽|高度|高)\s*[:：=]?\s*([+]?(?:\d+(?:\.\d+)?|\.\d+))\s*(mm|cm|毫米|厘米)?", text, flags=re.I)
        if not found and "长宽高" in name:
            triple = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)\s*", text, flags=re.I)
            if triple:
                found = [(axis, triple.group(index), triple.group(4)) for index, axis in enumerate(("长", "宽", "高"), 1)]
        for axis, number, own_unit in found:
            semantic = f"{prefix}_{ {'长': 'length', '宽': 'width', '高': 'height'}[axis[0]] }_mm"
            parsed = _fact(semantic.removesuffix("_mm"), f"{number}{own_unit or unit}", row["evidence"])
            if parsed:
                result.append(parsed)
    return result


def _sku_rows(sku: Mapping[str, Any]) -> list[dict[str, Any]]:
    path = f"input/source.json.skus[{sku.get('sku_id')}]"
    rows = _rows(sku.get("option_values"), f"{path}.option_values")
    rows.extend(_rows(sku.get("attributes"), f"{path}.attributes"))
    rows.extend(_rows(sku.get("attributes_zh"), f"{path}.attributes_zh"))
    for name in ("color_ru", "color_cn", "color_zh", "color", "material", "brand", "model", "package_quantity",
                 *[key for key in _ALIASES if key.startswith(("product_", "package_"))]):
        if name in sku:
            row = _fact(name, sku[name], f"{path}.{name}")
            if row:
                rows.append(row)
    rows.extend(_dimension_blocks(sku, path))
    return rows + _combined_measurements(rows)


def _dimension_blocks(record: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    result = []
    for prefix in ("product", "package"):
        for block_name in (f"{prefix}_dimensions", f"{prefix}_dimensions_mm"):
            block = record.get(block_name)
            if not isinstance(block, Mapping):
                continue
            unit = "mm" if block_name.endswith("_mm") else block.get("unit")
            for axis in ("length", "width", "height"):
                value = _present(block, (f"{axis}_mm", axis))
                if value is _MISSING:
                    continue
                own_unit = "mm" if f"{axis}_mm" in block else unit
                if not own_unit:
                    continue
                row = _fact(f"{prefix}_{axis}", f"{value}{own_unit}", f"{path}.{block_name}.{axis}")
                if row:
                    result.append(row)
            if "weight_g" in block:
                row = _fact(f"{prefix}_weight_g", block["weight_g"], f"{path}.{block_name}.weight_g")
                if row:
                    result.append(row)
    return result


def _indexed(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        semantic = _semantic(row["name"])
        keys = [f"exact:{_key(row['name'])}", *([semantic] if semantic else [])]
        for key in keys:
            result.setdefault(key, []).append(row)
    return result


def _one(rows: list[dict[str, Any]], semantic: str | None = None) -> dict[str, Any] | None:
    unique = {}
    for row in rows:
        value = _measurement(row, semantic) if semantic and semantic.endswith(("_mm", "_g")) else _quantity(row["value"]) if semantic == "package_quantity" else row["value"]
        if value is None:
            return None
        identity = repr(value) if not isinstance(value, str) else value.casefold().strip()
        unique.setdefault(identity, {**row, "value": value})
    return next(iter(unique.values())) if len(unique) == 1 else None


def _variant_conflict(source: Mapping[str, Any], semantic: str, proposed: Any) -> bool:
    """Long combined SKU labels can contradict a common fact, never prove one."""
    for sku in source.get("skus") or []:
        if not isinstance(sku, Mapping):
            continue
        facts = _indexed(_sku_rows(sku)).get(semantic, [])
        known = _one(facts, semantic)
        if facts and not known:
            return True
        if known and known["value"] != proposed:
            if str(known["value"]).strip().casefold() not in _equivalents(proposed, semantic):
                return True
        labels = [sku.get("sku_name"), sku.get("name"), sku.get("spec_text"),
                  *[item.get("value_cn", item.get("value")) for item in sku.get("option_values") or [] if isinstance(item, Mapping)]]
        if semantic.startswith("product_") and semantic.endswith("_mm") and not known:
            # A generic "规格1: 大号15cm" contradicts the idea that a product-level
            # size can be safely shared, but does not tell us which axis it is.
            if any(re.search(r"\d\s*(?:mm|cm|毫米|厘米)(?![a-z])", str(label or ""), flags=re.I) for label in labels):
                return True
        if semantic.endswith("_g") and semantic.startswith("product_"):
            for label in labels:
                for number, unit in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kg|公斤|千克|g|克)(?![a-z])", str(label or ""), flags=re.I):
                    if float(number) * (1000 if unit.casefold() in {"kg", "公斤", "千克"} else 1) != proposed:
                        return True
    return False


def build_basic_fields(directory: Path | str) -> dict[str, dict[str, Any]]:
    """Source-only editor facts; never uses category, AI output or manual edits."""
    source, rows = _source_rows(Path(directory))
    indexed = _indexed(rows + _combined_measurements(rows))
    result = {}
    for key in ("material", "package_quantity", *[key for key in _ALIASES if key.startswith(("product_", "package_"))]):
        row = _one(indexed.get(key, []), key)
        if row and not _variant_conflict(source, key, row["value"]):
            value = row["value"]
            if key == "material":
                if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
                    value = "、".join(value)
                elif not isinstance(value, str):
                    continue
            result[key] = {**{name: row[name] for name in ("source", "evidence", "status")}, "value": value}
    return result


def _cached_form(directory: Path, cache_root: Path, shop_id: str | None) -> dict[str, Any] | None:
    selection = read_json(directory / "input/category-selection.json")
    if not selection.get("category_id") or not selection.get("type_id"):
        return None
    bound = selection.get("shop_id") or selection.get("shop")
    if shop_id and bound and shop_id != bound:
        raise ValueError("店铺与当前类目确认不一致，请重新确认真实类目")
    # Creating a read client validates local shop readiness without sending HTTP.
    _, resolved = category_form._client(bound or shop_id)
    category_id = int(selection["category_id"])
    type_id = int(selection["type_id"])
    scope = category_form._scope(resolved, category_id, type_id, category_form.LANGUAGE)
    form = category_form._read(cache_root / f"ozon-category-form-{scope}.json")
    if (not form or form.get("scope") != scope or form.get("shop_id") != resolved
            or form.get("category_id") != category_id or form.get("type_id") != type_id
            or form.get("source") != "ozon_seller_api" or not isinstance(form.get("fields"), list)):
        return None
    return form


def _equivalents(value: Any, semantic: str | None) -> set[str]:
    exact = str(value).strip().casefold()
    groups = _COLOUR_GROUPS if semantic in {"color", "color_name"} else _MATERIAL_GROUPS if semantic == "material" else ()
    if semantic == "material" and exact.replace(" ", "") in {"纯棉", "100%棉"}:
        # A confirmed pure cotton fact may map to the weaker general cotton
        # option, but general cotton must never invent 100% purity.
        return {"纯棉", "100%棉", "100% 棉", "棉", "cotton", "хлопок"}
    if semantic == "brand":
        groups = (("无品牌", "无", "no brand", "нет бренда"),)
    for group in groups:
        if exact in {item.casefold() for item in group}:
            return {item.casefold() for item in group}
    return {exact}


def _receipt_options(cache_root: Path, form: Mapping[str, Any], field: Mapping[str, Any]) -> list[dict[str, Any]]:
    receipt = category_form._read(category_form._receipt_target(cache_root, form, int(field["attribute_id"])), fresh=False) or {}
    if receipt.get("scope") != form["scope"] or receipt.get("dictionary_id") != field["dictionary_id"]:
        return []
    result = []
    for option_id, row in (receipt.get("values") or {}).items():
        try:
            fresh = isinstance(row, Mapping) and time.time() - float(row.get("observed_at") or 0) < category_form.CACHE_TTL
        except (ValueError, TypeError, OverflowError):
            fresh = False
        if fresh and str(option_id).isdigit() and int(option_id) > 0 and str(row.get("value") or "").strip():
            result.append({"dictionary_value_id": int(option_id), "value": str(row.get("value") or "")})
    return result


def _dictionary_match(cache_root: Path, form: Mapping[str, Any], field: Mapping[str, Any], value: Any,
                      semantic: str | None, *, selected_type: bool = False) -> dict[str, Any] | None:
    options = _receipt_options(cache_root, form, field)
    matches = [option for option in options if option["dictionary_value_id"] == form["type_id"]] if selected_type else [
        option for option in options if option["value"].strip().casefold() in _equivalents(value, semantic)]
    return matches[0] if len(matches) == 1 else None


def _field_semantic(field: Mapping[str, Any]) -> str | None:
    if field["attribute_id"] == 9048:
        return "model"
    if field["attribute_id"] == 85:
        return "brand"
    if field["attribute_id"] == 10096:
        return "color"
    if field["attribute_id"] == 10097:
        return "color_name"
    return _semantic(field["name"])


def _field_fact(index: Mapping[str, Any], field: Mapping[str, Any]) -> dict[str, Any] | None:
    semantic = _field_semantic(field)
    # The seller merge model is never inferred from the title or internal IDs.
    return _one(index.get(semantic, []) if semantic else index.get(f"exact:{_key(field['name'])}", []), semantic)


def _typed_candidate(row: Mapping[str, Any], field: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    values = row["value"] if isinstance(row["value"], (list, tuple)) else [row["value"]]
    semantic = _field_semantic(field)
    if semantic and semantic.endswith(("_mm", "_g")):
        target_unit = _units(str(field["name"]), weight=semantic.endswith("_g"))
        if not target_unit:
            return None
        divisor = 1000 if target_unit == "kg" else 10 if target_unit == "cm" else 1
        values = [value / divisor for value in values]
        if field["control"] == "integer":
            if any(not float(value).is_integer() for value in values):
                return None
            values = [int(value) for value in values]
    if field["control"] == "boolean":
        conversions = {"是": True, "有": True, "да": True, "yes": True, "true": True,
                       "否": False, "无": False, "нет": False, "no": False, "false": False}
        values = [conversions.get(str(value).strip().casefold(), value) for value in values]
    return [{"value": value} for value in values]


def _build(directory: Path, cache_root: Path, *, shop_id: str | None, resolve_dictionaries: bool,
           ignore_saved: bool = False, all_skus_for_evidence: bool = False) -> dict[str, Any]:
    source, rows = _source_rows(directory)
    common_index = _indexed(rows + _combined_measurements(rows))
    basic_fields = build_basic_fields(directory)
    saved = {} if ignore_saved else read_json(directory / "input/listing-details.json").get("details") or {}
    basic_fields = {key: value for key, value in basic_fields.items() if key not in saved}
    confirmations = {} if ignore_saved else read_json(directory / "input/human-confirmations.json")
    common_saved = confirmations.get("attributes") or {}
    sku_saved = confirmations.get("sku_attributes") or {}
    skus = ([row for row in source.get("skus") or [] if isinstance(row, Mapping)] if all_skus_for_evidence else
            active_skus(directory, source.get("skus") or []))
    provenance: dict[str, Any] = {"attributes": {}, "per_sku_attributes": {}, "basics": basic_fields.copy()}
    result: dict[str, Any] = {"attributes": {}, "per_sku_attributes": {}, "basic_fields": basic_fields,
                              "provenance": provenance, "unresolved": [], "lookup_count": 0,
                              "seller_offer_ids": {}, "api_writes_performed": False}
    for index, sku in enumerate(skus, 1):
        sku_id = str(sku["sku_id"])
        result["seller_offer_ids"][sku_id] = {
            "value": offer_id_for(directory.name, sku, index), "source": "workbench_internal",
            "evidence": "input/source.json 的商品编号和真实 SKU 标识；工作台内部货号，不是条形码或型号",
            "status": "internal_generated",
        }
    selection = read_json(directory / "input/category-selection.json")
    bound_shop = selection.get("shop_id") or selection.get("shop")
    if shop_id and bound_shop and shop_id != bound_shop:
        raise ValueError("店铺与当前类目确认不一致，请重新确认真实类目")
    try:
        form = _cached_form(directory, cache_root, shop_id)
    except (ValueError, OSError, RuntimeError):
        # Legacy captures may carry research category IDs without a usable
        # authorized shop. Their confirmed source basics are still inspectable;
        # never switch to another account or fetch guessed category metadata.
        result["unresolved"].append({"reason": "当前店铺授权或官方类目不可用；已保留采集基础信息，请先确认店铺及真实类目"})
        return result
    if not form:
        result["unresolved"].append({"reason": "请先确认真实类目并读取官方表单；当前仅补全已采集的基础信息"})
        return result
    result.update(shop_id=form["shop_id"], category_id=form["category_id"], type_id=form["type_id"], scope=form["scope"])
    queried: set[tuple[int, str]] = set()
    sku_indexes = {str(sku["sku_id"]): _indexed(_sku_rows(sku)) for sku in skus}

    def propose(field: Mapping[str, Any], row: Mapping[str, Any], sku_id: str | None = None,
                *, selected_type: bool = False) -> None:
        key = str(field["attribute_id"])
        if key in (sku_saved.get(sku_id, {}) if sku_id else common_saved):
            return  # false/0/[]/null, including intentional clears, are manual choices.
        if sku_id and key in common_saved:
            return
        if field.get("editable") is False or field.get("attribute_complex_id") or field.get("complex_id"):
            return
        semantic = _field_semantic(field)
        candidates = None
        if field.get("dictionary_id"):
            raw_values = row["value"] if isinstance(row["value"], (list, tuple)) else [row["value"]]
            candidates = []
            for value in raw_values:
                candidate = _dictionary_match(cache_root, form, field, value, semantic, selected_type=selected_type)
                query = "" if selected_type else str(value).strip()
                # Stable small aliases are exact-equivalence, not fuzzy matching.
                if semantic == "color" and len(query) < 2:
                    query = next((item for group in _COLOUR_GROUPS if query.casefold() in {v.casefold() for v in group}
                                  for item in group if item.endswith("色")), query)
                if semantic == "brand" and query in {"无", "无品牌"}:
                    query = "Нет бренда"
                query = query if len(query) >= 2 else ""
                request_key = (int(key), query)
                if (candidate is None and resolve_dictionaries and request_key not in queried
                        and result["lookup_count"] < MAX_DICTIONARY_LOOKUPS):
                    queried.add(request_key)
                    result["lookup_count"] += 1
                    try:
                        category_form.dictionary_values(cache_root, form["category_id"], form["type_id"], int(key),
                                                        shop_id=form["shop_id"], q=query, limit=50)
                    except (ValueError, OSError, TimeoutError, RuntimeError) as error:
                        result["unresolved"].append({"attribute_id": int(key), "sku_id": sku_id,
                                                       "reason": "官方选项读取失败，请手动选择", "evidence": row["evidence"]})
                    candidate = _dictionary_match(cache_root, form, field, value, semantic, selected_type=selected_type)
                if candidate is None:
                    result["unresolved"].append({"attribute_id": int(key), "sku_id": sku_id,
                                                   "reason": "采集值尚无唯一匹配的当前类目官方选项，请核对选择", "evidence": row["evidence"]})
                    return
                candidates.append(candidate)
        else:
            candidates = _typed_candidate(row, field)
        if candidates is None:
            return
        try:
            validated = category_form.validate_attributes(form, {key: candidates}, cache_root=cache_root)["attributes"]
        except ValueError:
            result["unresolved"].append({"attribute_id": int(key), "sku_id": sku_id,
                                           "reason": "采集值与官方字段类型或数量限制不一致，请人工确认", "evidence": row["evidence"]})
            return
        if not validated.get(key):
            return
        target = result["per_sku_attributes"].setdefault(sku_id, {}) if sku_id else result["attributes"]
        meta_target = provenance["per_sku_attributes"].setdefault(sku_id, {}) if sku_id else provenance["attributes"]
        target[key] = validated[key]
        meta_target[key] = {"source": row["source"], "evidence": row["evidence"], "status": row["status"]}

    for field in form["fields"]:
        key = str(field["attribute_id"])
        if field["attribute_id"] == 8229:
            propose(field, {"value": form["category_name"], "source": "ozon_selected_category",
                            "evidence": f"已确认官方类目 {form['category_id']} / 商品类型 {form['type_id']}", "status": "confirmed"}, selected_type=True)
            continue
        semantic = _field_semantic(field)
        common_row = _field_fact(common_index, field)
        sku_rows = {sku_id: _field_fact(index, field) for sku_id, index in sku_indexes.items()}
        # Differing/partially available variant values must remain SKU-specific.
        if any(sku_rows.values()):
            for sku_id, row in sku_rows.items():
                if row:
                    propose(field, row, sku_id)
            continue
        if common_row and not (semantic and _variant_conflict(source, semantic, common_row["value"])):
            # Do not undo an explicit individual clear by installing a new
            # common default. The unaffected SKUs can receive individual values.
            blockers = {sku_id for sku_id in sku_indexes if key in sku_saved.get(sku_id, {})}
            if blockers and skus:
                for sku_id in sku_indexes:
                    if sku_id not in blockers:
                        propose(field, common_row, sku_id)
            else:
                propose(field, common_row)
    return result


def build_autofill(directory: Path | str, cache_root: Path | str, shop_id: str | None = None,
                   resolve_dictionaries: bool = False) -> dict[str, Any]:
    return _build(Path(directory), Path(cache_root), shop_id=shop_id, resolve_dictionaries=resolve_dictionaries)


def source_attribute_candidates(directory: Path | str, cache_root: Path | str,
                                shop_id: str | None = None) -> dict[str, Any]:
    """Re-derive provenance before saves without ignoring manual clear policy in UI."""
    return _build(Path(directory), Path(cache_root), shop_id=shop_id, resolve_dictionaries=False,
                  ignore_saved=True, all_skus_for_evidence=True)
