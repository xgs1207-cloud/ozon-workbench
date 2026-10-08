"""Same-origin browser-private keyword library; not an employee login system."""
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from pipeline import keyword_library as library

COOKIE_NAME = "ozon_workbench_keyword_identity"
PREFIX = "/api/workbench/keyword-library"
IDENTITY_INFO = {
    "mode": "signed_browser_profile",
    "message": "当前浏览器专属关键词库，独立于 Seerfar。不同浏览器各自隔离；同一浏览器共用，不等同于公司员工账号登录。请勿清除本站身份 Cookie，以免失去当前库的访问身份。",
}


class KeywordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=2000)


def register_keyword_library_routes(app: FastAPI, *, runtime_root: Path | str | Callable[[], Path]) -> None:
    """Runtime provider may be callable so test/service configured paths stay live."""
    router = APIRouter(prefix=PREFIX)

    def database() -> Path:
        root = runtime_root() if callable(runtime_root) else runtime_root
        return Path(root) / "employee-keywords.sqlite3"

    def identity(request: Request, response: Response) -> tuple[Path, str]:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and
                (urlsplit(origin).scheme, urlsplit(origin).netloc) != (request.url.scheme, request.url.netloc)):
            raise HTTPException(403, "关键词库请求必须来自当前工作台页面")
        token = request.cookies.get(COOKIE_NAME)
        if request.method not in {"GET", "HEAD"} and not token:
            # Never write to a new owner that a late initial GET Set-Cookie
            # could immediately replace. The page must finish identity setup.
            raise HTTPException(428, "请先打开关键词库完成身份初始化，再保存关键词")
        path = database()
        try:
            owner, created = library.browser_identity(path, token)
        except library.InvalidIdentity as error:
            raise HTTPException(401, str(error)) from error
        if created:
            response.set_cookie(COOKIE_NAME, created, max_age=library.IDENTITY_TTL,
                                httponly=True, secure=request.url.scheme == "https", samesite="strict", path=PREFIX)
        response.headers["Cache-Control"] = "no-store"
        return path, owner

    def run(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except library.KeywordNotFound as error:
            raise HTTPException(404, str(error)) from error
        except library.KeywordConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("")
    def records(request: Request, response: Response, q: str = Query(default="", max_length=300),
                category: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0, le=10000),
                limit: int = Query(default=50, ge=1, le=200)):
        path, owner = identity(request, response)
        return {"ok": True, **run(library.list_records, path, owner, q=q, category=category, offset=offset, limit=limit),
                "identity": IDENTITY_INFO}

    @router.get("/categories")
    def categories(request: Request, response: Response):
        path, owner = identity(request, response)
        return {"ok": True, "items": run(library.list_categories, path, owner), "identity": IDENTITY_INFO}

    @router.post("")
    def create(request: Request, response: Response, payload: KeywordRequest):
        path, owner = identity(request, response)
        return {"ok": True, "item": run(library.save_record, path, owner, **payload.model_dump())}

    @router.get("/{record_id}")
    def read(record_id: str, request: Request, response: Response):
        path, owner = identity(request, response)
        return {"ok": True, "item": run(library.get_record, path, owner, record_id)}

    @router.put("/{record_id}")
    def update(record_id: str, request: Request, response: Response, payload: KeywordRequest):
        path, owner = identity(request, response)
        return {"ok": True, "item": run(library.save_record, path, owner, record_id=record_id, **payload.model_dump())}

    @router.delete("/{record_id}")
    def delete(record_id: str, request: Request, response: Response):
        path, owner = identity(request, response)
        run(library.delete_record, path, owner, record_id)
        return {"ok": True, "deleted": True}

    app.include_router(router)
