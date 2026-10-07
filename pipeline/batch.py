"""批次：把一次"运行"的输入冻结下来，写 ``batches/<batch_id>/batch.json``。

与原项目 ``scripts/pipeline_runtime.py:create_batch`` 对齐的关键语义：

- **create_batch 只接新批次**：已经在跑（QUEUED/PROCESSING/UPLOADING）或已终态的商品一律拒绝；
  恢复既有任务走 :func:`pipeline.status.queue_product`，不重开批次 —— 这样"批次"的输入永远只有一份。
- 选新商品时只取 ``status=COLLECTED``，同一个 1688 offer 只保留**最新一次采集**。
- ``source_url`` 必须含 ``1688.com/offer/``，SKU 数 1–10，且必须通过采集绑定检查才入批。
- ``batch.json`` 里 ``inventory_submission_enabled`` **恒为 false**（原项目硬编码，永不提交库存）。
- 每个商品入批前冻结 SKU 快照，之后的分析/文案/生图/上传都读快照。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .status import (
    ATTENTION_STATES,
    MAX_SELECTED_SKUS,
    OFFER_ID_PATTERN,
    TERMINAL_STATES,
    freeze_sku_run_snapshot,
    load_status,
    now_iso,
    save_status,
    source_snapshot_binding,
)

SCHEMA_VERSION = "1.0.0"

BATCH_TERMINAL_STATES: frozenset[str] = frozenset(
    {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "AWAITING_MANUAL_UPLOAD"}
)

#: 已经在跑的商品不允许再入新批次
BLOCKING_STATES: frozenset[str] = frozenset({"QUEUED", "PROCESSING", "UPLOADING"})


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def offer_id_of(source_url: Any) -> str | None:
    match = OFFER_ID_PATTERN.search(str(source_url or ""))
    return match.group(1) if match else None


def _product_dirs(products_root: Path) -> list[Path]:
    if not products_root.is_dir():
        return []
    return sorted(
        path
        for path in products_root.iterdir()
        if path.is_dir() and (path / "status.json").is_file()
    )


def select_collected(products_root: Path | str) -> list[Path]:
    """取所有 ``status=COLLECTED`` 且带 source.json 的商品；同一 offer 只留最新采集。"""
    root = Path(products_root)
    latest: dict[str, Path] = {}
    for directory in _product_dirs(root):
        status = load_status(directory)
        if str(status.get("status") or "") != "COLLECTED":
            continue
        source_path = directory / "input" / "source.json"
        if not source_path.is_file():
            continue
        source = _read_json(source_path)
        key = offer_id_of(source.get("source_url")) or directory.name
        previous = latest.get(key)
        if previous is None:
            latest[key] = directory
            continue
        try:
            newer = (directory / "status.json").stat().st_mtime
            older = (previous / "status.json").stat().st_mtime
        except OSError:
            newer, older = 0.0, 0.0
        if newer >= older:
            latest[key] = directory
    return [latest[key] for key in sorted(latest)]


def _validate_for_batch(product_dir: Path, *, allow_terminal_store_retry: bool = False) -> dict[str, Any]:
    product_id = product_dir.name
    status_path = product_dir / "status.json"
    source_path = product_dir / "input" / "source.json"
    if not status_path.is_file() or not source_path.is_file():
        raise ValueError(f"Requested product does not exist: {product_id}")

    status = load_status(product_dir)
    state = str(status.get("status") or "").upper()
    api_write_count = int(status.get("api_write_count") or 0)

    if state in BLOCKING_STATES:
        # 已经在跑或排队：交回 queue_product() 恢复，不重开批次
        raise ValueError(f"Requested product is already running or queued: {product_id}")

    terminal_but_retryable = state in ATTENTION_STATES and api_write_count == 0
    if state in TERMINAL_STATES and not terminal_but_retryable and not allow_terminal_store_retry:
        raise ValueError(f"Requested product is already terminal: {product_id}")

    source = _read_json(source_path)
    source_url = str(source.get("source_url") or "")
    if "1688.com/offer/" not in source_url:
        raise ValueError(f"Requested product is not a 1688 capture: {product_id}")

    from .sku_selection import active_skus

    # 按"要上架"的 SKU 计数（选择文件没写时 = 全部）
    skus = active_skus(product_dir, source.get("skus") if isinstance(source.get("skus"), list) else [])
    if not 1 <= len(skus) <= MAX_SELECTED_SKUS:
        raise ValueError(
            f"{product_id}: 已选 SKU 必须在 1–{MAX_SELECTED_SKUS} 之间，实际 {len(skus)}"
        )
    return source


def load_batch(batches_root: Path | str, batch_id: str) -> dict[str, Any]:
    return _read_json(Path(batches_root) / str(batch_id) / "batch.json")


def create_batch(
    products_root: Path | str,
    *,
    product_ids: Sequence[str] | None = None,
    target_store_ids: Sequence[str] = (),
    auto_upload: bool = True,
    product_store_overrides: Mapping[str, Sequence[str]] | None = None,
    allow_terminal_store_retry: bool = False,
    freeze_snapshot: Callable[..., dict[str, Any]] | None = None,
    batches_root: Path | str | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """创建批次并冻结每个商品的输入快照。返回写盘的 ``batch.json`` 内容。"""
    root = Path(products_root)
    batches = Path(batches_root) if batches_root is not None else root.parent / "batches"
    freeze = freeze_snapshot or freeze_sku_run_snapshot
    target_stores = [str(item) for item in target_store_ids]
    overrides = {str(key): [str(x) for x in value] for key, value in (product_store_overrides or {}).items()}

    if product_ids is None:
        selected = select_collected(root)
    else:
        selected = []
        for product_id in product_ids:
            directory = root / str(product_id)
            if not directory.is_dir():
                raise ValueError(f"Requested product does not exist: {product_id}")
            selected.append(directory)

    if not selected:
        raise ValueError("批次没有可处理商品")

    batch_id = str(batch_id or f"B-{uuid.uuid4().hex[:12].upper()}")
    created_at = now_iso()
    review_mode = "automatic" if auto_upload else "manual"
    entries: list[dict[str, Any]] = []
    prepared: list[tuple[Path, dict[str, Any]]] = []

    for directory in selected:
        source = _validate_for_batch(
            directory, allow_terminal_store_retry=allow_terminal_store_retry
        )
        product_id = directory.name
        product_stores = overrides.get(product_id, target_stores)
        binding = source_snapshot_binding(directory)
        warnings: list[str] = []
        if binding is None:
            warnings.append("采集绑定缺失（collection_id / source-manifest 未就绪）")
        from .sku_selection import active_skus

        sku_count = len(active_skus(directory, source.get("skus") or []))
        entries.append(
            {
                "product_id": product_id,
                "selected_sku_count": sku_count,
                "status": "QUEUED",
                "current_step": "queue",
                "started_at": "unknown",
                "completed_at": "unknown",
                "warnings": warnings,
                "errors": [],
                "target_store_ids": product_stores,
                "publication_count": len(product_stores),
                "collection_id": (binding or {}).get("collection_id", "unknown"),
                "source_manifest_sha256": (binding or {}).get("source_manifest_sha256", "unknown"),
                "source_snapshot_binding": binding,
                "auto_upload": bool(auto_upload),
                "review_mode": review_mode,
                "manual_confirmation_required": False,
            }
        )
        prepared.append((directory, entries[-1]))

    batch = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "status": "QUEUED",
        "created_at": created_at,
        "started_at": "unknown",
        "completed_at": "unknown",
        "product_count": len(entries),
        "sku_count": sum(int(item["selected_sku_count"]) for item in entries),
        "processing_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "progress": 0,
        "target_store_ids": target_stores,
        "auto_upload": bool(auto_upload),
        "review_mode": review_mode,
        "manual_upload_required": not bool(auto_upload),
        "inventory_submission_enabled": False,
        "products": entries,
    }
    _write_json_atomic(batches / batch_id / "batch.json", batch)

    # 批次记录写成功之后，才冻结快照并改商品状态（顺序不能反）
    for directory, entry in prepared:
        snapshot = freeze(
            directory,
            batch_id,
            review_mode=entry["review_mode"],
            auto_upload=entry["auto_upload"],
            target_store_ids=entry["target_store_ids"],
        )
        status = load_status(directory)
        previous = status.get("status")
        status.update(
            {
                "batch_id": batch_id,
                "review_mode": entry["review_mode"],
                "auto_upload": entry["auto_upload"],
                "manual_confirmation_required": False,
                "task_authorized": True,
                "status": "QUEUED",
                "current_step": "queue",
                "last_run_at": now_iso(),
                "completed_at": "unknown",
                "failed_step": "unknown",
                "error_code": "unknown",
                "error_message": "unknown",
                "human_message": None,
                "attention_required": False,
                "target_store_ids": entry["target_store_ids"],
                "sku_run_snapshot": {
                    "path": "output/sku-run-snapshot.json",
                    "dependency_hash": snapshot.get("dependency_hash"),
                    "frozen_at": snapshot.get("frozen_at"),
                    "selected_sku_count": snapshot.get("selected_sku_count"),
                },
            }
        )
        if entry["source_snapshot_binding"]:
            status["source_snapshot_binding"] = entry["source_snapshot_binding"]
        else:
            status.pop("source_snapshot_binding", None)
        status.setdefault("history", []).append(
            {
                "from": previous,
                "to": "QUEUED",
                "at": now_iso(),
                "reason": f"User started batch task {batch_id}; no per-product review is required.",
            }
        )
        save_status(directory, status)

    return batch


def update_batch_status(
    batches_root: Path | str,
    batch_id: str,
    **fields: Any,
) -> dict[str, Any]:
    """更新批次汇总字段（供 runner 回写进度/成功失败计数）。"""
    path = Path(batches_root) / str(batch_id) / "batch.json"
    batch = _read_json(path)
    if not batch:
        raise ValueError(f"批次不存在：{batch_id}")
    batch.update(fields)
    _write_json_atomic(path, batch)
    return batch
