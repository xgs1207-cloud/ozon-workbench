"""Owned, offline browser QA. All provider HTTP is replaced; no real credentials."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from pipeline.performance_access import PerformanceAccess, WireResponse
from workbench_operations_api import register_operations_routes

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="ozon-operations-qa-")
app = FastAPI(title="离线运营中心验收")
shops = [{"id": "qa-a", "name": "离线验收店铺 A", "enabled": True, "credentials_ready": True},
         {"id": "qa-b", "name": "离线验收店铺 B", "enabled": True, "credentials_ready": True}]
rows = [{"shop": "qa-a", "offer_id": "xzj.jp.10.8.1", "product_id": "P000007", "source_sku_id": "red24",
         "source_note": "红色/内白 24cm/3.5L", "source_url": "https://detail.1688.com/offer/1053070588316.html",
         "ozon_product_id": "101", "import_status": "imported", "stock_status": "success", "warehouse_name": "离线验收仓库"},
        {"shop": "qa-b", "offer_id": "xzj.jp.10.8.1", "product_id": "P000008", "source_sku_id": "green",
         "source_note": "绿色，离线隔离验证", "ozon_product_id": "102", "import_status": "imported"}]


class SellerFixture:
    def __init__(self, shop):
        self.shop = shop
        self.credentials = SimpleNamespace(client_id="101" if shop == "qa-a" else "102")

    def post(self, path, body):
        sku = "9001" if self.shop == "qa-a" else "9002"
        if path == "/v3/product/info/list":
            return {"items": [{"id": 101 if self.shop == "qa-a" else 102, "sku": int(sku), "offer_id": body["offer_id"][0],
                               "name": "离线验收珐琅锅", "currency_code": "CNY", "price": "300",
                               "statuses": {"is_created": True, "status": "processed", "moderate_status": "approved"},
                               "stocks": {"has_stock": True}, "visibility_details": {"has_price": True}, "errors": []}]}
        if path == "/v1/analytics/data":
            from datetime import date, timedelta
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
            return {"page_count": 1, "total": 2, "queries": [
                {"sku": int(sku), "query": "кастрюля чугунная", "unique_search_users": 200, "unique_view_users": 80,
                 "order_count": 3, "gmv": 1500, "currency": "RUB", "position": 16.5},
                {"sku": int(sku), "query": "кастрюля тыква", "unique_search_users": 50, "unique_view_users": None,
                 "order_count": 0, "gmv": 0, "currency": "RUB", "position": None}]}
        raise AssertionError("Fixture rejects any unplanned Seller endpoint")


class PerformanceFixture:
    def request(self, method, path, *, body=None, query=None, **kwargs):
        if (method, path) == ("POST", "/api/client/token"):
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


register_operations_routes(app, runtime_root=Path(TEMP.name), seller_transport=SellerFixture,
                          shop_rows=lambda: shops, credential_context=credential_context,
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
def stores():
    return {"shops": shops, "authorization_context": {"can_submit_credentials": True}}


@app.get("/api/research/categories")
@app.get("/api/research/sessions")
@app.get("/api/collector/products")
def empty_bootstrap():
    return {"items": [], "recommended_count": 0}
