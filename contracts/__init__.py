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
            if filename == "keywords-ru.schema.json":
                # Category/fact-led copy no longer requires a keyword-library
                # query. Empty arrays mean no measured/user-selected keywords,
                # not fabricated analytics or weakened product-fact checks.
                for key in ("primary_keywords", "keyword_basis"):
                    schema["properties"][key]["minItems"] = 0
            if filename == "ozon-upload-payload.schema.json":
                # Add our media fields without editing ignored upstream files.
                schema.setdefault("properties", {}).update({
                    "studio_mode": {"type": "boolean"},
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
    if (name.removesuffix(".schema.json") in {"title-ru", "description-ru", "keywords-ru"}
            and isinstance(payload, Mapping) and payload.get("copy_policy_version") == 3):
        # Current workbench prose is not the archived five-section template.
        # Keep upstream contracts untouched and retain evidence field checks.
        schema["properties"] = dict(schema.get("properties") or {})
        schema["properties"]["copy_policy_version"] = {"type": "integer", "const": 3}
        if name.removesuffix(".schema.json") == "title-ru":
            schema["properties"]["title_ru"] = {**schema["properties"]["title_ru"], "maxLength": 200}
        if name.removesuffix(".schema.json") == "description-ru":
            schema["properties"]["sections"] = {
                "type": "object", "additionalProperties": False,
                "properties": {key: {"type": "string"} for key in
                               ("product_value", "usage_scenarios", "core_advantages", "usage_method", "notices")}}
            evidence = schema["properties"].get("section_evidence") or {}
            schema["properties"]["section_evidence"] = {**evidence, "minItems": 0}
    if (name.removesuffix(".schema.json") == "image-plan" and isinstance(payload, Mapping)
            and payload.get("max_main_images") is not None):
        # Explicit current guided-planner capacity, without turning the plan
        # into a freely edited studio draft or relaxing other legacy checks.
        from pipeline.sku_selection import MAX_SELECTED
        capacity = payload["max_main_images"]
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= MAX_SELECTED:
            return [f"主图规划容量须在 1–{MAX_SELECTED} 个规格以内"]
        schema["properties"] = dict(schema.get("properties") or {})
        schema["properties"]["max_main_images"] = {"type": "integer", "minimum": 1, "maximum": MAX_SELECTED}
        schema["properties"]["main_images"] = {**schema["properties"]["main_images"], "maxItems": capacity}
        strategy = schema["properties"]["variant_image_strategy"]
        schema["properties"]["variant_image_strategy"] = {**strategy, "properties": {
            **strategy["properties"], "variant_main_count": {
                **strategy["properties"]["variant_main_count"], "maximum": capacity}}}
    if (name.removesuffix(".schema.json") == "image-plan"
            and isinstance(payload, Mapping) and payload.get("studio_mode") is True):
        # Keep ignored upstream contracts and legacy whole-plan validation
        # intact. Studio drafts may have any number of independently made slots.
        schema["properties"] = dict(schema.get("properties") or {})
        schema["properties"].update({
            "studio_mode": {"type": "boolean", "const": True},
            "selected_slots": {"type": "array", "uniqueItems": True,
                               "items": {"type": "string", "minLength": 1}},
            "image_sets": {"type": "array", "items": {"type": "object"}},
        })
        schema["required"] = [*schema.get("required", []), "selected_slots"]
        for key in ("main_images", "detail_images"):
            field = dict(schema["properties"][key])
            field["minItems"] = 0
            field.pop("maxItems", None)
            schema["properties"][key] = field
        for field_name, count_names in (("variant_image_strategy", ("variant_main_count", "shared_detail_count")),
                                        ("generator_contract", ("exact_shared_detail_count",))):
            field = dict(schema["properties"][field_name])
            field["properties"] = dict(field["properties"])
            for count_name in count_names:
                field["properties"][count_name] = {"type": "integer", "minimum": 0}
            if field_name == "generator_contract":
                field["properties"]["raw_1688_image_direct_upload_forbidden"] = {"type": "boolean"}
            schema["properties"][field_name] = field
        schema["$defs"] = dict(schema.get("$defs") or {})
        planned = dict(schema["$defs"]["plannedImage"])
        planned["additionalProperties"] = True  # studio jobs/draft vs adopted metadata
        planned["properties"] = dict(planned["properties"])
        overlays = dict(planned["properties"]["overlay_plan"])
        overlays["minItems"] = 0  # text-free product photos are legitimate studio drafts
        planned["properties"]["overlay_plan"] = overlays
        operation = dict(planned["properties"]["operation"])
        operation["enum"] = [*operation.get("enum", []), "adopt_captured_original"]
        planned["properties"]["operation"] = operation
        schema["$defs"]["plannedImage"] = planned
        from pipeline.media_selection import selected_image_specs
        try:
            selected_image_specs(payload)
        except ValueError as error:
            return [str(error)]
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
