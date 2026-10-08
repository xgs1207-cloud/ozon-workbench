"""Separate single-image/set workspaces over one ordered publication gallery."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
from typing import Any, Sequence
import uuid

from . import image_jobs
from .guided_review import slot_fingerprint
from .listing_form import read_json, write_json, _require_editable
from .product_edit_lock import product_edit_lock, product_file_transaction


def media_state(directory: Path | str) -> dict[str, Any]:
    directory = Path(directory).resolve()
    plan = read_json(directory / image_jobs.PLAN_FILE)
    report = read_json(directory / image_jobs.REPORT_FILE)
    jobs = image_jobs.list_image_jobs(directory)
    rows = image_jobs._slots(plan)
    workspaces = {name: [row["slot"] for row in rows if row.get("workspace", "single") == name] for name in ("single", "set")}
    indexed = {row["slot"]: row for row in report.get("files") or []}
    newest = {row["slot"]: row for row in jobs}
    sets = []
    for entry in plan.get("image_sets") or []:
        members = [row for row in rows if row.get("set_id") == entry["id"] and row.get("workspace") == "set"]
        names = [row["slot"] for row in members]
        completed = sum(name in indexed and indexed[name].get("slot_fingerprint") == slot_fingerprint(
            next(row.get("generated_spec") or row for row in members if row["slot"] == name)) for name in names)
        counts = {state: sum((newest.get(name) or {}).get("status") == state for name in names)
                  for state in ("queued", "running", "failed", "unknown", "stale")}
        sets.append({**entry, "slots": names, "total": len(names), "completed": completed, **counts,
                     "unstarted": sum(name not in newest and name not in indexed for name in names)})
    from .reference_images import selected_reference_images
    source = read_json(directory / "input/source.json")
    references = selected_reference_images(directory, annotate=True)
    registry = [{"reference_id": row["id"], "source_path": row["path"], "source_sku_ids": row["source_sku_ids"],
                 "adoptions": [{"slot": entry.get("slot"), "path": entry.get("path"), "workspace": entry.get("workspace", "single"),
                                "set_id": entry.get("set_id"), "sha256": entry.get("sha256")}
                               for entry in report.get("files") or [] if entry.get("origin") == "captured"
                               and (entry.get("capture_receipt") or {}).get("source_path") == row["path"]]}
                for row in references]
    return {"image_plan": plan, "image_generation": report, "jobs": jobs, "sets": sets, "workspaces": workspaces,
            "selected_slots": plan.get("selected_slots") or [], "captured_reference_images": references,
            "sources": registry,
            "generated_image_paths": [row.get("path") for row in report.get("files") or [] if row.get("path")],
            "progress_policy": "展示真实队列状态和已完成张数；模型生成中不提供虚构百分比"}


def create_set(directory: Path | str, *, count: int, prompt: str = "", reference_ids: Sequence[str],
               source_sku_id: str | None = None, purposes: Sequence[str] | None = None,
               name: str | None = None) -> dict[str, Any]:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
        raise ValueError("本次套图生成数量须为 1–50 张，可另建新套图；不要求固定 9 张")
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        _require_editable(directory)
        set_id = "set-" + uuid.uuid4().hex[:12]
        from .image_insights import read_image_insights
        points = (read_image_insights(directory).get("payload") or {}).get("selling_points") or []
        guidance = [str(row.get("image_howto_zh") or row.get("prompt_zh") or "") for row in points]
        fallback = ("清晰呈现完整外观与真实颜色", "近景展示真实纹理和结构", "用自然光与适当构图展示已确认卖点")
        with product_file_transaction(directory, (image_jobs.PLAN_FILE, "input/guided-workflow.json")):
            slots = []
            for index in range(count):
                guidance_text = str((purposes or [])[index]) if index < len(purposes or []) else (
                    guidance[index % len(guidance)] if guidance else fallback[index % len(fallback)])
                role = "variant_main" if index == 0 else "detail"
                label = "主图" if index == 0 else ("细节图" if index % 2 else "场景图")
                purpose = f"{label}（第 {index + 1} 张）：{guidance_text}"
                # Preserve the user's original base_prompt in the set record;
                # editable per-slot prompt adds only visual presentation advice.
                instructions = ((prompt.rstrip() + "\n\n") if prompt.strip() else "根据所选商品的真实参考图，") + (
                    f"图片用途与展示建议（可编辑）：{purpose}。保持商品外观、颜色、结构与规格一致，"
                    "场景只作展示，不添加未经确认的功能、配件、尺寸或承诺。")
                created = image_jobs.add_image_slot(directory, prompt=instructions, reference_ids=reference_ids,
                    source_sku_id=source_sku_id, role=role, purpose=purpose, workspace="set", set_id=set_id)
                slots.append(created["slot"])
            plan = read_json(directory / image_jobs.PLAN_FILE)
            entry = {"id": set_id, "name": name or f"套图 {len(plan.get('image_sets') or []) + 1}",
                     "requested_count": count, "created_at": image_jobs._now(), "workspace": "set", "base_prompt": prompt}
            plan.setdefault("image_sets", []).append(entry)
            write_json(directory / image_jobs.PLAN_FILE, plan)
            from .guided_workflow import refresh_plan_metadata
            refresh_plan_metadata(directory)
        return {"set_id": set_id, "slots": slots, "image_plan": plan, "set": entry}


def generate_set(directory: Path | str, set_id: str, *, generator_factory=None, dispatch: bool = True) -> dict[str, Any]:
    """Only untouched slots: failures/unknowns require an explicit paid retry."""
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        _require_editable(directory)
        plan = read_json(directory / image_jobs.PLAN_FILE)
        members = [row for row in image_jobs._slots(plan) if row.get("workspace") == "set" and row.get("set_id") == set_id]
        if not members:
            raise ValueError("套图不存在或没有图位")
        receipts = {row["slot"]: row for row in read_json(directory / image_jobs.REPORT_FILE).get("files") or []}
        newest = {row["slot"]: row for row in image_jobs.list_image_jobs(directory)}
        pending, skipped = [], []
        for row in members:
            name = row["slot"]
            previous = newest.get(name) or {}
            if name in receipts or previous.get("status") in {"queued", "running", "failed", "unknown", "stale"}:
                skipped.append({"slot": name, "reason": "已完成/正在生成，或需逐张明确确认重试", "status": previous.get("status")})
            else:
                image_jobs._validate(directory, row)
                pending.append(name)
        # Validate configuration for every request before enqueuing any paid job.
        from models import load_web_image_generator
        with image_jobs._GUARD:
            if len(image_jobs._ACTIVE) + len(pending) > 256:
                return {"set_id": set_id, "jobs": [], "accepted": [], "skipped": skipped,
                        "rejected": [{"slot": slot, "reason": "队列容量不足，本批次尚未调用模型"} for slot in pending],
                        "model_requests_queued": 0, "queue_capacity": 256}
        generators = {name: generator_factory(name) if generator_factory else load_web_image_generator(slot_filter=[name]) for name in pending}
        jobs, rejected = [], []
        for index, name in enumerate(pending):
            try:
                jobs.append(image_jobs.enqueue_image(directory, name, generator=generators[name], dispatch=dispatch))
            except image_jobs.ImageJobConflict:
                # Another product can fill the shared process queue. Return
                # exactly what was accepted; never imply the whole set ran.
                rejected.extend({"slot": other, "reason": "队列暂时已满，该图位尚未调用模型"} for other in pending[index:])
                break
    return {"set_id": set_id, "jobs": jobs, "accepted": [row["slot"] for row in jobs], "skipped": skipped,
            "rejected": rejected, "model_requests_queued": len(jobs), "queue_capacity": 256}


def retry_image(directory: Path | str, slot: str, *, confirm_new_charge: bool, generator=None, dispatch=True) -> dict[str, Any]:
    if confirm_new_charge is not True:
        raise ValueError("重试会发起新的付费模型请求，请明确确认后再重试；旧请求可能已扣费")
    directory = Path(directory).resolve()
    previous = next((row for row in reversed(image_jobs.list_image_jobs(directory)) if row["slot"] == slot), {})
    if previous.get("status") in {"queued", "running"}:
        raise image_jobs.ImageJobConflict("当前图片仍在生成，不重复发起付费请求")
    job = image_jobs.enqueue_image(directory, slot, generator=generator, dispatch=dispatch)
    with product_edit_lock(directory):
        image_jobs._mark(directory, job["id"], retry_of=previous.get("id"))
    return {**job, "retry_of": previous.get("id")}


def adopt_original(directory: Path | str, *, reference_id: str | None = None, source_path: str | None = None,
                   source_sku_id: str | None = None, workspace: str = "single", set_id: str | None = None,
                   role: str = "detail") -> dict[str, Any]:
    directory = Path(directory).resolve()
    with product_edit_lock(directory):
        _require_editable(directory)
        if workspace == "set" and not any(row.get("id") == set_id for row in
                read_json(directory / image_jobs.PLAN_FILE).get("image_sets") or []):
            raise ValueError("请先创建真实套图工作区，再将原图加入该套图")
        from models.image_plan import _list_reference_images
        references = _list_reference_images(directory)
        if reference_id:
            chosen = next((row for row in references if row["id"] == reference_id), None)
        else:
            chosen = next((row for row in references if row["path"] == source_path), None)
        if not chosen or (reference_id and source_path and chosen["path"] != source_path):
            raise ValueError("请选择此商品当前采集原图库的一张真实图片")
        ids = image_jobs._selected(directory)
        sku = source_sku_id or (ids[0] if len(ids) == 1 else None)
        if sku is None:
            owners = image_jobs._reference_owners(read_json(directory / "input/source.json"), chosen["path"])
            if len(owners) == 1:
                sku = next(iter(owners))
        from .captured_images import CAPTURED_GENERATOR, capture_proof
        proof = capture_proof(directory, chosen["path"], source_sku_id=sku)
        paths = (image_jobs.PLAN_FILE, image_jobs.REPORT_FILE, "input/guided-workflow.json")
        with product_file_transaction(directory, paths):
            created = image_jobs.add_image_slot(directory, prompt="直接采用已封存的真实商品原图，不进行 AI 生图", reference_ids=[chosen["id"]],
                source_sku_id=sku, role=role, purpose="直接采用真实采集商品照片", workspace=workspace, set_id=set_id)
            plan = read_json(directory / image_jobs.PLAN_FILE)
            slot = created["slot"]
            row = next(row for row in image_jobs._slots(plan) if row["slot"] == slot)
            relative = f"output/generated-images/adopted/{slot}{Path(chosen['path']).suffix.lower()}"
            row.update(output_path=relative, origin="captured", operation="adopt_captured_original", status="generated", capture_receipt=proof)
            row["generated_spec"] = image_jobs.generated_spec(row)
            target = image_jobs._safe_file(directory, relative, "output/generated-images")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(directory / chosen["path"], target)
            # No conversion: original byte identity is part of the provenance.
            if image_jobs._hash(target) != proof["source_sha256"]:
                raise ValueError("原图复制内容校验失败，未采用此图片")
            report = read_json(directory / image_jobs.REPORT_FILE)
            files = list(report.get("files") or [])
            receipt = {"slot": slot, "path": relative, "bytes": target.stat().st_size, "sha256": proof["source_sha256"],
                "generator": CAPTURED_GENERATOR, "origin": "captured", "model": None, "generation_id": "adopt-" + uuid.uuid4().hex,
                "workspace": workspace, "set_id": set_id, "source_sku_id": sku,
                "slot_fingerprint": slot_fingerprint(row), "capture_receipt": proof}
            files.append(receipt)
            backends = {entry.get("generator", "unknown") for entry in files}
            report.update(schema_version="1.0.0", product_id=directory.name, files=files, final_images=True,
                generator=next(iter(backends)) if len(backends) == 1 else "mixed", generation_id=receipt["generation_id"],
                generated_slots=len(files), planned_slots=len(image_jobs._slots(plan)), note="真实采集原图与AI产物分别标注来源，均须人工审核内容")
            plan["generator_contract"]["raw_1688_image_direct_upload_forbidden"] = False
            write_json(directory / image_jobs.PLAN_FILE, plan)
            write_json(directory / image_jobs.REPORT_FILE, report)
            from .guided_workflow import refresh_plan_metadata
            refresh_plan_metadata(directory)
        return {"slot": slot, "adopted": receipt, "image_plan": plan, "model_calls": 0}
