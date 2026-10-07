"""Confirmed collection facts only: suggestions never call AI or write listings."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import category_form
from pipeline.listing_autofill import build_autofill, build_basic_fields, MAX_DICTIONARY_LOOKUPS
from pipeline.listing_form import product_form, read_json, save_product_form, write_json
from pipeline.ozon_http import FixtureTransport, OzonClient, PATH_TREE, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH


TREE = {"result": [{"description_category_id": 1001, "category_name": "儿童用品", "children": [
    {"type_id": 2001, "type_name": "抗压玩具", "children": []}]}]}
FIELDS = {"result": [
    {"id": 85, "name": "品牌", "type": "String", "dictionary_id": 1, "is_required": True},
    {"id": 8229, "name": "类型", "type": "String", "dictionary_id": 2, "is_required": True},
    {"id": 9048, "name": "型号名称（针对合并为一张商品卡片）", "type": "String", "is_required": True},
    {"id": 10096, "name": "商品颜色", "type": "String", "dictionary_id": 3, "is_aspect": True},
    {"id": 10097, "name": "颜色名称", "type": "String", "is_aspect": True},
    {"id": 10, "name": "材质", "type": "String", "dictionary_id": 4},
    {"id": 11, "name": "统一计量单位中的商品数量", "type": "Integer"},
    {"id": 12, "name": "高度，厘米", "type": "Integer"},
    {"id": 13, "name": "智能手机控制", "type": "Boolean"},
    {"id": 14, "name": "元件数量", "type": "Integer"},
    {"id": 15, "name": "复合组", "type": "String", "attribute_complex_id": 9},
]}


class ListingAutofillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.product = self.root / "products/P000001"
        self.cache = self.root / "cache"
        self.source = {"product_id": "P000001", "title_zh": "爆款最佳解压玩具 某品牌 型号200g", "skus": [
            {"sku_id": "S1", "purchase_price_cny": 12}, {"sku_id": "S2", "purchase_price_cny": 15}],
            "attributes_zh": {"raw": {}}}
        write_json(self.product / "input/source.json", self.source)
        write_json(self.product / "input/category-selection.json", {"category_id": 1001, "type_id": 2001, "shop_id": "shop-a"})
        self.transport = FixtureTransport({PATH_TREE: TREE, PATH_ATTRIBUTES: FIELDS,
                                           PATH_ATTRIBUTE_VALUES: self.dictionary_response,
                                           PATH_ATTRIBUTE_VALUES_SEARCH: self.dictionary_response})
        self.client = OzonClient(self.transport)
        self.client_patch = patch.object(category_form, "_client", return_value=(self.client, "shop-a"))
        self.client_patch.start()
        self.form = category_form.load_form(self.cache, 1001, 2001, shop_id="shop-a", client=self.client)
        self.transport.calls.clear()

    def tearDown(self):
        self.client_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def dictionary_response(body):
        values = {85: [{"id": 501, "value": "Нет бренда"}, {"id": 502, "value": "真实品牌"}],
                  8229: [{"id": 2001, "value": "抗压玩具"}],
                  10096: [{"id": 601, "value": "红色"}, {"id": 602, "value": "蓝色"}],
                  10: [{"id": 701, "value": "棉"}, {"id": 702, "value": "硅胶"}]}
        return {"result": values.get(body["attribute_id"], []), "has_next": False}

    def update(self, *, raw=None, attrs=None, skus=None, **extra):
        if raw is not None:
            self.source["attributes_zh"] = {"raw": raw}
        if attrs is not None:
            self.source["attributes_zh"] = attrs
        if skus is not None:
            self.source["skus"] = skus
        self.source.update(extra)
        write_json(self.product / "input/source.json", self.source)

    def receipts(self, *ids):
        for attribute_id in ids:
            category_form.dictionary_values(self.cache, 1001, 2001, attribute_id, shop_id="shop-a", client=self.client)
        self.transport.calls.clear()

    def build(self, **kwargs):
        return build_autofill(self.product, self.cache, shop_id="shop-a", **kwargs)

    def test_get_is_cached_only_and_source_identity_is_not_barcode_or_model(self):
        before = {str(path.relative_to(self.product)): path.read_bytes() for path in self.product.rglob("*") if path.is_file()}
        result = self.build()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(result["lookup_count"], 0)
        self.assertFalse(result["api_writes_performed"])
        self.assertEqual(result["attributes"], {})
        self.assertEqual(result["seller_offer_ids"]["S1"]["value"], "P000001-S1")
        self.assertEqual(result["seller_offer_ids"]["S1"]["status"], "internal_generated")
        self.assertNotIn("barcode", result["basic_fields"])
        self.assertNotIn("9048", result["attributes"])
        self.assertNotIn("sale_price", result["basic_fields"])
        after = {str(path.relative_to(self.product)): path.read_bytes() for path in self.product.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_before_category_can_fill_basic_facts(self):
        (self.product / "input/category-selection.json").unlink()
        self.update(raw={"材质": "硅胶", "包装数量": "2个"})
        result = self.build()
        self.assertEqual(result["basic_fields"]["material"]["value"], "硅胶")
        self.assertEqual(result["basic_fields"]["package_quantity"]["value"], 2)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(result["attributes"], {})

    def test_missing_or_stale_metadata_never_triggers_network_on_get_or_post(self):
        target = self.cache / f"ozon-category-form-{self.form['scope']}.json"
        os.utime(target, (time.time() - 90000, time.time() - 90000))
        self.update(raw={"材质": "硅胶"})
        result = self.build(resolve_dictionaries=True)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(result["attributes"], {})
        self.assertEqual(result["basic_fields"]["material"]["value"], "硅胶")

    def test_foreign_requested_shop_does_not_use_bound_cache(self):
        with self.assertRaisesRegex(ValueError, "店铺"):
            build_autofill(self.product, self.cache, shop_id="shop-b")

    def test_unavailable_shop_does_not_hide_independently_confirmed_basics(self):
        self.update(raw={"材质": "硅胶"})
        with patch.object(category_form, "_client", side_effect=ValueError("没有启用店铺")):
            result = self.build()
        self.assertEqual(result["basic_fields"]["material"]["value"], "硅胶")
        self.assertEqual(result["attributes"], {})
        self.assertEqual(self.transport.calls, [])

    def test_snapshot_plugin_name_cn_value_cn_and_duplicates_are_supported(self):
        write_json(self.product / "input/raw-snapshot.json", {"raw": {"product_attributes": [
            {"name_cn": "材质", "value_cn": "硅胶"}, {"name_cn": "材质", "value_cn": "硅胶"},
            {"name_cn": "品牌", "value_cn": "有可授权自有品牌否"}, {"name_cn": "重量", "value_cn": "200g"}]}})
        result = build_basic_fields(self.product)
        self.assertEqual(result["material"]["value"], "硅胶")
        self.assertEqual(result["product_weight_g"]["value"], 200)
        self.assertIn("raw-snapshot", result["material"]["evidence"])
        self.assertNotIn("package_weight_g", result)

    def test_conflicting_raw_attribute_rows_are_not_confirmed(self):
        write_json(self.product / "input/raw-snapshot.json", {"raw": {"product_attributes": [
            {"name_cn": "材质", "value_cn": "硅胶"}, {"name_cn": "材质", "value_cn": "棉"}]}})
        self.assertNotIn("material", build_basic_fields(self.product))
        self.receipts(10)
        self.assertNotIn("10", self.build()["attributes"])

    def test_carton_and_fabric_weight_are_not_retail_pack_or_product_weight(self):
        self.update(attrs={"raw": {"装箱数量": "200", "克重": "120g", "毛重": "4kg"},
                           "package_quantity": 200, "weight_g": "120g"})
        result = build_basic_fields(self.product)
        self.assertNotIn("package_quantity", result)
        self.assertNotIn("product_weight_g", result)
        self.assertNotIn("package_weight_g", result)

    def test_unitless_range_fraction_and_mixed_units_require_manual_confirmation(self):
        for raw in ({"重量": "200"}, {"重量": "100-200g"}, {"产品长度": "1.25mm"},
                    {"产品长度mm": "12cm"}, {"包装数量": "10-20"}):
            self.update(raw=raw)
            self.assertEqual(build_basic_fields(self.product), {})

    def test_exact_unit_conversion_and_product_package_scope(self):
        self.update(raw={"产品长度": "12.5cm", "产品重量": "0.2kg", "包装长度": "140mm", "含包装重量": "0.23kg"})
        result = build_basic_fields(self.product)
        self.assertEqual(result["product_length_mm"]["value"], 125)
        self.assertEqual(result["product_weight_g"]["value"], 200)
        self.assertEqual(result["package_length_mm"]["value"], 140)
        self.assertEqual(result["package_weight_g"]["value"], 230)
        self.assertNotIn("package_width_mm", result)

    def test_combined_explicit_axes_fill_only_known_dimensions(self):
        self.update(raw={"尺寸": "高10*宽7cm"})
        result = build_basic_fields(self.product)
        self.assertEqual(result["product_height_mm"]["value"], 100)
        self.assertEqual(result["product_width_mm"]["value"], 70)
        self.assertNotIn("product_length_mm", result)
        self.assertFalse(any(key.startswith("package_") for key in result))
        self.update(raw={"尺寸": "10*7*5cm"})
        self.assertEqual(build_basic_fields(self.product), {})

    def test_named_dimension_blocks_convert_without_guessing_unit(self):
        self.update(product_dimensions={"length": 12, "width": 7, "unit": "cm"},
                    package_dimensions_mm={"length": 140, "weight_g": 230})
        result = build_basic_fields(self.product)
        self.assertEqual(result["product_length_mm"]["value"], 120)
        self.assertEqual(result["product_width_mm"]["value"], 70)
        self.assertEqual(result["package_length_mm"]["value"], 140)
        self.assertEqual(result["package_weight_g"]["value"], 230)

    def test_variant_conflicting_long_spec_blocks_common_weight_without_inventing_sku_values(self):
        self.update(raw={"重量": "200g"}, skus=[
            {"sku_id": "S1", "sku_name": "南瓜100g红色", "option_values": [{"name_cn": "规格1", "value_cn": "南瓜100g红色"}]},
            {"sku_id": "S2", "sku_name": "南瓜180克蓝色", "option_values": [{"name_cn": "规格1", "value_cn": "南瓜180克蓝色"}]}])
        self.receipts(10096)
        result = self.build()
        self.assertNotIn("product_weight_g", result["basic_fields"])
        self.assertNotIn("10096", result["attributes"])
        self.assertEqual(result["per_sku_attributes"], {})

    def test_unlabeled_variant_size_does_not_become_common_known_axis(self):
        self.update(raw={"产品高度": "10cm"}, skus=[
            {"sku_id": "S1", "option_values": [{"name_cn": "规格1", "value_cn": "小号10cm"}]},
            {"sku_id": "S2", "option_values": [{"name_cn": "规格1", "value_cn": "大号15cm"}]}])
        self.assertNotIn("product_height_mm", build_basic_fields(self.product))
        self.assertNotIn("12", self.build()["attributes"])

    def test_material_list_stays_confirmed_text_in_basic_editor(self):
        self.update(attrs={"material": ["棉", "硅胶"]})
        self.assertEqual(build_basic_fields(self.product)["material"]["value"], "棉、硅胶")

    def test_explicit_sku_options_remain_independent_only_for_selected_real_skus(self):
        self.update(skus=[{"sku_id": "S1", "option_values": [{"name_cn": "颜色", "value_cn": "红色"}]},
                           {"sku_id": "S2", "option_values": [{"name_cn": "颜色", "value_cn": "蓝色"}]},
                           {"sku_id": "S3", "option_values": [{"name_cn": "颜色", "value_cn": "红色"}]}])
        write_json(self.product / "input/selected-skus.json", {"selected": ["S1", "S2", "foreign"]})
        self.receipts(10096)
        result = self.build()
        self.assertNotIn("10096", result["attributes"])
        self.assertEqual(set(result["per_sku_attributes"]), {"S1", "S2"})
        self.assertEqual(result["per_sku_attributes"]["S1"]["10096"][0]["dictionary_value_id"], 601)
        self.assertEqual(result["per_sku_attributes"]["S2"]["10096"][0]["dictionary_value_id"], 602)

    def test_false_and_zero_are_confirmed_exact_values(self):
        self.update(raw={"智能手机控制": False, "元件数量": 0})
        result = self.build()
        self.assertEqual(result["attributes"]["13"][0]["value"], False)
        self.assertEqual(result["attributes"]["14"][0]["value"], 0)

    def test_manual_common_and_sku_values_including_explicit_clear_are_preserved(self):
        self.update(raw={"智能手机控制": True, "元件数量": 5, "材质": "棉"})
        write_json(self.product / "input/human-confirmations.json", {
            "attributes": {"13": [{"value": False}], "14": [{"value": 0}]}, "sku_attributes": {"S1": {"10": []}}})
        self.receipts(10)
        result = self.build()
        self.assertNotIn("13", result["attributes"])
        self.assertNotIn("14", result["attributes"])
        self.assertNotIn("10", result["attributes"])
        self.assertNotIn("S1", result["per_sku_attributes"])
        self.assertEqual(result["per_sku_attributes"]["S2"]["10"][0]["value"], "棉")

    def test_listing_details_manual_null_and_partial_values_block_suggestions(self):
        self.update(raw={"材质": "硅胶", "包装数量": "2", "产品长度": "12cm"})
        write_json(self.product / "input/listing-details.json", {"details": {"material": None, "product_length_mm": 130}})
        result = self.build()
        self.assertNotIn("material", result["basic_fields"])
        self.assertNotIn("product_length_mm", result["basic_fields"])
        self.assertEqual(result["basic_fields"]["package_quantity"]["value"], 2)
        self.assertEqual(build_basic_fields(self.product)["material"]["value"], "硅胶")

    def test_type_uses_verified_type_id_and_no_brand_is_not_a_default(self):
        self.receipts(8229, 85)
        self.update(raw={"是否有自有品牌": "否"})
        result = self.build()
        self.assertEqual(result["attributes"]["8229"][0], {"value": "抗压玩具", "dictionary_value_id": 2001})
        self.assertNotIn("85", result["attributes"])
        self.assertEqual(result["provenance"]["attributes"]["8229"]["source"], "ozon_selected_category")
        self.update(raw={"品牌": "无品牌"})
        self.assertEqual(self.build()["attributes"]["85"][0]["dictionary_value_id"], 501)

    def test_dictionary_requires_unique_exact_match_not_fuzzy_or_purity_inference(self):
        self.update(raw={"材质": "棉"})
        self.receipts(10)
        target = category_form._receipt_target(self.cache, self.form, 10)
        receipt = read_json(target)
        receipt["values"] = {"701": {"value": "100%棉", "observed_at": time.time()}}
        write_json(target, receipt)
        self.assertNotIn("10", self.build()["attributes"])
        receipt["values"] = {"701": {"value": "棉", "observed_at": time.time()}, "702": {"value": "cotton", "observed_at": time.time()}}
        write_json(target, receipt)
        self.assertNotIn("10", self.build()["attributes"])

    def test_expired_foreign_scope_and_wrong_dictionary_receipts_are_not_used(self):
        self.update(raw={"材质": "棉"})
        for changed in ({"scope": "foreign"}, {"dictionary_id": 99},
                        {"values": {"701": {"value": "棉", "observed_at": time.time() - 90000}}}):
            target = category_form._receipt_target(self.cache, self.form, 10)
            receipt = {"scope": self.form["scope"], "dictionary_id": 4,
                       "values": {"701": {"value": "棉", "observed_at": time.time()}}}
            receipt.update(changed)
            write_json(target, receipt)
            self.assertNotIn("10", self.build()["attributes"])

    def test_post_resolves_bounded_dictionary_reads_and_reuses_cache(self):
        self.update(raw={"材质": "硅胶", "品牌": "真实品牌"}, skus=[
            {"sku_id": "S1", "option_values": [{"name_cn": "颜色", "value_cn": "红"}]},
            {"sku_id": "S2", "option_values": [{"name_cn": "颜色", "value_cn": "蓝色"}]}])
        first = self.build(resolve_dictionaries=True)
        self.assertLessEqual(first["lookup_count"], MAX_DICTIONARY_LOOKUPS)
        self.assertTrue(first["attributes"].get("10"))
        self.assertTrue(first["per_sku_attributes"]["S1"].get("10096"))
        self.assertTrue(all(call["path"] in {PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH} for call in self.transport.calls))
        before = len(self.transport.calls)
        second = self.build(resolve_dictionaries=True)
        self.assertEqual(second["lookup_count"], 0)
        self.assertEqual(len(self.transport.calls), before)

    def test_six_lookup_budget_even_many_distinct_sku_queries(self):
        self.update(skus=[{"sku_id": f"S{index}", "option_values": [{"name_cn": "颜色", "value_cn": f"未知色{index}"}]}
                          for index in range(20)])
        result = self.build(resolve_dictionaries=True)
        self.assertEqual(result["lookup_count"], MAX_DICTIONARY_LOOKUPS)
        self.assertEqual(len(self.transport.calls), MAX_DICTIONARY_LOOKUPS)

    def test_dimensions_match_explicit_official_unit_and_unknown_values_remain_blank(self):
        self.update(raw={"产品高度": "100mm", "型号": "/", "复合组": "不扁平提交"})
        result = self.build()
        self.assertEqual(result["attributes"]["12"][0]["value"], 10)
        self.assertNotIn("9048", result["attributes"])
        self.assertNotIn("15", result["attributes"])

    def test_manual_model_is_not_overwritten_by_collected_model(self):
        self.update(raw={"型号": "M-100"})
        write_json(self.product / "input/human-confirmations.json", {"attributes": {"9048": []}})
        self.assertNotIn("9048", self.build()["attributes"])

    def save(self, attributes, provenance=None, by_sku=None):
        with patch("pipeline.catalog.handle_field_completion"), patch("pipeline.guided_review.invalidate_from"):
            return save_product_form(self.product, self.cache, shop_id="shop-a", category_id=1001,
                                     type_id=2001, attributes=attributes, per_sku_attributes=by_sku,
                                     provenance=provenance)

    def test_save_rederives_source_provenance_and_returns_it_after_reload(self):
        self.update(raw={"型号": "M-100", "智能手机控制": False})
        proposed = self.build()
        result = self.save(proposed["attributes"], {
            "attributes": proposed["provenance"]["attributes"], "per_sku_attributes": {}})
        self.assertEqual(result["provenance"]["attributes"]["9048"]["source"], "collected_product")
        readback = product_form(self.product, self.cache, shop_id="shop-a")
        self.assertEqual(readback["provenance"], result["provenance"])
        self.assertNotIn("9048", self.build()["attributes"])
        again = self.save(result["attributes"])
        self.assertEqual(again["provenance"]["attributes"]["9048"]["source"], "collected_product")

    def test_forged_source_provenance_cannot_mark_manual_value_confirmed(self):
        self.update(raw={"型号": "M-100"})
        with self.assertRaisesRegex(ValueError, "采集来源"):
            self.save({"9048": [{"value": "invented"}]}, {"attributes": {
                "9048": {"source": "collected_product", "evidence": "fake"}}})
        self.assertFalse((self.product / "input/human-confirmations.json").exists())
        manual = self.save({"9048": [{"value": "manual"}]})
        self.assertEqual(manual["provenance"]["attributes"]["9048"]["source"], "manual")

    def test_editing_previous_source_value_degrades_to_manual(self):
        self.update(raw={"型号": "M-100"})
        result = self.build()
        self.save(result["attributes"], {"attributes": result["provenance"]["attributes"]})
        edited = self.save({"9048": [{"value": "M-200"}]})
        self.assertEqual(edited["provenance"]["attributes"]["9048"]["source"], "manual")

    def test_common_source_provenance_can_save_as_individual_sku_while_other_is_cleared(self):
        self.update(raw={"材质": "棉"})
        write_json(self.product / "input/human-confirmations.json", {"sku_attributes": {"S1": {"10": []}}})
        self.receipts(10)
        result = self.build()
        saved = self.save({}, {"per_sku_attributes": result["provenance"]["per_sku_attributes"]},
                          {"S1": {"10": []}, **result["per_sku_attributes"]})
        self.assertEqual(saved["per_sku_attributes"]["S1"]["10"], [])
        self.assertEqual(saved["provenance"]["per_sku_attributes"]["S2"]["10"]["source"], "collected_product")


if __name__ == "__main__":
    unittest.main()
