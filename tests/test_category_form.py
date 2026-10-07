"""Official category forms: fixture-only, zero Ozon writes and no secrets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import validate_contract
from market_intelligence import ozon_categories
from pipeline.category_form import dictionary_values, load_form, snapshot_for_product, validate_attributes
from pipeline.ozon_http import (
    FixtureTransport, OzonClient, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES,
    PATH_ATTRIBUTE_VALUES_SEARCH, PATH_TREE, normalize_attribute,
)


TREE = {"result": [{"description_category_id": 1001, "category_name": "家居", "children": [
    {"type_id": 2001, "type_name": "保温杯", "children": []},
    {"type_id": 2002, "type_name": "停用类目", "disabled": True},
]}]}
ATTRIBUTES = {"result": [
    {"id": 85, "name": "品牌", "description": "选择品牌", "type": "String", "is_required": True,
     "dictionary_id": 88, "group_id": 1, "group_name": "核心", "category_dependent": True},
    {"id": 10, "name": "数量", "type": "Integer", "is_required": True},
    {"id": 11, "name": "体积", "type": "Decimal"},
    {"id": 12, "name": "保温", "type": "Boolean"},
    {"id": 13, "name": "颜色", "type": "String", "is_collection": True, "max_value_count": 2, "is_aspect": True},
    {"id": 14, "name": "复合参数", "type": "String", "attribute_complex_id": 70, "complex_is_collection": True},
]}


class CategoryFormTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.transport = FixtureTransport({PATH_TREE: TREE, PATH_ATTRIBUTES: ATTRIBUTES,
                                           PATH_ATTRIBUTE_VALUES: {"result": [{"id": 501, "value": "Нет бренда"}], "has_next": False},
                                           PATH_ATTRIBUTE_VALUES_SEARCH: {"result": [{"id": 501, "value": "Нет бренда"}]}})
        self.client = OzonClient(self.transport)

    def tearDown(self):
        self.tmp.cleanup()

    def form(self, **kwargs):
        return load_form(self.root, 1001, 2001, shop_id="shop-a", client=self.client, **kwargs)

    def dictionary(self, **kwargs):
        return dictionary_values(self.root, 1001, 2001, 85, shop_id="shop-a", client=self.client, **kwargs)

    def validate(self, submitted):
        return validate_attributes(self.form(), submitted, cache_root=self.root)

    def test_form_keeps_metadata_and_does_not_load_dictionaries(self):
        form = self.form()
        self.assertEqual([call["path"] for call in self.transport.calls], [PATH_TREE, PATH_ATTRIBUTES])
        self.assertEqual(form["category_path"], ["家居", "保温杯"])
        self.assertEqual(form["required_attribute_ids"], [85, 10])
        self.assertEqual(form["aspect_attribute_ids"], [13])
        self.assertEqual(form["unsupported_attribute_ids"], [14])
        self.assertEqual(form["fields"][0]["description"], "选择品牌")
        self.assertEqual(form["fields"][0]["group_name"], "核心")
        self.assertEqual(form["fields"][-1]["attribute_complex_id"], 70)
        self.assertFalse(form["fields"][-1]["editable"])
        self.assertEqual(validate_contract("ozon-category-attributes", snapshot_for_product(form, "P000001")), [])
        self.assertEqual(normalize_attribute(ATTRIBUTES["result"][-1])["complex_id"], 70)

    def test_metadata_cache_shop_and_language_scoped(self):
        self.form()
        cached = self.form()
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(len(self.transport.calls), 2)
        load_form(self.root, 1001, 2001, shop_id="shop-b", client=self.client)
        self.assertEqual(len(self.transport.calls), 4)
        self.form(language="RU")
        self.assertEqual(len(self.transport.calls), 6)
        self.form(refresh=True)
        self.assertEqual(len(self.transport.calls), 8)

    def test_invalid_disabled_or_parent_type_never_fetch_attributes(self):
        for category_id, type_id in ((999, 2001), (1001, 2002), (1001, 999)):
            with self.assertRaisesRegex(ValueError, "末级类目树"):
                load_form(self.root, category_id, type_id, shop_id="shop-a", client=self.client)
        self.assertTrue(all(call["path"] == PATH_TREE for call in self.transport.calls))

    def test_explicit_nonexistent_disabled_shop_never_falls_back(self):
        registry = {"default_read_shop": "a", "shops": [{"id": "a", "enabled": True}, {"id": "b", "enabled": False}]}
        with patch.object(ozon_categories, "ensure_registry", return_value=registry):
            with self.assertRaisesRegex(ValueError, "不存在"):
                ozon_categories._client("absent")
            with self.assertRaisesRegex(ValueError, "停用"):
                ozon_categories._client("b")

    def test_default_read_shop_preferred_over_first_enabled(self):
        from pipeline.ozon_http import OzonCredentials
        registry = {"default_read_shop": "b", "shops": [{"id": "a", "enabled": True}, {"id": "b", "enabled": True}]}
        with patch.object(ozon_categories, "ensure_registry", return_value=registry), \
             patch("pipeline.stores.resolve_credentials", return_value={"ready": True}), \
             patch.object(ozon_categories.OzonCredentials, "from_shop", return_value=OzonCredentials("test-id", "test-key")) as resolve:
            _, resolved_id = ozon_categories._client(None)
        self.assertEqual(resolved_id, "b")
        self.assertEqual(resolve.call_args.args[0]["id"], "b")

    def test_default_disabled_or_expired_shop_never_uses_cached_other_store(self):
        registry = {"default_read_shop": "b", "shops": [{"id": "a", "enabled": True}, {"id": "b", "enabled": False}]}
        with patch.object(ozon_categories, "ensure_registry", return_value=registry):
            with self.assertRaisesRegex(ValueError, "停用"):
                ozon_categories._client(None)
        registry["shops"][1].update(enabled=True, expires_at="2020-01-01T00:00:00Z")
        with patch.object(ozon_categories, "ensure_registry", return_value=registry), \
             patch("pipeline.stores.resolve_credentials", return_value={"ready": True}):
            with self.assertRaisesRegex(ValueError, "已过期"):
                ozon_categories._client(None)

    def test_dictionary_is_paginated_and_cached_on_demand(self):
        self.transport.fixtures[PATH_ATTRIBUTE_VALUES] = lambda body: {
            "result": [{"id": body["last_value_id"] + 1, "value": "选项"}], "has_next": body["last_value_id"] == 0}
        first = self.dictionary(limit=1)
        self.assertTrue(first["has_next"])
        self.assertEqual(first["next_last_value_id"], 1)
        second = self.dictionary(limit=1, last_value_id=1)
        self.assertFalse(second["has_next"])
        self.assertEqual(self.transport.calls[-1]["body"]["last_value_id"], 1)
        before = len(self.transport.calls)
        self.assertTrue(self.dictionary(limit=1, last_value_id=1)["cache_hit"])
        self.assertEqual(len(self.transport.calls), before)

    def test_dictionary_search_has_no_undocumented_language_request(self):
        result = self.dictionary(q="Нет", limit=10)
        self.assertEqual(result["api_endpoint"], PATH_ATTRIBUTE_VALUES_SEARCH)
        self.assertNotIn("language", self.transport.calls[-1]["body"])
        self.assertEqual(self.transport.calls[-1]["body"]["value"], "Нет")
        with self.assertRaisesRegex(ValueError, "2 个字符"):
            self.dictionary(q="a")
        with self.assertRaises(ValueError):
            self.dictionary(limit=101)
        with self.assertRaises(ValueError):
            self.dictionary(q="Нет", last_value_id=1)

    def test_free_text_dictionary_ids_and_cross_shop_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "字典选项 ID"):
            self.validate({"85": "Нет бренда"})
        self.dictionary()
        result = self.validate({"85": [{"dictionary_value_id": 501, "value": "Нет бренда"}]})
        self.assertEqual(result["attributes"]["85"][0]["dictionary_value_id"], 501)
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.validate({"85": [{"dictionary_value_id": 501, "value": "Fake"}]})
        form_b = load_form(self.root, 1001, 2001, shop_id="shop-b", client=self.client)
        with self.assertRaisesRegex(ValueError, "官方字典"):
            validate_attributes(form_b, {"85": [{"dictionary_value_id": 501}]}, cache_root=self.root)

    def test_expired_receipt_does_not_allow_submission(self):
        self.dictionary()
        receipts = next(self.root.glob("ozon-dictionary-receipts-*.json"))
        data = json.loads(receipts.read_text(encoding="utf-8"))
        data["values"]["501"]["observed_at"] = 1
        receipts.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "官方字典"):
            self.validate({"85": [{"dictionary_value_id": 501}]})

    def test_typed_values_and_missing_required_draft_reporting(self):
        report = self.validate({"10": [{"value": "0"}], "11": "2.5", "12": False, "13": ["红", "蓝"]})
        self.assertEqual(report["attributes"]["10"][0]["value"], 0)
        self.assertEqual(report["attributes"]["11"][0]["value"], 2.5)
        self.assertIs(report["attributes"]["12"][0]["value"], False)
        self.assertEqual(report["missing_required_attribute_ids"], [85])
        self.assertFalse(report["complete"])
        self.assertTrue(report["warnings"])
        self.dictionary()
        complete = self.validate({"85": [{"dictionary_value_id": 501}], "10": "1"})
        self.assertTrue(complete["complete"])

    def test_invalid_numeric_boolean_collection_unknown_and_complex(self):
        for values in ({"10": "1.5"}, {"11": "nan"}, {"12": "yes"}, {"13": ["1", "2", "3"]},
                       {"14": "flattened"}, {"999": "invented"}, {"10": True}, {"10": [1, 2]},
                       {"13": [{"value": None}]}, {"13": [{"values": ["nested"]}]}):
            with self.assertRaises(ValueError, msg=str(values)):
                self.validate(values)

    def test_page_scope_conflicting_dictionary_attr_rejected(self):
        with self.assertRaisesRegex(ValueError, "不是官方字典"):
            dictionary_values(self.root, 1001, 2001, 10, shop_id="shop-a", client=self.client)

    def test_transport_errors_and_repr_do_not_reflect_credentials(self):
        import io
        import urllib.error
        from pipeline.ozon_http import OzonCredentials, OzonHttpError, UrllibTransport
        credentials = OzonCredentials("client-private-123", "private-key-xyz")
        self.assertNotIn("private-key-xyz", repr(credentials))
        def failed(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "private-key-xyz", {},
                                         io.BytesIO(b'client-private-123 private-key-xyz'))
        transport = UrllibTransport(credentials, urlopen=failed)
        with self.assertRaises(OzonHttpError) as caught:
            transport.post(PATH_TREE, {})
        self.assertNotIn("private-key-xyz", str(caught.exception))
        self.assertNotIn("client-private-123", caught.exception.body)


if __name__ == "__main__":
    unittest.main()
