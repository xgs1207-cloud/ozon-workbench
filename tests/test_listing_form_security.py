"""Binding and draft safety checks with real form logic and offline Ozon fixtures."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.category_form import load_form
from pipeline.listing_form import FORM_TRANSACTION_FILES, save_product_form, write_json
from pipeline.ozon_http import FixtureTransport, OzonClient, PATH_ATTRIBUTES, PATH_TREE


class ListingFormSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.product = self.root / "P000001"
        self.cache = self.root / "cache"
        write_json(self.product / "input/source.json", {
            "schema_version": "1.0.0", "product_id": "P000001",
            "skus": [{"sku_id": "S1", "color_ru": "красный"}, {"sku_id": "S2", "color_ru": "синий"}],
        })
        write_json(self.product / "input/category-selection.json", {
            "category_id": 1001, "type_id": 2001, "shop_id": "shop-a", "source": "ozon_seller_api",
        })
        write_json(self.product / "input/human-confirmations.json", {"material": "steel", "attributes": {}})
        tree = {"result": [{"description_category_id": 1001, "category_name": "家居", "children": [
            {"type_id": 2001, "type_name": "保温杯", "children": []}]}]}
        attrs = {"result": [
            {"id": 10, "name": "容量数字", "type": "Integer", "is_required": True},
            {"id": 11, "name": "开关", "type": "Boolean"},
            {"id": 85, "name": "品牌", "type": "String", "dictionary_id": 888},
            {"id": 15, "name": "复合参数", "type": "String", "attribute_complex_id": 20},
        ]}
        client = OzonClient(FixtureTransport({PATH_TREE: tree, PATH_ATTRIBUTES: attrs}))
        self.form = load_form(self.cache, 1001, 2001, shop_id="shop-a", client=client)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, *, shop="shop-a", category_id=1001, type_id=2001, attributes=None, by_sku=None):
        with patch("pipeline.listing_form.load_form", return_value=self.form):
            return save_product_form(self.product, self.cache, shop_id=shop, category_id=category_id,
                                     type_id=type_id, attributes=attributes or {}, per_sku_attributes=by_sku or {})

    def confirmation_bytes(self):
        return (self.product / "input/human-confirmations.json").read_bytes()

    def snapshot(self):
        return {path.relative_to(self.product).as_posix(): path.read_bytes()
                for path in self.product.rglob("*") if path.is_file()}

    def test_wrong_shop_binding_rejects_before_fetch_or_writing(self):
        before = self.confirmation_bytes()
        with patch("pipeline.listing_form.load_form") as load:
            with self.assertRaisesRegex(ValueError, "店铺与当前类目"):
                save_product_form(self.product, self.cache, shop_id="shop-b", category_id=1001,
                                  type_id=2001, attributes={"10": "1"})
            load.assert_not_called()
        self.assertEqual(self.confirmation_bytes(), before)
        self.assertFalse((self.product / "output/ozon-category.json").exists())

    def test_old_category_binding_rejects_before_network_or_write(self):
        before = self.confirmation_bytes()
        with patch("pipeline.listing_form.load_form") as load:
            with self.assertRaisesRegex(ValueError, "类目已变更"):
                save_product_form(self.product, self.cache, shop_id="shop-a", category_id=9999,
                                  type_id=2001, attributes={"10": "1"})
            load.assert_not_called()
        self.assertEqual(self.confirmation_bytes(), before)

    def test_forged_dictionary_or_complex_values_never_persist(self):
        before = self.confirmation_bytes()
        for values in ({"85": [{"value": "fake", "dictionary_value_id": 999}]}, {"15": "flattened"}):
            with self.assertRaises(ValueError):
                self.save(attributes=values)
            self.assertEqual(self.confirmation_bytes(), before)
            self.assertFalse((self.product / "output/ozon-category.json").exists())

    def test_unknown_sku_does_not_partially_save_valid_common_values(self):
        before = self.confirmation_bytes()
        with self.assertRaisesRegex(ValueError, "真实 SKU"):
            self.save(attributes={"10": "1"}, by_sku={"invented": {"10": "2"}})
        self.assertEqual(self.confirmation_bytes(), before)

    def test_false_and_zero_survive_draft_and_compilation(self):
        result = self.save(attributes={"10": [{"value": 0}], "11": [{"value": False}]})
        self.assertEqual(result["missing_required"], [])
        current = json.loads(self.confirmation_bytes())
        self.assertEqual(current["material"], "steel")
        self.assertEqual(current["attributes"]["10"][0]["value"], 0)
        self.assertIs(current["attributes"]["11"][0]["value"], False)
        values = {row["attribute_id"]: row["value"] for row in result["compiled"]["common_attributes"]}
        self.assertEqual(values[10], 0)
        self.assertIs(values[11], False)

    def test_partial_sku_required_fields_report_each_unfilled_sku(self):
        result = self.save(by_sku={"S1": {"10": "1"}})
        self.assertEqual(result["missing_by_sku"], {"S1": [], "S2": [10]})
        self.assertEqual(result["compiled"]["required_summary"]["missing_attribute_ids"], [10])

    def test_common_default_with_per_sku_override_is_lossless(self):
        result = self.save(attributes={"10": "1"}, by_sku={"S2": {"10": "2"}})
        self.assertEqual(result["compiled"]["required_summary"]["missing"], 0)
        self.assertEqual(result["compiled"]["common_attributes"][0]["value"], 1)
        self.assertEqual(result["compiled"]["attributes_by_sku"]["S2"][0]["value"], 2)

    def test_unloaded_official_dictionary_is_not_filled_with_inferred_free_text(self):
        from pipeline.attributes import compile_attributes
        snapshot = {"category_id": 1001, "type_id": 2001, "attributes": [
            {"attribute_id": 10097, "attribute_name": "颜色", "required": True,
             "dictionary_id": 888, "complex_id": None, "allowed_values": []},
            {"attribute_id": 20, "attribute_name": "容量", "required": True,
             "dictionary_id": 889, "complex_id": None, "allowed_values": []},
        ]}
        result = compile_attributes(product_id="P000001", category_snapshot=snapshot,
                                    fill_input={"skus": [{"sku_id": "S1", "color_ru": "красный", "capacity_ru": "1 л"}]})
        self.assertEqual(result["attributes_by_sku"], {})
        self.assertEqual(result["required_summary"]["missing_attribute_ids"], [10097, 20])

    def test_modern_form_compiles_only_saved_visible_values(self):
        from pipeline.attributes import compile_attributes
        snapshot = {"category_id": 1001, "type_id": 2001, "attributes": [
            {"attribute_id": 85, "attribute_name": "品牌", "required": True,
             "dictionary_id": 888, "allowed_values": [{"id": 501, "value": "Нет бренда"}]},
            {"attribute_id": 999, "attribute_name": "适用对象", "required": True,
             "dictionary_id": 889, "allowed_values": [{"id": 502, "value": "儿童"}]},
            {"attribute_id": 10097, "attribute_name": "颜色", "required": True,
             "dictionary_id": 890, "allowed_values": [{"id": 503, "value": "красный"}]},
        ]}
        result = compile_attributes(product_id="P000001", category_snapshot=snapshot,
                                    fill_input={"skus": [{"sku_id": "S1", "color_ru": "красный"}]},
                                    confirmed_only=True)
        self.assertEqual(result["common_attributes"], [])
        self.assertEqual(result["attributes_by_sku"], {})
        self.assertEqual(result["required_summary"]["missing_attribute_ids"], [85, 999, 10097])
        result = compile_attributes(product_id="P000001", category_snapshot=snapshot,
                                    fill_input={"skus": [{"sku_id": "S1"}]}, confirmed_only=True,
                                    human_attributes={"85": [{"value": "Нет бренда", "dictionary_value_id": 501}]})
        self.assertEqual(result["common_attributes"][0]["dictionary_value_id"], 501)
        self.assertEqual(result["required_summary"]["missing_attribute_ids"], [999, 10097])

    def test_submitted_product_backend_rejects_before_fetch_or_any_product_file_change(self):
        write_json(self.product / "status.json", {"api_write_count": 1})
        before = self.snapshot()
        with patch("pipeline.listing_form.load_form") as load:
            with self.assertRaisesRegex(ValueError, "已有 Ozon 写入"):
                save_product_form(self.product, self.cache, shop_id="shop-a", category_id=1001,
                                  type_id=2001, attributes={"10": "1"})
            load.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_submission_during_metadata_read_rejects_without_mutating_product_inputs(self):
        before = self.snapshot()
        def fetch(*args, **kwargs):
            write_json(self.product / "status.json", {"api_write_count": 1})
            return self.form
        with patch("pipeline.listing_form.load_form", side_effect=fetch):
            with self.assertRaisesRegex(ValueError, "已有 Ozon 写入"):
                save_product_form(self.product, self.cache, shop_id="shop-a", category_id=1001,
                                  type_id=2001, attributes={"10": "1"})
        after = self.snapshot()
        self.assertEqual(before, {path: data for path, data in after.items() if path != "status.json"})
        self.assertEqual(json.loads(after["status.json"])["api_write_count"], 1)

    def test_category_changed_during_metadata_read_does_not_restore_old_form(self):
        before = self.snapshot()
        def fetch(*args, **kwargs):
            write_json(self.product / "input/category-selection.json", {
                "category_id": 3001, "type_id": 4001, "shop_id": "shop-a", "source": "ozon_seller_api",
            })
            return self.form
        with patch("pipeline.listing_form.load_form", side_effect=fetch):
            with self.assertRaisesRegex(ValueError, "类目已变更"):
                save_product_form(self.product, self.cache, shop_id="shop-a", category_id=1001,
                                  type_id=2001, attributes={"10": "1"})
        after = self.snapshot()
        before.pop("input/category-selection.json")
        selected = json.loads(after.pop("input/category-selection.json"))
        self.assertEqual(before, after)
        self.assertEqual(selected["category_id"], 3001)

    def test_compile_failure_restores_every_existing_transaction_file(self):
        for path in FORM_TRANSACTION_FILES:
            if path == "input/human-confirmations.json":
                continue
            write_json(self.product / path, {"old": path, "api_write_count": 0})
        before = self.snapshot()
        def fail(ctx):
            ctx.write_json("output/attribute-fill-input.json", {"partial": "fill"})
            ctx.write_json("output/ozon-attributes-final.json", {"partial": "compiled"})
            raise RuntimeError("offline simulated compiler failure")
        with patch("pipeline.catalog.handle_field_completion", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "simulated compiler failure"):
                self.save(attributes={"10": "1"})
        self.assertEqual(before, self.snapshot())

    def test_compile_failure_removes_new_transaction_files_and_keeps_inputs(self):
        before = self.snapshot()
        def fail(ctx):
            ctx.write_json("output/attribute-fill-input.json", {"partial": "fill"})
            ctx.write_json("output/ozon-attributes-final.json", {"partial": "compiled"})
            raise RuntimeError("offline simulated compiler failure")
        with patch("pipeline.catalog.handle_field_completion", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.save(attributes={"10": "1"})
        self.assertEqual(before, self.snapshot())

    def test_persistence_failure_restores_previous_state_and_removes_new_schema_files(self):
        write_json(self.product / "status.json", {"api_write_count": 0, "completed_steps": ["validate_source"]})
        before = self.snapshot()
        original_write = write_json
        def fail_write(path, value):
            if path == self.product / "input/human-confirmations.json":
                raise OSError("offline simulated write failure")
            return original_write(path, value)
        with patch("pipeline.listing_form.write_json", side_effect=fail_write):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                self.save(attributes={"10": "1"})
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
