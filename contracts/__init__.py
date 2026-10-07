"""上游数据契约（从原项目 templates/ 拉取）的本地校验入口。

    from contracts import validate_contract
    problems = validate_contract("title-ru", payload)

契约文件在 ``contracts/original/``（用 ``contracts/fetch_contracts.ps1`` 拉取）。
``allow_extra=True`` 用于我们自己的产物（例如 ``status.json``）—— 它们是上游契约的**超集**，
上游必填字段仍然必须齐全，但我们额外记的字段不该被判失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .jsonschema_lite import format_problems, validate

CONTRACTS_DIR = Path(__file__).resolve().parent / "original"
LOCAL_CONTRACTS_DIR = Path(__file__).resolve().parent

__all__ = [
    "CONTRACTS_DIR",
    "LOCAL_CONTRACTS_DIR",
    "available_contracts",
    "load_contract",
    "validate_contract",
    "format_problems",
]


def _search_dirs() -> tuple[Path, ...]:
    """先找上游契约（original/），再找我们自己的契约（contracts/ 根目录）。"""
    return (CONTRACTS_DIR, LOCAL_CONTRACTS_DIR)


def available_contracts() -> list[str]:
    names: set[str] = set()
    for directory in _search_dirs():
        if not directory.is_dir():
            continue
        names.update(path.name.removesuffix(".schema.json") for path in directory.glob("*.schema.json"))
    return sorted(names)


def load_contract(name: str) -> dict[str, Any]:
    filename = name if name.endswith(".json") else f"{name.removesuffix('.schema.json')}.schema.json"
    for directory in _search_dirs():
        path = directory / filename
        if path.is_file():
            schema = json.loads(path.read_text(encoding="utf-8"))
            if filename == "ozon-upload-payload.schema.json":
                # Add our media fields without editing ignored upstream files.
                schema.setdefault("properties", {}).update({
                    "hashtags": {"type": "array", "maxItems": 30,
                                 "items": {"type": "string", "maxLength": 30}},
                    "videos": {"type": "array", "maxItems": 50, "items": {"type": "object"}},
                    "video_cover": {"type": "object"},
                })
            return schema
    raise FileNotFoundError(
        f"找不到契约 {filename}；先运行 contracts/fetch_contracts.ps1（可用契约：{len(available_contracts())} 个）"
    )


def validate_contract(name: str, payload: Any, *, allow_extra: bool = False) -> list[str]:
    """返回问题列表（空列表 = 通过）。``allow_extra`` 只放宽 additionalProperties。"""
    schema = dict(load_contract(name))
    if allow_extra:
        schema = _relax_extra(schema)
    return validate(schema, payload)


def _relax_extra(node: Any) -> Any:
    if isinstance(node, Mapping):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "additionalProperties" and value is False:
                result[key] = True
                continue
            result[key] = _relax_extra(value)
        return result
    if isinstance(node, list):
        return [_relax_extra(item) for item in node]
    return node
