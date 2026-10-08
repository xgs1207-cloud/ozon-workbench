"""Safe unified shop-management form, with injectable isolated persistence."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

NO_STORE = {"Cache-Control": "private, no-store"}
PATH = "/api/workbench/shop-management"


class ShopManagementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["create", "update"]
    shop_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    default_currency_code: Literal["CNY", "RUB", "USD", "EUR"] = "CNY"
    seller_client_id: SecretStr | None = None
    seller_api_key: SecretStr | None = None
    advertising_client_id: SecretStr | None = None
    advertising_client_secret: SecretStr | None = None
    make_default: bool = False


def register_shop_management_routes(
    app: FastAPI, *, runtime_root: Path | Callable[[], Path], credential_context: Callable,
    require_credentials: Callable, invalidate_shop_cache: Callable | None = None,
    registry_path=None, vault_root=None, transport_factory: Callable | None = None,
    performance_factory: Callable | None = None,
) -> None:
    from pipeline.shop_management import ShopManagement, ShopManagementError
    from pipeline.shop_authorization import ShopAuthorizationError
    from pipeline.performance_access import PerformanceAccessError

    previous_handler = app.exception_handlers.get(RequestValidationError, request_validation_exception_handler)

    async def redact_validation(request, error):
        if request.url.path == PATH:
            return JSONResponse(status_code=422, headers=NO_STORE,
                                content={"detail": "店铺表单格式不正确，请检查新增/修改方式、店铺 ID、名称、币种和凭据字段"})
        return await previous_handler(request, error)

    app.add_exception_handler(RequestValidationError, redact_validation)

    def guard(request: Request, response: Response):
        origin = request.headers.get("origin")
        parsed = urlsplit(origin) if origin else None
        if request.headers.get("sec-fetch-site") == "cross-site" or (parsed and
                (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc)):
            raise HTTPException(403, "店铺管理请求必须来自当前工作台页面", headers=NO_STORE)
        response.headers.update(NO_STORE)

    def resolve(value):
        return value() if callable(value) else value

    def management():
        return ShopManagement(resolve(runtime_root), registry_path=resolve(registry_path),
                              vault_root=resolve(vault_root), transport_factory=transport_factory,
                              performance_factory=performance_factory,
                              invalidate_shop_cache=invalidate_shop_cache)

    def safe(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except (ShopManagementError, ShopAuthorizationError, PerformanceAccessError) as error:
            status = error.http_status or 422
            raise HTTPException(502 if status >= 500 else status, str(error), headers=NO_STORE) from None
        except Exception:
            raise HTTPException(503, "店铺管理服务暂时不可用，请检查服务器配置与保险库备份", headers=NO_STORE) from None

    router = APIRouter(dependencies=[Depends(guard)])

    @router.get(PATH)
    def shops(request: Request):
        return {"ok": True, "shops": safe(lambda: management().list_shops()),
                "authorization_context": credential_context(request)}

    @router.post(PATH)
    def save(payload: ShopManagementPayload, request: Request):
        try:
            require_credentials(request)
        except HTTPException as error:
            raise HTTPException(error.status_code, error.detail, headers=NO_STORE) from None
        fields = payload.model_dump()
        for key in ("seller_client_id", "seller_api_key", "advertising_client_id", "advertising_client_secret"):
            value = getattr(payload, key)
            fields[key] = value.get_secret_value() if value is not None else None
        return safe(lambda: management().save(**fields))

    app.include_router(router)
