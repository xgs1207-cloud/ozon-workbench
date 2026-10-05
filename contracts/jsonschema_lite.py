"""对上游数据契约（JSON Schema）做轻量校验，不依赖第三方库。

只实现这些 schema 实际用到的关键字：``type / const / enum / required / properties /
additionalProperties / items / minItems / maxItems / uniqueItems / minLength / maxLength /
pattern / minimum / maximum / oneOf / anyOf / allOf / if-then-else / $ref``。
遇到不认识的关关键字一律忽略（不假装支持）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(expected: Any, value: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        types = _TYPE_MAP.get(str(name))
        if types is None:
            continue
        if isinstance(value, bool) and name in {"integer", "number"}:
            continue
        if isinstance(value, types):
            return True
    return False


def validate(schema: Mapping[str, Any], payload: Any, *, path: str = "$", root: Mapping[str, Any] | None = None) -> list[str]:
    """返回问题列表（空列表表示通过）。"""
    root = root if root is not None else schema
    problems: list[str] = []
    if not isinstance(schema, Mapping):
        return problems

    if "$ref" in schema:
        target = _resolve_ref(str(schema["$ref"]), root)
        if target is None:
            return [f"{path}: 无法解析 $ref {schema['$ref']}"]
        return validate(target, payload, path=path, root=root)

    for keyword in ("allOf",):
        for index, sub in enumerate(schema.get(keyword) or []):
            problems.extend(validate(sub, payload, path=path, root=root))

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword) or []
        if branches:
            results = [validate(sub, payload, path=path, root=root) for sub in branches]
            if keyword == "oneOf":
                if not any(not item for item in results):
                    detail = "; ".join(results[0][:2]) if results and results[0] else "无匹配分支"
                    problems.append(f"{path}: 不满足 oneOf 任一分支（{detail}）")
            elif not all(not item for item in results):
                problems.append(f"{path}: 不满足 anyOf 任一分支")

    condition = schema.get("if")
    if isinstance(condition, Mapping):
        if not validate(condition, payload, path=path, root=root):
            then = schema.get("then")
            if isinstance(then, Mapping):
                problems.extend(validate(then, payload, path=path, root=root))
        else:
            otherwise = schema.get("else")
            if isinstance(otherwise, Mapping):
                problems.extend(validate(otherwise, payload, path=path, root=root))

    if "const" in schema and payload != schema["const"]:
        problems.append(f"{path}: 期望常量 {schema['const']!r}，实际 {payload!r}")
    if "enum" in schema and payload not in schema["enum"]:
        problems.append(f"{path}: {payload!r} 不在枚举 {schema['enum']} 内")

    expected_type = schema.get("type")
    if expected_type is not None and not _type_ok(expected_type, payload):
        problems.append(f"{path}: 类型应为 {expected_type}，实际 {type(payload).__name__}")
        return problems

    if isinstance(payload, str):
        if "minLength" in schema and len(payload) < schema["minLength"]:
            problems.append(f"{path}: 长度 {len(payload)} 小于 minLength {schema['minLength']}")
        if "maxLength" in schema and len(payload) > schema["maxLength"]:
            problems.append(f"{path}: 长度 {len(payload)} 大于 maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(str(schema["pattern"]), payload):
            problems.append(f"{path}: 不匹配 pattern {schema['pattern']}")

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if "minimum" in schema and payload < schema["minimum"]:
            problems.append(f"{path}: {payload} 小于 minimum {schema['minimum']}")
        if "maximum" in schema and payload > schema["maximum"]:
            problems.append(f"{path}: {payload} 大于 maximum {schema['maximum']}")

    if isinstance(payload, list):
        if "minItems" in schema and len(payload) < schema["minItems"]:
            problems.append(f"{path}: 元素数 {len(payload)} 小于 minItems {schema['minItems']}")
        if "maxItems" in schema and len(payload) > schema["maxItems"]:
            problems.append(f"{path}: 元素数 {len(payload)} 大于 maxItems {schema['maxItems']}")
        if schema.get("uniqueItems"):
            seen = [repr(item) for item in payload]
            if len(set(seen)) != len(seen):
                problems.append(f"{path}: 元素应唯一")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(payload):
                problems.extend(validate(item_schema, item, path=f"{path}[{index}]", root=root))

    if isinstance(payload, Mapping):
        required = schema.get("required") or []
        for key in required:
            if key not in payload:
                problems.append(f"{path}: 缺少必填字段 {key}")
        properties = schema.get("properties") or {}
        for key, value in payload.items():
            if key in properties:
                problems.extend(validate(properties[key], value, path=f"{path}.{key}", root=root))
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                problems.append(f"{path}: 出现契约未定义的字段 {key}")
            elif isinstance(extra, Mapping):
                problems.extend(validate(extra, value, path=f"{path}.{key}", root=root))
    return problems


def _resolve_ref(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not ref.startswith("#"):
        return None
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not part:
            continue
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


def format_problems(problems: Sequence[str], limit: int = 8) -> str:
    if not problems:
        return "OK"
    head = list(problems[:limit])
    if len(problems) > limit:
        head.append(f"… 另有 {len(problems) - limit} 条")
    return "；".join(head)
