"""Current desktop selection follows the official 100-item import limit, offline."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from pipeline import guided_review
from pipeline.listing_form import read_json, write_json
from pipeline.sku_selection import MAX_SELECTED, SkuSelectionError, blocking_collection_issues, selection_state, set_selection


class CurrentSkuLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / "products/P000001"
        self.rows = [{"sku_id": f"S{index}", "purchase_price_cny": None} for index in range(1, 201)]
        write_json(self.directory / "input/source.json", {
            "product_id": "P000001", "title_zh": "离线测试商品",
            "source_url": "https://detail.1688.com/offer/123456789.html",
            "sku_selection_required": True, "collection_mode": "all_skus", "skus": self.rows,
        })
        write_json(self.directory / "input/guided-workflow.json", {"schema_version": "1.0.0"})
        from pipeline.status import new_status
        write_json(self.directory / "status.json", new_status("P000001"))
        self.root_patch = patch.object(api, "PRODUCTS_ROOT", self.root / "products")
        self.root_patch.start()
        self.client = TestClient(api.app)
        self.base = "/api/workbench/products/P000001"

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def ids(self, count):
        return [row["sku_id"] for row in self.rows[:count]]

    def test_eleven_and_hundred_selected_skus_are_supported_without_procurement_prices(self):
        self.assertEqual(MAX_SELECTED, 100)
        for count in (11, 100):
            with self.subTest(count=count):
                result = set_selection(self.directory, include=self.ids(count))
                self.assertEqual(len(result["selected"]), count)
                self.assertEqual(selection_state(self.directory)["active_count"], count)
                self.assertEqual(len(read_json(self.directory / "input/source.json")["skus"]), 200)

    def test_hundred_and_one_requires_batches_and_preserves_prior_selection(self):
        set_selection(self.directory, include=self.ids(100))
        target = self.directory / "input/selected-skus.json"
        before = target.read_bytes()
        with self.assertRaisesRegex(SkuSelectionError, "100.*分批"):
            set_selection(self.directory, include=self.ids(101))
        self.assertEqual(target.read_bytes(), before)

    def test_current_api_accepts_eleven_and_hundred_manual_prices(self):
        for count in (11, 100):
            with self.subTest(count=count):
                response = self.client.post(self.base + "/skus", json={"include": self.ids(count)})
                self.assertEqual(response.status_code, 200, response.text)
                response = self.client.put(self.base + "/prices", json={"prices": [
                    {"sku_id": sku_id, "price": 39.9, "currency": "CNY"} for sku_id in self.ids(count)]})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(response.json()["prices"]), count)

    def test_selection_api_101_fails_safely_and_has_actionable_batch_instruction(self):
        self.client.post(self.base + "/skus", json={"include": self.ids(100)})
        before = (self.directory / "input/selected-skus.json").read_bytes()
        response = self.client.post(self.base + "/skus", json={"include": self.ids(101)})
        self.assertEqual(response.status_code, 422)
        self.assertIn("分批", response.json()["detail"])
        self.assertEqual((self.directory / "input/selected-skus.json").read_bytes(), before)

    def test_all_selection_never_silently_truncates_200_captured_variants(self):
        response = self.client.post(self.base + "/skus", json={"all": True})
        self.assertEqual(response.status_code, 422)
        self.assertIn("分批", response.json()["detail"])
        self.assertFalse((self.directory / "input/selected-skus.json").exists())
        self.assertEqual(len(read_json(self.directory / "input/source.json")["skus"]), 200)

    def test_manual_price_limit_does_not_disable_exact_sku_binding(self):
        set_selection(self.directory, include=self.ids(11))
        values = [{"sku_id": sku_id, "price": 39.9, "currency": "CNY"} for sku_id in self.ids(10)]
        self.assertEqual(self.client.put(self.base + "/prices", json={"prices": values}).status_code, 422)
        self.assertFalse((self.directory / "input/manual-prices.json").exists())

    def test_101_price_entries_are_rejected_before_persistence(self):
        set_selection(self.directory, include=self.ids(100))
        values = [{"sku_id": sku_id, "price": 39.9, "currency": "CNY"} for sku_id in self.ids(101)]
        self.assertEqual(self.client.put(self.base + "/prices", json={"prices": values}).status_code, 422)
        self.assertFalse((self.directory / "input/manual-prices.json").exists())

    def test_eleven_and_hundred_skus_reach_selected_ai_context_without_editing_capture(self):
        from pipeline.selected_source import selected_source
        captured = read_json(self.directory / "input/source.json")
        before = (self.directory / "input/source.json").read_bytes()
        for count in (11, 100):
            set_selection(self.directory, include=self.ids(count))
            projected = selected_source(self.directory, captured, require_selection=True)
            self.assertEqual(projected["selected_sku_ids"], self.ids(count))
            self.assertEqual(len(projected["skus"]), count)
        self.assertEqual((self.directory / "input/source.json").read_bytes(), before)

    def test_real_all_capture_missing_procurement_price_remains_selectable_and_warns(self):
        from pipeline.context import StepContext
        from pipeline.listing_draft import _validate_current_source
        from pipeline.selected_source import selected_source
        response = self.client.post("/api/collector/products", json={
            "source_url": "https://detail.1688.com/offer/987654321.html", "title_zh": "全规格无采购价离线测试",
            "collection_mode": "all_skus", "skus": self.rows[:11],
        })
        self.assertEqual(response.status_code, 200, response.text)
        product_id = response.json()["product_id"]
        directory = self.root / "products" / product_id
        base = "/api/workbench/products/" + product_id
        source = read_json(directory / "input/source.json")
        self.assertEqual(source["skus"][0]["collection_issues"], ["缺少有效采购价"])
        skus = self.client.get(base + "/skus").json()["skus"]
        self.assertEqual(skus[0]["collection_issues"], ["缺少有效采购价"])
        self.assertEqual(skus[0]["blocking_collection_issues"], [])
        self.assertEqual(self.client.post(base + "/skus", json={"include": self.ids(11)}).status_code, 200)
        self.assertEqual(len(selected_source(directory, source, require_selection=True)["skus"]), 11)
        result = _validate_current_source(StepContext(directory, "validate_source"))
        self.assertEqual(result["checks"]["sku_count"], 11)
        self.assertEqual(len(result["warnings"]), 11)
        self.assertFalse(guided_review.manual_prices_complete(directory), "procurement warning must not fabricate sale prices")
        self.assertEqual(read_json(directory / "input/source.json")["skus"][0]["collection_issues"], ["缺少有效采购价"])
        # Both text-provider adapters explicitly select the current editable
        # studio blueprint. Its planning method does not contact the transport.
        from contracts import validate_contract
        from models.base import ImagePlanRequest
        from models.fake import FakeProvider
        from models.http_provider import HttpModelProvider
        write_json(directory / "input/guided-workflow.json", {"schema_version": "1.0.0"})
        request = ImagePlanRequest(product_id=product_id, product_dir=directory, source=source,
                                   source_refs=["input/source.json"], copy_bundle={"core_keyword": "Игрушка"},
                                   extra={"studio_mode": True})
        transport = unittest.mock.Mock()
        transport.generate.side_effect = AssertionError("No paid or external transport allowed")
        for provider in (FakeProvider(), HttpModelProvider(transport)):
            plan = provider.plan_images(request)
            self.assertTrue(plan["studio_mode"])
            self.assertEqual(len(plan["main_images"]), 11)
            self.assertEqual(validate_contract("image-plan", plan), [])
        transport.generate.assert_not_called()

    def test_only_exact_procurement_issue_is_nonblocking_identity_and_unknown_errors_remain(self):
        issues = ["缺少有效采购价", "原始规格标识缺失或重复，须人工核对", "未知采集错误"]
        row = {"collection_issues": issues}
        self.assertEqual(blocking_collection_issues(row), issues[1:])
        self.assertEqual(row["collection_issues"], issues)
        source = read_json(self.directory / "input/source.json")
        source["skus"][0]["collection_issues"] = issues
        write_json(self.directory / "input/source.json", source)
        with self.assertRaisesRegex(SkuSelectionError, "原始规格标识"):
            set_selection(self.directory, include=["S1"])

    def test_guided_review_uses_hundred_limit_but_legacy_review_remains_ten(self):
        set_selection(self.directory, include=self.ids(100))
        write_json(self.directory / guided_review.REVIEW_FILE, {
            "approved": {name: {"sha256": "current"} for name in guided_review.DEPENDENCIES}})
        with patch.object(guided_review, "digest", return_value="current"), \
                patch.object(guided_review, "problems", return_value=[]), \
                patch("pipeline.guided_workflow.workflow_status", return_value={"analysis": {"confirmed": True}}), \
                patch("pipeline.listing_draft.card_ready", return_value=True):
            result = guided_review.status(self.directory)
            self.assertTrue(result["sku_ready"])
            self.assertNotIn("尚未确认要上架的 SKU", result["blockers"])
            (self.directory / "input/guided-workflow.json").unlink()
            self.assertFalse(guided_review.status(self.directory)["sku_ready"])


if __name__ == "__main__":
    unittest.main()
