"""Read-only, shop-scoped Ozon listing forms and verified dictionary receipts.

The Seller API publishes a field schema, not the seller cabinet's HTML layout.
Metadata is cached for one day; dictionaries are loaded only on user demand.
Complex groups are preserved in metadata and are not silently flattened into
ordinary attributes by the current flat attribute compiler.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from market_intelligence.ozon_categories import _client, leaf_categories, load_tree
from pipeline.ozon_http import (
    OzonClient, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH,
    build_category_snapshot,
)
from pipeline.category_cache import invalidate_shop, read_cache, write_cache

CACHE_TTL = 86400
LANGUAGE = "ZH_HANS"


def invalidate_shop_cache(cache_root: Path | str, shop_id: str) -> None:
    """Drop only generated metadata/receipts after an account's key is replaced."""
    invalidate_shop(cache_root, shop_id)
_RECEIPT_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须为正整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} 必须为正整数") from error
    if number < 1 or str(value).strip() != str(number):
        raise ValueError(f"{label} 必须为正整数")
    return number


def _scope(shop_id: str, category_id: int, type_id: int, language: str) -> str:
    return hashlib.sha256(f"{shop_id}:{language}:{category_id}:{type_id}".encode("utf-8")).hexdigest()[:24]


def _read(target: Path, *, fresh: bool = True) -> dict[str, Any] | None:
    return read_cache(target, ttl=CACHE_TTL if fresh else None)


def _write(target: Path, value: Mapping[str, Any]) -> None:
    write_cache(target, value)


def _field(raw: Mapping[str, Any]) -> dict[str, Any]:
    attribute_id = _positive(raw.get("id") or raw.get("attribute_id"), "属性 ID")
    raw_type = str(raw.get("type") or "String")
    lower_type = raw_type.casefold()
    dictionary_id = int(raw.get("dictionary_id") or 0)
    complex_id = int(raw.get("attribute_complex_id") or raw.get("complex_id") or 0)
    is_collection = bool(raw.get("is_collection"))
    if dictionary_id:
        control = "dictionary"
    elif lower_type in {"integer", "int", "int32", "int64", "long"}:
        control = "integer"
    elif lower_type in {"decimal", "double", "float", "number"}:
        control = "decimal"
    elif lower_type in {"boolean", "bool"}:
        control = "boolean"
    elif lower_type in {"string", "text"}:
        control = "text"
    else:
        control = "unsupported"
    supported = not complex_id and control != "unsupported"
    return {
        "attribute_id": attribute_id,
        "attribute_name": str(raw.get("name") or raw.get("attribute_name") or f"属性 {attribute_id}"),
        "name": str(raw.get("name") or raw.get("attribute_name") or f"属性 {attribute_id}"),
        "description": str(raw.get("description") or ""),
        "required": bool(raw.get("is_required") if raw.get("is_required") is not None else raw.get("required")),
        "is_required": bool(raw.get("is_required") if raw.get("is_required") is not None else raw.get("required")),
        "type": raw_type,
        "control": control,
        "dictionary_id": dictionary_id or None,
        "category_dependent": bool(raw.get("category_dependent")),
        "is_collection": is_collection,
        "is_aspect": bool(raw.get("is_aspect")),
        "max_value_count": max(0, int(raw.get("max_value_count") or 0)),
        "group_id": int(raw.get("group_id") or 0),
        "group_name": str(raw.get("group_name") or "基本属性"),
        "attribute_complex_id": complex_id or None,
        "complex_id": complex_id or None,
        "complex_is_collection": bool(raw.get("complex_is_collection")),
        "editable": supported,
        "unsupported_reason": ("复合属性需按组填写，当前上架编译器尚不支持；已完整保留官方定义" if complex_id else
                               f"当前不支持官方字段类型 {raw_type}" if not supported else ""),
    }


def load_form(cache_root: Path | str, category_id: int, type_id: int, *,
              shop_id: str | None = None, refresh: bool = False,
              client: OzonClient | None = None, language: str = LANGUAGE) -> dict[str, Any]:
    """Load one real, enabled leaf's fields without fetching its dictionaries."""
    category_id, type_id = _positive(category_id, "类目 ID"), _positive(type_id, "类型 ID")
    if client is None:
        client, resolved_id = _client(shop_id)
    else:
        resolved_id = str(shop_id or "injected-client")
    tree = load_tree(cache_root, shop_id=resolved_id, refresh=refresh, client=client, language=language)
    leaf = next((row for row in leaf_categories(tree)
                 if row["category_id"] == category_id and row["type_id"] == type_id), None)
    if not leaf:
        raise ValueError("所选类目与商品类型不在该店铺的可用末级类目树中，禁止使用或伪造类目")
    scope = _scope(resolved_id, category_id, type_id, language)
    target = Path(cache_root) / f"ozon-category-form-{scope}.json"
    cached = _read(target) if not refresh else None
    if cached and cached.get("scope") == scope and isinstance(cached.get("fields"), list):
        return {**cached, "cache_hit": True}
    response = client.fetch_category_attributes(category_id=category_id, type_id=type_id, language=language)
    raw_fields = response.get("result")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("Ozon 返回空属性定义，无法生成真实上架表单")
    if any(not isinstance(row, Mapping) for row in raw_fields):
        raise ValueError("Ozon 属性定义格式异常，请刷新重试")
    fields = [_field(row) for row in raw_fields]
    if len({row["attribute_id"] for row in fields}) != len(fields):
        raise ValueError("Ozon 返回重复属性 ID，无法安全生成表单")
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    for field in fields:
        group_key = (field["group_id"], field["group_name"])
        group = groups.setdefault(group_key, {"group_id": group_key[0], "group_name": group_key[1], "attribute_ids": []})
        group["attribute_ids"].append(field["attribute_id"])
    result = {
        "schema_version": "1.0.0", "source": "ozon_seller_api", "api_endpoint": PATH_ATTRIBUTES,
        "shop_id": resolved_id, "language": language, "scope": scope,
        "cache_generation": int(tree.get("cache_generation") or 0),
        "category_id": category_id, "type_id": type_id,
        "category_name": leaf["name"], "category_path": leaf["path"],
        "fetched_at": _now(), "cache_hit": False, "cache_ttl_seconds": CACHE_TTL,
        "fields": fields, "groups": list(groups.values()),
        "required_attribute_ids": [row["attribute_id"] for row in fields if row["required"]],
        "aspect_attribute_ids": [row["attribute_id"] for row in fields if row["is_aspect"]],
        "unsupported_attribute_ids": [row["attribute_id"] for row in fields if not row["editable"]],
        "attributes_response": {"result": [dict(row) for row in raw_fields]},
        "layout_source": "workbench_generated_from_official_fields",
        "warnings": ["复合属性已保留，但当前不能填写或扁平提交"] if any(row["complex_id"] for row in fields) else [],
    }
    _write(target, result)
    return result


def snapshot_for_product(form: Mapping[str, Any], product_id: str) -> dict[str, Any]:
    """Generate the existing strict snapshot contract, with no metadata loss in form."""
    return build_category_snapshot(
        product_id=product_id, category_id=int(form["category_id"]), type_id=int(form["type_id"]),
        category_name=str(form["category_name"]), attributes_response=form["attributes_response"],
        fetched_at=str(form["fetched_at"]),
    )


def _receipt_target(cache_root: Path | str, form: Mapping[str, Any], attribute_id: int) -> Path:
    return Path(cache_root) / f"ozon-dictionary-receipts-{form['scope']}-{attribute_id}.json"


def dictionary_values(cache_root: Path | str, category_id: int, type_id: int, attribute_id: int, *,
                      shop_id: str | None = None, last_value_id: int = 0, q: str = "", limit: int = 50,
                      refresh: bool = False, client: OzonClient | None = None,
                      language: str = LANGUAGE) -> dict[str, Any]:
    """Load a single official dictionary page/search, cache it, record verified IDs."""
    attribute_id = _positive(attribute_id, "属性 ID")
    if client is None:
        client, resolved_id = _client(shop_id)
    else:
        resolved_id = str(shop_id or "injected-client")
    form = load_form(cache_root, category_id, type_id, shop_id=resolved_id, client=client, language=language)
    field = next((row for row in form["fields"] if row["attribute_id"] == attribute_id), None)
    if not field or not field["dictionary_id"]:
        raise ValueError("所选属性不属于此类目，或不是官方字典属性")
    q = str(q).strip()
    if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
        raise ValueError("每次最多读取 100 个字典选项，limit 必须为 1–100")
    if isinstance(last_value_id, bool) or int(last_value_id) < 0:
        raise ValueError("字典分页游标不能为负数")
    if q and len(q) < 2:
        raise ValueError("字典搜索至少输入 2 个字符")
    if q and last_value_id:
        raise ValueError("字典搜索不支持分页游标，请清空游标")
    request_key = hashlib.sha256(json.dumps([field["dictionary_id"], q, int(last_value_id), int(limit)], ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
    target = Path(cache_root) / f"ozon-dictionary-page-{form['scope']}-{attribute_id}-{request_key}.json"
    cached = _read(target) if not refresh else None
    if cached:
        return {**cached, "cache_hit": True}
    if q:
        response = client.search_attribute_values(attribute_id=attribute_id, category_id=int(category_id),
                                                  type_id=int(type_id), value=q, limit=int(limit), language=language)
    else:
        response = client.fetch_attribute_values(attribute_id=attribute_id, category_id=int(category_id),
                                                 type_id=int(type_id), limit=int(limit),
                                                 last_value_id=int(last_value_id), language=language)
    if not isinstance(response.get("result"), list):
        raise ValueError("Ozon 字典响应缺少选项列表")
    values = []
    for row in response["result"]:
        if isinstance(row, Mapping) and row.get("id") and str(row.get("value") or "").strip():
            values.append({"id": _positive(row["id"], "字典选项 ID"), "value": str(row["value"]),
                           "info": str(row.get("info") or ""), "picture": str(row.get("picture") or "")})
    if len(values) > int(limit):
        raise ValueError("Ozon 返回的字典选项数量超过请求上限")
    has_next = bool(response.get("has_next")) if not q else False
    if has_next and (not values or values[-1]["id"] <= int(last_value_id)):
        raise ValueError("Ozon 字典分页游标没有前进，请重新搜索选项")
    result = {
        "source": "ozon_seller_api", "api_endpoint": PATH_ATTRIBUTE_VALUES_SEARCH if q else PATH_ATTRIBUTE_VALUES,
        "shop_id": resolved_id, "language": language, "category_id": int(category_id), "type_id": int(type_id),
        "cache_generation": int(form.get("cache_generation") or 0),
        "attribute_id": attribute_id, "dictionary_id": field["dictionary_id"],
        "fetched_at": _now(), "cache_hit": False, "result": values, "has_next": has_next,
        "last_value_id": int(last_value_id), "next_last_value_id": values[-1]["id"] if has_next else None,
        "query": q, "search_limited": bool(q and len(values) == int(limit)),
    }
    receipts_path = _receipt_target(cache_root, form, attribute_id)
    with _RECEIPT_LOCK:
        receipts = _read(receipts_path, fresh=False) or {}
        observed = receipts.get("values") or {}
        if receipts.get("dictionary_id") != field["dictionary_id"]:
            observed = {}
        observed = {key: row for key, row in observed.items() if isinstance(row, Mapping)
                    and time.time() - float(row.get("observed_at") or 0) < CACHE_TTL}
        for value in values:
            observed[str(value["id"])] = {"value": value["value"], "observed_at": time.time()}
        _write(receipts_path, {"scope": form["scope"], "shop_id": resolved_id,
                               "cache_generation": int(form.get("cache_generation") or 0),
                               "dictionary_id": field["dictionary_id"], "values": observed})
    _write(target, result)
    return result


def _typed_value(field: Mapping[str, Any], value: Any) -> Any:
    if value is None:
        raise ValueError("属性值不能为空")
    kind = field["control"]
    if kind == "text":
        if isinstance(value, (dict, list, tuple, bool)):
            raise ValueError("请填写文本值")
        text = str(value).strip()
        if not text:
            raise ValueError("文本属性值不能为空")
        return text
    if kind == "boolean":
        if value is True or str(value).strip().casefold() == "true":
            return True
        if value is False or str(value).strip().casefold() == "false":
            return False
        raise ValueError("布尔属性只允许 true 或 false")
    if isinstance(value, bool):
        raise ValueError("数值属性不能使用布尔值")
    text = str(value).strip()
    if kind == "integer":
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("请填写整数，不能包含小数或单位")
        return int(text)
    if kind == "decimal":
        try:
            number = float(text)
        except (TypeError, ValueError) as error:
            raise ValueError("请填写数字，不能包含单位") from error
        if not math.isfinite(number):
            raise ValueError("数字必须为有限值")
        return number
    raise ValueError("该官方字段类型暂不支持填写")


def validate_attributes(form: Mapping[str, Any], submitted: Mapping[str, Any], *,
                        cache_root: Path | str) -> dict[str, Any]:
    """Validate a partial common-attribute draft; missing required fields are reported.

    Dictionary IDs and their text come only from recent authenticated receipts.
    Missing optional/required values are allowed in a draft, never fabricated.
    A field with is_aspect is explicitly a common default for all selected SKUs;
    callers must not represent it as independently filled SKU specifications.
    """
    if not isinstance(submitted, Mapping):
        raise ValueError("属性草稿必须为对象")
    fields = {str(row["attribute_id"]): row for row in form["fields"]}
    unknown = set(str(key) for key in submitted) - set(fields)
    if unknown:
        raise ValueError("包含不属于当前真实类目的属性 ID：" + "、".join(sorted(unknown)[:10]))
    normalized: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for key, raw_values in submitted.items():
        key = str(key)
        field = fields[key]
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        values = [row for row in values if row is not None and row != "" and row != {}]
        if not values:
            normalized[key] = []  # Explicit clearing is distinct from inheriting a common value.
            continue
        label = f"属性 {key}「{field['name']}」"
        if not field["editable"]:
            errors.append(f"{label}：{field['unsupported_reason']}")
            continue
        max_count = int(field["max_value_count"] or 0) if field["is_collection"] else 1
        if max_count and len(values) > max_count:
            errors.append(f"{label}：最多允许 {max_count} 个值")
            continue
        receipts = _read(_receipt_target(cache_root, form, int(key)), fresh=False) or {}
        allowed = receipts.get("values") or {} if receipts.get("dictionary_id") == field["dictionary_id"] else {}
        converted: list[dict[str, Any]] = []
        for row in values:
            record = row if isinstance(row, Mapping) else {"value": row}
            try:
                if set(record) - {"value", "dictionary_value_id", "id"}:
                    raise ValueError("属性值格式不正确，不能忽略嵌套或复合属性数据")
                if field["dictionary_id"]:
                    option_id = _positive(record.get("dictionary_value_id") or record.get("id"), "字典选项 ID")
                    observed = allowed.get(str(option_id))
                    if (not isinstance(observed, Mapping) or
                            time.time() - float(observed.get("observed_at") or 0) >= CACHE_TTL):
                        raise ValueError("请先从当前类目的官方字典读取并选择该选项，不能手写或伪造选项 ID")
                    canonical = str(observed["value"])
                    if record.get("value") not in (None, "", canonical):
                        raise ValueError("字典选项文本与 Ozon 官方选项不一致，请重新选择")
                    converted.append({"value": canonical, "dictionary_value_id": option_id})
                else:
                    if record.get("dictionary_value_id") or record.get("id"):
                        raise ValueError("此属性没有官方字典，不能填写字典选项 ID")
                    converted.append({"value": _typed_value(field, record.get("value")), "dictionary_value_id": None})
            except (ValueError, TypeError, OverflowError) as error:
                errors.append(f"{label}：{error}")
        if converted:
            normalized[key] = converted
            if field["is_aspect"]:
                warnings.append(f"{label} 是规格属性：本草稿值会作为全部已选 SKU 的共同默认值，请核对不同规格")
    if errors:
        raise ValueError("；".join(errors[:12]))
    required = [str(row["attribute_id"]) for row in form["fields"] if row["required"]]
    missing = [int(key) for key in required if not normalized.get(key)]
    return {"attributes": normalized, "required_total": len(required), "required_filled": len(required) - len(missing),
            "missing_required_attribute_ids": missing, "complete": not missing, "warnings": warnings,
            "shop_id": form["shop_id"], "category_id": form["category_id"], "type_id": form["type_id"],
            "schema_fetched_at": form["fetched_at"], "source": "ozon_seller_api"}
