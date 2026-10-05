"""采集入库的 HTTP 接口测试（需要 fastapi + httpx）。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))

if HAS_DEPS:
    import api as api_module
    from fastapi.testclient import TestClient

SOURCE_URL = "https://detail.1688.com/offer/987654321.html"

PAYLOAD = {
    "source_url": SOURCE_URL,
    "title_zh": "HTTP 入库测试",
    "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
    "skus": [{"sku_id": "S1", "purchase_price_cny": 10}],
}


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class CollectorApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        api_module.PRODUCTS_ROOT = self.root / "products"
        api_module.LIBRARY_ROOT = self.root / "keyword-library"
        self.client = TestClient(api_module.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_then_list_and_detail(self):
        response = self.client.post("/api/collector/products", json=PAYLOAD)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["product_id"], "P000001")
        self.assertEqual(body["counts"]["skus"], 1)

        listing = self.client.get("/api/collector/products").json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["items"][0]["status"], "COLLECTED")
        self.assertEqual(listing["items"][0]["sku_count"], 1)

        detail = self.client.get("/api/collector/products/P000001").json()
        self.assertEqual(detail["summary"]["product_id"], "P000001")
        self.assertEqual(detail["source"]["offer_id"], "987654321")
        self.assertEqual(detail["manifest"]["file_count"] > 0, True)

        missing = self.client.get("/api/collector/products/P999999")
        self.assertEqual(missing.status_code, 404)

    def test_status_filter(self):
        self.client.post("/api/collector/products", json=PAYLOAD)
        self.assertEqual(self.client.get("/api/collector/products", params={"status": "COLLECTED"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/collector/products", params={"status": "UPLOADED"}).json()["count"], 0)

    def test_duplicate_returns_409_with_options(self):
        self.client.post("/api/collector/products", json=PAYLOAD)
        response = self.client.post("/api/collector/products", json=PAYLOAD)
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["duplicate_of"], "P000001")
        self.assertIn("open_existing", detail["options"])

    def test_new_version_flag(self):
        self.client.post("/api/collector/products", json=PAYLOAD)
        response = self.client.post(
            "/api/collector/products", json=PAYLOAD, params={"allow_new_version": "true"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["product_id"], "P000002")
        self.assertEqual(response.json()["duplicate_of"], "P000001")

    def test_invalid_payload_returns_422(self):
        response = self.client.post(
            "/api/collector/products", json={"source_url": "https://www.ozon.ru/product/1", "skus": []}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_import_folder_endpoint(self):
        folder = self.root / "capture"
        (folder / "main-images").mkdir(parents=True)
        (folder / "main-images" / "m1.png").write_bytes(b"m1")
        response = self.client.post(
            "/api/collector/products/import-folder",
            json={
                "folder": str(folder),
                "source_url": SOURCE_URL,
                "skus": [{"sku_id": "S1", "purchase_price_cny": 9.5}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"]["main_images"], 1)

    def test_import_folder_missing_returns_422(self):
        response = self.client.post(
            "/api/collector/products/import-folder",
            json={"folder": str(self.root / "nope"), "source_url": SOURCE_URL, "skus": [{"sku_id": "S1", "purchase_price_cny": 1}]},
        )
        self.assertEqual(response.status_code, 422, response.text)

    # ---- Edge 插件适配：新端点 + 插件载荷形状 ----

    def test_workbench_summary(self):
        response = self.client.get("/api/workbench/summary")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["product_count"], 0)
        self.assertIn("ready_shop_count", body)

    def test_collector_duplicates_endpoint(self):
        # 未入库
        nope = self.client.get("/api/collector/duplicates", params={"source_url": SOURCE_URL})
        self.assertEqual(nope.status_code, 200)
        self.assertFalse(nope.json()["exists"])
        # 入库后查重命中
        self.client.post("/api/collector/products", json=PAYLOAD)
        hit = self.client.get("/api/collector/duplicates", params={"source_url": SOURCE_URL})
        self.assertTrue(hit.json()["exists"])
        self.assertEqual(hit.json()["product_id"], "P000001")

    def test_ozon_reference_page_endpoint(self):
        response = self.client.post(
            "/api/collector/ozon-reference-page",
            json={"source_url": "https://www.ozon.ru/product/123", "title": "test", "image_urls": ["https://x/1.jpg"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "waiting_ai_design")
        self.assertTrue(body["task"]["task_id"].startswith("ref-"))

    def test_ozon_reference_page_missing_url_returns_422(self):
        response = self.client.post("/api/collector/ozon-reference-page", json={"title": "x"})
        self.assertEqual(response.status_code, 422)

    def test_extension_payload_shape(self):
        """Edge 插件 POST 的载荷：title_cn / ozon_category_selection / main_images(detail_images) URL / product_attributes 数组 / sku.purchase_price。"""
        ext_payload = {
            "source_url": SOURCE_URL,
            "title_cn": "插件采集的纯棉床单",
            "ozon_category_selection": {
                "category_id": "17028731",
                "type_id": "92612",
                "category_path_zh": "住宅和花园/床上用品/床单",
                "category_path": ["住宅和花园", "床上用品", "床单"],
            },
            "product_attributes": [
                {"name": "材质", "value": "100% 棉"},
                {"name": "规格", "value": "200x200 cm"},
            ],
            "main_images": [
                {"url": "https://cbu01.alicdn.com/img/ibank/invalid-main.jpg"},
                "https://cbu01.alicdn.com/img/ibank/invalid-main2.jpg",
            ],
            "detail_images": [{"url": "https://cbu01.alicdn.com/img/ibank/invalid-detail.jpg"}],
            "skus": [
                {"sku_id": "SKU-001", "sku_name": "白色 200x200", "purchase_price": 35.5, "price_source": "sku"},
                {"sku_id": "SKU-002", "sku_name": "灰色 200x200", "purchase_price": 36, "image_url": "https://cbu01.alicdn.com/img/ibank/invalid-sku.jpg"},
            ],
            "selected_sku_ids": ["SKU-001", "SKU-002"],
        }
        response = self.client.post("/api/collector/products", json=ext_payload)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["counts"]["skus"], 2)
        # 远程图在测试环境下下载失败 → 计数为 0 但有警告，不阻断入库
        self.assertEqual(body["counts"]["main_images"], 0)
        self.assertTrue(any("下载失败" in w for w in body["warnings"]))
        # 类目映射正确
        self.assertEqual(body["ozon_category"]["category_id"], "17028731")
        self.assertEqual(body["ozon_category"]["type_id"], "92612")

        # 回读 source.json 验证 title / 属性落盘
        source = json.loads((self.root / "products" / "P000001" / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["title_zh"], "插件采集的纯棉床单")
        self.assertEqual(source["selected_category"]["category_id"], "17028731")
        self.assertEqual(source["attributes_zh"]["raw"]["材质"], "100% 棉")
        self.assertEqual(source["skus"][0]["purchase_price_cny"], 35.5)

    def test_redirect_routes(self):
        for path in ("/1688-collection", "/ozon-reference", "/command-center"):
            response = self.client.get(path, follow_redirects=False)
            self.assertIn(response.status_code, (303, 307), f"{path} -> {response.status_code}")
            self.assertTrue(response.headers["location"].startswith("/"))


if __name__ == "__main__":
    unittest.main()
