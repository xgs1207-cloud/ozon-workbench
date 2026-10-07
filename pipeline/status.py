"""商品状态机：对齐原项目 ``templates/status.schema.json`` 与 ``scripts/pipeline_runtime.py``。

**已知的来源差异（不要当 bug 修掉）**：

- ``status.schema.json`` 的 status 枚举里没有 ``FAILED``，而 ``pipeline_runtime.py`` 的
  ``ATTENTION_STATES`` 含 ``FAILED``（疑为历史遗留）。本模块**读取时容忍** ``FAILED``，
  但写入统一用 ``NEEDS_ATTENTION``。
- 断点就是 ``products/<product_id>/status.json``，没有 SQLite（``runtime/task-db.sqlite3`` 被 gitignore）。
- ``task_authorized=True`` 只由"运行/继续"路径写入（原项目注释：*Run Task is the only authorization*）。

设计目标：**纯状态，不碰模型层、不碰 Ozon API**，这样 M1–M4 都能共用，且可以离线测试。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .steps import (
    MAX_SELECTED_SKUS,
    PIPELINE_STEPS,
    STEP_STATUS,
    contiguous_prefix,
    is_pipeline_step,
    ordered,
)

SCHEMA_VERSION = "1.0.0"

PRODUCT_ID_PATTERN = re.compile(r"^P[0-9]{6}$")
COLLECTION_ID_PATTERN = re.compile(r"^COL-[A-Za-z0-9._-]{8,80}$")
OFFER_ID_PATTERN = re.compile(r"/offer/(\d{6,})")

#: 来自 status.schema.json 的 status 枚举，外加 runtime 会用到的 FAILED
STATUS_ENUM: tuple[str, ...] = (
    "COLLECTING",
    "COLLECTED",
    "QUEUED",
    "PROCESSING",
    "CATEGORY_MATCHED",
    "CONTENT_GENERATED",
    "IMAGES_GENERATED",
    "PRICED",
    "WAITING_MANUAL_REVIEW",
    "STOPPED",
    "UPLOADING",
    "UPLOADED",
    "PENDING_REMOTE",
    "HANDED_OFF_TO_OZON",
    "NEEDS_ATTENTION",
    "OZON_MODERATION",
    "ACTIVE",
    "ARCHIVED",
    "FAILED",
)

STATUS_BEFORE_REMOTE: tuple[str, ...] = (
    "COLLECTING",
    "COLLECTED",
    "QUEUED",
    "PROCESSING",
    "CATEGORY_MATCHED",
    "CONTENT_GENERATED",
    "IMAGES_GENERATED",
    "PRICED",
    "WAITING_MANUAL_REVIEW",
    "STOPPED",
)

#: 已经和 Ozon 产生过交集的状态：此时不许再把断点"截断成连续前缀"
REMOTE_STATES: frozenset[str] = frozenset(
    {"UPLOADING", "UPLOADED", "PENDING_REMOTE", "HANDED_OFF_TO_OZON", "OZON_MODERATION", "ACTIVE"}
)

TERMINAL_STATES: frozenset[str] = frozenset(
    {
        "UPLOADED",
        "OZON_MODERATION",
        "ACTIVE",
        "PENDING_REMOTE",
        "HANDED_OFF_TO_OZON",
        "NEEDS_ATTENTION",
        "ARCHIVED",
    }
)

ATTENTION_STATES: frozenset[str] = frozenset({"NEEDS_ATTENTION", "FAILED"})

STEP_NAME_EXTRA: tuple[str, ...] = ("none", "collect_source", "queue", "complete", "pricing", "rich_content",
                                    "download_images", "write_source", "qc", "ozon_draft", "human_review",
                                    "ozon_preflight", "ozon_update", "ozon_status", "archived", "manual_ozon_upload")

#: 需要人工回答的"关键歧义"信号（原项目 maybe_create_operator_question 的口径）
CRITICAL_SIGNALS: tuple[str, ...] = (
    "sku对应", "sku mapping", "sku mismatch", "变体对应", "商品结构", "product structure",
    "配件数量", "accessory count", "错商品", "product identity", "参考图", "reference image",
    "颜色对应", "color mapping",
)
AMBIGUITY_SIGNALS: tuple[str, ...] = (
    "无法确认", "不能确认", "不明确", "歧义", "线索", "不一致", "冲突",
    "unknown", "ambiguous", "missing", "mismatch", "conflict", "cannot determine",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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


def product_dir(products_root: Path | str, product_id: str) -> Path:
    if not PRODUCT_ID_PATTERN.match(str(product_id)):
        raise ValueError(f"产品编号格式非法（应为 P + 6 位数字）：{product_id}")
    return Path(products_root) / str(product_id)


# ---------------------------------------------------------------- 状态构造 / 读写


def new_status(
    product_id: str,
    *,
    collection_id: str | None = None,
    source_url: str | None = None,
    status: str = "COLLECTED",
) -> dict[str, Any]:
    """新建一份通过 ``validate_status`` 的初始状态。"""
    if not PRODUCT_ID_PATTERN.match(str(product_id)):
        raise ValueError(f"产品编号格式非法（应为 P + 6 位数字）：{product_id}")
    if status not in STATUS_ENUM:
        raise ValueError(f"未知状态：{status}")
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "status": status,
        "current_step": "collect_source",
        "progress": 0,
        "started_at": now_iso(),
        "completed_at": "unknown",
        "error_code": "unknown",
        "error_message": "unknown",
        "human_message": None,
        "warnings": [],
        "retry_count": 0,
        "task_authorized": False,
        "auto_upload": False,
        "review_mode": "automatic",
        "manual_confirmation_required": False,
        "attention_required": False,
        "host_recovery_state": "normal",
        "host_recovery_reason": "unknown",
        "ai_service_state": "normal",
        "ai_service_reason": "unknown",
        "ai_service_retry_after": "unknown",
        "ai_service_retry_count": 0,
        "active_step": None,
        "batch_id": "unknown",
        "completed_steps": ["collect_source"],
        "pending_steps": list(PIPELINE_STEPS),
        "failed_step": "unknown",
        "next_action": PIPELINE_STEPS[0],
        "retry_count_by_step": {},
        "image_slot_retry_count_by_slot": {},
        "api_write_count": 0,
        "last_run_at": "unknown",
        "collection_id": collection_id or "unknown",
        "source_url": source_url or "unknown",
        "ozon": {
            "upload_status": "not_started",
            "product_id": "unknown",
            "offer_id": "unknown",
            "task_id": "unknown",
            "shop_name": "unknown",
            "last_response": None,
            "errors": [],
        },
        "steps": [],
        "history": [],
    }


def load_status(product_dir: Path | str) -> dict[str, Any]:
    return _read_json(Path(product_dir) / "status.json")


def save_status(product_dir: Path | str, status: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(status)
    _write_json_atomic(Path(product_dir) / "status.json", payload)
    return payload


def _is_remote_or_written(status: Mapping[str, Any]) -> bool:
    return (
        str(status.get("status") or "").upper() in REMOTE_STATES
        or int(status.get("api_write_count") or 0) > 0
    )


def _computed_next(completed: Sequence[str]) -> tuple[list[str], str, int]:
    done = set(completed)
    pending = [step for step in PIPELINE_STEPS if step not in done]
    next_action = pending[0] if pending else "complete"
    progress = 100 if not pending else min(99, round(100 * len(completed) / (len(PIPELINE_STEPS) + 1)))
    return pending, next_action, progress


def normalize(status: Mapping[str, Any]) -> dict[str, Any]:
    """补齐缺省字段、按流水线顺序重排已完成步骤，并重算 pending/next_action/progress。

    **未提交过**（``api_write_count == 0`` 且不在远端状态）时，只保留连续前缀 ——
    这样旧顺序留下的乱序断点不会让流程跳步。
    """
    result: dict[str, Any] = dict(status)
    for key, value in new_status(str(result.get("product_id") or "P000000")).items():
        result.setdefault(key, value)
    result["warnings"] = list(result.get("warnings") or [])
    result["steps"] = list(result.get("steps") or [])
    result["history"] = list(result.get("history") or [])
    result["retry_count_by_step"] = dict(result.get("retry_count_by_step") or {})
    result["image_slot_retry_count_by_slot"] = dict(result.get("image_slot_retry_count_by_slot") or {})
    result["api_write_count"] = int(result.get("api_write_count") or 0)
    result["retry_count"] = int(result.get("retry_count") or 0)
    ozon = dict(result.get("ozon") or {})
    for key, value in new_status(str(result.get("product_id") or "P000000"))["ozon"].items():
        ozon.setdefault(key, value)
    result["ozon"] = ozon

    completed = ordered(result.get("completed_steps"))
    if not _is_remote_or_written(result):
        completed = contiguous_prefix(completed)
    result["completed_steps"] = completed

    pending, next_action, progress = _computed_next(completed)
    result["pending_steps"] = pending
    result["progress"] = progress

    active = result.get("active_step")
    if isinstance(active, dict) and str(active.get("name") or "") in set(completed):
        result["active_step"] = None

    current = str(result.get("current_step") or "")
    if current == "queue":
        pass
    elif not pending:
        result["current_step"] = "complete"
    elif current in set(completed) or not current or current == "none":
        result["current_step"] = next_action

    previous_next = result.get("next_action")
    if previous_next not in PIPELINE_STEPS and previous_next != "complete":
        result["next_action"] = next_action
    elif previous_next in PIPELINE_STEPS and previous_next in set(completed):
        result["next_action"] = next_action
    result.setdefault("next_action", next_action)
    result["failed_step"] = result.get("failed_step") or "unknown"
    result["error_code"] = result.get("error_code") or "unknown"
    result["error_message"] = result.get("error_message") or "unknown"
    return result


# ---------------------------------------------------------------- 运行 / 继续 / 失败


def queue_product(
    product_dir_path: Path | str,
    batch_id: str,
    *,
    review_mode: str = "automatic",
    auto_upload: bool = True,
    target_store_ids: Sequence[str] = (),
    sku_count: int | None = None,
) -> dict[str, Any]:
    """把商品放进某个批次（"运行 / 继续"路径），分支判定与原项目一致，外加一支：

    - ``same_batch_resume``：同批次续跑，**保留 completed_steps 与重试计数**
    - ``auto_upload_resume``：等人工确认但已授权自动上传 → 直接推进到 ``ozon_upload``
    - ``attention_resume``：**有意新增**。原项目把 NEEDS_ATTENTION 归进 TERMINAL_STATES，
      导致"失败后重试"会走整体重置、丢掉断点；工作台的"恢复任务"必须保住断点，故单列一支。
    - ``checkpoint_resume``：已授权且有断点（非终态）→ 置 QUEUED
    - 否则：整体重置（归档 warnings、清空重试计数、删 image-regeneration-request）
    """
    if not PRODUCT_ID_PATTERN.match(Path(product_dir_path).name):
        raise ValueError(f"产品目录名非法：{Path(product_dir_path).name}")
    status = normalize(load_status(product_dir_path))

    if sku_count is None:
        from .sku_selection import active_skus

        source = _read_json(Path(product_dir_path) / "input" / "source.json")
        skus = source.get("skus")
        sku_count = len(active_skus(product_dir_path, skus)) if isinstance(skus, list) else None
    if sku_count is not None and not 1 <= int(sku_count) <= MAX_SELECTED_SKUS:
        raise ValueError(
            f"{Path(product_dir_path).name}: 已选 SKU 必须在 1–{MAX_SELECTED_SKUS} 之间，实际 {sku_count}"
        )

    batch_id = str(batch_id)
    state = str(status.get("status") or "")
    next_action = status.get("next_action")
    ozon = status.get("ozon") or {}

    same_batch_resume = (
        str(status.get("batch_id") or "") == batch_id
        and status.get("task_authorized") is True
        and state in {"QUEUED", "PROCESSING", "STOPPED", *ATTENTION_STATES}
        and next_action in PIPELINE_STEPS
    )
    auto_upload_resume = (
        state == "WAITING_MANUAL_REVIEW"
        and bool(auto_upload)
        and status.get("manual_confirmation_required") is not True
        and int(status.get("api_write_count") or 0) == 0
        and (
            next_action in {"ozon_upload", "manual_ozon_upload"}
            or "ozon_upload" in (status.get("pending_steps") or [])
        )
        and str(ozon.get("upload_status") or "not_started") in {"not_started", "failed", "unknown", ""}
    )
    checkpoint_resume = (
        status.get("task_authorized") is True
        and next_action in PIPELINE_STEPS
        and state not in TERMINAL_STATES
        and state not in {"COLLECTED", "QUEUED", "PROCESSING", "UPLOADING"}
    )
    # 有意偏离原项目：原项目把 NEEDS_ATTENTION 归进 TERMINAL_STATES，
    # 于是"失败后换批次重试"会走整体重置分支、丢掉已完成步骤。
    # 工作台的"恢复任务"按钮必须能保住断点，所以这里单列一支：
    attention_resume = (
        state in ATTENTION_STATES
        and status.get("task_authorized") is True
        and next_action in PIPELINE_STEPS
    )

    previous = state
    common = {
        "batch_id": batch_id,
        "task_authorized": True,
        "review_mode": review_mode,
        "auto_upload": bool(auto_upload),
        "last_run_at": now_iso(),
        "completed_at": "unknown",
        "error_code": "unknown",
        "error_message": "unknown",
        "human_message": None,
        "attention_required": False,
        "host_recovery_state": "normal",
        "host_recovery_reason": "unknown",
        "ai_service_state": "normal",
        "ai_service_reason": "unknown",
        "ai_service_retry_after": "unknown",
    }

    if same_batch_resume:
        status.update(common)
        if state in ATTENTION_STATES or state == "STOPPED":
            status["status"] = "QUEUED"
            status["current_step"] = "queue"
        reason = f"Resumed batch task {batch_id} without resetting completed steps or retry counters."
    elif auto_upload_resume:
        status.update(common)
        pending = list(dict.fromkeys([*(status.get("pending_steps") or []), "ozon_upload"]))
        status["pending_steps"] = pending
        status["status"] = "QUEUED"
        status["current_step"] = "queue"
        status["next_action"] = "ozon_upload"
        reason = f"Resumed auto upload for batch task {batch_id}."
    elif attention_resume:
        status.update(common)
        status["status"] = "QUEUED"
        status["current_step"] = "queue"
        reason = (
            f"Resumed from {previous} without resetting completed steps "
            f"or retry counters for batch task {batch_id}."
        )
    elif checkpoint_resume:
        status.update(common)
        status["status"] = "QUEUED"
        status["current_step"] = "queue"
        reason = f"Resumed existing checkpoint at {next_action} for batch task {batch_id}."
    else:
        old_warnings = list(status.get("warnings") or [])
        if old_warnings:
            archived = list(status.get("warning_history") or [])
            archived.append({"batch_id": batch_id, "archived_at": now_iso(), "warnings": old_warnings})
            status["warning_history"] = archived
        status.update(common)
        status.update(
            {
                "status": "QUEUED",
                "current_step": "queue",
                "progress": 1,
                "warnings": [],
                "retry_count_by_step": {},
                "image_slot_retry_count_by_slot": {},
                "failed_step": "unknown",
            }
        )
        request = Path(product_dir_path) / "output" / "image-regeneration-request.json"
        if request.is_file():
            request.unlink()
        reason = f"User started batch task {batch_id}; no per-product review is required."

    status = normalize(status)
    status.setdefault("history", []).append(
        {"from": previous, "to": status.get("status"), "at": now_iso(), "reason": reason}
    )
    if target_store_ids:
        status["target_store_ids"] = list(target_store_ids)
    return save_status(product_dir_path, status)


def complete_step(
    product_dir_path: Path | str,
    step: str,
    *,
    duration_seconds: int = 0,
) -> dict[str, Any]:
    """标记某一步完成并推进状态。已完成的步骤重复调用是幂等的。"""
    if not is_pipeline_step(step):
        raise ValueError(f"未知流水线步骤：{step}")
    status = normalize(load_status(product_dir_path))
    if step in status["completed_steps"]:
        return save_status(product_dir_path, status)

    previous = status.get("status")
    status["completed_steps"] = ordered([*status["completed_steps"], step])
    target = STEP_STATUS.get(step, previous)
    if step == "ozon_upload" and str(previous or "").upper() in REMOTE_STATES:
        target = previous
    status["status"] = target
    status["active_step"] = None
    status["last_run_at"] = now_iso()
    entry = {
        "name": step,
        "status": "completed",
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "duration_seconds": int(duration_seconds),
        "retry_count": int((status.get("retry_count_by_step") or {}).get(step, 0)),
        "retryable": True,
        "error": None,
    }
    status.setdefault("steps", []).append(entry)
    status = normalize(status)
    if previous != status.get("status"):
        status.setdefault("history", []).append(
            {
                "from": previous,
                "to": status.get("status"),
                "at": now_iso(),
                "reason": f"Pipeline step {step} completed and its output was validated.",
            }
        )
    return save_status(product_dir_path, status)


def mark_needs_attention(
    product_dir_path: Path | str,
    step: str,
    reason: str,
    *,
    retryable: bool = True,
) -> dict[str, Any]:
    """把商品标成需要人工处理；并按原项目规则决定是否生成"关键歧义提问"。"""
    status = normalize(load_status(product_dir_path))
    previous = status.get("status")
    current = step if is_pipeline_step(step) else PIPELINE_STEPS[0]
    status.update(
        {
            "status": "NEEDS_ATTENTION",
            "current_step": current,
            "failed_step": step,
            "active_step": None,
            "error_code": "PIPELINE_NEEDS_ATTENTION",
            "error_message": reason,
            "human_message": reason,
            "attention_required": True,
            "last_run_at": now_iso(),
            "next_action": "retry_failed_step",
        }
    )
    status.setdefault("warnings", []).append(reason)
    retries = status.setdefault("retry_count_by_step", {})
    retries[step] = int(retries.get(step, 0))
    status.setdefault("steps", []).append(
        {
            "name": current,
            "status": "failed",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "retry_count": retries[step],
            "retryable": bool(retryable),
            "error": {
                "step": current,
                "reason": reason,
                "occurred_at": now_iso(),
                "retryable": bool(retryable),
            },
        }
    )
    status.setdefault("history", []).append(
        {"from": previous, "to": "NEEDS_ATTENTION", "at": now_iso(), "reason": reason}
    )
    saved = save_status(product_dir_path, status)
    maybe_create_operator_question(product_dir_path, step, reason)
    return saved


def maybe_create_operator_question(
    product_dir_path: Path | str,
    step: str,
    reason: str,
) -> dict[str, Any] | None:
    """只有"关键歧义"才打断流程问人；已授权批次永不弹问题（无人值守）。"""
    status = load_status(product_dir_path)
    if status.get("task_authorized") is True:
        return None
    text = str(reason or "")
    lowered = text.casefold()
    if not any(signal in lowered for signal in CRITICAL_SIGNALS):
        return None
    if not any(signal in lowered for signal in AMBIGUITY_SIGNALS):
        return None
    path = Path(product_dir_path) / "input" / "pending-question.json"
    if path.is_file():
        current = _read_json(path)
        if str(current.get("status") or "").upper() == "OPEN":
            return current
    question = {
        "schema_version": "1.0.0",
        "question_id": f"Q-{uuid.uuid4().hex[:12].upper()}",
        "product_id": Path(product_dir_path).name,
        "status": "OPEN",
        "step": step,
        "question": "系统无法确认商品结构、SKU 对应关系或配件数量。请用简单中文说明正确情况。",
        "reason": text,
        "created_at": now_iso(),
        "answer": "unknown",
        "answered_at": "unknown",
        "answered_by": "unknown",
    }
    _write_json_atomic(path, question)
    return question


# ---------------------------------------------------------------- 快照与绑定


def _sha256_of_files(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        digest.update(str(path.name).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def freeze_sku_run_snapshot(
    product_dir_path: Path | str,
    batch_id: str,
    *,
    review_mode: str = "automatic",
    auto_upload: bool = True,
    target_store_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """冻结本次运行的 SKU 快照：之后的分析/文案/生图/上传都读它，不再读会被改写的输入。"""
    directory = Path(product_dir_path)
    source_path = directory / "input" / "source.json"
    overrides_path = directory / "input" / "workbench-sku-overrides.json"
    category_path = directory / "input" / "category-selection.json"
    source = _read_json(source_path)
    from .sku_selection import active_skus

    skus = active_skus(directory, source.get("skus") if isinstance(source.get("skus"), list) else [])
    if not 1 <= len(skus) <= MAX_SELECTED_SKUS:
        raise ValueError(
            f"{directory.name}: 已选 SKU 必须在 1–{MAX_SELECTED_SKUS} 之间，实际 {len(skus)}"
        )
    dependencies = [source_path, overrides_path, category_path]
    selection_path = directory / "input" / "selected-skus.json"
    if selection_path.is_file():
        dependencies.append(selection_path)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "batch_id": batch_id,
        "frozen_at": now_iso(),
        "dependency_hash": _sha256_of_files(dependencies),
        "selected_sku_count": len(skus),
        "review_mode": review_mode,
        "auto_upload": bool(auto_upload),
        "target_store_ids": list(target_store_ids),
        "sku_ids": [str(item.get("sku_id") or "") for item in skus if isinstance(item, Mapping)],
        "note": "运行期间输入被改写时，必须用本快照重跑，而不是沿用中间产物。",
    }
    _write_json_atomic(directory / "output" / "sku-run-snapshot.json", snapshot)
    return snapshot


def source_snapshot_binding(product_dir_path: Path | str) -> dict[str, Any] | None:
    """绑定本次采集：``collection_id`` + ``input/source-manifest.json`` 的 SHA-256。

    采集产物还没有时返回 ``None``（M1 尚未接管采集），调用方据此跳过而不是编造。
    """
    directory = Path(product_dir_path)
    source = _read_json(directory / "input" / "source.json")
    manifest_path = directory / "input" / "source-manifest.json"
    collection_id = str(source.get("collection_id") or _read_json(manifest_path).get("collection_id") or "")
    if not COLLECTION_ID_PATTERN.match(collection_id):
        return None
    if not manifest_path.is_file():
        return None
    return {
        "product_id": directory.name,
        "collection_id": collection_id,
        "source_manifest_path": "input/source-manifest.json",
        "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------- 校验


def validate_status(payload: Mapping[str, Any]) -> list[str]:
    """按 status.schema.json 的核心约束做轻量校验（不依赖 jsonschema）。"""
    problems: list[str] = []
    required = (
        "schema_version",
        "product_id",
        "status",
        "current_step",
        "progress",
        "started_at",
        "completed_at",
        "error_code",
        "error_message",
        "warnings",
        "retry_count",
        "ozon",
        "steps",
        "history",
    )
    for key in required:
        if key not in payload:
            problems.append(f"缺少必填字段：{key}")
    product_id = str(payload.get("product_id") or "")
    if not PRODUCT_ID_PATTERN.match(product_id):
        problems.append(f"product_id 格式非法：{product_id!r}")
    status = str(payload.get("status") or "")
    if status not in STATUS_ENUM:
        problems.append(f"status 不在枚举内：{status!r}")
    step = str(payload.get("current_step") or "")
    if step not in PIPELINE_STEPS and step not in STEP_NAME_EXTRA:
        problems.append(f"current_step 不是合法步骤名：{step!r}")
    progress = payload.get("progress")
    if not isinstance(progress, int) or not 0 <= progress <= 100:
        problems.append(f"progress 必须是 0–100 的整数：{progress!r}")
    for key in ("warnings", "steps", "history"):
        if not isinstance(payload.get(key), list):
            problems.append(f"{key} 必须是数组")
    if int(payload.get("retry_count") or 0) < 0:
        problems.append("retry_count 不能为负")
    ozon = payload.get("ozon")
    if not isinstance(ozon, Mapping):
        problems.append("ozon 必须是对象")
    else:
        for key in ("upload_status", "product_id", "offer_id", "task_id", "last_response", "errors"):
            if key not in ozon:
                problems.append(f"ozon 缺少必填字段：{key}")
        if status == "UPLOADED" and ozon.get("upload_status") != "uploaded":
            problems.append("status=UPLOADED 时 ozon.upload_status 必须是 uploaded")
    snapshot = payload.get("sku_run_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, Mapping):
            problems.append("sku_run_snapshot 必须是对象")
        else:
            count = snapshot.get("selected_sku_count")
            if not isinstance(count, int) or not 1 <= count <= MAX_SELECTED_SKUS:
                problems.append("sku_run_snapshot.selected_sku_count 必须是 1–10")
            for key in ("path", "dependency_hash", "frozen_at"):
                if not snapshot.get(key):
                    problems.append(f"sku_run_snapshot 缺少：{key}")
    binding = payload.get("source_snapshot_binding")
    if binding is not None:
        sha = str(binding.get("source_manifest_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            problems.append("source_snapshot_binding.source_manifest_sha256 必须是 64 位小写 hex")
    return problems


#: 便于注入自定义冻结逻辑（例如并入 OSS/素材校验）
FreezeSnapshot = Callable[..., dict[str, Any]]
