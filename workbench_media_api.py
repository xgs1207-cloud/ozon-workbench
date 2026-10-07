"""Separate media workspaces; every paid operation is explicitly requested."""
from __future__ import annotations

from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from workbench_listing_api import directory_for, run_service

router = APIRouter(prefix="/api/workbench/products/{product_id}/guided/media")


class SlotRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    reference_ids: list[str] = Field(min_length=1, max_length=3)
    source_sku_id: str | None = None
    role: Literal["variant_main", "detail"] = "detail"
    purpose: str | None = Field(None, max_length=1000)
    workspace: Literal["single", "set"] = "single"
    set_id: str | None = Field(None, max_length=100)


class SlotEditRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    reference_ids: list[str] = Field(min_length=1, max_length=3)


class SetRequest(BaseModel):
    count: int = Field(ge=1, le=50)
    prompt: str = Field(default="", max_length=4000)
    reference_ids: list[str] = Field(min_length=1, max_length=3)
    source_sku_id: str | None = None
    purposes: list[str] | None = Field(None, max_length=50)
    name: str | None = Field(None, max_length=200)


class GenerateRequest(BaseModel):
    confirm_new_charge: bool = False


class AdoptRequest(BaseModel):
    reference_id: str | None = Field(None, max_length=100)
    source_path: str | None = Field(None, max_length=1000)
    source_sku_id: str | None = None
    workspace: Literal["single", "set"] = "single"
    set_id: str | None = Field(None, max_length=100)
    role: Literal["variant_main", "detail"] = "detail"


class SelectionRequest(BaseModel):
    selected_slots: list[str]


def media_call(fn, *args, **kwargs):
    from pipeline.image_jobs import ImageJobConflict
    try:
        return run_service(fn, *args, **kwargs)
    except HTTPException as error:
        if isinstance(error.__cause__, ImageJobConflict):
            raise HTTPException(status_code=409, detail=error.detail) from None
        raise


@router.get("")
def get_media(product_id: str):
    from pipeline.media_workspace import media_state
    return {"ok": True, **media_call(media_state, directory_for(product_id, edit=False))}


@router.get("/jobs")
def get_jobs(product_id: str):
    from pipeline.media_workspace import media_state
    state = media_call(media_state, directory_for(product_id, edit=False))
    return {"ok": True, "jobs": state["jobs"], "sets": state["sets"]}


@router.post("/slots")
def add_slot(product_id: str, request: SlotRequest):
    from pipeline.image_jobs import add_image_slot
    return {"ok": True, **media_call(add_image_slot, directory_for(product_id), **request.model_dump())}


@router.put("/slots/{slot}")
def edit_slot(product_id: str, slot: str, request: SlotEditRequest):
    from pipeline.guided_review import update_plan_slot
    from pipeline.guided_workflow import refresh_plan_metadata
    from pipeline.image_jobs import _slots, PLAN_FILE
    from pipeline.listing_form import read_json
    from pipeline.product_edit_lock import product_edit_lock
    directory = directory_for(product_id)
    with product_edit_lock(directory):
        row = next((row for row in _slots(read_json(directory / PLAN_FILE)) if row["slot"] == slot), {})
        if row.get("origin") == "captured":
            raise HTTPException(status_code=422, detail="原图保留真实采集内容；如需AI改图，请以该原图另建生图图位")
        plan = media_call(update_plan_slot, directory, slot=slot, **request.model_dump())
        media_call(refresh_plan_metadata, directory)
    return {"ok": True, "image_plan": plan}


@router.post("/slots/{slot}/generate", status_code=202)
def generate_slot(product_id: str, slot: str, request: GenerateRequest):
    from pipeline.image_jobs import enqueue_image, list_image_jobs, ImageJobConflict
    from pipeline.media_workspace import retry_image
    directory = directory_for(product_id)
    latest = next((row for row in reversed(list_image_jobs(directory)) if row["slot"] == slot), {})
    if latest.get("status") in {"failed", "unknown", "stale"}:
        if not request.confirm_new_charge:
            raise HTTPException(status_code=409, detail="上次任务失败或付费结果未知；请核对调用记录，并明确确认新的付费重试")
        job = media_call(retry_image, directory, slot, confirm_new_charge=True)
    else:
        job = media_call(enqueue_image, directory, slot)
    return {"ok": True, "job": job}


@router.post("/slots/{slot}/retry", status_code=202)
def retry_slot(product_id: str, slot: str, request: GenerateRequest):
    from pipeline.media_workspace import retry_image
    return {"ok": True, "job": media_call(retry_image, directory_for(product_id), slot,
            confirm_new_charge=request.confirm_new_charge)}


@router.post("/sets")
def add_set(product_id: str, request: SetRequest):
    from pipeline.media_workspace import create_set
    return {"ok": True, **media_call(create_set, directory_for(product_id), **request.model_dump())}


@router.post("/sets/{set_id}/generate", status_code=202)
def generate_whole_set(product_id: str, set_id: str):
    from pipeline.media_workspace import generate_set
    return {"ok": True, **media_call(generate_set, directory_for(product_id), set_id)}


@router.post("/adopt")
def adopt(product_id: str, request: AdoptRequest):
    from pipeline.media_workspace import adopt_original
    return {"ok": True, **media_call(adopt_original, directory_for(product_id), **request.model_dump())}


@router.put("/selection")
def selection(product_id: str, request: SelectionRequest):
    from pipeline.image_jobs import select_images
    plan = media_call(select_images, directory_for(product_id), request.selected_slots)
    return {"ok": True, "image_plan": plan, "selected_slots": plan["selected_slots"]}


@router.post("/confirm")
def confirm(product_id: str):
    from pipeline.guided_review import approve
    return {"ok": True, "review": media_call(approve, directory_for(product_id), "images")}
