"""本地工作台 HTTP 接口（关键词库 + 采集入库）。

启动（与既有工作台的 8765 分开，避免抢端口）：

    uvicorn api:app --app-dir ozon-workbench --host 127.0.0.1 --port 8766

环境变量：

- ``KEYWORD_LIBRARY_ROOT``：关键词库目录（默认 ``ozon-workbench/keyword-library``）
- ``WORKBENCH_PRODUCTS_ROOT``：商品目录（默认 ``ozon-workbench/products``）
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import tempfile
import hmac
from functools import wraps
from pathlib import Path
from typing import Any, Literal, Mapping

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from collector.ingest import (
    CaptureValidationError,
    DuplicateCaptureError,
    find_existing_capture,
    import_folder,
    ingest_capture,
)
from keyword_library import store
from keyword_library.scoring import ScoreConfig
from market_intelligence import store as market_store
from market_intelligence import recommend as market_recommend
from market_intelligence import sessions as research_sessions
from market_intelligence import ozon_categories
from market_intelligence import product_opportunities

LIBRARY_ROOT = Path(
    os.environ.get("KEYWORD_LIBRARY_ROOT")
    or (Path(__file__).resolve().parent / "keyword-library")
)

PRODUCTS_ROOT = Path(
    os.environ.get("WORKBENCH_PRODUCTS_ROOT")
    or (Path(__file__).resolve().parent / "products")
)

MARKET_DB_PATH = Path(
    os.environ.get("WORKBENCH_MARKET_DB_PATH")
    or (Path(__file__).resolve().parent / "runtime" / "market-intelligence.sqlite3")
)


def _locked_product_mutation(function):
    """Commit local edits under the same cross-worker lock as final submission."""
    @wraps(function)
    def wrapped(product_id, *args, **kwargs):
        from pipeline.product_edit_lock import product_edit_lock
        try:
            with product_edit_lock(_require_product(product_id)):
                _require_pre_submission_edit(_require_product(product_id))
                return function(product_id, *args, **kwargs)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    return wrapped

app = FastAPI(title="ozon-workbench · local workbench", version="0.2.0")


def _shop_authorization_context(request: Request) -> dict[str, Any]:
    """Never submit credentials over public HTTP, even behind a loopback proxy."""
    from urllib.parse import urlsplit

    host = (request.url.hostname or "").lower()
    local = host in {"127.0.0.1", "localhost", "::1"} and bool(
        request.client and request.client.host in {"127.0.0.1", "::1"}
    )
    secure = request.url.scheme == "https" or local
    origin = request.headers.get("origin")
    same_origin = True
    if origin:
        parsed = urlsplit(origin)
        same_origin = (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)
    if request.headers.get("sec-fetch-site") == "cross-site":
        same_origin = False
    reason = "" if secure and same_origin else (
        "授权请求必须来自当前工作台页面" if not same_origin else
        "请通过 HTTPS 或已建立 SSH 隧道的 http://127.0.0.1:8766 打开工作台后授权，公网 HTTP 禁止传输店铺密钥"
    )
    return {"can_submit_credentials": secure and same_origin, "reason": reason}


def _require_shop_admin(request: Request) -> None:
    context = _shop_authorization_context(request)
    if not context["can_submit_credentials"]:
        raise HTTPException(status_code=403, detail=context["reason"])


@app.exception_handler(RequestValidationError)
async def _validation_error_without_credentials(request: Request, error: RequestValidationError) -> Response:
    # FastAPI normally echoes the failing input. A malformed credential must never
    # be copied into a response, including failures in another field in this form.
    if request.url.path == "/api/workbench/stores/authorize":
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": item.get("loc"), "msg": item.get("msg"), "type": item.get("type")}
            for item in error.errors()
        ]})
    return await request_validation_exception_handler(request, error)


def _process_ready_background(session_id: str) -> None:
    """Background failures remain visible in logs without changing the HTTP acknowledgement."""
    import logging

    from market_intelligence.auto_publish import process_ready

    try:
        process_ready(MARKET_DB_PATH, PRODUCTS_ROOT, session_id=session_id)
    except Exception:  # noqa: BLE001 - no automatic retry after an ambiguous Ozon write
        logging.getLogger(__name__).exception("选词批次 %s 自动发布后台任务失败，需人工检查", session_id)


class MarketSnapshotRequest(BaseModel):
    source: str
    dataset: str
    capture_method: str
    period: str = ""
    period_kind: str = "calendar_month"
    page_url: str = ""
    captured_at: str = ""
    category_key: str | None = None
    records: list[dict[str, Any]] = Field(min_length=1, max_length=200)


def _market_auth(token: str | None) -> None:
    expected = os.environ.get("WORKBENCH_MARKET_INGEST_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="市场数据接口未配置写入令牌")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="市场数据接口令牌无效")


@app.post("/api/collector/market-snapshots")
def market_snapshot_ingest(
    payload: MarketSnapshotRequest,
    x_market_ingest_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Browser extension and future official-API adapters share this source-aware sink."""
    _market_auth(x_market_ingest_token)
    if len(json.dumps(payload.records, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="单次市场数据不能超过 1 MB")
    try:
        result = market_store.ingest_snapshot(MARKET_DB_PATH, **payload.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **result}


@app.get("/api/market-data/stats")
def market_data_stats(x_market_ingest_token: str | None = Header(default=None)) -> dict[str, Any]:
    _market_auth(x_market_ingest_token)
    return {"ok": True, **market_store.database_stats(MARKET_DB_PATH)}


@app.get("/api/market-data/observations")
def market_data_observations(
    dataset: str,
    source: str | None = None,
    category_key: str | None = None,
    period: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    x_market_ingest_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _market_auth(x_market_ingest_token)
    try:
        items = market_store.list_observations(
            MARKET_DB_PATH, dataset=dataset, source=source, category_key=category_key, period=period, limit=limit
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "count": len(items), "items": items}


@app.get("/api/research/config")
def research_config() -> dict[str, Any]:
    """Default explainable weights and evidence gates; no API calls."""
    return {"ok": True, "config": market_recommend.load_config(MARKET_DB_PATH).as_dict()}


class ResearchConfigRequest(BaseModel):
    min_history_months: int = 3
    min_demand: float = 0
    max_competition_density: float | None = None
    return_penalty: float = 8
    weights: dict[str, dict[str, float]] = Field(default_factory=dict)


@app.put("/api/research/config")
def update_research_config(request: ResearchConfigRequest) -> dict[str, Any]:
    try:
        config = market_recommend.save_config(MARKET_DB_PATH, request.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "config": config.as_dict()}


@app.get("/api/research/categories")
def research_categories() -> dict[str, Any]:
    return {"ok": True, **market_recommend.recommend(
        MARKET_DB_PATH, dataset="categories", config=market_recommend.load_config(MARKET_DB_PATH)
    )}


@app.get("/api/research/source-records")
def research_source_records(
    response: Response,
    dataset: Literal["categories", "keywords", "products"] = "categories",
    source: Literal["seerfar"] = "seerfar",
    category_key: str | None = Query(default=None, max_length=200),
    period: str | None = Query(default=None, max_length=7),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, Any]:
    """Read-only source report. The public workbench is behind nginx Basic Auth.

    Never put the market ingestion token into browser JavaScript just to view
    imported rows. FastAPI itself must remain bound to loopback on the server.
    """
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = market_store.list_source_records(
            MARKET_DB_PATH, dataset=dataset, source=source, category_key=category_key,
            period=period, q=q, limit=limit, offset=offset,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **result}


@app.get("/api/research/keywords")
def research_keywords(category_key: str = Query(..., min_length=1)) -> dict[str, Any]:
    return {"ok": True, **market_recommend.recommend(
        MARKET_DB_PATH, dataset="keywords", category_key=category_key,
        config=market_recommend.load_config(MARKET_DB_PATH)
    )}


@app.get("/api/research/product-opportunities")
def research_product_opportunities(
    response: Response,
    q: str = Query(default="", max_length=120),
    category_key: str = Query(default="", max_length=200),
    status: Literal["all", "recommended", "pending", "watch", "excluded"] = "recommended",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True, **product_opportunities.list_opportunities(
        MARKET_DB_PATH, q=q, category_key=category_key, status=status, limit=limit, offset=offset,
    )}


class ResearchSessionRequest(BaseModel):
    category_key: str
    primary_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list, max_length=10)


class AttachResearchProductRequest(BaseModel):
    product_id: str


class AutoPublishRequest(BaseModel):
    enabled: bool
    target_store_id: str = ""
    confirm: str = ""


@app.get("/api/research/sessions")
def list_research_sessions() -> dict[str, Any]:
    return {"ok": True, "items": research_sessions.list_sessions(MARKET_DB_PATH)}


@app.post("/api/research/sessions")
def create_research_session(request: ResearchSessionRequest) -> dict[str, Any]:
    try:
        result = research_sessions.create_session(MARKET_DB_PATH, **request.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "session": result}


@app.get("/api/research/sessions/{session_id}")
def get_research_session(session_id: str) -> dict[str, Any]:
    try:
        result = research_sessions.get_session(MARKET_DB_PATH, session_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True, "session": result}


@app.post("/api/research/sessions/{session_id}/products")
def attach_research_product(session_id: str, request: AttachResearchProductRequest) -> dict[str, Any]:
    try:
        result = research_sessions.attach_product(
            MARKET_DB_PATH, PRODUCTS_ROOT, session_id=session_id, product_id=request.product_id
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "session": result}


@app.put("/api/research/sessions/{session_id}/auto-publish")
def set_research_auto_publish(session_id: str, request: AutoPublishRequest,
                              background_tasks: BackgroundTasks) -> dict[str, Any]:
    if request.enabled and request.confirm != "ENABLE_AUTO_PUBLISH":
        raise HTTPException(status_code=400, detail="开启批次自动发布须明确确认 ENABLE_AUTO_PUBLISH")
    if request.enabled:
        if os.environ.get("WORKBENCH_AUTO_PUBLISH_ARMED") != "1":
            raise HTTPException(status_code=503, detail="服务器尚未启用自动发布总开关")
        from pipeline.stores import ensure_registry, shop_summary

        shops = shop_summary(ensure_registry(None))
        target = next((item for item in shops if item["id"] == request.target_store_id), None)
        if not target or not target.get("enabled") or not target.get("credentials_ready"):
            raise HTTPException(status_code=422, detail="目标店铺未启用或 Ozon API 凭据尚未就绪")
    try:
        result = research_sessions.set_auto_publish(
            MARKET_DB_PATH, session_id=session_id, enabled=request.enabled,
            target_store_id=request.target_store_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if request.enabled:
        background_tasks.add_task(_process_ready_background, session_id)
    return {"ok": True, "session": result}


@app.get("/api/research/sessions/{session_id}/publish-attempts")
def research_publish_attempts(session_id: str) -> dict[str, Any]:
    try:
        research_sessions.get_session(MARKET_DB_PATH, session_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True, "items": research_sessions.list_publish_attempts(MARKET_DB_PATH, session_id)}


@app.post("/api/research/sessions/{session_id}/process-ready")
def research_process_ready(session_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        session = research_sessions.get_session(MARKET_DB_PATH, session_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not session["auto_publish_enabled"]:
        raise HTTPException(status_code=409, detail="此批次尚未开启自动发布")
    if os.environ.get("WORKBENCH_AUTO_PUBLISH_ARMED") != "1":
        raise HTTPException(status_code=503, detail="服务器尚未启用自动发布总开关")
    background_tasks.add_task(_process_ready_background, session_id)
    return {"ok": True, "queued": True, "session_id": session_id}


@app.get("/api/ozon/categories")
def search_ozon_categories(q: str = Query(..., min_length=2), shop: str | None = None,
                           refresh: bool = False) -> dict[str, Any]:
    """Real leaf categories only; never turn Seerfar research IDs into listing IDs."""
    try:
        tree = ozon_categories.load_tree(MARKET_DB_PATH.parent, shop_id=shop, refresh=refresh)
    except Exception as error:  # noqa: BLE001 - credentials/API errors belong in UI
        raise HTTPException(status_code=503, detail=f"Ozon 官方类目暂不可用：{error}") from error
    items = ozon_categories.search(tree, q)
    return {"ok": True, "source": "ozon_seller_api", "items": items,
            "shop_id": tree.get("shop_id"), "fetched_at": tree.get("fetched_at"),
            "cache_hit": tree.get("cache_hit", False)}


class OfficialCategoryRequest(BaseModel):
    category_id: int = Field(gt=0)
    type_id: int = Field(gt=0)
    shop: str | None = None


@app.put("/api/workbench/products/{product_id}/ozon-category")
@_locked_product_mutation
def confirm_ozon_category(product_id: str, request: OfficialCategoryRequest) -> dict[str, Any]:
    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    from pipeline.category_form import load_form
    from pipeline.listing_form import persist_category_form, write_json

    try:
        form = load_form(MARKET_DB_PATH.parent, request.category_id, request.type_id, shop_id=request.shop)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Ozon 官方类目暂不可用：{error}") from error
    selected = {"category_id": request.category_id, "type_id": request.type_id,
                "category_path_zh": " / ".join(form["category_path"]), "source": "ozon_seller_api",
                "shop_id": form["shop_id"],
                "confirmed_by_user": True}
    path = directory / "input" / "category-selection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_json_file(path)
    if any(previous.get(key) != selected.get(key) for key in ("category_id", "type_id", "shop_id")):
        confirmations_path = directory / "input/human-confirmations.json"
        confirmations = _read_json_file(confirmations_path)
        for key in ("attributes", "sku_attributes", "attribute_provenance", "category_form_scope", "category_form_confirmed_at"):
            confirmations.pop(key, None)
        if confirmations_path.is_file():
            write_json(confirmations_path, confirmations)
        # A previous schema's inferred fill input must not survive a category change.
        for relative in ("output/attribute-fill-input.json", "output/ozon-dictionary-lookups.json"):
            stale = directory / relative
            if stale.is_file():
                stale.unlink()
    write_json(path, selected)
    persist_category_form(directory, form)
    from pipeline.guided_review import invalidate_from

    invalidate_from(directory, "product_analysis")
    return {"ok": True, "category": selected, "form": form, "api_writes_performed": False}


@app.get("/api/ozon/category-values")
def ozon_category_values(category_id: int = Query(..., gt=0), type_id: int = Query(..., gt=0),
                         attribute_id: int = Query(..., gt=0), shop: str | None = None,
                         q: str = Query("", max_length=200), last_value_id: int = Query(0, ge=0),
                         limit: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
    from pipeline.category_form import dictionary_values
    try:
        result = dictionary_values(MARKET_DB_PATH.parent, category_id, type_id, attribute_id,
                                   shop_id=shop, q=q, last_value_id=last_value_id, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"官方字典暂不可用：{error}") from error
    return {"ok": True, **result, "items": result["result"],
            "last_value_id": result.get("next_last_value_id") or last_value_id}


class ListingFormRequest(BaseModel):
    shop: str | None = None
    category_id: int = Field(gt=0)
    type_id: int = Field(gt=0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    per_sku_attributes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/workbench/products/{product_id}/listing-form")
def workbench_listing_form(product_id: str, shop: str | None = None, refresh: bool = False) -> dict[str, Any]:
    from pipeline.listing_form import product_form
    directory = _require_product(product_id)
    try:
        result = product_form(directory, MARKET_DB_PATH.parent,
                              shop_id=shop, refresh=refresh)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"官方类目表单暂不可用：{error}") from error
    return {"ok": True, **result}


@app.put("/api/workbench/products/{product_id}/listing-form")
def save_workbench_listing_form(product_id: str, request: ListingFormRequest) -> dict[str, Any]:
    from pipeline.listing_form import save_product_form
    from pipeline.context import PipelineGateError
    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    try:
        result = save_product_form(directory, MARKET_DB_PATH.parent, shop_id=request.shop,
                                  category_id=request.category_id, type_id=request.type_id,
                                  attributes=request.attributes, per_sku_attributes=request.per_sku_attributes,
                                  provenance=request.provenance)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PipelineGateError as error:
        raise HTTPException(status_code=409, detail=error.reason) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"保存官方表单失败：{error}") from error
    return {"ok": True, **result}


class ListingAutofillRequest(BaseModel):
    shop: str | None = None


def _listing_autofill(product_id: str, shop: str | None, *, resolve: bool) -> dict[str, Any]:
    from pipeline.listing_autofill import build_autofill

    directory = _require_product(product_id)
    try:
        result = build_autofill(directory, MARKET_DB_PATH.parent, shop_id=shop,
                                resolve_dictionaries=resolve)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"采集资料自动匹配暂不可用：{error}") from error
    return {"ok": True, **result}


@app.get("/api/workbench/products/{product_id}/listing-autofill")
def listing_autofill_cached(product_id: str, shop: str | None = None) -> dict[str, Any]:
    """Source + cached official metadata only; no model or live product write."""
    return _listing_autofill(product_id, shop, resolve=False)


@app.post("/api/workbench/products/{product_id}/listing-autofill")
def listing_autofill_resolve(product_id: str, request: ListingAutofillRequest) -> dict[str, Any]:
    """Bounded official dictionary reads; suggestions do not save a product."""
    return _listing_autofill(product_id, request.shop, resolve=True)


class ListingDetailsRequest(BaseModel):
    details: dict[str, Any] = Field(default_factory=dict, max_length=10)


@app.get("/api/workbench/products/{product_id}/listing-details")
def listing_details_get(product_id: str) -> dict[str, Any]:
    from pipeline.product_editor import read_listing_details

    return {"ok": True, **read_listing_details(_require_product(product_id))}


@app.put("/api/workbench/products/{product_id}/listing-details")
def listing_details_put(product_id: str, request: ListingDetailsRequest) -> dict[str, Any]:
    from pipeline.product_editor import save_listing_details

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    try:
        result = save_listing_details(directory, request.details)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail="商品资料保存失败，原资料已保留；请稍后重试") from error
    return {"ok": True, **result}

#: 远程采集入库单次请求的图片总量上限（base64 之后按解码后字节算）
MAX_CAPTURE_BYTES = int(os.environ.get("WORKBENCH_MAX_CAPTURE_BYTES") or 40 * 1024 * 1024)

_SCORE_FIELDS = (
    "lam",
    "min_heat_percentile",
    "max_competition_percentile",
    "min_search_volume",
    "max_competitor_count",
)


class CategoryRef(BaseModel):
    category_id: str
    type_id: str
    category_path_zh: str | None = None


class KeywordIn(BaseModel):
    keyword: str
    search_volume: float | None = None
    competitor_count: float | None = None
    ads_count: float | None = None
    cpc: float | None = None
    trend: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    category_id: str | None = None
    type_id: str | None = None
    category_path_zh: str | None = None


class IngestRequest(BaseModel):
    source: str = "seerfar"
    category: CategoryRef | None = None
    keywords: list[KeywordIn]


class ScoreRequest(BaseModel):
    lam: float | None = None
    min_heat_percentile: float | None = None
    max_competition_percentile: float | None = None
    min_search_volume: float | None = None
    max_competitor_count: float | None = None
    category_id: str | None = None
    type_id: str | None = None


class StatusRequest(BaseModel):
    keys: list[str]
    status: str
    reason: str | None = None
    product_id: str | None = None


def _config(payload: ScoreRequest | None) -> ScoreConfig:
    base = ScoreConfig()
    if payload is None:
        return base
    values = base.to_dict()
    for field in _SCORE_FIELDS:
        incoming = getattr(payload, field)
        if incoming is not None:
            values[field] = incoming
    return ScoreConfig(**values)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "library_root": str(LIBRARY_ROOT),
        "score_config": ScoreConfig().to_dict(),
    }


@app.post("/api/keywords/ingest")
def ingest(payload: IngestRequest) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in payload.keywords:
        category_id = item.category_id or (payload.category.category_id if payload.category else None)
        type_id = item.type_id or (payload.category.type_id if payload.category else None)
        if not category_id or not type_id:
            raise HTTPException(status_code=422, detail=f"关键词缺少类目：{item.keyword}")
        row = item.model_dump()
        row["category_id"] = str(category_id)
        row["type_id"] = str(type_id)
        if not row.get("category_path_zh") and payload.category:
            row["category_path_zh"] = payload.category.category_path_zh
        rows.append(row)
    if not rows:
        raise HTTPException(status_code=422, detail="keywords 为空")
    summary = store.upsert(LIBRARY_ROOT, rows, source=payload.source)
    return {"ok": True, **summary}


@app.get("/api/keywords")
def list_keywords(
    category_id: str | None = None,
    type_id: str | None = None,
    status: str | None = None,
    min_score: float | None = None,
    q: str | None = None,
    only_qualified: bool = False,
    order: str = Query("score", pattern="^(score|heat|competition|keyword)$"),
    limit: int = Query(100, ge=1, le=2000),
) -> dict[str, Any]:
    items = store.query(
        LIBRARY_ROOT,
        category_id=category_id,
        type_id=type_id,
        status=status,
        min_score=min_score,
        text=q,
        only_qualified=only_qualified,
        order=order,
        limit=limit,
    )
    return {"count": len(items), "items": items}


@app.get("/api/keywords/export")
def export_for_copy(
    category_id: str,
    type_id: str,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """给标题/简介生成用：优先已入库，其次达标，按分数降序。"""
    def collect(status: str | None, only_qualified: bool) -> list[dict[str, Any]]:
        return store.query(
            LIBRARY_ROOT,
            category_id=category_id,
            type_id=type_id,
            status=status,
            only_qualified=only_qualified,
            order="score",
            limit=limit,
        )

    chosen = collect(store.STATUS_IN_LIBRARY, False)
    if len(chosen) < limit:
        seen = {item["key"] for item in chosen}
        for item in collect(None, True):
            if item["key"] not in seen:
                chosen.append(item)
                seen.add(item["key"])
            if len(chosen) >= limit:
                break
    return {
        "category_id": category_id,
        "type_id": type_id,
        "count": len(chosen),
        "keywords": [
            {
                "keyword": item["keyword"],
                "score": item.get("score"),
                "status": item.get("status"),
                "search_volume": item.get("search_volume"),
                "competitor_count": item.get("competitor_count"),
            }
            for item in chosen
        ],
    }


@app.post("/api/keywords/score")
def rescore(payload: ScoreRequest | None = None) -> dict[str, Any]:
    config = _config(payload)
    category_id = payload.category_id if payload else None
    type_id = payload.type_id if payload else None
    if (category_id is None) != (type_id is None):
        raise HTTPException(status_code=422, detail="category_id 与 type_id 必须同时给出")
    summary = store.rescore(LIBRARY_ROOT, config, category_id=category_id, type_id=type_id)
    return {"ok": True, "score_config": config.to_dict(), **summary}


@app.post("/api/keywords/status")
def set_status(payload: StatusRequest) -> dict[str, Any]:
    try:
        summary = store.set_status(
            LIBRARY_ROOT,
            payload.keys,
            payload.status,
            reason=payload.reason,
            product_id=payload.product_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **summary}


@app.get("/api/keywords/categories")
def categories() -> dict[str, Any]:
    payload = store.index(LIBRARY_ROOT)
    return {
        "category_count": payload["category_count"],
        "total": payload["total"],
        "categories": payload["categories"],
    }


# ----------------------------------------------------------------- 采集入库（M1）


class FolderImportRequest(BaseModel):
    folder: str
    source_url: str | None = None
    category: CategoryRef | None = None
    skus: list[dict[str, Any]] | None = None
    title_zh: str | None = None
    allow_new_version: bool = False
    keywords: list[str] = Field(
        default_factory=list,
        description="来自选品清单的关键词：会写进 source.json 与 selected-keywords.json",
    )
    keyword_source: str = "collection_plan"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _product_summary(product_id: str) -> dict[str, Any]:
    directory = PRODUCTS_ROOT / product_id
    status = _read_json_file(directory / "status.json")
    source = _read_json_file(directory / "input" / "source.json")
    return {
        "product_id": product_id,
        "status": status.get("status"),
        "current_step": status.get("current_step"),
        "next_action": status.get("next_action"),
        "progress": status.get("progress"),
        "batch_id": status.get("batch_id"),
        "attention_required": status.get("attention_required"),
        "error_message": status.get("error_message"),
        "source_url": source.get("source_url"),
        "title_zh": source.get("title_zh"),
        "collection_id": source.get("collection_id"),
        "captured_at": source.get("captured_at"),
        "sku_count": len(source.get("skus") or []),
        "image_counts": source.get("images") or {},
    }


def _list_product_ids() -> list[str]:
    if not PRODUCTS_ROOT.is_dir():
        return []
    return sorted(
        path.name
        for path in PRODUCTS_ROOT.iterdir()
        if path.is_dir() and (path / "status.json").is_file()
    )


@app.post("/api/collector/products")
def collector_ingest(payload: dict[str, Any], allow_new_version: bool = False) -> dict[str, Any]:
    """采集入库：同一 offer 重复采集返回 409（不覆盖已有商品）。"""
    try:
        summary = ingest_capture(PRODUCTS_ROOT, payload, allow_new_version=allow_new_version)
    except DuplicateCaptureError as error:
        raise HTTPException(status_code=409, detail=error.to_dict()) from error
    except CaptureValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **summary}


@app.post("/api/collector/products/import-folder")
def collector_import_folder(request: FolderImportRequest) -> dict[str, Any]:
    """把本地素材文件夹导入为采集商品（不依赖浏览器插件）。"""
    category = request.category.model_dump() if request.category else None
    try:
        summary = import_folder(
            PRODUCTS_ROOT,
            request.folder,
            source_url=request.source_url,
            category=category,
            skus=request.skus,
            title_zh=request.title_zh,
            allow_new_version=request.allow_new_version,
            keywords=request.keywords or None,
            keyword_source=request.keyword_source,
        )
    except DuplicateCaptureError as error:
        raise HTTPException(status_code=409, detail=error.to_dict()) from error
    except CaptureValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **summary}


class CaptureUploadRequest(BaseModel):
    """远程采集入库：图片以 base64 随 JSON 传（不依赖 multipart，也不要求服务器能访问你的磁盘）。"""

    source_url: str
    title_zh: str | None = None
    category: CategoryRef | None = None
    skus: list[dict[str, Any]] = Field(..., min_length=1, max_length=10)
    keywords: list[str] = Field(default_factory=list)
    keyword_source: str = "collection_plan"
    keyword_category: CategoryRef | None = None
    images: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description="{main|sku|detail: [{name, data_base64}]}",
    )
    allow_new_version: bool = False


def _write_capture_images(request: CaptureUploadRequest, target: Path) -> None:
    """把 base64 图片落到临时目录，交给 import_folder 统一入库（去重/计数/清单都在那里）。"""
    if not request.images:
        raise HTTPException(status_code=422, detail="没有图片：请至少上传 main 图")
    if len(request.images) > 3:
        raise HTTPException(status_code=422, detail=f"未知的图片角色：{sorted(request.images)}")
    total = 0
    for role, entries in request.images.items():
        if role not in {"main", "sku", "detail"}:
            raise HTTPException(status_code=422, detail=f"未知的图片角色：{role}（可用 main/sku/detail）")
        if not entries:
            continue
        if len(entries) > 20:
            raise HTTPException(status_code=422, detail=f"{role} 图片过多（最多 20 张）")
        directory = target / f"{role}-images"
        directory.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            raw = str(entry.get("data_base64") or "")
            if not raw:
                raise HTTPException(status_code=422, detail=f"{role} 第 {index} 张缺少 data_base64")
            try:
                data = base64.b64decode(raw, validate=True)
            except (ValueError, binascii.Error) as error:
                raise HTTPException(status_code=422, detail=f"{role} 第 {index} 张 base64 解码失败：{error}") from error
            total += len(data)
            if total > MAX_CAPTURE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"图片总量超过上限 {MAX_CAPTURE_BYTES // (1024 * 1024)}MB（当前 {total // (1024 * 1024)}MB）",
                )
            name = _sanitize_filename(str(entry.get("name") or f"{role}-{index:03d}.png"))
            (directory / name).write_bytes(data)
    if total == 0:
        raise HTTPException(status_code=422, detail="上传的图片都是空的")


def _sanitize_filename(name: str) -> str:
    """只保留安全字符：去掉路径分隔符、折叠点串（防 `..` 之类"),并限制长度。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)      # ".." -> "."
    cleaned = cleaned.strip(".")                    # 去掉首尾的点
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[-80:] or "image.png"


@app.post("/api/collector/products/capture")
def collector_capture_upload(request: CaptureUploadRequest) -> dict[str, Any]:
    """远程采集入库（图片 base64）：Windows 本地采集 → SSH 隧道 → 服务器入库。"""
    workdir = Path(tempfile.mkdtemp(prefix="ozon-capture-"))
    try:
        _write_capture_images(request, workdir)
        descriptor: dict[str, Any] = {
            "source_url": request.source_url,
            "title_zh": request.title_zh,
            "skus": request.skus,
        }
        if request.category:
            descriptor["category"] = request.category.model_dump()
        if request.keywords:
            descriptor["keywords"] = request.keywords
            descriptor["keyword_source"] = request.keyword_source
        if request.keyword_category:
            descriptor["keyword_category"] = request.keyword_category.model_dump()
        (workdir / "product.json").write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary = import_folder(
            PRODUCTS_ROOT, workdir, allow_new_version=request.allow_new_version
        )
    except DuplicateCaptureError as error:
        raise HTTPException(status_code=409, detail=error.to_dict()) from error
    except CaptureValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {"ok": True, "via": "capture-upload", **summary}


class SkuSelectionRequest(BaseModel):
    """选择上架 SKU（未选中的不进 offer、不生成主图、不参与定价）。"""

    include: list[str] = Field(default_factory=list, description="要上架的 SKU（白名单）")
    exclude: list[str] = Field(default_factory=list, description="不上架的 SKU（从全部里去掉）")
    reason: str | None = Field(None, description="排除原因（记进文件，便于回溯）")
    note: str | None = None
    all: bool = Field(False, description="全部上架（等价于清除选择文件）")


class ManualSkuPrice(BaseModel):
    sku_id: str
    price: float = Field(gt=0)
    currency: str = Field(pattern="^(CNY|RUB)$")


class ManualPricesRequest(BaseModel):
    prices: list[ManualSkuPrice] = Field(min_length=1, max_length=10)


class Dimensions(BaseModel):
    length_mm: int = Field(gt=0)
    width_mm: int = Field(gt=0)
    height_mm: int = Field(gt=0)
    weight_g: int = Field(gt=0)


class ConfirmedMeasurementsRequest(BaseModel):
    product: Dimensions
    package: Dimensions


@app.get("/api/workbench/products/{product_id}/measurements")
def get_confirmed_measurements(product_id: str) -> dict[str, Any]:
    directory = _require_product(product_id)
    return {"ok": True, "overrides": _read_json_file(directory / "input" / "workbench-sku-overrides.json")}


@app.put("/api/workbench/products/{product_id}/measurements")
@_locked_product_mutation
def set_confirmed_measurements(product_id: str, request: ConfirmedMeasurementsRequest) -> dict[str, Any]:
    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    product = request.product.model_dump()
    package = request.package.model_dump()
    if any(package[name] < product[name] for name in product):
        raise HTTPException(status_code=422, detail="包装尺寸和重量不能小于商品本体")
    target = directory / "input" / "workbench-sku-overrides.json"
    current = _read_json_file(target)
    current["product"] = {**{f"product_{key}": value for key, value in product.items()},
                          **{f"package_{key}": value for key, value in package.items()}}
    target.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from pipeline.guided_review import invalidate_from

    invalidate_from(directory, "measurements")
    return {"ok": True, "overrides": current}


@app.get("/api/workbench/products/{product_id}/prices")
def get_manual_prices(product_id: str) -> dict[str, Any]:
    directory = _require_product(product_id)
    return {"ok": True, "required": (directory / "input" / "manual-pricing-required.json").is_file(),
            "prices": _read_json_file(directory / "input" / "manual-prices.json").get("prices") or {}}


@app.put("/api/workbench/products/{product_id}/prices")
@_locked_product_mutation
def set_manual_prices(product_id: str, request: ManualPricesRequest) -> dict[str, Any]:
    from pipeline.sku_selection import active_skus

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    source = _read_json_file(directory / "input" / "source.json")
    active = {str(row.get("sku_id")) for row in active_skus(directory, source.get("skus") or [])}
    prices = {item.sku_id: {"price": item.price, "currency": item.currency} for item in request.prices}
    if len(prices) != len(request.prices) or set(prices) != active:
        raise HTTPException(status_code=422, detail="必须为每个已选 SKU 分别填写一次售价，不能包含未选 SKU")
    path = directory / "input" / "manual-prices.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"prices": prices}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from pipeline.guided_review import invalidate_from

    invalidate_from(directory, "measurements")
    return {"ok": True, "prices": prices}


@app.get("/api/workbench/products/{product_id}/skus")
def workbench_product_skus(product_id: str) -> dict[str, Any]:
    """看某个商品采集到的 SKU 与当前上架范围。"""
    from pipeline.sku_selection import selection_state, source_skus

    directory = _require_product(product_id)
    state = selection_state(directory)
    rows = source_skus(directory)
    return {
        "ok": True,
        "product_id": product_id,
        **state,
        "skus": [
            {
                "sku_id": str(item.get("sku_id") or f"S{index}"),
                "sku_name": item.get("sku_name") or item.get("name_zh") or item.get("name") or item.get("spec_zh"),
                "option_values": item.get("option_values") or [],
                "image_url": item.get("image_url") or item.get("variant_image_url"),
                "image_path": item.get("image_path"),
                "collection_issues": item.get("collection_issues") or [],
                "color_ru": item.get("color_ru"),
                "capacity": item.get("capacity"),
                "purchase_price_cny": item.get("purchase_price_cny"),
                "listed": str(item.get("sku_id") or f"S{index}") in set(state["selected"]),
            }
            for index, item in enumerate(rows, start=1)
        ],
    }


@app.post("/api/workbench/products/{product_id}/skus")
@_locked_product_mutation
def workbench_set_product_skus(product_id: str, request: SkuSelectionRequest) -> dict[str, Any]:
    """设置上架 SKU。"""
    from pipeline.sku_selection import SkuSelectionError, clear_selection, set_selection, selection_state, source_skus

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    try:
        if request.all:
            if _read_json_file(directory / "input" / "source.json").get("sku_selection_required"):
                result = set_selection(directory, include=[str(row["sku_id"]) for row in source_skus(directory)])
            else:
                result = clear_selection(directory)
        else:
            result = set_selection(
                directory,
                include=request.include,
                exclude=request.exclude,
                reason=request.reason,
                note=request.note,
            )
    except SkuSelectionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    from pipeline.guided_review import invalidate_from

    invalidate_from(directory, "product_analysis")
    return {"ok": True, **result, "state": selection_state(directory)}


class LaunchRequest(BaseModel):
    """一键跑一个商品（默认干跑：fake 模型 + 占位生图，绝不碰 Ozon）。"""

    provider: str | None = Field(None, description="模型层：fake（默认）/ ark / http / none")
    image_generator: str | None = Field(None, description="生图后端：placeholder / doubao / none")
    uploader: str | None = Field(None, description="上传器：dry-run（默认）/ simulated / ozon-api")
    stores: list[str] = Field(default_factory=list, description="目标店铺；不填则用商品上已记录的")
    oss: str = Field("none", description="图片发布：cos / local / none")
    oss_root: str | None = None
    oss_base_url: str | None = None
    ozon_fixture: bool = Field(
        False, description="用 contracts/fixtures 做 Ozon 只读调用（离线演练）；否则尝试用配置的凭据"
    )
    execute_upload: bool = False
    step_budget: int = Field(30, ge=1, le=60)


@app.post("/api/workbench/products/{product_id}/launch")
def workbench_launch(product_id: str, request: LaunchRequest) -> dict[str, Any]:
    """在界面上点一下就能跑：授权 → 生图 → 发布图片 → 质检 → 载荷 → 提交（默认干跑）。"""
    from models import ModelError, load_image_generator, load_provider
    from pipeline.launch import launch_product
    from pipeline.upload import DryRunUploader, SimulatedUploader

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    if (directory / "input/guided-workflow.json").is_file():
        raise HTTPException(status_code=409, detail="分步商品请分别确认摘要、文案、图片和卡片；最后使用确认提交入口")

    provider = None
    if str(request.provider or "fake").lower() not in {"none", "off"}:
        try:
            provider = load_provider(request.provider)
        except ModelError as error:
            raise HTTPException(status_code=422, detail=f"模型层不可用：{error}") from error

    image_generator = None
    if request.image_generator and request.image_generator.lower() not in {"none", "off"}:
        try:
            image_generator = load_image_generator(request.image_generator)
        except ModelError as error:
            raise HTTPException(status_code=422, detail=f"生图后端不可用：{error}") from error

    uploader = None
    kind = str(request.uploader or "dry-run").lower()
    if kind not in {"none", "off"}:
        if kind in {"ozon", "ozon-api", "real"}:
            raise HTTPException(
                status_code=409,
                detail="界面上不允许真实提交：请用 CLI（pipeline.launch --uploader ozon-api --execute-upload）",
            )
        uploader = SimulatedUploader() if kind in {"simulated", "sim"} else DryRunUploader()

    publisher = None
    oss = str(request.oss or "none").lower()
    if oss == "cos":
        from pipeline.oss_cos import CosError, _storage_from_env

        try:
            publisher = _storage_from_env()
        except CosError as error:
            raise HTTPException(status_code=422, detail=f"COS 不可用：{error}") from error
    elif oss == "local":
        if not (request.oss_root and request.oss_base_url):
            raise HTTPException(status_code=422, detail="oss=local 需要 oss_root 与 oss_base_url")
        from pipeline.oss_local import LocalObjectStorage

        publisher = LocalObjectStorage(request.oss_root, request.oss_base_url)

    report = launch_product(
        directory,
        provider=provider,
        image_generator=image_generator,
        uploader=uploader,
        publisher=publisher,
        ozon_client=_ozon_client_for_launch(use_fixture=request.ozon_fixture,
                                           shop_id=request.stores[0] if request.stores else None),
        store_ids=request.stores,
        execute_upload=bool(request.execute_upload),
        step_budget=request.step_budget,
    )
    return {"ok": bool(report.get("ok")), "report": report}


def _ozon_client_for_launch(*, use_fixture: bool, shop_id: str | None = None) -> Any | None:
    """给一键跑准备 Ozon 只读客户端：夹具优先，否则用已启用店铺的凭据；都没有就返回 None（门禁会提示）。"""
    if use_fixture:
        from pipeline.ozon_http import FixtureTransport, OzonClient

        return OzonClient(FixtureTransport(directory=Path(__file__).resolve().parent / "contracts" / "fixtures"))
    try:
        from pipeline.ozon_http import OzonClient, OzonCredentials, UrllibTransport
        from pipeline.stores import load_registry
        from pipeline.shop_authorization import select_read_shop

        shop = select_read_shop(load_registry(), shop_id)
        return OzonClient(UrllibTransport(OzonCredentials.from_shop(shop)))
    except Exception:  # noqa: BLE001 - 缺凭据不在这里报错，让流水线给出可操作提示
        return None


@app.get("/api/collector/products")
def collector_list(status: str | None = None) -> dict[str, Any]:
    items = [_product_summary(product_id) for product_id in _list_product_ids()]
    if status:
        items = [item for item in items if str(item.get("status") or "") == status]
    return {"count": len(items), "items": items}


@app.get("/api/collector/products/{product_id}")
def collector_detail(product_id: str) -> dict[str, Any]:
    directory = PRODUCTS_ROOT / product_id
    if not (directory / "status.json").is_file():
        raise HTTPException(status_code=404, detail=f"商品不存在：{product_id}")
    return {
        "summary": _product_summary(product_id),
        "status": _read_json_file(directory / "status.json"),
        "source": {key: value for key, value in _read_json_file(directory / "input" / "source.json").items() if key != "videos"},
        "manifest": _read_json_file(directory / "input" / "source-manifest.json"),
    }


# ------------------------------------------------------- 效果回流（M4）


def _seller_transport(shop_id: str | None) -> Any:
    """为指定（或首个已启用）店铺构建 Seller 只读传输层。"""
    from pipeline.ozon_http import OzonCredentials, UrllibTransport
    from pipeline.stores import enabled_shop_ids, list_shops, load_registry

    registry = load_registry()
    shops = list_shops(registry)
    if not shops:
        raise HTTPException(status_code=400, detail="没有可用店铺：先配置 config/shops.json")
    enabled = set(enabled_shop_ids(registry))
    if shop_id:
        shop = next((item for item in shops if str(item.get("id")) == str(shop_id)), None)
        if shop is None:
            raise HTTPException(status_code=404, detail=f"店铺不存在：{shop_id}")
    else:
        shop = next((item for item in shops if str(item.get("id")) in enabled), shops[0])
    try:
        return UrllibTransport(OzonCredentials.from_shop(shop))
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"店铺凭据未就绪：{error}") from error


class ProductQueriesRequest(BaseModel):
    product_id: str
    shop: str | None = None
    days: int = 30
    skus: list[str] | None = None


@app.post("/api/analytics/product-queries")
def analytics_product_queries(request: ProductQueriesRequest) -> dict[str, Any]:
    """拉取一个已上架商品的搜索表现并落盘（只读，不发写请求）。"""
    from collector.product_queries import ProductQueriesError, collect

    product_dir = PRODUCTS_ROOT / request.product_id
    if not (product_dir / "status.json").is_file():
        raise HTTPException(status_code=404, detail=f"商品不存在：{request.product_id}")
    transport = _seller_transport(request.shop)
    try:
        report = collect(
            product_dir, transport, skus=request.skus, days=request.days
        )
    except ProductQueriesError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "product_id": request.product_id, "items": report["items"]}


@app.get("/api/analytics/product-queries/{product_id}")
def analytics_product_queries_read(product_id: str) -> dict[str, Any]:
    from collector.product_queries import read_report

    report = read_report(PRODUCTS_ROOT / product_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"尚无效果数据：{product_id}（先点拉取）")
    return report


class SearchPhrasesRequest(BaseModel):
    campaigns: list[str] = Field(..., min_length=1, max_length=10)
    days: int = 14
    category_id: str | None = None
    type_id: str | None = None
    category_path_zh: str | None = None
    feed_library: bool = True


@app.get("/api/analytics/search-phrases/config")
def search_phrases_config() -> dict[str, Any]:
    cid = str(os.environ.get("OZON_PERFORMANCE_CLIENT_ID") or "").strip()
    secret = str(os.environ.get("OZON_PERFORMANCE_CLIENT_SECRET") or "").strip()
    return {
        "ready": bool(cid and secret),
        "missing": [
            name for name, ok in (
                ("OZON_PERFORMANCE_CLIENT_ID", bool(cid)),
                ("OZON_PERFORMANCE_CLIENT_SECRET", bool(secret)),
            ) if not ok
        ],
    }


@app.post("/api/analytics/search-phrases")
def analytics_search_phrases(request: SearchPhrasesRequest) -> dict[str, Any]:
    """跑 Performance API SEARCH_PHRASES 报表，真实词写进关键词库（可关）。"""
    from collector.performance_http import UrllibPerformanceTransport
    from collector.search_phrases import (
        PERF_BASE_URL,
        PerformanceCredentials,
        SearchPhrasesError,
        run,
    )

    creds = PerformanceCredentials.from_env()
    if not creds.client_id or not creds.client_secret:
        raise HTTPException(
            status_code=400,
            detail="缺少 Performance API 密钥：设置环境变量 OZON_PERFORMANCE_CLIENT_ID / "
                   "OZON_PERFORMANCE_CLIENT_SECRET（广告后台「设置→API-ключи」创建）",
        )
    transport = UrllibPerformanceTransport(PERF_BASE_URL)
    try:
        result = run(
            transport,
            campaigns=request.campaigns,
            days=request.days,
            category_id=request.category_id,
            type_id=request.type_id,
            category_path_zh=request.category_path_zh,
            library_root=LIBRARY_ROOT if request.feed_library else None,
            credentials=creds,
        )
    except SearchPhrasesError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, **result}


# ------------------------------------------------------- 选词与文案生成（M2）


class KeywordSelectionRequest(BaseModel):
    keywords: list[Any] | None = None
    from_library: bool = False
    limit: int = Field(10, ge=1, le=50)
    category_id: str | None = None
    type_id: str | None = None
    library_root: str | None = None


class CopyRequestModel(BaseModel):
    provider: str | None = None
    steps: list[str] = Field(default_factory=lambda: ["product_analysis", "russian_copy"])


def _require_product(product_id: str) -> Path:
    if not re.fullmatch(r"P[0-9]{6}", product_id):
        raise HTTPException(status_code=404, detail="商品编号无效")
    directory = (PRODUCTS_ROOT / product_id).resolve()
    if not directory.is_relative_to(PRODUCTS_ROOT.resolve()):
        raise HTTPException(status_code=404, detail="商品编号无效")
    if not (directory / "status.json").is_file():
        raise HTTPException(status_code=404, detail=f"商品不存在：{product_id}")
    return directory


def _require_pre_submission_edit(directory: Path) -> None:
    from pipeline.status import load_status

    if int(load_status(directory).get("api_write_count") or 0) > 0 or (directory / "runtime/listing-submit-attempt.json").is_file():
        raise HTTPException(status_code=409, detail="商品已经向 Ozon 发起写入，请另建版本后再修改")


@app.get("/api/workbench/products/{product_id}/keywords")
def get_selected_keywords(product_id: str) -> dict[str, Any]:
    from pipeline.selection import load_selected_keywords

    directory = _require_product(product_id)
    payload = load_selected_keywords(directory)
    return {"product_id": product_id, "selection": payload}


@app.put("/api/workbench/products/{product_id}/keywords")
@_locked_product_mutation
def put_selected_keywords(product_id: str, request: KeywordSelectionRequest) -> dict[str, Any]:
    """选词：直接给词，或从关键词库按分数取（``from_library=true``）。"""
    from pipeline.selection import select_from_library, set_selected_keywords

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    try:
        if request.from_library:
            payload = select_from_library(
                directory,
                request.library_root or LIBRARY_ROOT,
                category_id=request.category_id,
                type_id=request.type_id,
                limit=request.limit,
            )
        else:
            if not request.keywords:
                raise HTTPException(status_code=422, detail="keywords 为空；或设 from_library=true")
            payload = set_selected_keywords(directory, request.keywords, source="manual")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    from pipeline.guided_review import invalidate_from

    invalidate_from(directory, "product_analysis")
    return {"ok": True, "selection": payload}


@app.post("/api/workbench/products/{product_id}/copy")
def generate_copy(product_id: str, request: CopyRequestModel) -> dict[str, Any]:
    """跑模型步骤生成产品信息总结与俄文标题/简介（fake 也可，用于自检）。"""
    from models import ModelError, load_provider
    from pipeline.context import PipelineGateError
    from pipeline.handlers import MODEL_HANDLERS, run_single_step

    directory = _require_product(product_id)
    unknown = [step for step in request.steps if step not in MODEL_HANDLERS]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"不是模型步骤：{', '.join(unknown)}（可选：{', '.join(sorted(MODEL_HANDLERS))}）"
        )
    try:
        provider = load_provider(request.provider)
    except ModelError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    results: list[dict[str, Any]] = []
    for step in request.steps:
        try:
            result = run_single_step(directory, step, provider=provider)
        except PipelineGateError as error:
            raise HTTPException(
                status_code=409,
                detail={"step": error.step, "reason": error.reason, "details": error.details, "done": results},
            ) from error
        results.append({"step": step, **result})
    return {"ok": True, "provider": getattr(provider, "name", "unknown"), "results": results}


# ------------------------------------------------- 产物 / 台账 / 预检 / 运行（M3–M4）


class RunPipelineRequest(BaseModel):
    provider: str | None = Field(None, description="模型层：fake / none（默认 fake）")
    image_generator: str | None = Field(None, description="生图后端：placeholder / none")
    uploader: str | None = Field(None, description="上传器：dry-run / simulated / none")
    until: str | None = None
    step_budget: int = Field(8, ge=1, le=25)
    execute_upload: bool = Field(False, description="只有显式打开且服务侧 app_mode=production 才会真提交")


ARTIFACT_FILES: tuple[tuple[str, str], ...] = (
    ("采集输入", "input/source.json"),
    ("已选关键词", "input/selected-keywords.json"),
    ("商品分析", "output/product-analysis.json"),
    ("俄文文案", "output/copy-ru.json"),
    ("图片计划", "output/image-plan.json"),
    ("生图报告", "output/image-generation-report.json"),
    ("图片质检", "output/image-qc-report.json"),
    ("最终属性", "output/ozon-attributes-final.json"),
    ("上传载荷", "output/store-runs"),
    ("上传汇总", "output/upload-summary.json"),
    ("发布台账", "output/store-publications.json"),
    ("运行报告", "output/run-report.json"),
)


@app.get("/api/workbench/products/{product_id}/artifacts")
def product_artifacts(product_id: str) -> dict[str, Any]:
    """列出该商品的产物与关键摘要（界面用它显示"做到哪一步了"）。"""
    directory = _require_product(product_id)
    files: list[dict[str, Any]] = []
    for label, relative in ARTIFACT_FILES:
        path = directory / relative
        if path.is_dir():
            stored = sorted(item for item in path.rglob("*") if item.is_file())
            files.append(
                {
                    "label": label,
                    "path": relative,
                    "kind": "dir",
                    "exists": True,
                    "file_count": len(stored),
                }
            )
        else:
            files.append(
                {
                    "label": label,
                    "path": relative,
                    "kind": "file",
                    "exists": path.is_file(),
                    "bytes": path.stat().st_size if path.is_file() else 0,
                }
            )

    qc = _read_json_file(directory / "output" / "image-qc-report.json")
    attributes = _read_json_file(directory / "output" / "ozon-attributes-final.json")
    plan = _read_json_file(directory / "output" / "image-plan.json")
    return {
        "product_id": product_id,
        "summary": _product_summary(product_id),
        "files": files,
        "image_qc": {
            "decision": qc.get("decision"),
            "score": qc.get("score"),
            "critical_failures": qc.get("critical_failures"),
        },
        "attributes": attributes.get("required_summary"),
        "planned_images": {
            "main": len(plan.get("main_images") or []),
            "detail": len(plan.get("detail_images") or []),
        },
    }


@app.get("/api/workbench/products/{product_id}/publications")
def product_publications(product_id: str) -> dict[str, Any]:
    """发布台账 + 分发计划（幂等状态一目了然）。"""
    from pipeline.publications import load_publications, plan_publications
    from pipeline.stores import enabled_shop_ids, load_registry

    directory = _require_product(product_id)
    publications = load_publications(directory)
    store_ids = [str(item) for item in (publications.get("stores") or {}).keys()]
    if not store_ids:
        status = _read_json_file(directory / "status.json")
        store_ids = [str(item) for item in (status.get("target_store_ids") or [])]
    plan = plan_publications(directory, store_ids, enabled_store_ids=enabled_shop_ids(load_registry())) if store_ids else []
    return {"product_id": product_id, "publications": publications, "plan": plan}


@app.get("/api/workbench/doctor")
def workbench_doctor(product_id: str | None = None) -> dict[str, Any]:
    """上线前预检：还差什么才能真正提交（不发起任何 Ozon 调用）。"""
    from pipeline.doctor import run_doctor

    return run_doctor(PRODUCTS_ROOT, product_ids=[product_id] if product_id else None)


@app.get("/api/workbench/summary")
def workbench_summary() -> dict[str, Any]:
    """Edge 插件「测试连接」用的轻量状态：只说服务活着、有多少商品、店铺是否就绪。"""
    from pipeline.stores import ensure_registry, shop_summary

    product_ids = _list_product_ids()
    shops = shop_summary(ensure_registry(None))
    ready_shops = [s for s in shops if s.get("enabled") and s.get("credentials_ready")]
    return {
        "ok": True,
        "version": app.version,
        "products_root": str(PRODUCTS_ROOT),
        "product_count": len(product_ids),
        "shop_count": len(shops),
        "ready_shop_count": len(ready_shops),
        "ready_shops": [s.get("id") for s in ready_shops],
    }


@app.get("/api/collector/duplicates")
def collector_duplicates(source_url: str = Query(...)) -> dict[str, Any]:
    """Edge 插件采集前查重：同一 1688 offer 是否已入库。"""
    existing = find_existing_capture(PRODUCTS_ROOT, source_url)
    if existing is None:
        return {"exists": False, "source_url": source_url}
    return {"exists": True, "source_url": source_url, "product_id": existing["product_id"]}


@app.post("/api/collector/ozon-reference-page")
def collector_ozon_reference(payload: dict[str, Any]) -> dict[str, Any]:
    """Edge 插件采集 Ozon 参考页：把页面数据存到 references/，返回 task_id 供操作台打开。

    这是只读采集（不调 Ozon 写接口），数据用于 AI 商品卡生成时参考竞品文案/图片。
    """
    import uuid as _uuid

    source_url = str(payload.get("source_url") or "").strip()
    if not source_url:
        raise HTTPException(status_code=422, detail="缺少 source_url")
    ref_dir = Path(__file__).resolve().parent / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"ref-{_uuid.uuid4().hex[:12]}"
    ref_file = ref_dir / f"{task_id}.json"
    ref_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "status": "waiting_ai_design",
        "task": {"task_id": task_id, "source_url": source_url, "path": str(ref_file)},
        "title": payload.get("title") or payload.get("title_ru") or "",
    }


# --- Edge 插件「打开共享工作台」的入口路由：跳到单文件操作台 ---
@app.get("/1688-collection", include_in_schema=False)
def redirect_1688_collection(product_id: str | None = None, task_id: str | None = None) -> Any:
    params = []
    if product_id:
        params.append(f"product_id={product_id}")
    if task_id:
        params.append(f"task_id={task_id}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/{suffix}", status_code=303)


@app.get("/ozon-reference", include_in_schema=False)
def redirect_ozon_reference(product_id: str | None = None, task_id: str | None = None) -> Any:
    params = ["view=ozon-reference"]
    if product_id:
        params.append(f"product_id={product_id}")
    if task_id:
        params.append(f"task_id={task_id}")
    return RedirectResponse(url=f"/?{'&'.join(params)}", status_code=303)


@app.get("/command-center", include_in_schema=False)
def redirect_command_center(task_center: str | None = None, product_id: str | None = None) -> Any:
    params = []
    if task_center:
        params.append(f"task_center={task_center}")
    if product_id:
        params.append(f"product_id={product_id}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/{suffix}", status_code=303)


@app.post("/api/workbench/products/{product_id}/run")
def run_pipeline(product_id: str, request: RunPipelineRequest) -> dict[str, Any]:
    """跑流水线（默认干跑 + fake 模型层 + 占位生图，绝不会真提交）。"""
    from models import ModelError, load_provider
    from models.local_image import LocalPlaceholderGenerator
    from pipeline.runner import run_product
    from pipeline.upload import DryRunUploader, SimulatedUploader

    directory = _require_product(product_id)

    provider = None
    if (request.provider or "fake").lower() not in {"none", "off"}:
        try:
            provider = load_provider(request.provider)
        except ModelError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    image_generator = None
    if request.image_generator and request.image_generator.lower() not in {"none", "off"}:
        image_generator = LocalPlaceholderGenerator()

    uploader = None
    if request.uploader and request.uploader.lower() not in {"none", "off"}:
        uploader = SimulatedUploader() if request.uploader.lower().startswith("sim") else DryRunUploader()

    try:
        report = run_product(
            directory,
            until=request.until,
            dry_run=not request.execute_upload,
            step_budget=request.step_budget,
            provider=provider,
            uploader=uploader,
            image_generator=image_generator,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True, "report": report}


# ------------------------------------------- 选品清单 / 采集清单（M1 ↔ M2 的桥）


class SourcingPlanRequest(BaseModel):
    category_id: str | None = None
    type_id: str | None = None
    top: int = Field(20, ge=1, le=500)
    statuses: list[str] = Field(default_factory=lambda: ["qualified", "in_library"])
    min_score: float | None = None
    translate: bool = Field(False, description="用模型给中文找货词（否则用 Seerfar 中文类目名）")
    provider: str | None = None


class CollectionPlanRequest(BaseModel):
    top: int | None = Field(None, ge=1, le=500)
    only: list[str] = Field(default_factory=list)
    from_library: bool = Field(False, description="True 时直接从关键词库重建，忽略现有选品清单")
    category_id: str | None = None
    type_id: str | None = None


class MarkCollectedRequest(BaseModel):
    product_id: str
    keyword: str = Field(..., min_length=2)


def _lab_root() -> Path:
    """清单类产物的落点：默认与关键词库同级（这样 output/ 被 .gitignore 覆盖）。"""
    return Path(LIBRARY_ROOT).parent if str(LIBRARY_ROOT) not in {"", "."} else Path(".")


@app.post("/api/workbench/sourcing-plan")
def workbench_sourcing_plan(request: SourcingPlanRequest) -> dict[str, Any]:
    """生成选品清单（Ozon 复核 + 1688 找货链接），并写到 output/。"""
    from collector.sourcing import build_sourcing_plan, write_plan
    from models import ModelError, load_provider

    provider = None
    if request.translate:
        try:
            provider = load_provider(request.provider)
        except ModelError as error:
            raise HTTPException(status_code=422, detail=f"模型层不可用：{error}") from error

    plan = build_sourcing_plan(
        LIBRARY_ROOT,
        category_id=request.category_id,
        type_id=request.type_id,
        top_n=request.top,
        statuses=tuple(request.statuses),
        min_score=request.min_score,
        provider=provider,
        translate=request.translate,
    )
    written = write_plan(_lab_root(), plan, limit=50)
    return {"ok": True, "plan": plan, "written": written}


@app.get("/api/workbench/sourcing-plan")
def workbench_sourcing_plan_read() -> dict[str, Any]:
    """读回已生成的选品清单（界面刷新用）。"""
    path = _lab_root() / "output" / "sourcing-plan.json"
    payload = _read_json_file(path)
    if not payload:
        raise HTTPException(status_code=404, detail=f"还没有选品清单：先 POST /api/workbench/sourcing-plan（{path}）")
    return {"ok": True, "plan": payload, "path": str(path)}


@app.post("/api/workbench/collection-plan")
def workbench_collection_plan(request: CollectionPlanRequest) -> dict[str, Any]:
    """选品清单 → 采集清单（带"哪些词已采集"的状态跟踪），并写到 output/。"""
    from collector.collection_plan import build_collection_plan, write_collection_plan

    rows: list[dict[str, Any]] = []
    if request.from_library:
        from collector.sourcing import build_sourcing_plan

        rows = list(
            build_sourcing_plan(
                LIBRARY_ROOT,
                category_id=request.category_id,
                type_id=request.type_id,
                top_n=0,
            ).get("rows")
            or []
        )
    else:
        payload = _read_json_file(_lab_root() / "output" / "sourcing-plan.json")
        rows = list(payload.get("rows") or [])
        if not rows:
            raise HTTPException(
                status_code=404,
                detail="还没有选品清单：先 POST /api/workbench/sourcing-plan，或用 from_library=true 从库重建",
            )

    plan = build_collection_plan(
        rows, products_root=PRODUCTS_ROOT, top=request.top, only=request.only
    )
    written = write_collection_plan(_lab_root(), plan)
    return {"ok": True, "plan": plan, "written": written}


@app.get("/api/workbench/collection-plan")
def workbench_collection_plan_read() -> dict[str, Any]:
    path = _lab_root() / "output" / "collection-plan.json"
    payload = _read_json_file(path)
    if not payload:
        raise HTTPException(status_code=404, detail=f"还没有采集清单：先 POST /api/workbench/collection-plan（{path}）")
    return {"ok": True, "plan": payload, "path": str(path)}


@app.post("/api/workbench/collection-plan/mark")
def workbench_mark_collected(request: MarkCollectedRequest) -> dict[str, Any]:
    """把关键词补记到已有商品（等价于 CLI 的 --mark）。"""
    from collector.collection_plan import mark_keyword

    directory = _require_product(request.product_id)
    try:
        result = mark_keyword(directory, request.keyword)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **result}


@app.get("/api/workbench/keyword-products")
def workbench_keyword_products() -> dict[str, Any]:
    """关键词 → 商品 的汇总（回答"这个词下有几个商品、走到哪一步"）。"""
    from pipeline.doctor import product_keywords
    from pipeline.status import load_status, normalize

    index: dict[str, list[dict[str, Any]]] = {}
    for product_id in _list_product_ids():
        directory = PRODUCTS_ROOT / product_id
        status = normalize(load_status(directory))
        for text in product_keywords(directory):
            index.setdefault(text, []).append(
                {
                    "product_id": product_id,
                    "status": status.get("status"),
                    "current_step": status.get("current_step"),
                }
            )
    return {"ok": True, "count": len(index), "keywords": index}


# ----------------------------------------------------------------- 操作台（网页）

WEB_CONSOLE = Path(__file__).resolve().parent / "web" / "console.html"
WEB_CONSOLE_V2 = Path(__file__).resolve().parent / "web" / "research-workbench.html"


class StoreActionRequest(BaseModel):
    """操作台里的店铺参数（真提交还需要 confirm）。"""

    store: str | None = None
    confirm: str | None = None


def _store_for(directory: Path, requested: str | None) -> str:
    """没指定店铺时，用商品台账里第一个目标店铺，再退回注册表里第一个已启用的。"""
    if requested:
        return str(requested)
    bound = _read_json_file(directory / "input/category-selection.json").get("shop_id")
    if bound:
        return str(bound)
    from pipeline.status import load_status, normalize

    targets = normalize(load_status(directory)).get("target_store_ids") or []
    if targets:
        return str(targets[0])
    from pipeline.stores import enabled_shop_ids, ensure_registry

    registry = ensure_registry(None)
    enabled = enabled_shop_ids(registry)
    if registry.get("default_read_shop") in enabled:
        return str(registry["default_read_shop"])
    if enabled:
        return str(enabled[0])
    raise HTTPException(status_code=400, detail="没有可用店铺：先在 config/shops.json 里启用一个（pipeline.stores --enable <id>）")


@app.get("/", include_in_schema=False)
def workbench_console() -> Any:
    """研究到上架的决策工作台；旧的专家台保留在 /advanced。"""
    from fastapi.responses import HTMLResponse

    if not WEB_CONSOLE_V2.is_file():
        raise HTTPException(status_code=404, detail=f"缺少页面文件：{WEB_CONSOLE_V2}")
    return HTMLResponse(WEB_CONSOLE_V2.read_text(encoding="utf-8"))


@app.get("/advanced", include_in_schema=False)
def advanced_console() -> Any:
    return HTMLResponse(WEB_CONSOLE.read_text(encoding="utf-8"))


@app.get("/api/workbench/stores")
def workbench_stores(request: Request, response: Response) -> dict[str, Any]:
    """店铺清单（**不含密钥**，只说 enabled / 凭据是否就绪）。"""
    from pipeline.stores import ensure_registry, shop_summary

    shops = shop_summary(ensure_registry(None))
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True, "count": len(shops), "shops": shops,
            "authorization_context": _shop_authorization_context(request)}


class ShopAuthorizationRequest(BaseModel):
    shop_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    display_name: str = Field(min_length=1, max_length=100)
    client_id: SecretStr
    api_key: SecretStr
    default_currency_code: Literal["CNY", "RUB", "USD", "EUR"] = "CNY"


class ShopSettingsRequest(BaseModel):
    enabled: bool | None = None
    make_default: bool | None = None


@app.post("/api/workbench/stores/authorize")
def authorize_workbench_shop(request: Request, payload: ShopAuthorizationRequest,
                             response: Response) -> dict[str, Any]:
    _require_shop_admin(request)
    from pipeline.shop_authorization import authorize_shop

    try:
        shop = authorize_shop(shop_id=payload.shop_id, display_name=payload.display_name,
                              client_id=payload.client_id.get_secret_value(),
                              api_key=payload.api_key.get_secret_value(),
                              default_currency_code=payload.default_currency_code)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    from pipeline.category_form import invalidate_shop_cache

    invalidate_shop_cache(MARKET_DB_PATH.parent, payload.shop_id)
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True, "shop": shop, "api_writes_performed": False}


@app.post("/api/workbench/stores/{shop_id}/test")
def test_workbench_shop(shop_id: str, request: Request, response: Response) -> dict[str, Any]:
    _require_shop_admin(request)
    from pipeline.shop_authorization import test_shop_connection

    try:
        shop = test_shop_connection(shop_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True, "shop": shop, "api_writes_performed": False}


@app.put("/api/workbench/stores/{shop_id}/settings")
def settings_workbench_shop(shop_id: str, payload: ShopSettingsRequest,
                            request: Request, response: Response) -> dict[str, Any]:
    _require_shop_admin(request)
    from pipeline.shop_authorization import set_default_shop, set_shop_enabled, list_authorized_shops

    try:
        if payload.enabled is not None:
            set_shop_enabled(shop_id, payload.enabled)
        if payload.make_default:
            set_default_shop(shop_id)
        shops = list_authorized_shops()
        shop = next((item for item in shops if item["id"] == shop_id), None)
        if shop is None:
            raise ValueError("店铺不存在")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True, "shop": shop, "api_writes_performed": False}


@app.post("/api/workbench/products/{product_id}/publish-images")
def publish_images(product_id: str, dry_run: bool = False) -> dict[str, Any]:
    """把图片发布到对象存储并写 output/image-public-urls.json（Ozon 要能匿名抓到）。"""
    from pipeline.oss_cos import _storage_from_env

    directory = _require_product(product_id)
    storage = _storage_from_env(dry_run=dry_run)
    try:
        summary = storage.publish_product(directory, write_urls=not dry_run)
    except Exception as error:  # noqa: BLE001 - 存储配置/网络问题都如实返回
        raise HTTPException(status_code=422, detail=f"发布失败：{error}") from error
    return {"ok": True, "dry_run": dry_run, "summary": summary}


@app.post("/api/workbench/products/{product_id}/preflight")
def preflight_product(product_id: str, request: StoreActionRequest | None = None) -> dict[str, Any]:
    """提交前预检（只读：店铺/凭据 + production 载荷 + 图片匿名可达性 + 币种）。"""
    from pipeline.preflight import preflight

    directory = _require_product(product_id)
    return {"ok": True, "report": preflight(directory, shop=_store_for(directory, request.store if request else None))}


@app.post("/api/workbench/products/{product_id}/verify")
def verify_product(product_id: str, request: StoreActionRequest | None = None) -> dict[str, Any]:
    """提交后核对（只读）：读回 Ozon 上的 SKU / 属性 / 图片 / 变体合并。"""
    from pipeline.ozon_verify import verify_submitted

    directory = _require_product(product_id)
    return {"ok": True, "report": verify_submitted(directory, store_id=_store_for(directory, request.store if request else None))}


@app.post("/api/workbench/products/{product_id}/submit")
def submit_product(product_id: str, request: StoreActionRequest) -> dict[str, Any]:
    """**真实提交**（写 Ozon）：必须显式 confirm="SUBMIT"，并在 production 模式下执行。

    这一路会：跑完剩余步骤（缺什么补什么）→ 发布图片（若已配置发布器）→ 构建 production 载荷 → 提交 → 记账。
    """
    if str(request.confirm or "").strip().upper() != "SUBMIT":
        raise HTTPException(status_code=400, detail='真提交需要 {"confirm": "SUBMIT"}（这是写操作）')

    from models import ModelError, load_provider
    from pipeline.launch import launch_product
    from pipeline.ozon_write import OzonWriteUploader

    directory = _require_product(product_id)
    if (directory / "input/guided-workflow.json").is_file():
        from workbench_listing_api import run_service
        from pipeline.listing_draft import submit_listing
        store = _store_for(directory, request.store)
        return {"ok": True, "store": store,
                "report": run_service(submit_listing, directory, shop=store)}
    if research_sessions.session_for_product(MARKET_DB_PATH, product_id):
        raise HTTPException(status_code=409, detail="选词批次商品须走逐项审核与批次自动发布，不可用旧入口绕过审核")
    store = _store_for(directory, request.store)

    provider = None
    try:
        provider = load_provider(os.environ.get("WORKBENCH_WEB_PROVIDER", "ark"))
    except ModelError:
        provider = None

    image_generator = None
    if os.environ.get("ARK_IMAGE_MODEL"):
        try:
            from models.doubao_image import DoubaoImageGenerator

            image_generator = DoubaoImageGenerator.from_env()
        except ModelError:
            image_generator = None

    try:
        report = launch_product(
            directory,
            provider=provider,
            image_generator=image_generator,
            uploader=OzonWriteUploader(),
            execute_upload=True,
            store_ids=[store],
        )
    except Exception as error:  # noqa: BLE001 - 真提交失败要如实回给界面
        raise HTTPException(status_code=422, detail=f"提交失败：{error}") from error
    return {"ok": True, "store": store, "report": report}

@app.get("/api/workbench/products/{product_id}/summary")
def product_summary(product_id: str) -> dict[str, Any]:
    """商品要点（只读）：文案、属性、价格、图片、质检、阻断项 —— 给操作台"看要点"用。"""
    directory = _require_product(product_id)
    output = directory / "output"

    copy = _read_json_file(output / "copy-ru.json")
    bundle = copy.get("copy_bundle") if isinstance(copy.get("copy_bundle"), Mapping) else {}
    pricing = _read_json_file(output / "pricing-result.json")
    attributes = _read_json_file(output / "ozon-attributes-final.json")
    urls = _read_json_file(output / "image-public-urls.json")
    qc = _read_json_file(output / "image-qc-report.json")
    plan = _read_json_file(output / "image-plan.json")
    category = _read_json_file(output / "ozon-category.json")

    price_rows = {str(item.get("sku_id")): item for item in (pricing.get("skus") or []) if isinstance(item, Mapping)}
    variants = []
    for item in attributes.get("attributes_by_sku", {}) or {}:
        row = price_rows.get(str(item)) or {}
        variants.append(
            {
                "sku_id": str(item),
                "color": (attributes.get("attributes_by_sku", {}).get(item) or [{}])[0].get("value")
                if attributes.get("attributes_by_sku", {}).get(item)
                else None,
                "price_cny": row.get("selling_price_cny"),
                "price_rub": row.get("selling_price_rub"),
            }
        )

    image_urls = urls.get("urls") if isinstance(urls.get("urls"), Mapping) else {}
    return {
        "ok": True,
        "product_id": product_id,
        "category": category,
        # 文案产物：copy_bundle 里放的是"套件"，标题/简介/标签也可能在顶层（真机实测两种都在）
        "title_ru": copy.get("title_ru") or bundle.get("title_ru"),
        "short_title_ru": copy.get("short_title_ru") or bundle.get("short_title_ru"),
        "description_ru": copy.get("description_ru") or bundle.get("description_ru") or "",
        "description_sections": copy.get("description_sections") or bundle.get("description_sections") or {},
        "hashtags": copy.get("hashtags") or copy.get("hashtags_ru") or bundle.get("hashtags") or [],
        "primary_keywords": copy.get("primary_keywords")
        or bundle.get("primary_keywords")
        or copy.get("keywords_ru")
        or [],
        "attributes": attributes.get("common_attributes") or [],
        "required_summary": attributes.get("required_summary") or {},
        "variants": variants,
        "images": image_urls,
        "planned_slots": [
            str(item.get("slot"))
            for item in ((plan.get("main_images") or []) + (plan.get("detail_images") or []))
            if isinstance(item, Mapping)
        ],
        "image_qc": {"decision": qc.get("decision"), "score": qc.get("score"), "blocking": qc.get("blocking") or []},
        "blockers": _blockers_for(directory),
    }

def _blockers_for(directory: Path) -> list[str]:
    """复用 doctor 的商品级诊断，给操作台显示"还差什么"。失败时如实返回原因，不吞掉。"""
    try:
        from pipeline.doctor import diagnose_product
        from pipeline.stores import enabled_shop_ids, ensure_registry

        return list(diagnose_product(directory, enabled_store_ids=enabled_shop_ids(ensure_registry(None))).get("blockers") or [])
    except Exception as error:  # noqa: BLE001
        return [f"阻断项检查失败：{type(error).__name__}: {error}"]

@app.get("/api/workbench/steps")
def workbench_steps() -> dict[str, Any]:
    """工序顺序与中文名（**单一来源**：pipeline.steps，页面不写死）。"""
    from pipeline.steps import PIPELINE_STEPS, STEP_LABELS_ZH

    steps = [{"step": step, "label": STEP_LABELS_ZH.get(step, step)} for step in PIPELINE_STEPS]
    return {"ok": True, "count": len(steps), "steps": steps}


class GuidedApprovalRequest(BaseModel):
    section: str = Field(pattern="^(grouping|copy|image_plan|images|fields)$")


class GuidedCopyEditRequest(BaseModel):
    title_ru: str = Field(min_length=1, max_length=255)
    description_ru: str = Field(min_length=1, max_length=6000)


class GuidedImageSlotRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    reference_ids: list[str] = Field(min_length=1, max_length=3)


class GuidedImageGenerateRequest(BaseModel):
    slot: str = Field(min_length=1, max_length=100)


class HumanFactsRequest(BaseModel):
    material: str | None = None
    package_quantity: int | None = Field(default=None, gt=0)
    certifications: list[str] = Field(default_factory=list)


class HumanAttributesRequest(BaseModel):
    attributes: dict[str, str] = Field(default_factory=dict)


@app.put("/api/workbench/products/{product_id}/guided/facts")
@_locked_product_mutation
def guided_confirm_facts(product_id: str, request: HumanFactsRequest) -> dict[str, Any]:
    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    target = directory / "input" / "human-confirmations.json"
    current = _read_json_file(target)
    current.update({key: value for key, value in request.model_dump().items() if value not in (None, "", [])})
    target.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from pipeline.guided_review import invalidate_from

    invalidate_from(directory, "product_analysis")
    return {"ok": True, "confirmations": current,
            "next": "请重新运行豆包分析；未确认的主张不会自动写入商品卡"}


@app.put("/api/workbench/products/{product_id}/guided/attributes")
def guided_confirm_attributes(product_id: str, request: HumanAttributesRequest) -> dict[str, Any]:
    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    selection = _read_json_file(directory / "input/category-selection.json")
    if not selection.get("category_id") or not selection.get("type_id"):
        raise HTTPException(status_code=409, detail="先确认 Ozon 官方类目并读取填写表单")
    # Keep the legacy route, but apply the same official dictionary/type gates.
    result = save_workbench_listing_form(product_id, ListingFormRequest(
        shop=selection.get("shop_id"), category_id=int(selection["category_id"]),
        type_id=int(selection["type_id"]),
        attributes={key: [{"value": value}] for key, value in request.attributes.items()},
        per_sku_attributes=_read_json_file(directory / "input/human-confirmations.json").get("sku_attributes") or {},
    ))
    return {"ok": True, "attributes": result["compiled"]}


class GuidedGroupingRequest(BaseModel):
    strategy: str = Field(pattern="^(suggested|separate_cards)$")


@app.put("/api/workbench/products/{product_id}/guided/grouping")
@_locked_product_mutation
def guided_choose_grouping(product_id: str, request: GuidedGroupingRequest) -> dict[str, Any]:
    from contracts import validate_contract
    from pipeline.sku_selection import active_skus

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    path = directory / "output" / "platform-grouping-result.json"
    result = _read_json_file(path)
    if not result:
        raise HTTPException(status_code=409, detail="先运行准备流程，取得 Ozon 变体规则")
    if request.strategy == "separate_cards":
        source = _read_json_file(directory / "input" / "source.json")
        count = len(active_skus(directory, source.get("skus") or []))
        result.update(platform_card_count=count, platform_can_merge=False,
                      upload_strategy="separate_cards", reason="运营确认逐 SKU 拆卡，不使用变体合并")
    elif result.get("upload_strategy") == "rule_required":
        raise HTTPException(status_code=422, detail="Ozon 规则尚不允许确认合并；可选择逐 SKU 拆卡")
    errors = validate_contract("platform-grouping-result", result)
    if errors:
        raise HTTPException(status_code=422, detail="SKU 分组无效：" + "；".join(errors[:3]))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if (directory / "input/guided-workflow.json").is_file():
        from pipeline.listing_draft import grouping_scope
        from pipeline.context import write_json
        write_json(directory / "input/listing-grouping-choice.json", {
            "strategy": request.strategy, "scope": grouping_scope(directory)})
    return {"ok": True, "grouping": result}


@app.post("/api/workbench/products/{product_id}/guided/prepare")
def guided_prepare(product_id: str, request: StoreActionRequest) -> dict[str, Any]:
    """Prepare Ark copy, real Ozon attributes and image plan; never write Ozon or generate paid images."""
    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    store = _store_for(directory, request.store)
    bound = _read_json_file(directory / "input/category-selection.json").get("shop_id")
    if bound and bound != store:
        raise HTTPException(status_code=409, detail="目标店铺与类目确认不一致，请为该店铺重新选择官方类目")
    if (directory / "input/guided-workflow.json").is_file():
        from workbench_listing_api import run_service
        from pipeline.listing_draft import prepare_listing_card
        return {"ok": True, "report": run_service(prepare_listing_card, directory, shop=store)}
    return workbench_launch(product_id, LaunchRequest(provider="ark", image_generator="none",
                                                       uploader="none", oss="none", stores=[store]))


@app.get("/api/workbench/products/{product_id}/guided")
def guided_product(product_id: str) -> dict[str, Any]:
    from pipeline.guided_review import status as review_status

    directory = _require_product(product_id)
    return {"ok": True, "product_id": product_id, "review": review_status(directory),
            "workflow": __import__("pipeline.guided_workflow", fromlist=["workflow_status"]).workflow_status(directory),
            "video_library": __import__("pipeline.source_videos", fromlist=["list_source_videos"]).list_source_videos(directory),
            "media_selection": _read_json_file(directory / "input/listing-media.json"),
            "source": {key: value for key, value in _read_json_file(directory / "input" / "source.json").items() if key != "videos"},
            "selected_keywords": _read_json_file(directory / "input" / "selected-keywords.json"),
            "analysis": _read_json_file(directory / "output" / "product-analysis.json"),
            "copy": _read_json_file(directory / "output" / "copy-ru.json"),
            "image_plan": _read_json_file(directory / "output" / "image-plan.json"),
            "generated_image_paths": [path.relative_to(directory).as_posix()
                for path in (directory / "output/generated-images").rglob("*")
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                and path.resolve().is_relative_to(directory.resolve())],
            "image_qc": _read_json_file(directory / "output" / "image-qc-report.json"),
            "grouping": _read_json_file(directory / "output" / "platform-grouping-result.json"),
            "attributes": _read_json_file(directory / "output" / "ozon-attributes-final.json"),
            "category_attributes": _read_json_file(directory / "output" / "ozon-category-attributes.json"),
            "category_selection": _read_json_file(directory / "input" / "category-selection.json"),
            "human_confirmations": _read_json_file(directory / "input" / "human-confirmations.json"),
            "measurements": _read_json_file(directory / "input" / "workbench-sku-overrides.json"),
            "manual_prices": _read_json_file(directory / "input" / "manual-prices.json")}


@app.put("/api/workbench/products/{product_id}/guided/copy")
@_locked_product_mutation
def guided_edit_copy(product_id: str, request: GuidedCopyEditRequest) -> dict[str, Any]:
    from pipeline.guided_review import update_copy

    try:
        directory = _require_product(product_id)
        _require_pre_submission_edit(directory)
        result = update_copy(directory, **request.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "copy": result}


@app.put("/api/workbench/products/{product_id}/guided/image-plan/{slot}")
@_locked_product_mutation
def guided_edit_image_slot(product_id: str, slot: str, request: GuidedImageSlotRequest) -> dict[str, Any]:
    from pipeline.guided_review import update_plan_slot

    try:
        directory = _require_product(product_id)
        _require_pre_submission_edit(directory)
        plan = update_plan_slot(directory, slot=slot, **request.model_dump())
        if (directory / "input/guided-workflow.json").is_file():
            from pipeline.guided_workflow import refresh_plan_metadata
            refresh_plan_metadata(directory)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "image_plan": plan}


@app.post("/api/workbench/products/{product_id}/guided/approve")
@_locked_product_mutation
def guided_approve(product_id: str, request: GuidedApprovalRequest,
                   background_tasks: BackgroundTasks) -> dict[str, Any]:
    from pipeline.guided_review import approve

    try:
        review = approve(_require_product(product_id), request.section)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    session_id = research_sessions.session_for_product(MARKET_DB_PATH, product_id)
    if session_id and review["ready_to_preflight"] and not (_require_product(product_id) / "input/guided-workflow.json").is_file():
        session = research_sessions.get_session(MARKET_DB_PATH, session_id)
        if session["auto_publish_enabled"] and os.environ.get("WORKBENCH_AUTO_PUBLISH_ARMED") == "1":
            background_tasks.add_task(_process_ready_background, session_id)
    return {"ok": True, "review": review}


@app.post("/api/workbench/products/{product_id}/guided/generate-image")
@_locked_product_mutation
def guided_generate_image(product_id: str, request: GuidedImageGenerateRequest) -> dict[str, Any]:
    """One paid Ark image request at a time, after plan approval; supports one-slot redo."""
    from contracts import validate_contract
    from models import ImageRequest, ModelError
    from models.doubao_image import DoubaoImageGenerator
    from pipeline.guided_review import slot_fingerprint, status as review_status
    from pipeline.image_qc import run_image_qc

    directory = _require_product(product_id)
    _require_pre_submission_edit(directory)
    if (directory / "input/guided-workflow.json").is_file():
        from pipeline.guided_workflow import workflow_status
        if workflow_status(directory)["plan"]["status"] != "ready":
            raise HTTPException(status_code=409, detail="图片规划已过期，请先按最新规格和文案重新规划")
    if not review_status(directory)["sections"]["image_plan"]["approved"]:
        raise HTTPException(status_code=409, detail="先确认整套图片规划和参考图，再生成图片")
    plan = _read_json_file(directory / "output" / "image-plan.json")
    slots = {str(item.get("slot")): item for item in
             [*(plan.get("main_images") or []), *(plan.get("detail_images") or [])]
             if isinstance(item, Mapping)}
    if request.slot not in slots:
        raise HTTPException(status_code=422, detail="图位不在已确认的图片计划中")
    try:
        generator = DoubaoImageGenerator.from_env(slot_filter=[request.slot])
        result = generator.generate(ImageRequest(product_id=product_id, product_dir=directory,
                                                 source=_read_json_file(directory / "input" / "source.json")))
    except (ModelError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"豆包生图失败：{error}") from error
    report_path = directory / "output" / "image-generation-report.json"
    report = _read_json_file(report_path)
    files = {str(item.get("slot")): item for item in report.get("files") or []
             if isinstance(item, Mapping) and item.get("generator") == "doubao"}
    for item in result.get("generated") or []:
        slot_name = str(item["slot"])
        if slot_name not in slots or item.get("path") != slots[slot_name].get("output_path"):
            raise HTTPException(status_code=422, detail="豆包返回图位与当前图片计划不一致")
        files[slot_name] = {"slot": slot_name, "path": item["path"], "bytes": item["bytes"],
                            "generator": "doubao", "slot_fingerprint": slot_fingerprint(slots[slot_name])}
    report.update(schema_version="1.0.0", product_id=product_id, generator="doubao", final_images=True,
                  planned_slots=len(slots), generated_slots=len(files), files=list(files.values()), note=result.get("note"))
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    qc = run_image_qc(directory, generator_name="doubao", produces_final_images=True)
    failures = validate_contract("image-qc-report", qc)
    if failures:
        raise HTTPException(status_code=422, detail="图片质检结果不符合契约：" + "；".join(failures[:3]))
    (directory / "output" / "image-qc-report.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"ok": True, "generated": result.get("generated"), "skipped": result.get("skipped"), "qc": qc}


@app.get("/api/workbench/products/{product_id}/media/{relative_path:path}")
def guided_media(product_id: str, relative_path: str) -> Any:
    from fastapi.responses import FileResponse

    directory = _require_product(product_id).resolve()
    permitted = [directory / "input" / name for name in ("main-images", "sku-images", "detail-images")]
    permitted.append(directory / "output" / "generated-images")
    target = (directory / relative_path).resolve()
    if not target.is_file() or target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=404, detail="图片不存在")
    if not any(target.is_relative_to(root.resolve()) for root in permitted):
        raise HTTPException(status_code=403, detail="图片路径不允许访问")
    return FileResponse(target)


# Stepwise flow is isolated from legacy CLI orchestration and advanced console.
from workbench_listing_api import router as listing_flow_router
app.include_router(listing_flow_router)


@app.get("/assets/listing-flow.js", include_in_schema=False)
def listing_flow_script():
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).resolve().parent / "web/listing-flow.js", media_type="text/javascript")
