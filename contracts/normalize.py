"""按契约把模型输出"机械地"归一化（不改语义、不编造数据）。

真实踩到：真模型（火山方舟）返回的 JSON 常有三类**机械错误**（不是内容错）：
1. 多塞了契约里没有的键（契约是 additionalProperties:false，直接判不合规）；
2. 数组字段给 ``null``（契约要求 array）；
3. 对象字段给 ``null``（契约要求 object）。

这三类都可以安全地机械修正：丢未知键不等于编造，``null → [] / {}`` 表达的就是"没有"。
**真正的语义错误（值不对、缺必填）不在这里修**，仍然交给校验与重试。
"""

from __future__ import annotations

from typing import Any, Mapping

_ARRAY_TYPES = {"array"}
_OBJECT_TYPES = {"object"}


def _declared_types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {str(item) for item in declared}
    return set()


def _properties(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("properties",):
        value = schema.get(key)
        if isinstance(value, Mapping):
            return value
    # oneOf/anyOf/then 里可能才是真正形状（例如 dimensions）
    for key in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, Mapping) and isinstance(branch.get("properties"), Mapping):
                    return branch["properties"]
    return {}


def _allows_additional(schema: Mapping[str, Any]) -> bool:
    value = schema.get("additionalProperties")
    return value is not False


def normalize_payload(payload: Any, schema: Mapping[str, Any]) -> tuple[Any, list[str]]:
    """返回 (归一化后的 payload, 修正说明)。schema 来自契约文件。"""
    notes: list[str] = []
    result = _normalize(payload, schema, path="$", notes=notes)
    return result, notes


def _normalize(value: Any, schema: Mapping[str, Any], *, path: str, notes: list[str]) -> Any:
    if not isinstance(schema, Mapping):
        return value

    declared = _declared_types(schema)

    # null → []/{}（契约要求的类型是确定的，null 一律不接受）
    if value is None:
        if declared & _ARRAY_TYPES:
            notes.append(f"{path}: null → []（契约要求 array）")
            return []
        if declared & _OBJECT_TYPES:
            notes.append(f"{path}: null → {{}}（契约要求 object）")
            return {}
        return None

    if isinstance(value, Mapping) and ("object" in declared or _properties(schema) or not declared):
        properties = _properties(schema)
        if not properties:
            return dict(value)
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in properties:
                cleaned[key] = _normalize(item, properties[key], path=f"{path}.{key}", notes=notes)
            elif _allows_additional(schema):
                cleaned[key] = item
            else:
                notes.append(f"{path}.{key}: 契约里没有这个字段，已丢弃")
        return cleaned

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            return [_normalize(item, item_schema, path=f"{path}[{index}]", notes=notes) for index, item in enumerate(value)]
        return list(value)

    # 值本身是对象/数组，但契约给的是 oneOf（例如 dimensions 允许两种形状）：逐分支试一遍
    for key in ("oneOf", "anyOf"):
        branches = schema.get(key)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if not isinstance(branch, Mapping):
                continue
            branch_types = _declared_types(branch)
            if branch_types and isinstance(value, Mapping) and "object" not in branch_types:
                continue
            if branch_types and isinstance(value, list) and "array" not in branch_types:
                continue
            candidate, branch_notes = normalize_payload(value, branch)
            if branch_notes:
                notes.extend(branch_notes)
                return candidate
        # 没有分支需要修 → 原样返回
        return value

    return value
