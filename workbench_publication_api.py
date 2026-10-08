"""Read-only publication caches and explicitly confirmed inventory actions."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, StrictInt

from workbench_listing_api import directory_for, run_service
from pipeline import listing_publications as service

router = APIRouter(prefix="/api/workbench")


def _known_shop(shop: str) -> str:
    from pipeline.stores import load_registry, list_shops
    if not any(str(row.get("id")) == shop and row.get("enabled") for row in list_shops(load_registry(None))):
        raise HTTPException(422, "请选择已授权并启用的店铺")
    return shop


class WarehouseRequest(BaseModel):
    shop: str = Field(min_length=1, max_length=100)


class ConfigRequest(WarehouseRequest):
    warehouse_id: str | StrictInt
    stock: StrictInt = Field(default=100, ge=0, le=1_000_000)
    stock_by_sku: dict[str, StrictInt] = Field(default_factory=dict, max_length=10)
    source_note: str = Field(default="", max_length=500)


class ContinueRequest(WarehouseRequest):
    confirm: str
    retry_unknown: StrictBool = False


@router.get("/warehouses")
def warehouses(shop: str):
    return run_service(service.read_warehouses, _known_shop(shop))


@router.post("/warehouses/refresh")
def refresh_warehouses(request: WarehouseRequest):
    return run_service(service.refresh_warehouses, _known_shop(request.shop))


@router.get("/products/{product_id}/publication-config")
def publication_config(product_id: str, shop: str):
    return {"ok": True, "config": run_service(service.read_config, directory_for(product_id, edit=False), shop=_known_shop(shop))}


@router.put("/products/{product_id}/publication-config")
def save_publication_config(product_id: str, request: ConfigRequest):
    fields = request.model_dump()
    fields["shop"] = _known_shop(request.shop)
    return {"ok": True, "config": run_service(service.save_config, directory_for(product_id), **fields)}


@router.get("/publications")
def publications(shop: str | None = None, q: str = Query(default="", max_length=100),
                 limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000)):
    return run_service(service.list_publications, shop=_known_shop(shop) if shop else None,
                       q=q, limit=limit, offset=offset)


@router.get("/products/{product_id}/listing-publications")
def product_publications(product_id: str, shop: str | None = None, q: str = Query(default="", max_length=100),
                         limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000)):
    directory = directory_for(product_id, edit=False)
    return run_service(service.list_publications, shop=_known_shop(shop) if shop else None,
                       q=q, limit=limit, offset=offset, product_id=directory.name, db_path=service.registry_path(directory))


@router.post("/products/{product_id}/publications/continue")
def continue_publication(product_id: str, request: ContinueRequest):
    fields = request.model_dump()
    fields["shop"] = _known_shop(request.shop)
    return run_service(service.continue_stocks, directory_for(product_id, edit=False), **fields)
