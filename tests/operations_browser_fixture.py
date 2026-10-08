"""Owned, offline browser QA. All provider HTTP is replaced; no real credentials."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline import shop_authorization as authorization, stores as shop_registry
from pipeline.ozon_http import OzonHttpError
from pipeline.performance_access import PerformanceAccess, WireResponse
from workbench_operations_api import register_operations_routes
from workbench_shop_management_api import register_shop_management_routes

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="ozon-operations-qa-")
RUNTIME = Path(TEMP.name)
REGISTRY = RUNTIME / "config" / "shops.json"
VAULT = RUNTIME / "shop-vault"
app = FastAPI(title="离线运营中心验收")
SELLER_CALLS = []
rows = [{"shop": "qa-a", "offer_id": "xzj.jp.10.8.1", "product_id": "P000007", "source_sku_id": "red24",
         "source_note": "红色/内白 24cm/3.5L", "source_url": "https://detail.1688.com/offer/1053070588316.html",
         "ozon_product_id": "101", "import_status": "imported", "stock_status": "success", "warehouse_name": "离线验收仓库"},
        {"shop": "qa-b", "offer_id": "xzj.jp.10.8.1", "product_id": "P000008", "source_sku_id": "green",
         "source_note": "绿色，离线隔离验证", "ozon_product_id": "102", "import_status": "imported"}]


class SellerFixture:
    def __init__(self, shop):
        self.shop = shop
        selected = authorization._shop(authorization._read_registry(REGISTRY, VAULT), shop)
        client, _ = shop_registry.credential_values(selected)
        self.credentials = SimpleNamespace(client_id=client, shop_id=shop)

    @staticmethod
    def catalog(shop):
        if shop not in {"qa-a", "qa-b"}:
            return []
        items = [{"id": 101 if shop == "qa-a" else 102,
                  "sku": 9001 if shop == "qa-a" else 9002,
                  "offer_id": "xzj.jp.10.8.1",
                  "name": "离线验收红色内白珐琅锅（24cm/3.5L）" if shop == "qa-a" else "离线验收绿色商品（跨店同货号）"}]
        if shop == "qa-a":
            items.extend({"id": 300 + i, "sku": 9100 + i,
                          "offer_id": f"offline.qa-a.{i:03d}",
                          "name": f"离线已有商品样本 {i:03d}（无真实投放或货源）"}
                         for i in range(1, 51))
        return items

    def post(self, path, body):
        SELLER_CALLS.append((self.shop, path, dict(body)))
        catalog = self.catalog(self.shop)
        if path == "/v3/product/list":
            cursor = body.get("last_id", "")
            prefix = f"offline:{self.shop}:"
            if cursor and (not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit()):
                raise AssertionError("Fixture rejects an unknown shop cursor")
            offset = int(cursor[len(prefix):]) if cursor else 0
            limit = body.get("limit", 50)
            page = catalog[offset:offset + limit]
            next_offset = offset + len(page)
            return {"result": {"items": [{"product_id": item["id"], "sku": item["sku"],
                                          "offer_id": item["offer_id"], "archived": False} for item in page],
                               "last_id": prefix + str(next_offset) if next_offset < len(catalog) else "",
                               "total": len(catalog)}}
        if path == "/v3/product/info/list":
            requested_ids = {str(value) for value in body.get("product_id", [])}
            requested_offers = set(body.get("offer_id", []))
            selected = [item for item in catalog if str(item["id"]) in requested_ids or item["offer_id"] in requested_offers]
            return {"items": [{**item, "currency_code": "CNY", "price": "300", "is_archived": False,
                               "statuses": {"is_created": True, "status": "processed", "moderate_status": "approved"},
                               "stocks": {"has_stock": True}, "visibility_details": {"has_price": True},
                               "images": [], "primary_image": [], "errors": []} for item in selected]}
        if path == "/v1/analytics/data":
            from datetime import date, timedelta
            sku = str(next(row["value"] for row in body["filters"] if row["key"] == "sku"))
            if sku not in {str(item["sku"]) for item in catalog}:
                raise AssertionError("Fixture rejects unknown SKU analytics")
            end = date.fromisoformat(body["date_to"])
            daily = []
            for i in range(7):
                day = end - timedelta(days=6-i)
                values = {"revenue": 120*i, "ordered_units": i, "hits_view_search": 80+i*12,
                          "hits_view_pdp": 10+i*3, "hits_tocart_search": i, "hits_tocart_pdp": i,
                          "session_view_search": 50+i*9, "session_view_pdp": 8+i*2}
                daily.append({"dimensions": [{"id": sku}, {"id": str(day)}], "metrics": [values[k] for k in body["metrics"]]})
            return {"result": {"data": daily, "totals": [sum(x["metrics"][i] for x in daily) for i in range(len(body["metrics"]))]}}
        if path == "/v1/analytics/product-queries/details":
            sku = str(body["skus"][0])
            if sku not in {str(item["sku"]) for item in catalog}:
                raise AssertionError("Fixture rejects unknown SKU query analytics")
            return {"page_count": 1, "total": 2, "queries": [
                {"sku": int(sku), "query": "кастрюля чугунная", "unique_search_users": 200, "unique_view_users": 80,
                 "order_count": 3, "gmv": 1500, "currency": "RUB", "position": 16.5},
                {"sku": int(sku), "query": "кастрюля тыква", "unique_search_users": 50, "unique_view_users": None,
                 "order_count": 0, "gmv": 0, "currency": "RUB", "position": None}]}
        raise AssertionError("Fixture rejects any unplanned Seller endpoint")


class AuthorizationFixture:
    """Only /roles and category tree; never constructs a real Seller transport."""
    def __init__(self, credentials):
        self.credentials = credentials

    def post(self, path, body):
        if self.credentials.api_key == "offline-rejected-seller-key":
            raise OzonHttpError("Explicit offline authorization rejection", status=401)
        if path == "/v1/roles":
            return {"roles": [{"name": "离线验收只读权限", "methods": [
                "/v1/description-category/tree", "/v1/description-category/attribute",
                "/v1/description-category/attribute/values", "/v3/product/list",
                "/v3/product/info/list", "/v1/analytics/data", "/v1/analytics/product-queries/details"]}],
                    "expires_at": "2099-01-01T00:00:00Z"}
        if path == "/v1/description-category/tree":
            return {"result": [{"description_category_id": 100, "children": [{"type_id": 200, "children": []}]}]}
        raise AssertionError("Fixture rejects any authorization mutation or unknown read")


# Explicitly owned, temporary credential registry: exactly two real Fernet
# authorizations with synthetic credentials, never touching config/shops.json.
shop_registry.save_registry({"schema_version": shop_registry.SCHEMA_VERSION, "default_read_shop": "qa-a",
                            "shops": [{"id": "qa-a", "name": "qa-a", "display_name": "离线验收店铺 A",
                                       "enabled": False, "client_id_env": "OZON_QA_A_CLIENT_ID",
                                       "api_key_env": "OZON_QA_A_API_KEY", "default_currency_code": "CNY"}]}, REGISTRY)
for shop_id, name, client in (("qa-a", "离线验收店铺 A", "101"), ("qa-b", "离线验收店铺 B", "102")):
    authorization.authorize_shop(shop_id, name, client, f"offline-qa-seller-key-{shop_id}",
                                 registry_path=REGISTRY, vault_root=VAULT,
                                 transport_factory=AuthorizationFixture)


def authorized_shops():
    return authorization.list_authorized_shops(registry_path=REGISTRY, vault_root=VAULT)


class PerformanceFixture:
    def request(self, method, path, *, body=None, query=None, **kwargs):
        if (method, path) == ("POST", "/api/client/token"):
            if body.get("client_secret") == "offline-rejected-ad-secret":
                raise OzonHttpError("Explicit offline advertising authorization rejection", status=401)
            value = {"access_token": "offline-fixture-bearer-token", "expires_in": 1800, "token_type": "Bearer"}
        elif path == "/api/client/campaign" and method == "GET":
            value = {"list": [{"id": "10", "title": "离线广告活动（未真实投放）", "state": "CAMPAIGN_STATE_INACTIVE",
                               "paymentType": "CPC", "advObjectType": "SKU", "placement": ["PLACEMENT_TOP_PROMOTION"]}]}
        elif (method, path) == ("POST", "/api/client/statistics"):
            value = {"UUID": str(uuid.uuid4())}
        elif (method, path) == ("GET", "/api/client/statistics/report"):
            return WireResponse(";离线协议验收，不是真实广告数据\nsku;Показы;Клики;Расход, Р, с НДС;Заказы;Выручка, Р\n9001;500;4;15,20;1;300,00\n".encode(), "text/csv; charset=UTF-8")
        elif method == "GET" and path.startswith("/api/client/statistics/"):
            value = {"UUID": path.rsplit("/", 1)[1], "state": "OK"}
        else:
            raise AssertionError("Fixture rejects any unplanned Performance endpoint")
        return WireResponse(json.dumps(value).encode())


def credential_context(request):
    secure = request.url.hostname == "127.0.0.1"
    return {"can_submit_credentials": secure, "reason": "离线验收只允许本机表单" if not secure else ""}


def require_credentials(request):
    if not credential_context(request)["can_submit_credentials"]:
        raise HTTPException(403, "仅允许本机离线验收")


register_shop_management_routes(app, runtime_root=RUNTIME, registry_path=REGISTRY, vault_root=VAULT,
                                transport_factory=AuthorizationFixture,
                                performance_factory=lambda root: PerformanceAccess(root, transport=PerformanceFixture()),
                                credential_context=credential_context, require_credentials=require_credentials,
                                invalidate_shop_cache=lambda shop: None)
register_operations_routes(app, runtime_root=RUNTIME, seller_transport=SellerFixture,
                          shop_rows=authorized_shops, credential_context=credential_context,
                          require_credentials=require_credentials, publication_rows=lambda: rows,
                          performance_factory=lambda root: PerformanceAccess(root, transport=PerformanceFixture()))


@app.get("/")
def page():
    return FileResponse(ROOT / "web/research-workbench.html")


@app.get("/assets/{name}")
def asset(name: str):
    target = ROOT / "web" / name
    if target.parent != ROOT / "web" or not target.is_file() or target.suffix not in {".js", ".css"}:
        raise HTTPException(404)
    return FileResponse(target, media_type="text/javascript" if target.suffix == ".js" else "text/css")


@app.get("/api/workbench/stores")
def stores(request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return {"ok": True, "shops": authorized_shops(), "authorization_context": credential_context(request)}


class ShopSettingsFixture(BaseModel):
    enabled: bool | None = None
    make_default: bool = False


@app.post("/api/workbench/stores/{shop_id}/test")
def test_shop(shop_id: str, request: Request, response: Response):
    require_credentials(request)
    response.headers["Cache-Control"] = "private, no-store"
    shop = authorization.test_shop_connection(shop_id, registry_path=REGISTRY, vault_root=VAULT,
                                             transport_factory=AuthorizationFixture)
    return {"ok": True, "shop": shop, "api_writes_performed": False}


@app.put("/api/workbench/stores/{shop_id}/settings")
def shop_settings(shop_id: str, payload: ShopSettingsFixture, request: Request, response: Response):
    require_credentials(request)
    response.headers["Cache-Control"] = "private, no-store"
    if payload.enabled is not None:
        authorization.set_shop_enabled(shop_id, payload.enabled, registry_path=REGISTRY, vault_root=VAULT,
                                       transport_factory=AuthorizationFixture)
    if payload.make_default:
        authorization.set_default_shop(shop_id, registry_path=REGISTRY, vault_root=VAULT)
    shop = next(row for row in authorized_shops() if row["id"] == shop_id)
    return {"ok": True, "shop": shop, "api_writes_performed": False}


@app.get("/api/research/categories")
@app.get("/api/research/sessions")
@app.get("/api/collector/products")
def empty_bootstrap():
    return {"items": [], "recommended_count": 0}
