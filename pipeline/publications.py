"""多店铺发布台账与分发计划（M4，纯本地）。

两条来自原项目的硬规则：

1. **已收到 Ozon ``task_id`` 的店铺不重复创建** —— 否则同一商品会在同一店铺被建两次；
2. **每家店铺独立保存 offer / task_id / 请求哈希 / 状态 / 错误，单店失败不影响其他店**。

台账落点 ``products/<id>/output/store-publications.json``，形状对齐上游 ``store-publications`` 契约。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "1.0.0"

ACTION_CREATE = "create"
ACTION_SKIP = "skip"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def publications_path(product_dir: Path | str) -> Path:
    return Path(product_dir) / "output" / "store-publications.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def empty_publications(product_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "updated_at": None,
        "stores": {},
    }


def load_publications(product_dir: Path | str) -> dict[str, Any]:
    directory = Path(product_dir)
    payload = _read_json(publications_path(directory))
    if not payload:
        return empty_publications(directory.name)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("product_id", directory.name)
    payload.setdefault("stores", {})
    return payload


def save_publications(product_dir: Path | str, payload: Mapping[str, Any]) -> dict[str, Any]:
    directory = Path(product_dir)
    body = dict(payload)
    body["updated_at"] = now_iso()
    path = publications_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return body


def _store_entry(payload: dict[str, Any], store_id: str, *, selected: bool = True) -> dict[str, Any]:
    stores = payload.setdefault("stores", {})
    entry = stores.get(store_id)
    if not isinstance(entry, dict):
        entry = {"selected": selected, "status": "not_started", "sku_publications": []}
        stores[store_id] = entry
    entry.setdefault("selected", selected)
    entry.setdefault("status", "not_started")
    entry.setdefault("sku_publications", [])
    return entry


def record_publication(
    product_dir: Path | str,
    store_id: str,
    *,
    sku_id: str,
    offer_id: str | None = None,
    task_id: str | None = None,
    ozon_product_id: str | None = None,
    status: str | None = None,
    errors: Sequence[Any] = (),
    publication_db_path: Path | str | None = None,
) -> dict[str, Any]:
    """登记一次（或一条 SKU 的）发布结果；同一 store+sku 覆盖更新。

    每条 SKU 行也保留 ``status``（``submitted`` / ``imported`` / ``failed`` …），
    这样"提交后确认终态"能落在台账上，而不是只留在店铺级别。
    """
    payload = load_publications(product_dir)
    entry = _store_entry(payload, store_id)
    previous = next(
        (item for item in entry["sku_publications"] if str(item.get("sku_id")) == str(sku_id)), {}
    )
    rows = [item for item in entry["sku_publications"] if str(item.get("sku_id")) != str(sku_id)]
    rows.append(
        {
            "sku_id": str(sku_id),
            "offer_id": offer_id,
            "task_id": task_id,
            "ozon_product_id": ozon_product_id,
            "status": status or previous.get("status"),
            "errors": list(errors),
        }
    )
    entry["sku_publications"] = rows
    if status:
        entry["status"] = status
    else:
        entry["status"] = "submitted" if any(item.get("task_id") for item in rows) else entry["status"]
    saved = save_publications(product_dir, payload)
    # Observation only: this hook never writes stock or imports a product.
    from .listing_publications import record_import_observation
    record_import_observation(product_dir, store_id, sku_id=sku_id, offer_id=offer_id,
        task_id=task_id, ozon_product_id=ozon_product_id, status=status, errors=errors,
        db_path=publication_db_path)
    return saved


def store_has_task(product_dir: Path | str, store_id: str) -> bool:
    """该店铺是否已经拿到过 Ozon ``task_id``（拿到就不再重复创建）。"""
    payload = load_publications(product_dir)
    entry = (payload.get("stores") or {}).get(store_id) or {}
    return any(str(item.get("task_id") or "").strip() for item in (entry.get("sku_publications") or []))


def store_status(product_dir: Path | str, store_id: str) -> str:
    payload = load_publications(product_dir)
    entry = (payload.get("stores") or {}).get(store_id) or {}
    return str(entry.get("status") or "not_started")


def plan_publications(
    product_dir: Path | str,
    store_ids: Iterable[str],
    *,
    sku_ids: Sequence[str] | None = None,
    enabled_store_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """生成分发计划：每家店铺是 ``create`` 还是 ``skip``，以及原因。

    ``enabled_store_ids`` 给出时，未启用的店铺会被跳过（防止误传）。
    """
    directory = Path(product_dir)
    payload = load_publications(directory)
    rows: list[dict[str, Any]] = []
    for store_id in dict.fromkeys(str(item) for item in store_ids):
        entry = _store_entry(payload, store_id, selected=True)
        _store_entry(payload, store_id)["selected"] = True
        if enabled_store_ids is not None and store_id not in set(enabled_store_ids):
            rows.append({"store_id": store_id, "action": ACTION_SKIP, "reason": "店铺未启用"})
            continue
        if store_has_task(directory, store_id):
            rows.append(
                {
                    "store_id": store_id,
                    "action": ACTION_SKIP,
                    "reason": "该店铺已拿到 Ozon task_id，按幂等规则不重复创建",
                }
            )
            continue
        if store_status(directory, store_id) in {"uploading", "submitted"}:
            rows.append(
                {
                    "store_id": store_id,
                    "action": ACTION_SKIP,
                    "reason": f"该店铺状态为 {store_status(directory, store_id)}，等待回执",
                }
            )
            continue
        rows.append(
            {
                "store_id": store_id,
                "action": ACTION_CREATE,
                "reason": "尚未提交",
                "sku_count": len(sku_ids or []),
            }
        )
    save_publications(directory, payload)
    return rows


def render_plan(plan: Sequence[Mapping[str, Any]]) -> str:
    lines = ["| 店铺 | 动作 | 原因 |", "|---|---|---|"]
    for row in plan:
        lines.append(f"| {row.get('store_id')} | {row.get('action')} | {row.get('reason')} |")
    return "\n".join(lines)
