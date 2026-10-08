"""Local rich-content editing and explicitly requested AI/storage operations."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictInt

from pipeline import rich_content as service
from workbench_listing_api import directory_for, run_service, web_provider

router = APIRouter(prefix="/api/workbench/products/{product_id}/rich-content")


class DraftRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    context_fingerprint: str = Field(min_length=64, max_length=64)
    blocks: list[dict] = Field(max_length=60)


class ImportRequest(BaseModel):
    filename: str = Field(max_length=250)
    data_base64: str = Field(max_length=14_000_000)


class GenerateRequest(DraftRequest):
    prompt: str = Field(min_length=1, max_length=6000)
    history: list[dict] = Field(default_factory=list, max_length=12)


class ApplyRequest(BaseModel):
    revision: StrictInt = Field(ge=0)
    context_fingerprint: str = Field(min_length=64, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=100)
    blocks: list[dict] | None = Field(default=None, max_length=60)


class PublishRequest(BaseModel):
    confirm: str


def _run(fn, *args, **kwargs):
    try:
        return run_service(fn, *args, **kwargs)
    except HTTPException as error:
        if isinstance(error.__cause__, service.RichContentConflict):
            raise HTTPException(409, str(error.__cause__)) from error
        raise


@router.get("")
def read_content(product_id: str):
    return _run(service.read_content, directory_for(product_id, edit=False))


@router.put("")
def save_content(product_id: str, request: DraftRequest):
    return _run(service.save_content, directory_for(product_id), **request.model_dump())


@router.post("/images")
def import_image(product_id: str, request: ImportRequest):
    return _run(service.import_image, directory_for(product_id), **request.model_dump())


@router.get("/images/{image_id}")
def image_preview(product_id: str, image_id: str):
    path = _run(service.media_path, directory_for(product_id, edit=False), image_id)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.post("/generate")
def generate_content(product_id: str, request: GenerateRequest):
    return _run(service.generate_content, directory_for(product_id), provider=web_provider(), **request.model_dump())


@router.put("/apply")
def apply_candidate(product_id: str, request: ApplyRequest):
    return _run(service.apply_candidate, directory_for(product_id), **request.model_dump())


@router.post("/publish")
def publish_content(product_id: str, request: PublishRequest):
    if request.confirm != "PUBLISH_MEDIA":
        raise HTTPException(400, "公开富内容图片需要明确确认 PUBLISH_MEDIA")
    return _run(service.publish_content, directory_for(product_id))
