"""Read-only unified projections, precise title cleanup, and shipping dimensions."""
from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.listing_document import (clean_missing_color_suffix, read_listing_document,
                                       variant_color, variant_title)
from pipeline.listing_form import read_json, write_json
from pipeline.listing_offer_ids import reserve_offer_ids
from pipeline.product_editor import save_listing_details
from pipeline.measurements import collect_measurements


class ListingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.product = self.root / "products/P000001"
        self.database = self.root / "runtime/offers.sqlite3"
        write_json(self.product / "input/source.json", {"product_id": "P000001", "title_zh": "商品", "skus": [{"sku_id": "S1", "name": "规格一"}, {"sku_id": "S2", "name": "规格二"}]})
        write_json(self.product / "input/selected-skus.json", {"selected": ["S1"]})
        write_json(self.product / "input/category-selection.json", {"shop_id": "shop-a", "category_id": 1, "type_id": 2})
        self.form = {"source": "ozon_seller_api", "shop_id": "shop-a", "category_id": 1, "type_id": 2,
                     "fields": [{"attribute_id": 85, "name": "品牌", "required": True},
                                {"attribute_id": 9048, "name": "型号名称", "required": True},
                                {"attribute_id": 4191, "name": "简介", "required": False},
                                {"attribute_id": 23171, "name": "#主题标签", "required": False},
                                {"attribute_id": 77, "name": "真实其他必填", "required": True}]}
        write_json(self.product / "input/category-form.json", self.form)
        self.workflow = {"selected_sku_ids": ["S1"], "analysis": {"confirmed": True, "status": "confirmed", "fingerprint": "analysis-current", "payload": {"facts": {"materials": []}}},
                         "copy": {"confirmed": True, "selected": True, "status": "confirmed", "fingerprint": "copy-current", "payload": {"title_ru": "Игрушка — не указан", "description_ru": "Описание игрушки", "hashtags": ["#игрушка"]}}}
        self.workflow_patch = patch("pipeline.guided_workflow.workflow_status", return_value=self.workflow)
        self.workflow_patch.start()

    def tearDown(self):
        self.workflow_patch.stop()
        self.tmp.cleanup()

    def read(self):
        return read_listing_document(self.product, shop="shop-a", offer_db_path=self.database)

    def test_get_only_reads_local_files_no_rpc_or_number_allocation(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch("pipeline.category_form.load_form", side_effect=AssertionError("no RPC")), patch("pipeline.listing_offer_ids.reserve_offer_ids", side_effect=AssertionError("no allocation")):
            result = self.read()
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.database.exists())
        self.assertEqual(result["api_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["missing"]["offer_ids"], ["S1"])

    def test_same_selected_copy_fields_used_by_document_card_and_variant(self):
        result = self.read()
        self.assertEqual(result["copy"]["title_ru"], "Игрушка")
        self.assertEqual(result["copy"]["title_ru"], result["card"]["title_ru"])
        self.assertEqual(result["selected_skus"][0]["title_ru"], result["card"]["title_ru"])
        self.assertEqual(result["copy"]["description_ru"], result["operational_fields"]["description_ru"])
        self.assertEqual(result["copy"]["hashtags"], result["card"]["hashtags"])
        self.assertEqual(len(result["selected_skus"]), 1)

    def test_reserved_offer_in_document_and_canonical_helper_same(self):
        result = reserve_offer_ids(self.product, "shop-a", "employee", db_path=self.database,
                                   now=datetime(2026, 10, 8, 5, tzinfo=timezone.utc))
        document = self.read()
        self.assertEqual(document["selected_skus"][0]["offer_id"], result["offers"]["S1"])
        self.assertEqual(document["operational_fields"]["offer_ids"], result["offers"])

    def test_official_missing_and_duplicates_are_distinct(self):
        result = self.read()
        self.assertEqual(result["official_excluded_attribute_ids"], [85, 4191, 23171])
        self.assertNotIn(9048, result["official_excluded_attribute_ids"], "model name has no duplicate operational control")
        self.assertEqual(result["missing"]["required_attribute_ids"], [77, 85, 9048])
        self.assertTrue(any(row["name"] == "真实其他必填" for row in result["missing"]["required_attributes"]))
        self.assertTrue(result["operational_fields"]["product_optional"])
        self.assertEqual(len(result["missing"]["operational"]), 5)  # shipping 4 + price, not item 4

    def test_image_observations_remain_advisory_not_promoted_to_facts(self):
        with patch("pipeline.image_insights.read_image_insights", return_value={"status": "ready", "payload": {"visible_observations": [{"description_zh": "看上去像硅胶"}]}, "warning_zh": "需要核实"}):
            result = self.read()
        self.assertTrue(result["image_suggestions"]["advisory_only"])
        self.assertFalse(result["image_suggestions"]["automatic_fact_updates"])
        self.assertFalse(any("硅胶" in str(row) for row in result["facts"]))

    def test_foreign_shop_or_old_category_never_reuses_wrong_official_form(self):
        with self.assertRaisesRegex(ValueError, "店铺"):
            read_listing_document(self.product, shop="another", offer_db_path=self.database)
        old = self.form | {"type_id": 9}
        write_json(self.product / "input/category-form.json", old)
        self.assertTrue(self.read()["missing"]["official_form_missing"])
        self.assertIsNone(self.read()["card"]["form"])

    def test_precise_missing_suffix_cleanup_retains_real_product_words(self):
        for suffix in (" — 未指定", " — не указан", " — НЕ УКАЗАНО", " - not specified"):
            self.assertEqual(clean_missing_color_suffix("Игрушка" + suffix), "Игрушка")
        for name in ("Не указан", "Игрушка Не указан", "游戏未指定的世界", "Игрушка — красный", 'Игра «Не указан»'):
            self.assertEqual(clean_missing_color_suffix(name), name)
        self.assertEqual(clean_missing_color_suffix("Игрушка — не указан", has_real_color=True), "Игрушка")

    def test_real_color_and_explicit_name_not_removed(self):
        self.assertEqual(variant_title("Игрушка", {"color_ru": "красный"}), "Игрушка — красный")
        self.assertEqual(variant_title("Товар — не указан", {"color_ru": "розовый"}), "Товар — розовый")
        self.assertEqual(variant_title("Товар — 未指定", {"color_ru": "розовый"}), "Товар — розовый")
        self.assertEqual(variant_title("Игрушка", {"name_ru": "Настоящее название"}), "Настоящее название")
        self.assertEqual(variant_title("Игрушка", {"name_ru": "Игрушка — не указан"}), "Игрушка")
        self.assertEqual(variant_color({}, [{"attribute_id": 10096, "attribute_name": "商品颜色", "value": "Красный"}]), "Красный")

    def test_shipping_only_save_keeps_item_unknown_and_compiles_independent_package(self):
        fields = {"package_length_mm": 100, "package_width_mm": 80, "package_height_mm": 60, "package_weight_g": 120}
        saved = save_listing_details(self.product, fields)
        self.assertEqual(saved["details"], fields)
        overrides = read_json(self.product / "input/workbench-sku-overrides.json")
        result = collect_measurements(product_id=self.product.name, source=read_json(self.product / "input/source.json"), overrides=overrides, product_dir=self.product)
        self.assertIsNone(result["product"])
        self.assertEqual(result["package"]["weight_g"], 120)
        document = self.read()
        self.assertEqual(document["operational_fields"]["package"]["weight_g"], 120)
        self.assertTrue(all(value is None for value in document["operational_fields"]["product"].values()))
        self.assertFalse(any(row["key"].startswith("package") for row in document["missing"]["operational"]))

    def test_known_partial_item_axis_still_must_not_exceed_package(self):
        overrides = {"product": {"product_weight_g": 140, "package_weight_g": 120}}
        result = collect_measurements(product_id=self.product.name, source=read_json(self.product / "input/source.json"), overrides=overrides, product_dir=self.product)
        self.assertFalse(result["hierarchy_ok"])
        with self.assertRaisesRegex(ValueError, "包装"):
            save_listing_details(self.product, overrides["product"])


if __name__ == "__main__":
    unittest.main()
