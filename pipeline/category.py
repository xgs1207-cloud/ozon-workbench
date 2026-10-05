"""``category_match`` handler：用 Ozon **只读**接口拉真实类目与属性快照。

门禁要点（与原项目一致）：

- 必须能从采集输入里拿到 ``category_id`` / ``type_id``（没选类目就是硬失败）；
- 快照来源必须是 ``/v1/description-category/attribute``（契约里的 ``api_endpoint`` 常量）；
- 类目在类目树里找不到时**不谎报确认**：写 ``api_match_needs_review`` 并告警；
  类目树整体为空则直接失败（连"存在性"都无法判断）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from contracts import format_problems, validate_contract

from .context import PipelineGateError, StepContext
from .ozon_http import OzonClient, OzonHttpError, attach_dictionary_values, build_category_snapshot, find_category_in_tree

CATEGORY_FILE = "output/ozon-category.json"
SNAPSHOT_FILE = "output/ozon-category-attributes.json"
SELECTION_FILE = "input/category-selection.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _selected_category(ctx: StepContext, source: Mapping[str, Any]) -> dict[str, Any]:
    selection = ctx.read_json(SELECTION_FILE)
    if not selection:
        selection = source.get("selected_category") if isinstance(source.get("selected_category"), Mapping) else {}
    return dict(selection or {})


def handle_category_match(ctx: StepContext) -> dict[str, Any]:
    client = getattr(ctx, "ozon_client", None)
    if client is None:
        raise PipelineGateError(
            ctx.step,
            "未配置 Ozon 只读客户端（run_product(ozon_client=...)）：类目必须来自 Ozon Seller API",
        )
    source = ctx.require_json("input/source.json")
    selected = _selected_category(ctx, source)
    category_id = int(selected.get("category_id") or 0)
    type_id = int(selected.get("type_id") or 0)
    if category_id < 1 or type_id < 1:
        raise PipelineGateError(
            ctx.step,
            "采集时没有选择 Ozon 类目（input/category-selection.json 缺 category_id/type_id）",
        )

    warnings: list[str] = []
    try:
        tree = client.fetch_category_tree()
        found = find_category_in_tree(tree, category_id=category_id, type_id=type_id)
        if not (tree.get("result") or []):
            raise PipelineGateError(ctx.step, "Ozon 类目树返回为空，无法确认类目（检查语言/凭据权限）")
        attributes_response = client.fetch_category_attributes(category_id=category_id, type_id=type_id)
    except OzonHttpError as error:
        raise PipelineGateError(ctx.step, f"Ozon 只读接口失败：{error}") from error

    if found is None:
        match_status = "api_match_needs_review"
        warnings.append(f"类目 {category_id}/{type_id} 不在返回的类目树里：请人工确认后再提交")
    else:
        match_status = "api_confirmed"
    category_name = str((found or {}).get("name") or selected.get("category_path_zh") or "unknown")

    snapshot = build_category_snapshot(
        product_id=ctx.product_dir.name,
        category_id=category_id,
        type_id=type_id,
        category_name=category_name,
        attributes_response=attributes_response,
        fetched_at=now_iso(),
    )
    try:
        snapshot = attach_dictionary_values(snapshot, client=client)
    except OzonHttpError as error:
        warnings.append(f"字典值补充失败（不阻断）：{error}")

    problems = validate_contract("ozon-category-attributes", snapshot)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "类目属性快照不符合 ozon-category-attributes 契约",
            {"problems": problems[:8], "summary": format_problems(problems)},
        )
    ctx.write_json(SNAPSHOT_FILE, snapshot)
    ctx.write_json(
        CATEGORY_FILE,
        {
            "schema_version": "1.0.0",
            "product_id": ctx.product_dir.name,
            "metadata_source": "ozon_seller_api",
            "category_id": category_id,
            "type_id": type_id,
            "category_name": category_name,
            "category_path": (found or {}).get("path"),
            "match_status": match_status,
            "fetched_at": snapshot["fetched_at"],
            "api_endpoint": "/v1/description-category/attribute",
        },
    )
    warnings.extend(snapshot.get("warnings") or [])
    if match_status != "api_confirmed":
        warnings.append("类目匹配状态为 api_match_needs_review（上传载荷会记录，但不阻断）")
    return {
        "warnings": warnings,
        "artifacts": [SNAPSHOT_FILE, CATEGORY_FILE],
        "match_status": match_status,
        "attributes": len(snapshot["attributes"]),
        "dictionary_attributes": len([a for a in snapshot["attributes"] if a.get("dictionary_id")]),
    }


CATEGORY_HANDLERS = {"category_match": handle_category_match}


def category_handlers(client: OzonClient | None = None) -> dict[str, Any]:
    return dict(CATEGORY_HANDLERS) if client is not None else {}
