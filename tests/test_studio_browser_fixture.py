"""Contracts for the populated browser fixture, never production provider tests."""
import importlib.util
from io import BytesIO
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from PIL import Image


class StudioBrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("owned_studio_fixture_test", Path(__file__).with_name("studio_browser_fixture.py"))
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.TEMP.cleanup()

    def setUp(self):
        self.fixture.STATE = self.fixture.initial_state()
        self.client = TestClient(self.fixture.app, base_url="http://127.0.0.1")

    def tearDown(self):
        self.client.close()

    def test_bootstrap_and_populated_listing_contract(self):
        self.assertTrue(self.client.get("/health").json()["offline_fixture"])
        products = self.client.get("/api/collector/products").json()["items"]
        self.assertEqual(len(products), 2)
        self.assertEqual(products[0]["sku_count"], 2)
        self.assertTrue(products[0]["thumbnail_url"].endswith(".png"))
        self.assertEqual(self.client.get(products[0]["thumbnail_url"]).status_code, 200)
        base = "/api/workbench/products/P000007/"
        guided = self.client.get(base + "guided").json()
        self.assertTrue(guided["workflow"]["analysis"]["confirmed"])
        self.assertIn("红色", guided["workflow"]["analysis"]["payload"]["selling_points"][0]["text_zh"])
        self.assertEqual(guided["image_backend"]["name"], "offline")
        self.assertEqual(self.client.get(base + "skus").json()["selected"], ["red24"])
        form = self.client.get(base + "listing-form").json()
        self.assertEqual(form["form"]["shop_id"], "qa-a")
        self.assertEqual(len(form["selected_skus"]), 1)
        self.assertIn("10096", form["per_sku_attributes"]["red24"])
        document = self.client.get(base + "listing-document").json()["document"]
        self.assertTrue(document["offer_ids"]["complete"])
        self.assertEqual(document["selected_skus"][0]["offer_id"], "xzj.jp.10.8.1")
        self.assertEqual(document["selected_skus"][0]["manual_price"], {"price": 260, "currency": "CNY"})
        self.assertEqual(document["operational_fields"]["package"], {"weight_g": 5800, "length_mm": 340, "width_mm": 290, "height_mm": 220})
        self.assertEqual(document["card"]["form"]["fields"], form["form"]["fields"])
        self.assertEqual(document["card"]["attributes"], form["attributes"])
        self.assertEqual(self.client.get(base + "listing-details").json()["details"]["package_weight_g"], 5800)
        self.assertEqual(self.client.get("/api/workbench/stores").status_code, 200)
        self.assertEqual(self.client.get("/api/workbench/shop-management").status_code, 200)
        self.assertEqual(self.client.get("/api/operations/products").status_code, 200)
        warehouses = self.client.get("/api/workbench/warehouses", params={"shop": "qa-a"}).json()
        self.assertEqual(warehouses["shop"], "qa-a")
        self.assertTrue(warehouses["complete"])
        self.assertTrue(warehouses["items"][0]["eligible"])
        self.assertEqual(self.client.post("/api/workbench/warehouses/refresh", json={"shop": "qa-b"}).json()["shop"], "qa-b")

    def test_local_media_and_independent_workspaces(self):
        base = "/api/workbench/products/P000007/"
        media = self.client.get(base + "guided/media").json()
        self.assertEqual(media["image_plan"]["selected_slots"], ["offline-main", "offline-detail"])
        self.assertEqual(len(media["sets"]), 1)
        self.assertTrue(any(job["status"] == "failed" for job in media["jobs"]))
        image = self.client.get(base + "media/images/sku/red.png")
        self.assertEqual(image.status_code, 200)
        self.assertIn("image/png", image.headers["content-type"])
        self.assertTrue(image.content.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(BytesIO(image.content)) as parsed:
            self.assertEqual(parsed.size, (900, 1200))
            self.assertEqual(parsed.format, "PNG")
        self.assertEqual(self.client.get(base + "media/images/sku/red.svg").status_code, 404)
        self.assertTrue(all(ref["path"].endswith(".png") for ref in media["captured_reference_images"]))
        selected = self.client.put(base + "guided/media/selection", json={"selected_slots": ["offline-detail", "offline-main"]})
        self.assertEqual(selected.json()["image_plan"]["selected_slots"], ["offline-detail", "offline-main"])
        self.assertEqual(self.client.put(base + "guided/media/selection", json={"selected_slots": ["unknown"]}).status_code, 422)
        self.assertEqual(self.client.get(base + "media/../../config/shops.json").status_code, 404)

    def test_keyword_cookie_isolation_and_edit_flow(self):
        endpoint = "/api/workbench/keyword-library"
        original = self.client.get(endpoint).json()
        self.assertEqual(original["total"], 3)
        created = self.client.post(endpoint, json={"category": "厨房 / 铸铁锅", "text": "эмалированная кастрюля", "note": "离线新增验收"}).json()["item"]
        self.assertEqual(self.client.get(endpoint, params={"q": "离线新增"}).json()["total"], 1)
        self.assertEqual(self.client.put(endpoint + "/" + created["id"], json={"category": created["category"], "text": created["text"], "note": "修改后的备注"}).status_code, 200)
        with TestClient(self.fixture.app, base_url="http://127.0.0.1") as other:
            self.assertEqual(other.get(endpoint).json()["total"], 3)
            self.assertEqual(other.put(endpoint + "/" + created["id"], json={"category": "厨房", "text": "a"}).status_code, 404)
        self.assertEqual(self.client.delete(endpoint + "/" + created["id"]).status_code, 200)
        self.assertEqual(self.client.get(endpoint).json()["total"], 3)

    def test_rich_editor_mutation_is_owned_and_real_publication_is_blocked(self):
        base = "/api/workbench/products/P000007/"
        rich = self.client.get(base + "rich-content").json()
        self.assertEqual(rich["attribute_id"], 11254)
        blocks = [{"id": "owned-text", "type": "text", "title": "Title", "text": "Offline only"}]
        saved = self.client.put(base + "rich-content", json={"blocks": blocks}).json()
        self.assertEqual(saved["blocks"], blocks)
        for suffix in ["guided/submit", "guided/publish-media", "publications/continue"]:
            self.assertEqual(self.client.post(base + suffix, json={}).status_code, 409)
        self.assertEqual(self.client.post(base + "guided/media/slots/offline-main/generate", json={}).status_code, 404)
        self.assertTrue(str(self.fixture.RUNTIME).startswith(str(Path(self.fixture.TEMP.name))))
        self.assertNotEqual(self.fixture.operations.REGISTRY, self.fixture.operations.ROOT / "config/shops.json")


if __name__ == "__main__":
    unittest.main()
