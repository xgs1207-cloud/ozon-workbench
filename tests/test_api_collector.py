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


if __name__ == "__main__":
    unittest.main()
