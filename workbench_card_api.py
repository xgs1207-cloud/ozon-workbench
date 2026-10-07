"""Unified local listing document and explicit offer-number reservation routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from workbench_listing_api import directory_for, run_service

router = APIRouter(prefix="/api/workbench")


class PrefixRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100)
    shop: str = Field(min_length=1, max_length=100)
    prefix: str = Field(min_length=1, max_length=28)


class ReserveRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100)
    shop: str | None = Field(default=None, max_length=100)
    prefix: str | None = Field(default=None, min_length=1, max_length=28)


def _database():
    from pipeline.listing_offer_ids import registry_path
    return registry_path()


def _known_shop(shop: str) -> str:
    from pipeline.stores import load_registry, list_shops
    if not any(str(row["id"]) == shop and row.get("enabled") for row in list_shops(load_registry(None))):
        raise HTTPException(422, "请选择已授权并启用的店铺")
    return shop


@router.get("/offer-prefix")
def read_prefix(profile_id: str, shop: str):
    from pipeline.listing_offer_ids import read_profile
    return {"ok": True, "profile": run_service(read_profile, profile_id, _known_shop(shop), db_path=_database())}


@router.put("/offer-prefix")
def write_prefix(request: PrefixRequest):
    from pipeline.listing_offer_ids import save_profile
    return {"ok": True, "profile": run_service(save_profile, request.profile_id, _known_shop(request.shop), request.prefix, db_path=_database())}


@router.get("/products/{product_id}/listing-document")
def listing_document(product_id: str, shop: str | None = None):
    import api
    from pipeline.listing_document import read_listing_document
    directory = directory_for(product_id, edit=False)
    return {"ok": True, "document": run_service(read_listing_document, directory, shop=shop,
                                                cache_root=api.MARKET_DB_PATH.parent, offer_db_path=_database())}


@router.post("/products/{product_id}/offer-ids/reserve")
def reserve_offers(product_id: str, request: ReserveRequest):
    import api
    from pipeline.listing_offer_ids import reserve_offer_ids
    directory = directory_for(product_id)
    shop = _known_shop(api._store_for(directory, request.shop))
    return {"ok": True, "offer_ids": run_service(reserve_offer_ids, directory, shop, request.profile_id, request.prefix, db_path=_database())}
