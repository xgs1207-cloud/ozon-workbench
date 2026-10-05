"""操作台（网页控制台）与新增接口的测试：页面可打开、店铺接口不泄密、真提交必须显式确认。"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))

if HAS_DEPS:
    import api as api_module
    from fastapi.testclient import TestClient


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        api_module.PRODUCTS_ROOT = self.root / "products"
        registry = self.root / "shops.json"
        from pipeline.stores import ensure_registry, save_registry

        data = ensure_registry(None)
        data["shops"] = [
            {
                "id": "default",
                "name": "default",
                "display_name": "我的店",
                "enabled": True,
                "client_id_env": "OZON_DEFAULT_CLIENT_ID",
                "api_key_env": "OZON_DEFAULT_API_KEY",
                "default_currency_code": "CNY",
            }
        ]
        save_registry(data, registry)
        self.registry = registry
        self.client = TestClient(api_module.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_console_page_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ozon 上品工作台", response.text)
        self.assertIn("/api/workbench/doctor", response.text)

    def test_stores_endpoint_never_leaks_secrets(self):
        secret = "super-secret-value"
        with unittest.mock.patch.dict(
            os.environ, {"OZON_DEFAULT_CLIENT_ID": "1000", "OZON_DEFAULT_API_KEY": secret}
        ):
            response = self.client.get("/api/workbench/stores")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["shops"][0]["id"], "default")
        self.assertTrue(payload["shops"][0]["credentials_ready"])
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("1000", json.dumps(payload, ensure_ascii=False))

    def test_submit_requires_explicit_confirmation(self):
        response = self.client.post("/api/workbench/products/P-not-exist/submit", json={"store": "default"})
        # 没有 confirm → 直接 400；商品不存在也必须先被 confirm 拦住（不能误触发写）
        self.assertEqual(response.status_code, 400)
        self.assertIn("SUBMIT", response.json()["detail"])

    def test_submit_with_confirm_on_missing_product_is_404(self):
        response = self.client.post(
            "/api/workbench/products/P-not-exist/submit", json={"store": "default", "confirm": "SUBMIT"}
        )
        self.assertEqual(response.status_code, 404)

    def test_preflight_on_missing_product_is_404(self):
        response = self.client.post("/api/workbench/products/P-not-exist/preflight", json={"store": "default"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
