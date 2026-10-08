"""Read-only post-listing operations; advertising never shares Seller secrets."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

SHOP_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class ShopPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop: str = Field(pattern=SHOP_PATTERN)


class SyncPayload(ShopPayload):
    offer_id: str = Field(min_length=1, max_length=100)
    days: Literal[7, 30] = 7
    include_traffic: bool = True


class CatalogReadPayload(ShopPayload):
    query: str = Field(default="", max_length=100)
    limit: int = Field(default=50, ge=1, le=100)
    previous_page_token: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")


class CatalogAddPayload(ShopPayload):
    page_token: str = Field(pattern=r"^[a-f0-9-]{36}$")
    offer_ids: list[str] = Field(min_length=1, max_length=100)
    analyze: bool = False
    days: Literal[7, 30] = 7
    include_traffic: bool = True


class SchedulePayload(ShopPayload):
    enabled: bool
    days: Literal[7, 30] = 7
    interval_hours: int = Field(default=24, ge=6, le=168)
    include_traffic: bool = True


class AdvertisingPayload(ShopPayload):
    client_id: SecretStr
    client_secret: SecretStr


class ReportPayload(ShopPayload):
    campaigns: list[str] = Field(min_length=1, max_length=10)
    date_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


def register_operations_routes(
    app: FastAPI, *, runtime_root: Path | Callable[[], Path],
    seller_transport: Callable, shop_rows: Callable,
    credential_context: Callable, require_credentials: Callable,
    publication_rows: Callable | None = None, performance_factory: Callable | None = None,
    start_worker: bool = True,
) -> None:
    """All paths resolve at request time, avoiding real-data reads in test apps."""
    from pipeline import operations, operations_catalog
    from pipeline.performance_access import PerformanceAccess, PerformanceAccessError
    from pipeline.operations_worker import OperationsWorker

    # Keep factory-created test/embedded apps just as safe as the main app.
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.responses import JSONResponse
    previous_validation_handler = app.exception_handlers.get(RequestValidationError,
                                                               request_validation_exception_handler)

    async def validation_without_secrets(request, error):
        if request.url.path == "/api/operations/advertising/authorize":
            return JSONResponse(status_code=422, content={"detail": "广告授权表单格式不正确，请检查店铺 ID 和两个凭据字段"})
        return await previous_validation_handler(request, error)

    app.add_exception_handler(RequestValidationError, validation_without_secrets)

    def root():
        return Path(runtime_root() if callable(runtime_root) else runtime_root)

    def store():
        return operations.Store(root() / "operations.sqlite3")

    def performance():
        return (performance_factory or PerformanceAccess)(root() / "performance")

    def known_shop(shop, *, enabled=False):
        row = next((row for row in shop_rows() if row["id"] == shop), None)
        if row is None:
            raise HTTPException(404, "店铺不存在，请先添加店铺授权")
        if enabled and not row.get("enabled"):
            raise HTTPException(409, "店铺已停用，不能开启新的同步任务")
        return row

    def read_publications():
        if publication_rows is not None:
            return publication_rows()
        from pipeline.listing_publications import list_publications
        rows, offset = [], 0
        # Bound discovery; never silently omit larger ledgers.
        while offset < 100000:
            result = list_publications(limit=100, offset=offset,
                                       db_path=root() / "listing-publications.sqlite3")
            rows.extend(result["items"])
            if len(rows) >= result["total"]:
                return rows
            offset += 100
        raise HTTPException(409, "上架记录超过当前同步范围，请按店铺分批导入")

    def guard(request: Request, response: Response):
        origin = request.headers.get("origin")
        parsed = urlsplit(origin) if origin else None
        if request.headers.get("sec-fetch-site") == "cross-site" or (parsed and
                (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc)):
            raise HTTPException(403, "运营中心请求必须来自当前工作台页面")
        response.headers["Cache-Control"] = "private, no-store"

    from fastapi import Depends
    router = APIRouter(prefix="/api/operations", dependencies=[Depends(guard)])
    worker = OperationsWorker(store=store, seller_transport=seller_transport,
                              shop_enabled=lambda shop: bool(known_shop(shop).get("enabled")))
    # Exposed for isolated full-stack verification and graceful service shutdown.
    app.state.operations_worker = worker
    if start_worker:
        app.add_event_handler("startup", worker.start)
        app.add_event_handler("shutdown", worker.stop)

    def safe(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except operations_catalog.CatalogError as error:
            raise HTTPException(error.http_status or 422, str(error)) from None
        except PerformanceAccessError as error:
            status = error.http_status or 422
            if status >= 500:
                status = 502
            raise HTTPException(status, str(error)) from None
        except KeyError:
            raise HTTPException(404, "未找到对应店铺的商品或任务") from None
        except ValueError as error:
            # Only our service modules' documented, sanitized errors reach here.
            raise HTTPException(422, str(error)) from None

    @router.get("/config")
    def config(request: Request):
        return {"ok": True, "shops": [
            {"id": row["id"], "name": row.get("display_name") or row.get("name") or row["id"],
             "enabled": bool(row.get("enabled")), "credentials_ready": bool(row.get("credentials_ready")),
             "is_default": bool(row.get("is_default"))}
            for row in shop_rows()
        ], "credential_security": credential_context(request), "advertising_write_enabled": False,
            "automatic_card_updates_enabled": False,
            "access_model": "shared_workbench", "employee_rbac_available": False,
            "worker": {"running": bool(worker._thread and worker._thread.is_alive()), "last_error": worker.last_error},
            "message": "首版只读监测；尚未接入员工账号和权限，不会自动改卡或开启广告。"}

    @router.post("/discover")
    def discover():
        allowed = {row["id"] for row in shop_rows()}
        result = safe(store().discover, [row for row in read_publications() if row.get("shop") in allowed])
        return {"ok": True, **result}

    @router.get("/products")
    def products(shop: str | None = Query(default=None, pattern=SHOP_PATTERN),
                 q: str = Query(default="", max_length=100), limit: int = Query(default=30, ge=1, le=100),
                 offset: int = Query(default=0, ge=0, le=100000)):
        if shop:
            known_shop(shop)
        return {"ok": True, **safe(store().list_products, shop=shop, q=q, limit=limit, offset=offset)}

    @router.post("/catalog/read")
    def read_catalog(payload: CatalogReadPayload):
        row = known_shop(payload.shop, enabled=True)
        if not row.get("credentials_ready"):
            raise HTTPException(409, "店铺 Seller 授权缺失，请先在店铺授权中保存凭据")
        cache = store()
        arguments = {"last_id": "", "limit": payload.limit, "query": payload.query,
                     "visibility": "ALL", "seen_cursors": ()}
        if payload.previous_page_token:
            previous = safe(cache.catalog_page, payload.shop, payload.previous_page_token)
            if not previous.get("has_more") or not previous.get("next_cursor"):
                raise HTTPException(409, "此批商品已到最后一页，请重新读取首页")
            # Only the server-owned page determines the next official cursor and scope.
            arguments.update(last_id=previous["next_cursor"], limit=previous["limit"],
                             query=previous.get("query", ""), visibility=previous["visibility"],
                             seen_cursors=previous.get("seen_cursors", ()))
        try:
            transport = seller_transport(payload.shop)
        except Exception:
            raise HTTPException(409, "店铺 Seller 授权不可用，请重新验证授权") from None
        page = safe(operations_catalog.browse_catalog, payload.shop, transport, store=cache, **arguments)
        public = {key: page.get(key) for key in (
            "items", "page_token", "fetched_at", "expires_at", "has_more", "total",
            "total_source", "filtered_count", "warning_codes")}
        return {"ok": True, **public, "api_writes_performed": False,
                "visibility": "ALL", "message": "仅读取非归档商品；名称筛选只作用于当前页。"}

    @router.post("/catalog/add")
    def add_catalog(payload: CatalogAddPayload):
        known_shop(payload.shop, enabled=True)
        cache = store()
        result = safe(cache.import_catalog_rows, payload.shop, payload.page_token, payload.offer_ids)
        queued, deduplicated, queue_errors = 0, 0, []
        if payload.analyze:
            # An import succeeds independently of queue capacity. Never hide a partial enqueue.
            for item in result["items"]:
                try:
                    job = cache.enqueue(payload.shop, item["offer_id"], days=payload.days,
                                        include_traffic=payload.include_traffic)
                    if job.get("deduplicated"):
                        deduplicated += 1
                    else:
                        queued += 1
                except (ValueError, KeyError, sqlite3.Error):
                    queue_errors.append({"offer_id": item["offer_id"], "code": "queue_unavailable"})
            if queued or deduplicated:
                worker.wake()
        return {"ok": True, **result, "jobs_enqueued": queued, "queued": queued, "deduplicated": deduplicated,
                "queue_errors": queue_errors, "api_writes_performed": False,
                "message": "已加入只读商品运营记录；未重新上架或修改店铺商品。"}

    @router.get("/product")
    def product(shop: str = Query(pattern=SHOP_PATTERN), offer_id: str = Query(min_length=1, max_length=100)):
        known_shop(shop)
        return {"ok": True, **safe(store().detail, shop, offer_id)}

    @router.post("/sync")
    def sync(payload: SyncPayload):
        known_shop(payload.shop, enabled=True)
        job = safe(store().enqueue, payload.shop, payload.offer_id, days=payload.days,
                   include_traffic=payload.include_traffic)
        worker.wake()
        return {"ok": True, "job": job, "api_writes_performed": False}

    @router.get("/jobs")
    def jobs(shop: str | None = Query(default=None, pattern=SHOP_PATTERN),
             limit: int = Query(default=30, ge=1, le=100)):
        if shop:
            known_shop(shop)
        return {"ok": True, "items": safe(store().jobs, shop=shop, limit=limit)}

    @router.get("/schedule")
    def schedule(shop: str = Query(pattern=SHOP_PATTERN)):
        known_shop(shop)
        return {"ok": True, **safe(store().schedule, shop)}

    @router.put("/schedule")
    def save_schedule(payload: SchedulePayload):
        known_shop(payload.shop, enabled=payload.enabled)
        result = safe(store().save_schedule, payload.shop, enabled=payload.enabled,
                      days=payload.days, interval_hours=payload.interval_hours,
                      include_traffic=payload.include_traffic)
        worker.wake()
        return {"ok": True, **result}

    @router.get("/advertising/status")
    def advertising_status(shop: str = Query(pattern=SHOP_PATTERN)):
        known_shop(shop)
        result = safe(performance().public_status, shop)
        return {"ok": True, **result, "ready": result.get("configured", False),
                "status": result.get("connection_status", "not_configured")}

    @router.post("/advertising/authorize")
    def authorize(payload: AdvertisingPayload, request: Request):
        require_credentials(request)
        known_shop(payload.shop, enabled=True)
        result = safe(performance().authorize, payload.shop, payload.client_id.get_secret_value(),
                      payload.client_secret.get_secret_value())
        return {"ok": True, **result, "api_writes_performed": False}

    @router.delete("/advertising/authorize")
    def revoke(request: Request, shop: str = Query(pattern=SHOP_PATTERN)):
        require_credentials(request)
        known_shop(shop)
        safe(performance().remove, shop)
        return {"ok": True, "configured": False, "api_writes_performed": False}

    @router.get("/advertising/campaigns")
    def campaigns(shop: str = Query(pattern=SHOP_PATTERN), page: int = Query(default=1, ge=1, le=10000)):
        known_shop(shop, enabled=True)
        return {"ok": True, **safe(performance().list_campaigns, shop, page=page, page_size=50)}

    @router.get("/advertising/reports")
    def reports(shop: str = Query(pattern=SHOP_PATTERN), limit: int = Query(default=30, ge=1, le=100)):
        known_shop(shop)
        access = performance()
        if not safe(access.public_status, shop).get("configured"):
            return {"ok": True, "items": [], "configured": False}
        result = safe(access.list_reports, shop, limit=limit)
        return {"ok": True, **(result if isinstance(result, dict) else {"items": result})}

    @router.post("/advertising/reports")
    def create_report(payload: ReportPayload):
        known_shop(payload.shop, enabled=True)
        result = safe(performance().request_report, payload.shop, payload.campaigns,
                      payload.date_from, payload.date_to)
        return {"ok": True, **result, "status": result.get("state"), "api_writes_performed": False}

    @router.get("/advertising/reports/{report_id}")
    def report(report_id: str, shop: str = Query(pattern=SHOP_PATTERN)):
        known_shop(shop, enabled=True)
        access = performance()
        result = safe(access.poll_report, shop, report_id)
        if str(result.get("state")).upper() in {"OK", "READY", "COMPLETED"}:
            result.update(safe(access.download_report, shop, report_id))
        return {"ok": True, **result, "status": result.get("state")}

    app.include_router(router)
