"""Unified form routing redacts secrets and enforces secure same-origin writes."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pipeline.performance_access import PerformanceAccess
from tests.test_shop_authorization import FakeTransport as SellerFixture, SECRET_CLIENT, SECRET_KEY
from tests.test_performance_access import FakeTransport as AdvertisingFixture, CLIENT, SECRET
from workbench_shop_management_api import PATH, register_shop_management_routes


class ShopManagementApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seller = SellerFixture()
        self.advertising = AdvertisingFixture()
        self.invalidated = []
        self.app = FastAPI()
        def secure(request):
            if request.url.scheme != "https":
                raise HTTPException(403, "请通过 HTTPS 授权")
        register_shop_management_routes(
            self.app, runtime_root=lambda: self.root,
            registry_path=lambda: self.root / "config" / "shops.json", vault_root=lambda: self.root / "vault",
            transport_factory=lambda _: self.seller,
            performance_factory=lambda root: PerformanceAccess(root, transport=self.advertising),
            invalidate_shop_cache=self.invalidated.append,
            credential_context=lambda request: {"can_submit_credentials": request.url.scheme == "https"},
            require_credentials=secure)
        self.client = TestClient(self.app, base_url="https://testserver")
        self.payload = {"mode": "create", "shop_id": "shop-a", "display_name": "店铺甲",
                        "seller_client_id": SECRET_CLIENT, "seller_api_key": SECRET_KEY,
                        "advertising_client_id": CLIENT, "advertising_client_secret": SECRET}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_create_edit_then_cached_list_complete_story(self):
        result = self.client.post(PATH, json=self.payload)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        self.assertTrue(result.json()["advertising_saved"])
        self.assertNotIn(SECRET, result.text)
        self.seller.calls.clear()
        self.advertising.calls.clear()
        edited = self.client.post(PATH, json={"mode": "update", "shop_id": "shop-a", "display_name": "改过的店名"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertFalse(edited.json()["seller_saved"])
        self.assertEqual(self.seller.calls, [])
        self.assertEqual(self.advertising.calls, [])
        cached = self.client.get(PATH)
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(cached.headers["cache-control"], "private, no-store")
        row = next(row for row in cached.json()["shops"] if row["id"] == "shop-a")
        self.assertEqual(row["display_name"], "改过的店名")
        self.assertTrue(row["advertising"]["configured"])
        for secret in (SECRET_CLIENT, SECRET_KEY, CLIENT, SECRET):
            self.assertNotIn(secret, cached.text)

    def test_csrf_and_insecure_prevent_any_auth_storage(self):
        for headers in ({"origin": "https://evil.invalid"}, {"sec-fetch-site": "cross-site"}):
            response = self.client.post(PATH, json=self.payload, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
        with TestClient(self.app, base_url="http://testserver") as insecure:
            response = insecure.post(PATH, json=self.payload)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.seller.calls, [])
        self.assertEqual(self.advertising.calls, [])
        self.assertFalse((self.root / "config" / "shops.json").exists())

    def test_validation_rejects_bad_fields_without_reflecting_input(self):
        for fields in ({"mode": "bad"}, {"shop_id": "../bad"}, {"display_name": ""},
                       {"default_currency_code": "GBP"}, {"extra": SECRET},
                       {"seller_api_key": {"secret": SECRET}}):
            response = self.client.post(PATH, json={**self.payload, **fields})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(SECRET, response.text)
            self.assertNotIn(SECRET_KEY, response.text)
            self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_duplicate_create_and_unknown_update_correct_status(self):
        self.assertEqual(self.client.post(PATH, json=self.payload).status_code, 200)
        self.assertEqual(self.client.post(PATH, json=self.payload).status_code, 409)
        response = self.client.post(PATH, json={"mode": "update", "shop_id": "not-existing", "display_name": "未知"})
        self.assertEqual(response.status_code, 404)

    def test_ad_failure_returns_safe_partial_with_created_shop(self):
        self.advertising.failure = RuntimeError(SECRET)
        response = self.client.post(PATH, json=self.payload)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["partial"])
        self.assertFalse(result["advertising_saved"])
        self.assertTrue(result["seller_saved"])
        self.assertNotIn(SECRET, response.text)
        self.assertTrue(result["shop"]["credentials_ready"])

    def test_no_ad_or_product_mutation_routes(self):
        self.assertEqual(self.client.post(PATH + "/activate", json=self.payload).status_code, 404)
        self.assertEqual(self.client.post(PATH + "/publish", json=self.payload).status_code, 404)


if __name__ == "__main__":
    unittest.main()
