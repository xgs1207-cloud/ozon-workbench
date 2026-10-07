"""Durable, explicitly requested single-image jobs.

Paid I/O runs on immutable capture snapshots, outside the product editor lock.
A restart never replays a charged request. Only the matching slot is committed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Mapping, Sequence
import uuid

from contracts import validate_contract
from .listing_form import read_json, write_json, _require_editable
from .product_edit_lock import product_edit_lock, product_file_transaction
from .sku_selection import selection_state
from .guided_review import REAL_IMAGE_GENERATORS, slot_fingerprint

JOBS_FILE = "output/image-jobs.json"
PLAN_FILE = "output/image-plan.json"
REPORT_FILE = "output/image-generation-report.json"
_INSTANCE = uuid.uuid4().hex
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="image-slot")
_ACTIVE: set[str] = set()
_GUARD = threading.Lock()
_FIELDS = ("slot", "prompt", "reference_image_ids", "reference_product_images", "output_path", "russian_text",
           "source_sku_id", "variant_scope", "shared_across_variants", "image_type", "purpose")


class ImageJobConflict(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slots(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in [*(plan.get("main_images") or []), *(plan.get("detail_images") or [])]
            if isinstance(row, dict)]


def generated_spec(slot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(slot.get(key)) for key in _FIELDS if key in slot}


def _input_fingerprint(slot: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(generated_spec(slot), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _studio_blueprint(directory: Path) -> dict[str, Any]:
    from models.image_plan import build_image_plan
    # The neutral structural seed is never used as buyer-facing text or prompt.
    return build_image_plan(product_dir=directory, source=read_json(directory / "input/source.json"),
                            source_refs=["input/source.json", "input/selected-skus.json"],
                            copy_bundle={"core_keyword": "Товар"}, generated_by="manual_studio")


def _empty_studio(directory: Path) -> dict[str, Any]:
    plan = _studio_blueprint(directory)
    plan.update(main_images=[], detail_images=[], selected_slots=[], studio_mode=True)
    plan["listing_context"]["title_ru"] = None
    plan["creative_direction"]["core_keyword"] = None
    plan["image_set_structure"] = ["自主选择任意数量的图片"]
    plan["variant_image_strategy"].update(variant_main_count=0, shared_detail_count=0)
    plan["generator_contract"]["exact_shared_detail_count"] = 0
    return plan


def _safe_file(root: Path, relative: Any, prefix: str | None = None) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("图片文件路径无效")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or (prefix and not path.is_relative_to((root / prefix).resolve())):
        raise ValueError("图片路径必须位于当前商品的允许目录")
    return path


def _selected(directory: Path) -> list[str]:
    state = selection_state(directory)
    if (not state.get("has_selection") or state.get("unknown_in_selection")
            or not 1 <= state.get("active_count", 0) <= 10):
        raise ValueError("请先确认 1–10 个有效的上架规格，再生成图片")
    return list(state["selected"])


def _reference_owners(source: Mapping[str, Any], relative: str) -> set[str]:
    """Use explicit capture associations only; never pair files by list order."""
    owners: set[str] = set()
    for row in source.get("image_sources") or []:
        if isinstance(row, Mapping) and row.get("path") == relative:
            if row.get("source_sku_id"):
                owners.add(str(row["source_sku_id"]))
            owners.update(str(item) for item in row.get("source_sku_ids") or [] if item)
    for row in source.get("skus") or []:
        if not isinstance(row, Mapping) or not row.get("sku_id"):
            continue
        refs = [row.get("image_path"), *(row.get("image_refs") or [])]
        if relative in refs:
            owners.add(str(row["sku_id"]))
    return owners


def _validate(directory: Path, slot: Mapping[str, Any]) -> dict[str, str]:
    selected = _selected(directory)
    sku = str(slot.get("source_sku_id") or "")
    if sku and sku not in selected:
        raise ValueError("此图位对应的规格未被选中，请先重新选择规格")
    prompt, refs = slot.get("prompt"), slot.get("reference_product_images")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("请填写并保存 1–4000 字的图片提示词")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 3 or len(set(refs)) != len(refs):
        raise ValueError("每次生图请选择 1–3 张不同的真实采集参考图，不会自动截断")
    source = read_json(directory / "input/source.json")
    allowed = set(source.get("stored_images") or [])
    paths = ["input/source.json", "input/source-manifest.json", "input/selected-skus.json"]
    if (directory / "input/raw-snapshot.json").is_file():
        paths.append("input/raw-snapshot.json")
    for relative in refs:
        path = _safe_file(directory, relative, "input")
        if relative not in allowed or path.parent.name not in {"sku-images", "main-images", "detail-images"} or not path.is_file():
            raise ValueError("参考图必须是此商品已采集的真实原图")
        owners = _reference_owners(source, relative)
        if owners - set(selected):
            raise ValueError("参考图明确属于未选上架规格，请改选当前规格的原图")
        if sku and owners and sku not in owners:
            raise ValueError("参考图与当前图位规格不一致，请选择同规格原图")
        paths.append(relative)
    output = _safe_file(directory, slot.get("output_path"), "output/generated-images")
    if output.suffix.lower() != ".png":
        raise ValueError("生图输出须为当前商品工作区的 PNG")
    for relative in paths:
        if not _safe_file(directory, relative).is_file():
            raise ValueError("采集资料缺少原始校验记录，请重新采集")
    return {relative: _hash(_safe_file(directory, relative)) for relative in paths}


def _journal(directory: Path) -> dict[str, Any]:
    journal = read_json(directory / JOBS_FILE)
    journal.setdefault("schema_version", "1.0.0")
    journal.setdefault("jobs", [])
    return journal


def _pid_alive(pid: Any) -> bool:
    try:
        if isinstance(pid, bool) or int(pid) <= 0:
            return False
        if int(pid) == os.getpid():
            return True
        if os.name == "nt":
            # os.kill(pid, 0) terminates processes on Windows; query only.
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _process_birth(pid: Any) -> str | None:
    """Read process creation identity so a recycled PID cannot keep jobs alive."""
    try:
        if isinstance(pid, bool) or int(pid) <= 0:
            return None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(value) for value in times)):
                    return None
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        contents = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        return contents.rpartition(")")[2].split()[19]
    except (OSError, TypeError, ValueError, IndexError):
        return None


def _recover(directory: Path, journal: dict[str, Any]) -> None:
    changed = False
    with _GUARD:
        active = set(_ACTIVE)
    for job in journal["jobs"]:
        if job.get("status") not in {"queued", "running"}:
            continue
        owner_current = job.get("pid") == os.getpid() and job.get("instance") == _INSTANCE
        abandoned = (owner_current and job.get("id") not in active) or not _pid_alive(job.get("pid"))
        if job.get("process_birth") is not None and _process_birth(job.get("pid")) != job["process_birth"]:
            abandoned = True
        if job.get("pid") == os.getpid() and job.get("instance") != _INSTANCE:
            abandoned = True
        if abandoned:
            job.update(status="unknown" if job.get("started_at") else "failed", finished_at=_now(),
                       message="服务重启或任务中断；未自动重试，请核对模型调用记录后自行重新生成")
            changed = True
    if changed:
        write_json(directory / JOBS_FILE, journal)


def list_image_jobs(directory: Path | str) -> list[dict[str, Any]]:
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        journal = _journal(directory)
        _recover(directory, journal)
        public = ("id", "slot", "status", "queued_at", "started_at", "finished_at", "message", "generator", "model",
                  "slot_fingerprint", "generation_id", "sha256", "path", "bytes")
        return [{key: row[key] for key in public if key in row} for row in journal["jobs"]][-100:]


def _mark(directory: Path, job_id: str, **changes: Any) -> None:
    journal = _journal(directory)
    job = next(row for row in journal["jobs"] if row["id"] == job_id)
    job.update(changes)
    write_json(directory / JOBS_FILE, journal)


def enqueue_image(directory: Path | str, slot_name: str, *, generator: Any = None, dispatch: bool = True) -> dict[str, Any]:
    """Persist a job before dispatch; injection/dispatch=False support offline tests."""
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        _require_editable(directory)
        plan = read_json(directory / PLAN_FILE)
        slot = next((row for row in _slots(plan) if row.get("slot") == slot_name), None)
        if slot is None:
            raise ImageJobConflict("图位不存在，请先添加一张图片或保存当前图位")
        snapshot_hashes = _validate(directory, slot)
        journal = _journal(directory)
        _recover(directory, journal)
        if any(row.get("slot") == slot_name and row.get("status") in {"queued", "running"} for row in journal["jobs"]):
            raise ImageJobConflict("当前图位正在生成；可以切换到其他图位继续生成")
        with _GUARD:
            if len(_ACTIVE) >= 24:
                raise ImageJobConflict("生图队列暂时已满，请等待部分图片完成后再添加")
        if generator is None:
            from models import load_web_image_generator
            generator = load_web_image_generator(slot_filter=[slot_name])
        if str(getattr(generator, "name", "")) not in REAL_IMAGE_GENERATORS:
            raise ValueError("请配置正式的 RightAPI 或豆包生图后端")
        if plan.get("studio_mode") is not True:
            receipts = {row.get("slot"): row for row in read_json(directory / REPORT_FILE).get("files") or []
                        if isinstance(row, Mapping)}
            selected_slots = []
            for previous in _slots(plan):
                receipt = receipts.get(previous["slot"]) or {}
                if (receipt.get("slot_fingerprint") == slot_fingerprint(previous)
                        and receipt.get("generator") in REAL_IMAGE_GENERATORS
                        and _safe_file(directory, previous["output_path"], "output/generated-images").is_file()):
                    previous["generated_spec"] = generated_spec(previous)
                    selected_slots.append(previous["slot"])
            plan.update(studio_mode=True, selected_slots=selected_slots)
            write_json(directory / PLAN_FILE, plan)
            from .guided_workflow import refresh_plan_metadata
            refresh_plan_metadata(directory)
        job_id = uuid.uuid4().hex
        snapshot = directory / "output/image-jobs" / job_id / "snapshot"
        snapshot.mkdir(parents=True)
        for relative in snapshot_hashes:
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(directory / relative, target)
        isolated = deepcopy(plan)
        isolated["main_images"] = [deepcopy(slot)] if slot in (plan.get("main_images") or []) else []
        isolated["detail_images"] = [] if isolated["main_images"] else [deepcopy(slot)]
        isolated["selected_slots"] = [slot_name]
        write_json(snapshot / PLAN_FILE, isolated)
        job = {"id": job_id, "slot": slot_name, "status": "queued", "queued_at": _now(),
               "slot_fingerprint": slot_fingerprint(slot), "snapshot_hashes": snapshot_hashes,
               "input_fingerprint": _input_fingerprint(slot),
               "generator": generator.name, "pid": os.getpid(), "instance": _INSTANCE,
               "process_birth": _process_birth(os.getpid())}
        journal["jobs"].append(job)
        with _GUARD:
            _ACTIVE.add(job_id)
        try:
            write_json(directory / JOBS_FILE, journal)
        except BaseException:
            with _GUARD:
                _ACTIVE.discard(job_id)
            raise
    if dispatch:
        try:
            _EXECUTOR.submit(run_image_job, directory, job_id, generator)
        except BaseException:
            with product_edit_lock(directory):
                _mark(directory, job_id, status="failed", message="生图队列未能启动，尚未调用模型", finished_at=_now())
            with _GUARD:
                _ACTIVE.discard(job_id)
            raise
    return {key: value for key, value in job.items() if key not in {"snapshot_hashes", "pid", "instance", "process_birth"}}


def _safe_error(error: Exception) -> str:
    message = str(error) if isinstance(error, ValueError) or error.__class__.__name__ == "ModelError" else "生图任务失败；已保留之前完成的图片"
    message = re.sub(r"https?://\S+", "[图片地址已隐藏]", message)
    message = re.sub(r"(?:Bearer\s+\S+|sk-[\w-]+)", "[凭据已隐藏]", message, flags=re.I)
    for key in ("RIGHTAPI_API_KEY", "ARK_API_KEY"):
        secret = os.environ.get(key)
        if secret:
            message = message.replace(secret, "[凭据已隐藏]")
    return message[:600]


def run_image_job(directory: Path | str, job_id: str, generator: Any) -> None:
    directory = Path(directory).resolve()
    snapshot = directory / "output/image-jobs" / job_id / "snapshot"
    try:
        with product_edit_lock(directory):
            _require_editable(directory)
            journal = _journal(directory)
            job = next(row for row in journal["jobs"] if row["id"] == job_id)
            if job.get("status") != "queued":
                return
            plan = read_json(directory / PLAN_FILE)
            slot = next((row for row in _slots(plan) if row.get("slot") == job["slot"]), None)
            if slot is None or _input_fingerprint(slot) != job["input_fingerprint"] or _validate(directory, slot) != job["snapshot_hashes"]:
                _mark(directory, job_id, status="stale", message="提示词、参考图或规格已改变，旧任务未调用模型", finished_at=_now())
                return
            _mark(directory, job_id, status="running", started_at=_now(), message="正在生成此图；其他图位可继续操作")
        # No product lock, no live product output paths, and no provider retry.
        from models import ImageRequest
        result = generator.generate(ImageRequest(product_id=directory.name, product_dir=snapshot,
                                                  source=read_json(snapshot / "input/source.json"), slot=job["slot"]))
        backend = str(result.get("generator") or generator.name)
        generated = result.get("generated") or []
        captured_plan = read_json(snapshot / PLAN_FILE)
        captured = _slots(captured_plan)[0]
        if backend not in REAL_IMAGE_GENERATORS or result.get("final_images") is not True or len(generated) != 1:
            raise ValueError("模型未返回唯一的正式图片，之前的工作图已保留")
        item = generated[0]
        if item.get("slot") != job["slot"] or item.get("path") != captured.get("output_path"):
            raise ValueError("生图结果与请求图位不一致，之前的工作图已保留")
        staged = _safe_file(snapshot, item.get("path"), "output/generated-images")
        if not staged.is_file() or staged.stat().st_size <= 0:
            raise ValueError("模型没有生成可用的图片文件，之前的工作图已保留")
        # The provider validates image bytes; independently probe the saved file.
        from .image_probe import probe_image
        probe = probe_image(staged)
        if not probe.get("ok"):
            raise ValueError("生图文件无法读取，之前的工作图已保留")
        sha = _hash(staged)
        with product_edit_lock(directory):
            _require_editable(directory)
            plan = read_json(directory / PLAN_FILE)
            current = next((row for row in _slots(plan) if row.get("slot") == job["slot"]), None)
            if current is None or _input_fingerprint(current) != job["input_fingerprint"] or _validate(directory, current) != job["snapshot_hashes"]:
                _mark(directory, job_id, status="stale", finished_at=_now(), message="生成期间提示词、参考图或规格改变；旧结果已保留在任务快照，未覆盖工作图")
                return
            target = _safe_file(directory, current["output_path"], "output/generated-images")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                history = directory / "output/image-history" / job["slot"] / f"{job_id}-previous.png"
                history.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, history)
            report = read_json(directory / REPORT_FILE)
            files = {str(row.get("slot")): row for row in report.get("files") or [] if isinstance(row, Mapping)}
            generated_row = {"slot": job["slot"], "path": current["output_path"], "bytes": staged.stat().st_size,
                             "source_sku_id": current.get("source_sku_id"),
                             "sha256": sha, "generator": backend, "model": result.get("model"),
                             "generation_id": job_id, "slot_fingerprint": slot_fingerprint(current)}
            files[job["slot"]] = generated_row
            names = {str(row["slot"]) for row in _slots(plan)}
            files = {name: row for name, row in files.items() if name in names}
            backends = {row.get("generator") for row in files.values()}
            combined = next(iter(backends)) if len(backends) == 1 else "mixed"
            report.update(schema_version="1.0.0", product_id=directory.name, generator=combined,
                          final_images=True, generation_id=job_id, planned_slots=len(names),
                          generated_slots=len(files), files=list(files.values()), note=result.get("note"))
            current["generated_spec"] = generated_spec(current)
            # Atomically preserve unrelated reports and images even on write failure.
            with product_file_transaction(directory, (PLAN_FILE, REPORT_FILE, current["output_path"], JOBS_FILE)):
                temporary = target.with_name(f".{target.name}.{job_id}.tmp")
                shutil.copyfile(staged, temporary)
                os.replace(temporary, target)
                write_json(directory / PLAN_FILE, plan)
                write_json(directory / REPORT_FILE, report)
                _mark(directory, job_id, status="completed", finished_at=_now(), message="图片已生成；可选择、排序或单独重做",
                      model=result.get("model"), generation_id=job_id, sha256=sha, path=current["output_path"], bytes=generated_row["bytes"])
            if plan.get("selected_slots") != []:
                from .image_qc import run_image_qc
                write_json(directory / "output/image-qc-report.json", run_image_qc(directory, generator_name=combined, produces_final_images=True))
            if plan.get("studio_mode") is True:
                from .guided_workflow import refresh_plan_metadata
                refresh_plan_metadata(directory)
    except Exception as error:
        message = _safe_error(error)
        unknown = any(token in message for token in ("结果未知", "HTTP 5", "下载失败", "保存失败", "无效 JSON", "未返回"))
        with product_edit_lock(directory):
            existing = next((row for row in _journal(directory)["jobs"] if row["id"] == job_id), {})
            # A completed image remains completed if optional metadata refresh fails.
            if existing.get("status") != "completed":
                _mark(directory, job_id, status="unknown" if unknown else "failed", finished_at=_now(), message=message)
    finally:
        with _GUARD:
            _ACTIVE.discard(job_id)


def add_image_slot(directory: Path | str, *, prompt: str, reference_ids: Sequence[str],
                   source_sku_id: str | None = None, role: str = "detail", purpose: str | None = None) -> dict[str, Any]:
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        _require_editable(directory)
        ids = _selected(directory)
        sku = source_sku_id or (ids[0] if len(ids) == 1 else None)
        if sku is not None and sku not in ids:
            raise ValueError("图位规格不在已确认的上架规格中")
        if role == "variant_main" and not sku:
            raise ValueError("多规格主图请指定对应的上架规格")
        from models.image_plan import _list_reference_images
        plan = read_json(directory / PLAN_FILE)
        if not plan:
            plan = _empty_studio(directory)
        plan["studio_mode"] = True
        plan.setdefault("selected_slots", [row["slot"] for row in _slots(plan)
                         if any(saved.get("slot") == row["slot"] for saved in read_json(directory / REPORT_FILE).get("files") or [])])
        refs = _list_reference_images(directory)
        plan["reference_images"] = refs
        reference_index = {row["id"]: row["path"] for row in refs}
        choices = list(dict.fromkeys(reference_ids))
        if not 1 <= len(choices) <= 3 or any(choice not in reference_index for choice in choices):
            raise ValueError("请选择 1–3 张不同的真实采集参考图")
        if not prompt.strip() or len(prompt) > 4000:
            raise ValueError("提示词须为 1–4000 字")
        slot_name = ("main" if role == "variant_main" else "image") + "-" + uuid.uuid4().hex[:12]
        blueprint = _studio_blueprint(directory)
        row = deepcopy(blueprint["main_images"][0] if role == "variant_main" else blueprint["detail_images"][0])
        row.update({"slot": slot_name, "image_type": "main" if role == "variant_main" else "detail",
               "layout_type": "sku_main" if role == "variant_main" else "core_benefit",
               "purpose": purpose or ("突出商品真实卖点的单张图片"), "buyer_question": "Как выглядит товар?",
               "visual_goal": "展示所选商品的真实外观", "scene": "studio", "scene_description": "根据参考原图展示商品",
               "purchase_reason": "展示已确认卖点", "russian_text": [], "overlay_plan": [],
               "reference_image_ids": choices, "reference_product_images": [reference_index[key] for key in choices],
               "operation": "generate_from_reference", "output_path": f"output/generated-images/studio/{slot_name}.png",
               "status": "planned", "failure_reason": None, "design_rationale": "自主编辑提示词的独立图片：使用真实商品参考图并保持外观与规格一致，通过构图和光线体现卖点。",
               "prompt": prompt.strip(), "prompt_brief": purpose or "用构图、角度与光线展示真实卖点",
               "variant_scope": "sku" if sku else "shared", "shared_across_variants": sku is None,
               "variant_kind": "seller_specification", "variant_value": "用户所选规格"})
        if sku:
            row["source_sku_id"] = sku
        else:
            row.pop("source_sku_id", None)
        _validate(directory, row)
        plan["main_images" if role == "variant_main" else "detail_images"].append(row)
        plan["variant_image_strategy"].update(variant_main_count=len(plan["main_images"]), shared_detail_count=len(plan["detail_images"]))
        plan["generator_contract"]["exact_shared_detail_count"] = len(plan["detail_images"])
        errors = validate_contract("image-plan", plan)
        if errors:
            raise ValueError("图片图位校验失败：" + "；".join(errors[:4]))
        write_json(directory / PLAN_FILE, plan)
        from .guided_workflow import refresh_plan_metadata
        refresh_plan_metadata(directory)
        return {"slot": slot_name, "image_plan": plan}


def select_images(directory: Path | str, selected_slots: Sequence[str]) -> dict[str, Any]:
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        _require_editable(directory)
        plan = read_json(directory / PLAN_FILE)
        if not plan:
            if selected_slots:
                raise ValueError("尚未添加图片图位")
            # Empty draft media is an explicit choice; not an upload-ready report.
            _selected(directory)
            plan = _empty_studio(directory)
        plan.update(studio_mode=True, selected_slots=list(selected_slots))
        from .media_selection import selected_image_specs
        specs = selected_image_specs(plan)
        files = {row.get("slot"): row for row in read_json(directory / REPORT_FILE).get("files") or [] if isinstance(row, Mapping)}
        for spec in specs:
            saved = files.get(spec["slot"]) or {}
            target = _safe_file(directory, spec.get("output_path"), "output/generated-images")
            if (not target.is_file() or saved.get("slot_fingerprint") != slot_fingerprint(spec)
                    or saved.get("generator") not in REAL_IMAGE_GENERATORS):
                raise ValueError("所选图片尚未生成成功或版本已变更，请先生成后再选择")
            if saved.get("sha256") and saved["sha256"] != _hash(target):
                raise ValueError("所选图片文件发生变化，请重新生成并确认")
        write_json(directory / PLAN_FILE, plan)
        if specs:
            from .image_qc import run_image_qc
            report = read_json(directory / REPORT_FILE)
            write_json(directory / "output/image-qc-report.json", run_image_qc(directory,
                       generator_name=report.get("generator"), produces_final_images=True))
        from .guided_workflow import refresh_plan_metadata
        refresh_plan_metadata(directory)
        return plan
