"""Stepwise listing endpoints: local drafts first, an explicit live-write boundary last."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/workbench/products/{product_id}")


def directory_for(product_id: str, *, edit: bool = True) -> Path:
    import api
    directory = api._require_product(product_id)
    if edit:
        api._require_pre_submission_edit(directory)
    return directory


def run_service(fn, *args, **kwargs):
    from models import ModelError
    from pipeline.context import PipelineGateError
    from pipeline.ozon_write import OzonWriteError
    try:
        return fn(*args, **kwargs)
    except (ValueError, ModelError, PipelineGateError, OzonWriteError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=422, detail="这不是有效的 Ozon XLSX 模板") from error


def web_provider():
    from models import load_provider
    return run_service(load_provider, os.environ.get("WORKBENCH_WEB_PROVIDER", "ark"))


class GenerateRequest(BaseModel):
    force: bool = False


class CandidateGenerateRequest(GenerateRequest):
    revalidate_only: bool = False


class FingerprintRequest(BaseModel):
    input_fingerprint: str = Field(min_length=1, max_length=100)


class CandidateChoiceRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=100)
    title_ru: str | None = Field(None, min_length=1, max_length=200)
    description_ru: str | None = Field(None, min_length=1, max_length=6000)
    hashtags: list[str] | None = Field(None, max_length=30)


@router.post("/guided/analyze")
def analyze(product_id: str, request: GenerateRequest):
    from pipeline.guided_workflow import analyze_selected_product
    return {"ok": True, **run_service(analyze_selected_product, directory_for(product_id),
                                     web_provider(), force=request.force)}


@router.post("/guided/analysis/confirm")
def confirm_analysis(product_id: str, request: FingerprintRequest):
    from pipeline.guided_workflow import confirm_analysis as confirm
    return {"ok": True, "workflow": run_service(confirm, directory_for(product_id), request.input_fingerprint)}


@router.post("/guided/candidates")
def generate_candidates(product_id: str, request: CandidateGenerateRequest):
    from pipeline.guided_workflow import generate_copy_candidates
    return {"ok": True, **run_service(generate_copy_candidates, directory_for(product_id),
                                     web_provider(), force=request.force, revalidate_only=request.revalidate_only)}


@router.put("/guided/candidates/choose")
def choose_candidate(product_id: str, request: CandidateChoiceRequest):
    from pipeline.guided_workflow import choose_copy_candidate
    return {"ok": True, **run_service(choose_copy_candidate, directory_for(product_id),
                                     request.candidate_id, **request.model_dump(exclude={"candidate_id"}))}


@router.post("/guided/copy/confirm")
def confirm_copy(product_id: str, request: FingerprintRequest):
    from pipeline.guided_workflow import confirm_selected_copy
    from pipeline.guided_review import approve
    from pipeline.listing_form import read_json
    from pipeline.listing_defaults import persist_user_defaults
    from pipeline.product_edit_lock import product_edit_lock
    import api
    directory = directory_for(product_id)
    with product_edit_lock(directory):
        workflow = run_service(confirm_selected_copy, directory, request.input_fingerprint)
        selection = read_json(directory / "input/category-selection.json")
        run_service(persist_user_defaults, directory, api.MARKET_DB_PATH.parent, shop_id=selection.get("shop_id"),
                    resolve_dictionaries=False)
        return {"ok": True, "workflow": workflow, "review": run_service(approve, directory, "copy")}


@router.post("/guided/plan")
def plan_images(product_id: str, request: GenerateRequest):
    from pipeline.guided_workflow import plan_selected_images
    return {"ok": True, **run_service(plan_selected_images, directory_for(product_id),
                                     web_provider(), force=request.force)}


class ImageInsightsRequest(GenerateRequest):
    image_paths: list[str] = Field(min_length=1, max_length=3)


@router.post("/guided/image-insights")
def image_insights(product_id: str, request: ImageInsightsRequest):
    """Explicit advisory vision call; never certify facts or publish a listing."""
    from pipeline.image_insights import analyze_image_insights
    result = run_service(analyze_image_insights, directory_for(product_id),
                         image_paths=request.image_paths, force=request.force)
    return {"ok": True, "insights": {**(result.get("payload") or {}),
        "status": result.get("status"), "warning_zh": result.get("warning_zh")},
        "cache_hit": result.get("cache_hit", False), "model_calls": result.get("model_calls", 0)}


class StoreRequest(BaseModel):
    store: str | None = None
    confirm: str | None = None
    retry_rejected: bool = False


@router.post("/guided/prepare-card")
def prepare_card(product_id: str, request: StoreRequest):
    import api
    from pipeline.listing_draft import prepare_listing_card
    directory = directory_for(product_id)
    return {"ok": True, "report": run_service(prepare_listing_card, directory,
                                              shop=api._store_for(directory, request.store))}


@router.post("/guided/publish-media")
def publish_media(product_id: str, request: StoreRequest):
    from pipeline.guided_review import status
    from pipeline.oss_cos import _storage_from_env
    from pipeline.product_edit_lock import product_edit_lock
    from pipeline.listing_form import _require_editable
    directory = directory_for(product_id)
    if request.confirm != "PUBLISH_MEDIA":
        raise HTTPException(400, "公开图片需要明确确认 PUBLISH_MEDIA")
    with product_edit_lock(directory):
        run_service(_require_editable, directory)
        review = status(directory)
        if not review["sections"]["images"]["approved"]:
            raise HTTPException(409, "先确认整套正式图片，再发布图片地址")
        try:
            result = _storage_from_env().publish_product(directory)
        except Exception as error:
            raise HTTPException(422, "图片存储发布失败，请检查 COS 配置和权限") from error
    if result.get("missing") or not result.get("https_ok"):
        raise HTTPException(422, "图片尚未全部发布为可用 HTTPS 地址")
    return {"ok": True, "publication": result, "api_writes_performed": False}


class VideoPublicationChoice(BaseModel):
    video_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=200)
    source_sku_id: str | None = Field(default=None, max_length=100)


class VideoPublicationRequest(BaseModel):
    confirm: str
    rights_confirmed: bool = False
    videos: list[VideoPublicationChoice] = Field(min_length=1, max_length=5)


@router.post("/guided/publish-videos")
def publish_videos(product_id: str, request: VideoPublicationRequest):
    from pipeline.video_publish import publish_source_videos
    if request.confirm != "PUBLISH_VIDEOS":
        raise HTTPException(400, "公开原视频需要明确确认 PUBLISH_VIDEOS")
    result = run_service(publish_source_videos, directory_for(product_id),
                         [row.model_dump() for row in request.videos],
                         rights_confirmed=request.rights_confirmed)
    return {"ok": True, "publication": result,
            "selection": {"rights_confirmed": True, "videos": result["videos"]},
            "api_writes_performed": False}


@router.post("/guided/submit")
def submit(product_id: str, request: StoreRequest):
    import api
    from pipeline.listing_draft import submit_listing
    directory = directory_for(product_id, edit=False)
    if request.confirm != "SUBMIT":
        raise HTTPException(400, "提交会写入真实 Ozon 店铺，需要确认 SUBMIT")
    report = run_service(submit_listing, directory,
                         shop=api._store_for(directory, request.store),
                         retry_rejected=request.retry_rejected)
    return {"ok": bool(report.get("ok")), "report": report}


@router.get("/videos")
def videos(product_id: str):
    from pipeline.source_videos import list_source_videos
    from pipeline.context import read_json
    directory = directory_for(product_id, edit=False)
    result = run_service(list_source_videos, directory)
    return {"ok": True, **result,
            "selection": read_json(directory / "input/listing-media.json")}


@router.post("/videos/{video_id}/download")
def download_video(product_id: str, video_id: str):
    from pipeline.source_videos import download_source_video
    return {"ok": True, "video": run_service(download_source_video, directory_for(product_id), video_id)}


@router.get("/videos/{video_id}/file")
def video_file(product_id: str, video_id: str):
    from pipeline.source_videos import video_file as resolve
    path, mime = run_service(resolve, directory_for(product_id, edit=False), video_id)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, no-store"})


@router.post("/videos/upload")
async def upload_video(product_id: str, request: Request, filename: str = "video.mp4"):
    from pipeline.source_videos import store_uploaded_video
    from starlette.concurrency import run_in_threadpool
    directory = directory_for(product_id)
    limit = 100 * 1024 * 1024
    with tempfile.TemporaryFile() as handle:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, "首版视频上传上限 100 MB")
            handle.write(chunk)
        handle.seek(0)
        result = await run_in_threadpool(run_service, store_uploaded_video, directory, handle, filename)
    return {"ok": True, "video": result}


class VideoChoice(BaseModel):
    video_id: str
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(default="", max_length=200)
    source_sku_id: str | None = None


class MediaChoiceRequest(BaseModel):
    rights_confirmed: bool = False
    videos: list[VideoChoice] = Field(default_factory=list, max_length=5)
    video_cover: dict[str, Any] | None = None


def validate_public_media_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("上架视频需要不带账号密码的稳定 HTTPS 链接")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise ValueError("上架视频不能使用本机或内网地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("上架视频不能使用内网地址")
    # Temporary 1688 player URLs are capture evidence, never an Ozon publishing URL.
    if any(host == domain or host.endswith("." + domain) for domain in ("1688.com", "alicdn.com", "taobao.com")):
        raise ValueError("1688 临时视频链接不能直接用于上架，请使用 Ozon 当前支持的稳定视频地址")
    return value.strip()


@router.put("/videos/selection")
def select_videos(product_id: str, request: MediaChoiceRequest):
    from pipeline.context import read_json, write_json
    from pipeline.product_edit_lock import product_edit_lock
    from pipeline.source_videos import list_source_videos, validate_listing_videos
    from pipeline.sku_selection import selection_state
    directory = directory_for(product_id)
    if (request.videos or request.video_cover) and not request.rights_confirmed:
        raise HTTPException(422, "请确认视频使用授权、无联系方式及与所选规格一致")
    with product_edit_lock(directory):
        from pipeline.listing_form import _require_editable
        run_service(_require_editable, directory)
        rows = list_source_videos(directory)["videos"]
        known = {str(row["video_id"]): row for row in rows}
        active = set(selection_state(directory)["selected"])
        choices = []
        for item in request.videos:
            if item.video_id not in known:
                raise HTTPException(422, "视频不属于当前采集商品")
            if item.source_sku_id and item.source_sku_id not in active:
                raise HTTPException(422, "视频绑定的规格不在当前上架范围")
            choices.append({**item.model_dump(), "url": run_service(validate_public_media_url, item.url)})
        cover = request.video_cover
        if cover:
            raise HTTPException(422, "视频封面须单独验证 8–30 秒短视频，首版暂不接受未经检查的封面")
        result = {"rights_confirmed": request.rights_confirmed, "videos": choices}
        result["videos"] = run_service(validate_listing_videos, directory, result)
        write_json(directory / "input/listing-media.json", result)
    return {"ok": True, "selection": result, "api_writes_performed": False}


@router.post("/listing-template")
async def upload_template(product_id: str, request: Request):
    from pipeline.listing_export import inspect_template
    from pipeline.product_edit_lock import product_edit_lock
    directory = directory_for(product_id)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(413, "类目模板不得超过 5 MB")
    root = directory / "runtime"
    root.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=root, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    try:
        inspected = run_service(inspect_template, temporary)
        with product_edit_lock(directory):
            from pipeline.listing_form import _require_editable
            run_service(_require_editable, directory)
            os.replace(temporary, root / "listing-template.xlsx")
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "template": inspected}


@router.get("/listing-template")
def template_status(product_id: str):
    from pipeline.listing_export import inspect_template
    directory = directory_for(product_id, edit=False)
    path = directory / "runtime/listing-template.xlsx"
    return {"ok": True, "template": run_service(inspect_template, path) if path.is_file() else None}


@router.post("/listing-export")
def export_excel(product_id: str, request: StoreRequest):
    import api
    from pipeline.listing_draft import canonical_listing, card_fingerprint
    from pipeline.listing_export import export_listing_xlsx
    from pipeline.context import read_json, write_json
    from pipeline.product_edit_lock import product_edit_lock
    directory = directory_for(product_id, edit=False)
    with product_edit_lock(directory):
        payload = run_service(canonical_listing, directory, shop=api._store_for(directory, request.store))
        path = directory / "runtime/listing-template.xlsx"
        if not path.is_file():
            raise HTTPException(409, "请先上传当前真实类目最新下载的 Ozon 模板")
        with tempfile.TemporaryDirectory(prefix="listing-export-", dir=directory / "runtime") as temporary:
            staging = Path(temporary) / "export.xlsx"
            result = run_service(export_listing_xlsx, payload, path, staging)
            os.replace(staging, directory / "output/ozon-listing.xlsx")
        result.pop("path", None)
        write_json(directory / "runtime/listing-export-state.json", {
            "card_fingerprint": card_fingerprint(directory),
            "media": read_json(directory / "input/listing-media.json"),
            "urls": read_json(directory / "output/image-public-urls.json"),
            "template_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"ok": True, "report": result,
            "download_url": f"/api/workbench/products/{product_id}/listing-export/file"}


@router.get("/listing-export/file")
def download_excel(product_id: str):
    from pipeline.context import read_json
    from pipeline.listing_draft import card_fingerprint, _modern_ready
    from pipeline.guided_review import status
    directory = directory_for(product_id, edit=False)
    path = directory / "output/ozon-listing.xlsx"
    if not path.is_file():
        raise HTTPException(404, "尚未导出商品模板")
    run_service(_modern_ready, directory, plan=True)
    previous = read_json(directory / "runtime/listing-export-state.json")
    template = directory / "runtime/listing-template.xlsx"
    if (not status(directory)["ready_to_preflight"] or
        previous.get("card_fingerprint") != card_fingerprint(directory) or
        previous.get("media") != read_json(directory / "input/listing-media.json") or
        previous.get("urls") != read_json(directory / "output/image-public-urls.json") or
        not template.is_file() or previous.get("template_sha256") != hashlib.sha256(template.read_bytes()).hexdigest()):
        raise HTTPException(409, "商品资料或模板已变化，请重新确认并导出")
    return FileResponse(path, filename=f"{product_id}-ozon.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Cache-Control": "private, no-store"})
