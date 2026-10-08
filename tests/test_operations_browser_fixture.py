"""Offline browser fixture contract uses only owned temp data and fake wires."""
import importlib.util
from pathlib import Path
import unittest

from fastapi.testclient import TestClient


class OperationsBrowserFixtureTests(unittest.TestCase):
    def test_isolated_shop_edit_catalog_pagination_and_identity_story(self):
        fixture_path = Path(__file__).with_name("operations_browser_fixture.py")
        spec = importlib.util.spec_from_file_location("owned_operations_fixture_test", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        client = TestClient(fixture.app, base_url="http://127.0.0.1")
        try:
            self.assertEqual(len(client.get("/api/workbench/stores").json()["shops"]), 2)
            self.assertEqual(client.get("/api/workbench/shop-management").status_code, 200)
            client.post("/api/operations/discover", json={})
            first = client.post("/api/operations/catalog/read", json={"shop": "qa-a", "limit": 50}).json()
            self.assertEqual(len(first["items"]), 50)
            self.assertTrue(first["has_more"])
            self.assertTrue(all(row["thumbnail"] is None for row in first["items"]))
            second = client.post("/api/operations/catalog/read", json={"shop": "qa-a", "previous_page_token": first["page_token"]}).json()
            self.assertEqual(len(second["items"]), 1)
            self.assertFalse(second["has_more"])
            imported = client.post("/api/operations/catalog/add", json={"shop": "qa-a", "page_token": first["page_token"],
                                                                        "offer_ids": ["xzj.jp.10.8.1", "offline.qa-a.001"]}).json()
            self.assertEqual((imported["existing"], imported["imported"]), (1, 1))
            existing = next(row for row in imported["items"] if row["offer_id"] == "xzj.jp.10.8.1")
            self.assertEqual(existing["product_id"], "P000007")
            self.assertEqual(existing["source_note"], "红色/内白 24cm/3.5L")
            b = client.post("/api/operations/catalog/read", json={"shop": "qa-b"}).json()
            self.assertEqual(b["items"][0]["ozon_product_id"], "102")
            forbidden = client.post("/api/operations/catalog/add", json={"shop": "qa-b", "page_token": first["page_token"],
                                                                        "offer_ids": ["offline.qa-a.001"]})
            self.assertEqual(forbidden.status_code, 404)

            modified = client.post("/api/workbench/shop-management", json={"mode": "update", "shop_id": "qa-a",
                                                                           "display_name": "离线改名验收", "default_currency_code": "RUB"})
            self.assertEqual(modified.status_code, 200)
            self.assertFalse(modified.json()["seller_saved"])
            self.assertEqual(client.post("/api/workbench/stores/qa-a/test", json={}).status_code, 200)
            disabled = client.put("/api/workbench/stores/qa-a/settings", json={"enabled": False})
            self.assertFalse(disabled.json()["shop"]["enabled"])
            self.assertEqual(client.post("/api/operations/catalog/read", json={"shop": "qa-a"}).status_code, 409)
            self.assertTrue(client.put("/api/workbench/stores/qa-a/settings", json={"enabled": True}).json()["shop"]["enabled"])
            new_shop = client.post("/api/workbench/shop-management", json={"mode": "create", "shop_id": "qa-c",
                                                                          "display_name": "离线新增验收", "seller_client_id": "103",
                                                                          "seller_api_key": "offline-qa-c-seller-key",
                                                                          "advertising_client_id": "qa-c@advertising.performance.ozon.ru",
                                                                          "advertising_client_secret": "offline-qa-c-ad-secret"})
            self.assertEqual(new_shop.status_code, 200, new_shop.text)
            self.assertTrue(new_shop.json()["advertising_saved"])
            self.assertEqual(len(client.get("/api/workbench/stores").json()["shops"]), 3)
            self.assertEqual(client.post("/api/operations/catalog/read", json={"shop": "qa-c"}).json()["items"], [])
        finally:
            client.close()
            fixture.TEMP.cleanup()


if __name__ == "__main__":
    unittest.main()
