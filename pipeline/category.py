"""``category_match`` handler：用 Ozon **只读**接口拉真实类目与属性快照。

门禁要点（与原项目一致）：

- 必须能从采集输入里拿到 ``category_id`` / ``type_id``（没选类目就是硬失败）；
- 快照来源必须是 ``/v1/description-category/attribute``（契约里的 ``api_endpoint`` 常量）；
- 类目在类目树里找不到时**不谎报确认**：写 ``api_match_needs_review`` 并告警；
  类目树整体为空则直接失败（连"存在性"都无法判断）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import format_problems, validate_contract

from .context import PipelineGateError, StepContext
from .ozon_http import (
    OzonClient,
    OzonHttpError,
    _to_int_or_none,
    attach_dictionary_values,
    build_category_snapshot,
    find_category_in_tree,
)

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


# ------------------------------------------------------- 类目名 → 真实 category_id/type_id

BINDINGS_PATH = Path("config") / "category-bindings.json"


def _iter_tree(
    tree_response: Mapping[str, Any],
    *,
    path: list[str] | None = None,
    inherited_category: int | None = None,
    inherited_name: str | None = None,
) -> list[dict[str, Any]]:
    """把类目树摊平成叶子列表（type 级），带上完整路径。"""
    rows: list[dict[str, Any]] = []
    for node in tree_response.get("result") or []:
        if not isinstance(node, Mapping):
            continue
        name = str(node.get("category_name") or node.get("type_name") or node.get("name") or "")
        here = [*(path or []), name] if name else list(path or [])
        category_id = _to_int_or_none(node.get("description_category_id") or node.get("category_id")) or inherited_category
        category_name = str(node.get("category_name") or inherited_name or "")
        type_id = _to_int_or_none(node.get("type_id"))
        if type_id is not None and category_id is not None:
            rows.append(
                {
                    "category_id": int(category_id),
                    "type_id": int(type_id),
                    "category_name": category_name or name,
                    "type_name": str(node.get("type_name") or name),
                    "path": here,
                }
            )
        children = [child for child in (node.get("children") or []) if isinstance(child, Mapping)]
        if children:
            rows.extend(
                _iter_tree(
                    {"result": children},
                    path=here,
                    inherited_category=category_id,
                    inherited_name=category_name or name,
                )
            )
    return rows


def search_tree(tree_response: Mapping[str, Any], query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """按名字在类目树里找候选（中文/俄文都能匹配）；精确 > 前缀 > 包含。"""
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    rows = _iter_tree(tree_response)
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        names = [str(row.get("category_name") or ""), str(row.get("type_name") or ""), *[str(item) for item in row.get("path") or []]]
        folded = [item.casefold() for item in names if item]
        if not any(needle in item for item in folded):
            continue
        if any(item == needle for item in folded):
            rank = 0
        elif any(item.startswith(needle) for item in folded):
            rank = 1
        else:
            rank = 2
        scored.append((rank, row))
    scored.sort(key=lambda item: (item[0], len(item[1]["path"]), item[1]["type_id"]))
    return [row for _, row in scored[: max(1, int(limit))]]


def resolve_bindings(
    tree_response: Mapping[str, Any],
    names: Sequence[str],
    *,
    limit_per_name: int = 3,
) -> dict[str, Any]:
    """给一批类目名找候选；返回 ``{name: {"candidates": [...], "status": ...}}``。"""
    resolved: dict[str, Any] = {}
    for name in names:
        candidates = search_tree(tree_response, name, limit=limit_per_name)
        resolved[str(name)] = {
            "status": "matched" if candidates else "not_found",
            "candidates": candidates,
        }
    return resolved


def load_bindings(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path else BINDINGS_PATH
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def save_bindings(bindings: Mapping[str, Any], path: Path | str | None = None) -> Path:
    target = Path(path) if path else BINDINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bindings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def binding_for(bindings: Mapping[str, Any], *names: Any) -> dict[str, Any] | None:
    """按中文/俄文类目名查已保存的绑定（用于 Seerfar 导入时套用真实类目）。"""
    table = bindings.get("bindings") if isinstance(bindings.get("bindings"), Mapping) else bindings
    for name in names:
        key = str(name or "").strip()
        if not key:
            continue
        entry = (table or {}).get(key)
        if isinstance(entry, Mapping) and entry.get("category_id") and entry.get("type_id"):
            return {
                "category_id": str(entry["category_id"]),
                "type_id": str(entry["type_id"]),
                "name": key,
                "source": entry.get("source") or "bindings",
            }
    return None


def bind_categories(
    names: Sequence[str],
    *,
    client: OzonClient | None = None,
    fixture_dir: Path | str | None = None,
    bindings_path: Path | str | None = None,
    limit_per_name: int = 3,
    auto_pick_unique: bool = False,
) -> dict[str, Any]:
    """把类目名解析成真实 id 并写入 ``config/category-bindings.json``。"""
    if client is None:
        if fixture_dir:
            from .ozon_http import FixtureTransport, OzonClient as _Client

            client = _Client(FixtureTransport(directory=Path(fixture_dir)))
        else:
            raise ValueError("需要 Ozon 只读客户端：给 client，或用 fixture_dir 离线演练")
    tree = client.fetch_category_tree()
    resolved = resolve_bindings(tree, names, limit_per_name=limit_per_name)

    bindings: dict[str, Any] = {}
    unmatched: list[str] = []
    for name, body in resolved.items():
        candidates = body["candidates"]
        if not candidates:
            unmatched.append(name)
            continue
        picked = candidates[0]
        if auto_pick_unique:
            strong = [
                item
                for item in candidates
                if name.casefold() in {str(item["type_name"]).casefold(), str(item["category_name"]).casefold()}
            ]
            if len(strong) == 1:
                picked = strong[0]
            elif len(strong) > 1:
                unmatched.append(name)
                continue
        bindings[name] = {
            "category_id": picked["category_id"],
            "type_id": picked["type_id"],
            "category_name": picked["category_name"],
            "type_name": picked["type_name"],
            "path": picked["path"],
            "source": "ozon_seller_api",
            "candidates": candidates if not auto_pick_unique else candidates[:1],
        }

    document = {
        "schema_version": "1.0.0",
        "generated_at": now_iso(),
        "source": "ozon_seller_api",
        "bindings": bindings,
        "unmatched": unmatched,
        "warnings": (
            ["以下类目名在类目树里没找到，需人工指定： " + "、".join(unmatched)] if unmatched else []
        ),
    }
    path = save_bindings(document, bindings_path)
    return {"ok": True, "written": str(path), "bound": len(bindings), "unmatched": unmatched, "bindings": bindings}


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="类目名 → Ozon 真实 category_id/type_id（只读）")
    parser.add_argument("--name", action="append", dest="names", help="类目名（可重复，支持中文/俄文）")
    parser.add_argument("--from-library", default=None, help="从关键词库的类目名集合解析（传库目录）")
    parser.add_argument("--fixture-dir", default=None, help="用夹具代替真实网络（离线演练）")
    parser.add_argument("--shop", default=None, help="真实模式下的店铺 id（默认第一家）")
    parser.add_argument("--bindings", default=None, help="绑定文件路径（默认 config/category-bindings.json）")
    parser.add_argument("--auto-pick-unique", action="store_true", help="唯一精确匹配时自动选定")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    names = list(args.names or [])
    if args.from_library:
        from keyword_library import store as keyword_store

        seen: list[str] = []
        for record in keyword_store.load_all(args.from_library):
            extra = record.get("extra") if isinstance(record.get("extra"), Mapping) else {}
            for candidate in (extra.get("category_name_zh"), extra.get("category_name_ru")):
                text = str(candidate or "").strip()
                if text and text not in seen:
                    seen.append(text)
        names.extend(seen)
    if not names:
        parser.error("至少给一个 --name，或用 --from-library 从关键词库取类目名")

    client = None
    if not args.fixture_dir:
        from .ozon_http import OzonClient, OzonCredentials, UrllibTransport
        from .stores import list_shops, load_registry

        shops = list_shops(load_registry())
        shop = next((item for item in shops if str(item.get("id")) == str(args.shop)), None) if args.shop else (shops[0] if shops else None)
        if not shop:
            print(json.dumps({"ok": False, "error": "没有可用店铺：先配置 config/shops.json"}, ensure_ascii=False, indent=2))
            return 1
        try:
            client = OzonClient(UrllibTransport(OzonCredentials.from_shop(shop)))
        except OzonHttpError as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
            return 1

    try:
        summary = bind_categories(
            names,
            client=client,
            fixture_dir=args.fixture_dir,
            bindings_path=args.bindings,
            auto_pick_unique=args.auto_pick_unique,
        )
    except (OzonHttpError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"已写入 {summary['written']}：绑定 {summary['bound']} 个类目名")
        for name, body in summary["bindings"].items():
            print(f"  {name} → category_id={body['category_id']} type_id={body['type_id']}（{body['category_name']} / {body['type_name']}）")
        for name in summary["unmatched"]:
            print(f"  ⚠️ {name}: 没找到候选，需要人工指定")
    return 0 if summary["bound"] else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
