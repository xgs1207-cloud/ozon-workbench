"""User defaults are local policy, never newly verified supplier facts."""
from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import category_form
from pipeline.listing_autofill import build_autofill, source_attribute_candidates
from pipeline.listing_defaults import (apply_user_defaults, ensure_defaults_state,
                                      field_display_metadata, persist_user_defaults)
from pipeline.listing_form import product_form, read_json, save_product_form, write_json
from pipeline.ozon_http import FixtureTransport, OzonClient, PATH_TREE, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH


TREE = {"result": [{"description_category_id": 1001, "category_name": "儿童用品", "children": [
    {"type_id": 2001, "type_name": "抗压玩具", "children": []}]}]}
FIELDS = {"result": [
    {"id": 85, "name": "品牌", "type": "String", "dictionary_id": 1, "is_required": True},
    {"id": 9048, "name": "型号名称（针对合并为一张商品卡片）", "type": "String", "is_required": True},
    {"id": 200, "name": "原产国", "type": "String", "dictionary_id": 2},
    {"id": 201, "name": "签名18+", "type": "Boolean"},
    {"id": 202, "name": "原厂包装数量", "type": "Integer"},
    {"id": 203, "name": "统一计量单位中的商品数量", "type": "Integer"},
    {"id": 204, "name": "标记代码", "type": "String"},
    {"id": 205, "name": "保证", "type": "String"},
    {"id": 206, "name": "卖家代码", "type": "String"},
    {"id": 207, "name": "组合成类似的产品", "type": "String"},
    {"id": 208, "name": "欧亚经济联盟的HS编码", "type": "String"},
    {"id": 209, "name": "保质期", "type": "Integer"},
    {"id": 23171, "name": "#主题标签", "type": "String"},
]}


class ListingDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.product = self.root / "products/P000001"
        self.cache = self.root / "cache"
        self.source = {"product_id": "P000001", "title_zh": "玩具", "skus": [
            {"sku_id": "S1"}, {"sku_id": "S2"}], "attributes_zh": {"raw": {}}}
        write_json(self.product / "input/source.json", self.source)
        write_json(self.product / "input/category-selection.json", {"category_id": 1001, "type_id": 2001, "shop_id": "shop-a"})
        self.transport = FixtureTransport({PATH_TREE: TREE, PATH_ATTRIBUTES: deepcopy(FIELDS),
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
        return {"result": {85: [{"id": 501, "value": "Нет бренда"}, {"id": 502, "value": "真实品牌"}],
                           200: [{"id": 601, "value": "中国"}, {"id": 602, "value": "俄罗斯"}]}.get(body["attribute_id"], []), "has_next": False}

    def seed(self, *ids):
        for key in ids:
            category_form.dictionary_values(self.cache, 1001, 2001, key, shop_id="shop-a", client=self.client)
        self.transport.calls.clear()

    def defaults(self, **kwargs):
        return build_autofill(self.product, self.cache, shop_id="shop-a", include_defaults=True, **kwargs)

    def update(self, raw):
        self.source["attributes_zh"] = {"raw": raw}
        write_json(self.product / "input/source.json", self.source)

    def save(self, attributes, provenance=None, per_sku=None):
        with patch("pipeline.catalog.handle_field_completion"), patch("pipeline.guided_review.invalidate_from"):
            return save_product_form(self.product, self.cache, shop_id="shop-a", category_id=1001, type_id=2001,
                                     attributes=attributes, per_sku_attributes=per_sku, provenance=provenance)

    def test_get_defaults_does_not_create_model_file_or_fetch_dictionary(self):
        before = {str(path.relative_to(self.product)): path.read_bytes() for path in self.product.rglob("*") if path.is_file()}
        value = self.defaults()
        self.assertNotIn("9048", value["attributes"])
        self.assertNotIn("85", value["attributes"])
        self.assertNotIn("200", value["attributes"])
        self.assertEqual(value["attributes"]["201"][0]["value"], False)
        self.assertEqual(value["attributes"]["202"][0]["value"], 1)
        self.assertEqual(self.transport.calls, [])
        after = {str(path.relative_to(self.product)): path.read_bytes() for path in self.product.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_model_random_once_shared_by_variants_and_stable_after_reload(self):
        first = ensure_defaults_state(self.product)
        self.assertRegex(first["model_name"], r"^WB-[A-F0-9]{12}$")
        self.assertEqual(first, ensure_defaults_state(self.product))
        result = self.defaults()
        self.assertEqual(result["attributes"]["9048"][0]["value"], first["model_name"])
        self.assertFalse(any("9048" in values for values in result["per_sku_attributes"].values()))
        second_product = self.root / "products/P000002"
        other = ensure_defaults_state(second_product)
        self.assertNotEqual(first["model_name"], other["model_name"])

    def test_supplier_models_do_not_split_requested_shared_merge_name(self):
        self.source["skus"][0]["model"] = "Factory-A"
        self.source["skus"][1]["model"] = "Factory-B"
        write_json(self.product / "input/source.json", self.source)
        name = ensure_defaults_state(self.product)["model_name"]
        result = self.defaults()
        self.assertEqual(result["attributes"]["9048"][0]["value"], name)
        self.assertFalse(any("9048" in values for values in result["per_sku_attributes"].values()))

    def test_only_unique_current_official_dictionary_values_used(self):
        self.seed(85, 200)
        result = self.defaults()
        self.assertEqual(result["attributes"]["85"], [{"value": "Нет бренда", "dictionary_value_id": 501}])
        self.assertEqual(result["attributes"]["200"], [{"value": "中国", "dictionary_value_id": 601}])
        self.assertEqual(result["provenance"]["attributes"]["85"]["source"], "user_requested_default")
        self.assertEqual(self.transport.calls, [])

    def test_default_dictionary_reads_bounded_and_reused(self):
        result = self.defaults(resolve_dictionaries=True)
        self.assertEqual(result["lookup_count"], 2)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(all(call["path"] == PATH_ATTRIBUTE_VALUES_SEARCH for call in self.transport.calls))
        self.assertEqual(self.defaults(resolve_dictionaries=True)["lookup_count"], 0)

    def test_ambiguous_and_missing_no_brand_option_never_guessed(self):
        self.seed(85)
        target = category_form._receipt_target(self.cache, self.form, 85)
        receipt = read_json(target)
        receipt["values"]["503"] = {**receipt["values"]["501"], "value": "无品牌"}
        write_json(target, receipt)
        self.assertNotIn("85", self.defaults()["attributes"])
        receipt["values"] = {"502": receipt["values"]["502"]}
        write_json(target, receipt)
        self.assertNotIn("85", self.defaults()["attributes"])

    def test_captured_real_brand_country_and_adult_flags_not_overwritten(self):
        self.seed(85, 200)
        self.update({"品牌": "真实品牌", "原产国": "俄罗斯", "签名18+": True, "原厂包装数量": 2})
        value = self.defaults()
        self.assertEqual(value["attributes"]["85"][0]["dictionary_value_id"], 502)
        self.assertEqual(value["attributes"]["200"][0]["dictionary_value_id"], 602)
        self.assertEqual(value["attributes"]["201"][0]["value"], True)
        self.assertEqual(value["attributes"]["202"][0]["value"], 2)
        self.assertEqual(value["field_display"]["85"]["display"], "attention")
        self.assertEqual({row["attribute_id"] for row in value["unresolved"] if row.get("source") == "user_requested_default"}, {85, 200, 201, 202, 9048})
        self.assertEqual(product_form(self.product, self.cache, shop_id="shop-a")["field_display"]["85"]["display"], "attention")

    def test_saved_real_brand_not_hidden_by_no_brand_display_policy(self):
        self.seed(85)
        self.save({"85": [{"dictionary_value_id": 502}]})
        self.assertEqual(product_form(self.product, self.cache, shop_id="shop-a")["field_display"]["85"]["display"], "attention")
        self.assertEqual(self.defaults()["field_display"]["85"]["display"], "attention")

    def test_optional_blank_policy_suppresses_collected_proposals_but_not_saved_values(self):
        self.update({"统一计量单位中的商品数量": 4, "标记代码": "123", "保证": "一年", "卖家代码": "abc",
                     "组合成类似的产品": "merge", "欧亚经济联盟的HS编码": "12", "保质期": 90})
        for key in range(203, 210):
            self.assertNotIn(str(key), self.defaults()["attributes"])
        saved = self.save({"206": [{"value": "manual"}], "203": []})
        ensure_defaults_state(self.product)
        with patch("pipeline.catalog.handle_field_completion"), patch("pipeline.guided_review.invalidate_from"):
            result = persist_user_defaults(self.product, self.cache, shop_id="shop-a")
        self.assertEqual(result["attributes"]["206"], saved["attributes"]["206"])
        self.assertEqual(result["attributes"]["203"], [])
        self.assertEqual(result["defaults"]["seller_offer_ids"]["S1"]["value"], "P000001-S1")

    def test_manual_common_overrides_and_clears_preserved(self):
        self.seed(85, 200)
        ensure_defaults_state(self.product)
        self.save({"85": [], "9048": [{"value": "MY-GROUP"}], "200": [{"dictionary_value_id": 602}],
                   "201": [{"value": True}], "202": []})
        suggestions = self.defaults()
        for key in ("85", "9048", "200", "201", "202"):
            self.assertNotIn(key, suggestions["attributes"])

    def test_individual_clear_is_not_undone_by_new_common_default(self):
        ensure_defaults_state(self.product)
        self.save({}, per_sku={"S1": {"9048": []}})
        result = self.defaults()
        self.assertNotIn("9048", result["attributes"])
        self.assertNotIn("9048", result["per_sku_attributes"].get("S1", {}))
        self.assertEqual(result["per_sku_attributes"]["S2"]["9048"][0]["value"], ensure_defaults_state(self.product)["model_name"])

    def test_required_blank_field_remains_required_not_bypassed(self):
        changed = deepcopy(FIELDS)
        next(row for row in changed["result"] if row["id"] == 205)["is_required"] = True
        self.transport.fixtures[PATH_ATTRIBUTES] = changed
        self.form = category_form.load_form(self.cache, 1001, 2001, shop_id="shop-a", refresh=True, client=self.client)
        result = product_form(self.product, self.cache, shop_id="shop-a")
        self.assertIn(205, result["missing_required"])
        self.assertTrue(result["field_display"]["205"]["required"])
        self.assertNotIn("205", self.defaults()["attributes"])

    def test_display_metadata_does_not_change_official_schema(self):
        before = deepcopy(self.form)
        meta = field_display_metadata(self.form)
        self.assertEqual(self.form, before)
        self.assertEqual(meta["85"]["display"], "hidden")
        self.assertEqual(meta["203"]["display"], "hidden")
        self.assertEqual(meta["9048"]["display"], "hidden")
        self.assertEqual(meta["23171"]["display"], "standard")
        self.assertFalse(meta["23171"]["hide_when_default"])
        self.assertTrue(meta["203"]["hide_when_blank"])
        self.assertTrue(meta["9048"]["hide_when_default"])

    def test_live_schema_boolean_marking_alias_stays_blank(self):
        form = {"fields": [{"attribute_id": 23536, "name": "需要标记代码", "required": False, "control": "boolean"}]}
        meta = field_display_metadata(form)
        self.assertEqual(meta["23536"]["default_policy"], "leave_blank")
        self.assertEqual(meta["23536"]["display"], "hidden")

    def test_legacy_scalar_brand_display_metadata_is_supported(self):
        self.assertEqual(field_display_metadata(self.form, attributes={"85": "真实品牌"})["85"]["display"], "attention")
        # A legacy dictionary placeholder is repairable, not a confirmed no-brand value.
        self.assertEqual(field_display_metadata(self.form, attributes={"85": "无品牌"})["85"]["display"], "attention")

    def test_valid_saved_defaults_all_have_hidden_policy_and_keep_payload_values(self):
        result = persist_user_defaults(self.product, self.cache, shop_id="shop-a")
        loaded = product_form(self.product, self.cache, shop_id="shop-a")
        for key in ("85", "9048", "200", "201", "202"):
            self.assertEqual(loaded["field_display"][key]["display"], "hidden", key)
            self.assertTrue(loaded["field_display"][key]["hide_when_default"])
            self.assertEqual(result["attributes"][key], loaded["attributes"][key])
        self.assertEqual(loaded["attributes"]["201"][0]["value"], False)
        self.assertEqual(loaded["field_display"]["9048"]["default_match_values"], [result["attributes"]["9048"][0]["value"]])
        compiled = read_json(self.product / "output/ozon-attributes-final.json")
        self.assertTrue({85, 9048, 200, 201, 202}.issubset({row["attribute_id"] for row in compiled["attributes"]}))

    def test_nondefault_saved_values_stay_actionable_for_every_default_semantic(self):
        ensure_defaults_state(self.product)
        meta = field_display_metadata(self.form, directory=self.product,
                                     attributes={"9048": [{"value": "MY-MODEL"}], "200": [{"value": "Россия", "dictionary_value_id": 602}],
                                                 "201": [{"value": True}], "202": [{"value": 2}], "205": [{"value": "一年"}]})
        for key in ("9048", "200", "201", "202", "205"):
            self.assertEqual(meta[key]["display"], "attention", key)

    def test_sku_override_and_source_conflict_expose_default_control(self):
        self.update({"原产国": "俄罗斯", "签名18+": True, "原厂包装数量": 2})
        meta = field_display_metadata(self.form, directory=self.product,
                                     per_sku_attributes={"S2": {"85": [{"value": "真实品牌", "dictionary_value_id": 502}]}})
        for key in ("85", "200", "201", "202"):
            self.assertEqual(meta[key]["display"], "attention", key)

    def test_missing_dictionary_id_is_never_hidden_as_valid_default(self):
        meta = field_display_metadata(self.form, attributes={"85": [{"value": "Нет бренда"}], "200": [{"value": "中国"}]})
        self.assertEqual(meta["85"]["display"], "attention")
        self.assertEqual(meta["200"]["display"], "attention")

    def test_wrong_shop_does_not_create_defaults_state(self):
        with self.assertRaisesRegex(ValueError, "店铺"):
            persist_user_defaults(self.product, self.cache, shop_id="foreign")
        self.assertFalse((self.product / "input/listing-defaults.json").exists())

    def test_default_provenance_verified_and_reloaded_as_default_not_source_fact(self):
        self.seed(85, 200)
        ensure_defaults_state(self.product)
        proposal = self.defaults()
        result = self.save(proposal["attributes"], {
            "attributes": proposal["provenance"]["attributes"], "per_sku_attributes": proposal["provenance"]["per_sku_attributes"]})
        self.assertTrue(all(meta["source"] == "user_requested_default" for meta in result["provenance"]["attributes"].values()))
        self.assertEqual(product_form(self.product, self.cache, shop_id="shop-a")["provenance"], result["provenance"])

    def test_forged_default_cannot_claim_manual_model_or_wrong_dictionary(self):
        ensure_defaults_state(self.product)
        with self.assertRaisesRegex(ValueError, "采集来源"):
            self.save({"9048": [{"value": "FORGED"}]}, {"attributes": {"9048": {"source": "user_requested_default"}}})
        self.assertFalse((self.product / "input/human-confirmations.json").exists())

    def test_confirmed_copy_tags_auto_fill_validated_official_attribute(self):
        confirmed = {"copy": {"confirmed": True, "fingerprint": "current", "payload": {"hashtags": ["#игрушка", "#антистресс"]}}}
        with patch("pipeline.guided_workflow.workflow_status", return_value=confirmed):
            result = self.defaults()
            self.assertEqual(result["attributes"]["23171"][0]["value"], "#игрушка #антистресс")
            meta = result["provenance"]["attributes"]["23171"]
            self.assertEqual(meta["source"], "ai_generated_copy")
            self.assertEqual(meta["copy_fingerprint"], "current")
            self.save({"23171": result["attributes"]["23171"]}, {"attributes": {"23171": meta}})
        with patch("pipeline.guided_workflow.workflow_status", return_value={"copy": {"confirmed": False}}):
            self.assertNotIn("23171", self.defaults()["attributes"])

    def test_invalid_or_unconfirmed_tags_not_inferred_from_keywords(self):
        for copy in ({"confirmed": False}, {"confirmed": True, "fingerprint": "x", "payload": {"hashtags": ["invalid no hash"]}}):
            with patch("pipeline.guided_workflow.workflow_status", return_value={"copy": copy}):
                self.assertNotIn("23171", self.defaults()["attributes"])

    def test_manual_tags_are_not_overwritten_by_new_copy(self):
        self.save({"23171": [{"value": "#manual"}]})
        with patch("pipeline.guided_workflow.workflow_status", return_value={"copy": {"confirmed": True, "fingerprint": "new", "payload": {"hashtags": ["#новый"]}}}):
            self.assertNotIn("23171", self.defaults()["attributes"])

    def test_explicit_persist_saves_and_second_persist_avoids_recompile(self):
        with patch("pipeline.catalog.handle_field_completion") as compile_card, patch("pipeline.guided_review.invalidate_from"):
            first = persist_user_defaults(self.product, self.cache, shop_id="shop-a")
            self.assertTrue(first["attributes"]["85"])
            self.assertTrue(first["attributes"]["9048"])
            self.assertEqual(compile_card.call_count, 1)
            second = persist_user_defaults(self.product, self.cache, shop_id="shop-a")
            self.assertEqual(first["attributes"], second["attributes"])
            self.assertEqual(compile_card.call_count, 1)

    def test_defaults_run_through_real_local_attribute_compiler(self):
        result = persist_user_defaults(self.product, self.cache, shop_id="shop-a")
        compiled = read_json(self.product / "output/ozon-attributes-final.json")
        self.assertEqual(compiled["required_summary"]["missing"], 0)
        values = {str(row["attribute_id"]): row for row in compiled["attributes"]}
        self.assertEqual(values["85"]["dictionary_value_id"], 501)
        self.assertEqual(values["9048"]["value"], result["attributes"]["9048"][0]["value"])
        self.assertEqual(values["201"]["value"], False)

    def test_post_submit_defaults_do_not_write_or_randomize(self):
        write_json(self.product / "runtime/listing-submit-attempt.json", {"status": "unknown"})
        with self.assertRaisesRegex(ValueError, "写入"):
            ensure_defaults_state(self.product)
        self.assertFalse((self.product / "input/listing-defaults.json").exists())


if __name__ == "__main__":
    unittest.main()
