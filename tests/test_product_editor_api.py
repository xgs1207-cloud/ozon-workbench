"""Desktop product editor endpoints: draft-only, explicit clearing and no paid/live writes."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))
if HAS_DEPS:
    import api as api_module
    from fastapi.testclient import TestClient

from collector.ingest import ingest_capture


@unittest.skipUnless(HAS_DEPS, "Requires fastapi and httpx")
class ProductEditorApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        products = self.root / "products"
        self.products_patch = patch.object(api_module, "PRODUCTS_ROOT", products)
        self.products_patch.start()
        self.addCleanup(self.products_patch.stop)
        self.client = TestClient(api_module.app)
        self.addCleanup(self.client.close)
        captured = ingest_capture(products, {
            "source_url": "https://detail.1688.com/offer/131313131.html",
            "title_zh": "产品表单测试用品",
            "skus": [{"sku_id": "S1", "purchase_price_cny": 18.5}],
        })
        self.product_id = captured["product_id"]
        self.directory = products / self.product_id
        self.base = f"/api/workbench/products/{self.product_id}"
        self.details = {
            "material": "硅胶", "package_quantity": 1,
            "product_length_mm": 70, "product_width_mm": 60,
            "product_height_mm": 100, "product_weight_g": 200,
            "package_length_mm": 80, "package_width_mm": 70,
            "package_height_mm": 110, "package_weight_g": 230,
        }

    def write_json(self, relative, value):
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def read_json(self, relative):
        return json.loads((self.directory / relative).read_text(encoding="utf-8"))

    def snapshot(self):
        return {str(path.relative_to(self.directory)): path.read_bytes()
                for path in self.directory.rglob("*") if path.is_file()}

    def test_get_empty_details_is_read_only(self):
        before = self.snapshot()
        response = self.client.get(self.base + "/listing-details")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["details"], dict)
        self.assertIsInstance(payload["provenance"], dict)
        self.assertFalse(payload["saved"])
        self.assertFalse(payload["api_writes_performed"])
        self.assertEqual(before, self.snapshot())

    def test_save_full_details_persists_and_syncs_confirmed_measurements(self):
        source_bytes = (self.directory / "input/source.json").read_bytes()
        response = self.client.put(self.base + "/listing-details", json={"details": self.details})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["saved"])
        self.assertFalse(payload["api_writes_performed"])
        self.assertEqual(payload["details"], self.details)
        self.assertEqual(self.client.get(self.base + "/listing-details").json()["details"], self.details)
        self.assertEqual(source_bytes, (self.directory / "input/source.json").read_bytes())
        overrides = self.read_json("input/workbench-sku-overrides.json")
        for key in self.details:
            if key not in ("material", "package_quantity"):
                self.assertEqual(overrides["product"][key], self.details[key])
        confirmations = self.read_json("input/human-confirmations.json")
        self.assertEqual(confirmations["material"], "硅胶")
        self.assertEqual(confirmations["package_quantity"], 1)

    def test_partial_dimensions_are_saved_as_draft_not_confirmed_complete_measurements(self):
        partial = {"product_height_mm": 100, "product_width_mm": 70}
        response = self.client.put(self.base + "/listing-details", json={"details": partial})
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get(self.base + "/listing-details").json()["details"]
        self.assertEqual(saved["product_height_mm"], 100)
        self.assertEqual(saved["product_width_mm"], 70)
        self.assertNotIn("product_length_mm", saved)
        overrides_file = self.directory / "input/workbench-sku-overrides.json"
        if overrides_file.exists():
            self.assertFalse(self.read_json("input/workbench-sku-overrides.json").get("product"))
        self.assertFalse(response.json()["api_writes_performed"])

    def test_explicit_null_clears_stale_confirmations_and_measurements_but_preserves_other_drafts(self):
        self.write_json("input/manual-prices.json", {"prices": {"S1": {"price": 99, "currency": "CNY"}}})
        manual_prices = (self.directory / "input/manual-prices.json").read_bytes()
        self.write_json("input/human-confirmations.json", {
            "material": "old", "package_quantity": 2,
            "attributes": {"9048": [{"value": "operator model"}]},
            "sku_attributes": {"S1": {"10097": [{"value": "синий"}]}},
        })
        response = self.client.put(self.base + "/listing-details", json={"details": self.details})
        self.assertEqual(response.status_code, 200, response.text)
        clear = self.client.put(self.base + "/listing-details", json={"details": {
            "material": None, "package_quantity": None, "product_length_mm": None,
        }})
        self.assertEqual(clear.status_code, 200, clear.text)
        saved = self.client.get(self.base + "/listing-details").json()["details"]
        self.assertFalse(saved.get("material"))
        self.assertIsNone(saved.get("package_quantity"))
        self.assertIsNone(saved.get("product_length_mm"))
        confirmations = self.read_json("input/human-confirmations.json")
        self.assertFalse(confirmations.get("material"))
        self.assertIsNone(confirmations.get("package_quantity"))
        self.assertEqual(confirmations["attributes"]["9048"][0]["value"], "operator model")
        self.assertEqual(confirmations["sku_attributes"]["S1"]["10097"][0]["value"], "синий")
        self.assertNotIn("product_length_mm", self.read_json("input/workbench-sku-overrides.json").get("product", {}))
        self.assertEqual(manual_prices, (self.directory / "input/manual-prices.json").read_bytes())

    def test_legacy_manual_material_and_quantity_are_visible_without_rewriting_files(self):
        self.write_json("input/human-confirmations.json", {"material": "cotton", "package_quantity": 3})
        before = self.snapshot()
        response = self.client.get(self.base + "/listing-details")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["details"]["material"], "cotton")
        self.assertEqual(response.json()["details"]["package_quantity"], 3)
        self.assertEqual(before, self.snapshot())

    def test_null_clear_cannot_resurrect_legacy_aliases_supplier_values_or_model_guesses(self):
        self.write_json("input/human-confirmations.json", {
            "material_zh": "旧材质", "product_weight_g": 220,
            "product_dimensions_mm": {"length": 70, "width": 60, "height": 100},
        })
        source = self.read_json("input/source.json")
        source["attributes_zh"] = {"material": "供应商材质", "package_quantity": 12}
        self.write_json("input/source.json", source)
        response = self.client.put(self.base + "/listing-details", json={"details": {
            "material": None, "package_quantity": None,
            "product_height_mm": None, "product_weight_g": None,
        }})
        self.assertEqual(response.status_code, 200, response.text)
        human = self.read_json("input/human-confirmations.json")
        self.assertNotIn("material_zh", human)
        self.assertNotIn("product_weight_g", human)
        self.assertNotIn("product_dimensions_mm", human)
        from models.http_provider import enrich_facts_from_inputs
        payload = {"facts": {"materials": ["模型猜测材质"], "package_quantity": 88,
                             "dimensions": {"length_mm": 999, "width_mm": 999, "height_mm": 999},
                             "weight": {"value_g": 999}}}
        enrich_facts_from_inputs(payload, SimpleNamespace(source=source, product_dir=self.directory))
        self.assertEqual(payload["facts"]["materials"], [])
        self.assertEqual(payload["facts"]["package_quantity"], "unknown")
        self.assertEqual(payload["facts"]["dimensions"], "unknown")
        self.assertEqual(payload["facts"]["weight"], "unknown")

    def test_unknown_and_invalid_fields_are_rejected_without_partial_persistence(self):
        for details in (
            {"not_a_listing_field": "value"}, {"api_key": "unacceptable"},
            {"product_length_mm": -1}, {"product_weight_g": 0},
            {"package_width_mm": True}, {"product_height_mm": 1.5},
            {"package_weight_g": "230"}, {"product_width_mm": "70mm"},
            {"package_quantity": False}, {"package_quantity": -1},
            {"material": ["硅胶"]},
        ):
            with self.subTest(details=details):
                before = self.snapshot()
                response = self.client.put(self.base + "/listing-details", json={"details": details})
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(before, self.snapshot())

    def test_inconsistent_package_measurements_are_rejected_before_saving(self):
        for key in ("package_length_mm", "package_width_mm", "package_height_mm", "package_weight_g"):
            with self.subTest(key=key):
                before = self.snapshot()
                invalid = {**self.details, key: 1}
                response = self.client.put(self.base + "/listing-details", json={"details": invalid})
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertEqual(before, self.snapshot())

    def test_nan_numeric_value_is_rejected_without_persistence(self):
        before = self.snapshot()
        response = self.client.put(self.base + "/listing-details",
                                   content='{"details":{"product_weight_g":NaN}}',
                                   headers={"Content-Type": "application/json"})
        self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(before, self.snapshot())

    def test_missing_product_endpoints_are_404(self):
        base = "/api/workbench/products/P999999"
        for method, path, kwargs in (
            ("get", "/listing-details", {}),
            ("put", "/listing-details", {"json": {"details": {"material": "cotton"}}}),
            ("get", "/listing-autofill", {}),
            ("post", "/listing-autofill", {"json": {"shop": "store-a"}}),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(self.client, method)(base + path, **kwargs).status_code, 404)

    def test_autofill_get_uses_cached_only_post_requests_optional_dictionary_resolution(self):
        proposed = {"attributes": {}, "per_sku_attributes": {}, "basic_fields": {},
                    "provenance": {}, "unresolved": [], "lookup_count": 0,
                    "api_writes_performed": False, "seller_offer_ids": {}}
        before = self.snapshot()
        with patch("pipeline.listing_autofill.build_autofill", return_value=proposed) as build, \
                patch("pipeline.runner.run_product", side_effect=AssertionError("No pipeline in read-only autofill")) as run, \
                patch("pipeline.ozon_write.post_import", side_effect=AssertionError("No live product write in autofill")) as write:
            first = self.client.get(self.base + "/listing-autofill", params={"shop": "store-a"})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertFalse(first.json()["api_writes_performed"])
            self.assertEqual(build.call_args.kwargs.get("shop_id"), "store-a")
            self.assertFalse(build.call_args.kwargs.get("resolve_dictionaries", False))
            second = self.client.post(self.base + "/listing-autofill", json={"shop": "store-a"})
            self.assertEqual(second.status_code, 200, second.text)
            self.assertFalse(second.json()["api_writes_performed"])
            self.assertEqual(build.call_args.kwargs.get("shop_id"), "store-a")
            self.assertTrue(build.call_args.kwargs.get("resolve_dictionaries"))
            run.assert_not_called()
            write.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_saved_details_rejects_editing_after_any_live_submission(self):
        status = self.read_json("status.json")
        status["api_write_count"] = 1
        self.write_json("status.json", status)
        before = self.snapshot()
        response = self.client.put(self.base + "/listing-details", json={"details": self.details})
        self.assertIn(response.status_code, (400, 409), response.text)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
