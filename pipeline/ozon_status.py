"""提交后的终态跟踪：查 ``/v1/product/import/info``（**只读**）并回写台账。

Ozon 的 ``/v3/product/import`` 只返回任务号，最终结果要另查一次。这里把这步做完整：

- ``confirm_task()``：按次数/间隔轮询，直到所有条目进入终态或超时（**只读，不产生任何写请求**）；
- ``apply_confirmation()``：把逐项结果（``imported`` / ``failed`` + 错误码）写进
  ``output/store-publications.json``、``output/store-runs/<店铺>/import-info.json`` 与 ``ozon-result.json``；
- ``upload_product()`` 在拿到 ``task_id`` 后，如果 uploader 支持 ``confirm``，会自动确认一次；
- 也可以事后手动确认：``python -m pipeline.ozon_status --product-dir <商品> --store <店铺>``
  （支持 ``--fixture`` 离线演练）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .ozon_http import OzonCredentials, OzonHttpError, Transport, UrllibTransport
from .ozon_write import PATH_IMPORT_INFO
from .publications import load_publications, record_publication, save_publications

SCHEMA_VERSION = "1.0.0"

#: Ozon 逐项终态 / 非终态
TERMINAL_ITEM_STATUSES = {"imported", "failed", "not_created", "cancelled", "skipped", "rejected"}
PENDING_ITEM_STATUSES = {"pending", "processing", "in_progress", "unknown", ""}

IMPORT_INFO_FILE = "import-info.json"
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_INTERVAL_SECONDS = 5.0


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------- 解析


def parse_import_info(
    response: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """解析 ``/v1/product/import/info`` 响应 → 逐项状态 + 是否全部终态。"""
    result = response.get("result") if isinstance(response.get("result"), Mapping) else response
    task_id = result.get("task_id")
    raw_items = [item for item in (result.get("items") or []) if isinstance(item, Mapping)]

    offer_to_sku: dict[str, str] = {}
    for index, variant in enumerate(((payload or {}).get("variants") or []), start=1):
        if isinstance(variant, Mapping):
            offer_to_sku[str(variant.get("offer_id"))] = str(variant.get("source_sku_id") or f"S{index}")

    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for raw in raw_items:
        offer_id = str(raw.get("offer_id") or "")
        item_errors = [
            {
                "code": str(item.get("code") or "OZON_ITEM_ERROR"),
                "message": str(item.get("message") or ""),
                "attribute_id": item.get("attribute_id"),
                "level": item.get("level"),
                "field": item.get("field"),
                "description": item.get("description"),
            }
            for item in (raw.get("errors") or [])
            if isinstance(item, Mapping)
        ]
        status = str(raw.get("status") or "unknown").strip().lower()
        product_id = raw.get("product_id")
        # An "imported" label cannot erase an attached error. Preserve the raw
        # label for diagnosis; only a clean item counts as imported.
        blocking_errors = [item for item in item_errors
            if str(item.get("level") or "").casefold() not in {"warning", "warn", "info", "information"}]
        effective_status = "failed" if status == "imported" and blocking_errors else status
        if effective_status == "imported" and not (type(product_id) is int and product_id > 0):
            effective_status = "unknown"
        items.append(
            {
                "source_sku_id": offer_to_sku.get(offer_id) or offer_id or "unknown",
                "offer_id": offer_id or "unknown",
                "product_id": int(product_id) if isinstance(product_id, int) and product_id > 0 else None,
                "status": effective_status,
                "raw_status": status,
                "errors": item_errors,
            }
        )
        errors.extend(item_errors)

    counts = {
        "total": len(items),
        "imported": len([item for item in items if item["status"] == "imported"]),
        "failed": len([item for item in items if item["status"] in {"failed", "rejected", "not_created", "cancelled"}]),
        "pending": len([item for item in items if item["status"] in PENDING_ITEM_STATUSES]),
    }
    unknown = [
        item["status"]
        for item in items
        if item["status"] not in TERMINAL_ITEM_STATUSES and item["status"] not in PENDING_ITEM_STATUSES
    ]
    returned_offers = [item["offer_id"] for item in items]
    expected_offers = set(offer_to_sku)
    missing_offers = sorted(expected_offers - set(returned_offers))
    unexpected_offers = sorted(set(returned_offers) - expected_offers) if expected_offers else []
    duplicate_offers = sorted(offer for offer in set(returned_offers) if returned_offers.count(offer) > 1)
    total = result.get("total")
    reported_total = total if type(total) is int and total >= 0 else None
    incomplete_total = reported_total is not None and reported_total != len(items)
    terminal = bool(items) and counts["pending"] == 0 and not unknown and not (
        missing_offers or unexpected_offers or duplicate_offers or incomplete_total)
    return {
        "task_id": str(task_id) if task_id not in (None, "", 0) else None,
        "items": items,
        "errors": errors,
        "counts": counts,
        "unknown_statuses": sorted(set(unknown)),
        "terminal": terminal,
        "missing_offers": missing_offers,
        "unexpected_offers": unexpected_offers,
        "duplicate_offers": duplicate_offers,
        "reported_total": reported_total,
        "response_complete": not (missing_offers or unexpected_offers or duplicate_offers or incomplete_total),
    }


# --------------------------------------------------------------------- 轮询（只读）


def confirm_task(
    transport: Transport,
    task_id: str | int,
    *,
    payload: Mapping[str, Any] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """轮询 import 任务直到终态或超时。**只读**：只调 ``/v1/product/import/info``。"""
    attempts = 0
    history: list[dict[str, Any]] = []
    parsed: dict[str, Any] = {"items": [], "counts": {"total": 0, "imported": 0, "failed": 0, "pending": 0}, "terminal": False, "errors": []}
    raw_response: Mapping[str, Any] | None = None
    while attempts < max(1, int(max_attempts)):
        attempts += 1
        try:
            raw_response = transport.post(PATH_IMPORT_INFO, {"task_id": int(task_id)})
        except (OzonHttpError, ValueError) as error:
            return {
                "task_id": str(task_id),
                "attempts": attempts,
                "terminal": False,
                "confirmed": False,
                "error": f"查询 import 状态失败：{error}",
                "items": [],
                "counts": {"total": 0, "imported": 0, "failed": 0, "pending": 0},
                "errors": [],
                "history": history,
                "checked_at": now_iso(),
            }
        parsed = parse_import_info(raw_response, payload=payload)
        history.append({"attempt": attempts, "counts": parsed["counts"], "terminal": parsed["terminal"]})
        if parsed["terminal"]:
            break
        if attempts < max(1, int(max_attempts)):
            sleep(interval_seconds)

    return {
        "task_id": str(task_id),
        "attempts": attempts,
        "terminal": bool(parsed.get("terminal")),
        "confirmed": bool(parsed.get("terminal")),
        "timed_out": not bool(parsed.get("terminal")),
        "items": parsed.get("items") or [],
        "counts": parsed.get("counts") or {"total": 0, "imported": 0, "failed": 0, "pending": 0},
        "errors": parsed.get("errors") or [],
        "unknown_statuses": parsed.get("unknown_statuses") or [],
        "missing_offers": parsed.get("missing_offers") or [],
        "response_complete": parsed.get("response_complete", False),
        "history": history,
        "raw_response": dict(raw_response) if isinstance(raw_response, Mapping) else None,
        "checked_at": now_iso(),
    }


# --------------------------------------------------------------------- 回写

STORE_STATUS_BY_CONFIRMATION = {
    "imported": "created",
    "failed": "failed",
}


def _sku_for_offer(product_dir: Path | str, store_id: str, offer_id: str) -> str | None:
    """在台账里按 offer_id 找已有的 sku_id。"""
    if not offer_id:
        return None
    payload = load_publications(product_dir)
    entry = (payload.get("stores") or {}).get(store_id) or {}
    for row in entry.get("sku_publications") or []:
        if str(row.get("offer_id") or "") == offer_id and row.get("sku_id"):
            return str(row["sku_id"])
    return None


def apply_confirmation(
    product_dir: Path | str,
    *,
    store_id: str,
    confirmation: Mapping[str, Any],
    task_id: str | None = None,
) -> dict[str, Any]:
    """把确认结果写进台账、店铺运行目录与 ozon-result.json。"""
    directory = Path(product_dir)
    resolved_task = str(task_id or confirmation.get("task_id") or "") or None
    counts = confirmation.get("counts") or {}
    items = [item for item in (confirmation.get("items") or []) if isinstance(item, Mapping)]

    run_dir = directory / "output" / "store-runs" / store_id
    _write_json(
        run_dir / IMPORT_INFO_FILE,
        {
            "schema_version": SCHEMA_VERSION,
            "product_id": directory.name,
            "shop_name": store_id,
            "task_id": resolved_task,
            "checked_at": confirmation.get("checked_at") or now_iso(),
            "attempts": confirmation.get("attempts"),
            "terminal": bool(confirmation.get("terminal")),
            "timed_out": bool(confirmation.get("timed_out")),
            "error": confirmation.get("error"),
            "counts": counts,
            "items": items,
            "history": confirmation.get("history") or [],
            "api_writes_performed": False,
            "note": "只读查询 /v1/product/import/info，不产生写请求",
        },
    )

    for item in items:
        offer_id = str(item.get("offer_id") or "")
        sku_id = str(item.get("source_sku_id") or "")
        # 台账里已有同 offer_id 的行时，以台账的 sku_id 为准（避免事后确认时把同一 SKU 记成两条）
        known = _sku_for_offer(directory, store_id, offer_id)
        if known and (not sku_id or sku_id == offer_id):
            sku_id = known
        record_publication(
            directory,
            store_id,
            sku_id=sku_id or offer_id or "unknown",
            offer_id=offer_id or None,
            task_id=resolved_task,
            ozon_product_id=str(item["product_id"]) if item.get("product_id") else None,
            status=str(item.get("status") or "unknown"),
            errors=item.get("errors") or [],
        )

    # 店铺级状态：全部成功 → created；有失败且无成功 → failed；否则保持 submitted/processing
    payload = load_publications(directory)
    entry = (payload.get("stores") or {}).get(store_id) or {}
    rows = entry.get("sku_publications") or []
    if items:
        # Old task-only ledgers used a '*' placeholder. Once real offers are
        # available it must not prevent an otherwise complete task becoming
        # terminal, or be mistaken for another submitted SKU.
        rows = [row for row in rows if str(row.get("sku_id") or "") != "*"]
        entry["sku_publications"] = rows
    statuses = {str(row.get("status") or "") for row in rows}
    imported = counts.get("imported") or 0
    failed = counts.get("failed") or 0
    if confirmation.get("error") or not confirmation.get("terminal"):
        store_status = "processing"
    elif rows and statuses and statuses <= {"imported"}:
        store_status = "created"
    elif failed and not imported:
        store_status = "failed"
    elif imported:
        store_status = "partially_created"
    else:
        store_status = entry.get("status") or "submitted"
    entry["status"] = store_status
    entry["task_id"] = resolved_task
    entry["import_confirmed_at"] = confirmation.get("checked_at") or now_iso()
    entry["import_counts"] = counts
    # A clean import creates the card, not proof that asynchronous video
    # ingestion/moderation has completed. Only explicit media readback can add
    # that separate observational evidence; never submit again from polling.
    video_payload = _read_json(run_dir / "payload.json")
    from .ozon_write import media_for_variant
    video_expected = any(media_for_variant(video_payload, row)[0]
                         for row in video_payload.get("variants") or [] if isinstance(row, Mapping))
    if video_expected:
        # Keep observational extensions outside the strict upstream result and
        # publication contracts. Import polling never claims video acceptance.
        _write_json(run_dir / "video-import-status.json", {
            "product_id": directory.name, "store": store_id,
            "video_readback_status": "awaiting_readback", "video_readback_required": True,
            "import_status": store_status, "buyer_playback_verified": False,
            "automatic_resubmit": False, "updated_at": now_iso(),
        })
    payload["updated_at"] = now_iso()
    _write_json(directory / "output/store-publications.json", payload)

    result_path = run_dir / "ozon-result.json"
    if result_path.is_file():
        result = _read_json(result_path)
        if result:
            from .upload import _contract_task_id

            result["task_id"] = _contract_task_id(resolved_task)
            if store_status == "created":
                result["status"] = "created"
                result["error_code"] = None
                result["error_message"] = None
                result["failed_step"] = None
                result["errors"] = []
            elif store_status == "failed":
                result["status"] = "failed"
                result["error_code"] = "OZON_IMPORT_FAILED"
                result["error_message"] = str((confirmation.get("errors") or [{}])[0].get("message") or "Ozon 拒绝了导入")
                result["errors"] = list(confirmation.get("errors") or [])
            else:
                result["status"] = "processing"
            result["moderation_status"] = "unknown"
            _write_json(result_path, result)

    return {
        "store_id": store_id,
        "task_id": resolved_task,
        "status": store_status,
        "counts": counts,
        "terminal": bool(confirmation.get("terminal")),
        "items": items,
        "ok": store_status not in {"failed"} and not bool(confirmation.get("error")),
        "error": confirmation.get("error"),
        "missing_offers": list(confirmation.get("missing_offers") or []),
        "video_readback_required": video_expected,
        "import_success_is_video_success": False,
    }


# --------------------------------------------------------------------- 事后确认


def confirm_product(
    product_dir: Path | str,
    store_id: str | None = None,
    *,
    task_id: str | None = None,
    transport_factory: Callable[[OzonCredentials], Transport] | None = None,
    registry_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """对已提交的店铺做一次（或多次）状态确认，并回写台账。"""
    directory = Path(product_dir)
    payload_doc = load_publications(directory)
    stores = payload_doc.get("stores") or {}
    targets = [store_id] if store_id else list(stores.keys())
    if not targets:
        raise ValueError("台账里没有已提交的店铺（output/store-publications.json）")

    from .upload import _read_json as _read

    results: dict[str, Any] = {}
    for target in targets:
        entry = stores.get(target) or {}
        payload = _read(directory / "output" / "store-runs" / str(target) / "payload.json")
        run_dir = directory / "output/store-runs" / str(target)
        receipt = _read(run_dir / "submission-receipt.json")
        legacy_result = _read(run_dir / "ozon-result.json")
        resolved = task_id or entry.get("task_id") or receipt.get("task_id") or next(
            (str(row.get("task_id")) for row in (entry.get("sku_publications") or []) if row.get("task_id")),
            None,
        ) or legacy_result.get("task_id")
        if str(resolved or "").strip() in {"", "unknown", "0"}:
            resolved = None
        if not resolved:
            results[target] = {"status": "skipped", "reason": "该店铺没有 task_id（还没提交成功）"}
            continue
        try:
            transport = _transport_for_store(
                target,
                transport_factory=transport_factory,
                registry_path=registry_path,
                env=env,
            )
        except Exception as error:  # noqa: BLE001 - 交给调用方显示
            results[target] = {"status": "failed", "reason": str(error)}
            continue
        confirmation = confirm_task(
            transport,
            resolved,
            payload=payload,
            max_attempts=max_attempts,
            interval_seconds=interval_seconds,
            sleep=sleep,
        )
        results[target] = apply_confirmation(
            directory, store_id=target, confirmation=confirmation, task_id=resolved
        )
    return {
        "ok": bool(results) and all(row.get("ok", False) for row in results.values()),
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "checked_at": now_iso(),
        "stores": results,
        "api_writes_performed": False,
    }


def _transport_for_store(
    store_id: str,
    *,
    transport_factory: Callable[[OzonCredentials], Transport] | None,
    registry_path: Path | str | None,
    env: Mapping[str, str] | None,
) -> Transport:
    from .stores import list_shops, load_registry

    registry = load_registry(registry_path)
    shop = next((item for item in list_shops(registry) if str(item.get("id")) == str(store_id)), None)
    if not shop:
        raise ValueError(f"店铺 {store_id} 不在注册表里（config/shops.json）")
    try:
        credentials = OzonCredentials.from_shop(shop, env)
    except OzonHttpError:
        if transport_factory is None:
            raise
        # 显式注入传输层（夹具演练）时不需要真凭据
        credentials = OzonCredentials(client_id="fixture", api_key="fixture", shop_id=str(store_id))
    factory = transport_factory or (lambda creds: UrllibTransport(creds))
    return factory(credentials)


# --------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确认 Ozon import 任务的最终状态（只读）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--store", default=None, help="不指定则确认台账里所有已提交店铺")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--registry", default=None)
    parser.add_argument("--fixture", default=None, help="用夹具响应代替真实网络（离线演练）")
    args = parser.parse_args(argv)

    factory = None
    if args.fixture:
        from .ozon_http import FixtureTransport

        fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        factory = lambda credentials: FixtureTransport({PATH_IMPORT_INFO: fixture})  # noqa: E731

    try:
        summary = confirm_product(
            args.product_dir,
            args.store,
            task_id=args.task_id,
            transport_factory=factory,
            registry_path=args.registry,
            max_attempts=args.attempts,
            interval_seconds=args.interval,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
    failed = [store for store, body in summary["stores"].items() if body.get("status") == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
