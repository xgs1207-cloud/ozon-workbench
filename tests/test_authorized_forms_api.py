"""Authorized store → true category → typed/dictionary SKU drafts, offline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

import api
from pipeline.ozon_http import FixtureTransport, PATH_TREE, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH

FAKE_TREE = {"result": [{"description_category_id": 2000001, "category_name": "玩具", "children": [
    {"type_id": 93080, "type_name": "抗压玩具", "children": []},
    {"type_id": 93081, "type_name": "益智玩具", "children": []},
    {"type_id": 99999, "type_name": "已停用", "disabled": True, "children": []},
]}]}
FAKE_ATTRIBUTES = {"result": [
    {"id": 85, "name": "品牌", "type": "string", "is_required": True, "dictionary_id": 10, "group_name": "基本信息"},
    {"id": 101, "name": "数量", "type": "integer", "is_required": True, "description": "包装内件数", "group_name": "基本信息"},
    {"id": 102, "name": "材质", "type": "string", "dictionary_id": 11, "is_collection": True, "max_value_count": 2, "group_name": "基本信息"},
    {"id": 103, "name": "颜色", "type": "string", "is_required": True, "dictionary_id": 12, "is_aspect": True, "group_name": "规格"},
    {"id": 105, "name": "含电池", "type": "boolean", "group_name": "基本信息"},
    {"id": 106, "name": "复合项目", "type": "string", "attribute_complex_id": 700, "group_name": "复合信息"},
]}


def fake_transport(credentials=None, **kwargs):
    def values(body):
        options = {85: [{"id": 501, "value": "Нет бренда"}],
                   102: [{"id": 51, "value": "硅胶"}, {"id": 52, "value": "塑料"}],
                   103: [{"id": 61, "value": "Красный"}, {"id": 62, "value": "Синий"}]}
        return {"result": options[int(body["attribute_id"])], "has_next": False}
    return FixtureTransport({PATH_TREE: FAKE_TREE, PATH_ATTRIBUTES: FAKE_ATTRIBUTES,
        PATH_ATTRIBUTE_VALUES: values, PATH_ATTRIBUTE_VALUES_SEARCH: values,
        "/v1/roles": {"roles": [{"name": "Product Read", "methods": [PATH_TREE, PATH_ATTRIBUTES,
            PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH]}], "expires_at": "2099-12-01T00:00:00Z"}})


class AuthorizedFormsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"WORKBENCH_SHOP_REGISTRY_PATH": str(self.root / "config/shops.json"),
                                          "WORKBENCH_SHOP_VAULT_ROOT": str(self.root / "vault")})
        self.env.start()
        self.products = patch.object(api, "PRODUCTS_ROOT", self.root / "products")
        self.market = patch.object(api, "MARKET_DB_PATH", self.root / "runtime/market.sqlite3")
        self.products.start(); self.market.start()
        self.transports = []
        def factory(*args, **kwargs):
            transport = fake_transport(*args, **kwargs)
            self.transports.append(transport)
            return transport
        self.http = patch("pipeline.ozon_http.UrllibTransport", factory)
        self.category_http = patch("market_intelligence.ozon_categories.UrllibTransport", factory)
        self.http.start(); self.category_http.start()
        self.client = TestClient(api.app, base_url="https://testserver")
        self.payload = {"shop_id": "qa-store", "display_name": "测试店铺", "client_id": "2412348",
                        "api_key": "qa-fake-key-not-a-real-secret-12345", "default_currency_code": "CNY"}

    def tearDown(self):
        self.client.close()
        self.category_http.stop(); self.http.stop(); self.products.stop(); self.market.stop(); self.env.stop()
        self.tmp.cleanup()

    def authorize(self):
        response = self.client.post("/api/workbench/stores/authorize", json=self.payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def product(self):
        result = self.client.post("/api/collector/products", json={"title_zh": "测试抗压玩具",
            "source_url": "https://detail.1688.com/offer/123456789.html", "collection_mode": "all_skus",
            "skus": [{"sku_id": "S1", "name": "红色", "purchase_price_cny": 1.5},
                     {"sku_id": "S2", "name": "蓝色", "purchase_price_cny": 1.8}]})
        self.assertEqual(result.status_code, 200, result.text)
        product_id = result.json()["product_id"]
        response = self.client.post(f"/api/workbench/products/{product_id}/skus", json={"include": ["S1", "S2"]})
        self.assertEqual(response.status_code, 200, response.text)
        base = f"/api/workbench/products/{product_id}"
        result = self.client.put(base + "/ozon-category", json={"shop": "qa-store", "category_id": 2000001, "type_id": 93080})
        self.assertEqual(result.status_code, 200, result.text)
        return product_id, base

    def dictionary(self, attribute_id):
        result = self.client.get("/api/ozon/category-values", params={"shop": "qa-store",
            "category_id": 2000001, "type_id": 93080, "attribute_id": attribute_id})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["items"]

    def test_encrypted_authorization_to_full_form_resolves_only_user_defaults_without_write(self):
        report = self.authorize()
        self.assertFalse(report["api_writes_performed"])
        self.assertFalse(report["shop"]["capabilities"]["import_products"])
        product_id, base = self.product()
        categories = self.client.get("/api/ozon/categories", params={"q": "抗压", "shop": "qa-store"}).json()
        self.assertEqual(categories["items"][0]["type_id"], 93080)
        form = self.client.get(base + "/listing-form", params={"shop": "qa-store"}).json()
        self.assertEqual(len(form["form"]["fields"]), 6)
        self.assertEqual(form["missing_required"], [101, 103])
        self.assertEqual(form["attributes"]["85"][0]["value"], "Нет бренда")
        self.assertEqual(form["provenance"]["attributes"]["85"]["source"], "user_requested_default")
        paths = [call["path"] for transport in self.transports for call in transport.calls]
        self.assertLessEqual(sum(path in {PATH_ATTRIBUTE_VALUES, PATH_ATTRIBUTE_VALUES_SEARCH} for path in paths), 6)
        self.assertTrue(all(path in {"/v1/roles", PATH_TREE, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES,
                                    PATH_ATTRIBUTE_VALUES_SEARCH} for path in paths))
        raw_registry = (self.root / "config/shops.json").read_text(encoding="utf-8")
        for secret in (self.payload["client_id"], self.payload["api_key"]):
            self.assertNotIn(secret, raw_registry)
            self.assertNotIn(secret, json.dumps(self.client.get("/api/workbench/stores").json()))
            self.assertNotIn(secret.encode(), next((self.root / "vault").glob("*.fernet")).read_bytes())

    def test_typed_multi_and_sku_values_survive_compile_and_import_builder(self):
        self.authorize(); product_id, base = self.product()
        for key in (85, 102, 103): self.dictionary(key)
        draft = {"shop": "qa-store", "category_id": 2000001, "type_id": 93080,
            "attributes": {"85": [{"value": "Нет бренда", "dictionary_value_id": 501}],
                "101": [{"value": "2"}], "105": [{"value": False}],
                "102": [{"value": "硅胶", "dictionary_value_id": 51}, {"value": "塑料", "dictionary_value_id": 52}]},
            "per_sku_attributes": {"S1": {"103": [{"value": "Красный", "dictionary_value_id": 61}]},
                                   "S2": {"103": [{"value": "Синий", "dictionary_value_id": 62}]}}}
        saved = self.client.put(base + "/listing-form", json=draft)
        self.assertEqual(saved.status_code, 200, saved.text)
        data = saved.json()
        self.assertEqual(data["missing_required"], [])
        self.assertEqual(data["compiled"]["required_summary"]["missing"], 0)
        self.assertEqual(data["compiled"]["attributes_by_sku"]["S2"][0]["dictionary_value_id"], 62)
        from pipeline.ozon_write import build_import_request
        compiled = data["compiled"]
        request = build_import_request({"category": {"category_id": 2000001, "type_id": 93080},
            "attributes": compiled["common_attributes"], "variants": [
                {"source_sku_id": key, "offer_id": key, "price": "99", "display_name_ru": "Тест",
                 "attributes": rows} for key, rows in compiled["attributes_by_sku"].items()]})
        attrs = {row["id"]: row["values"] for row in request["items"][0]["attributes"]}
        self.assertEqual([row["dictionary_value_id"] for row in attrs[102]], [51, 52])
        self.assertEqual(attrs[105], [{"value": "false"}])
        reloaded = self.client.get(base + "/listing-form").json()
        self.assertEqual(reloaded["per_sku_attributes"], draft["per_sku_attributes"])
        status = json.loads((self.root / "products" / product_id / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status.get("api_write_count", 0), 0)

    def test_partial_sku_required_does_not_count_as_all_skus_filled(self):
        self.authorize(); _, base = self.product(); self.dictionary(103)
        saved = self.client.put(base + "/listing-form", json={"shop": "qa-store", "category_id": 2000001,
            "type_id": 93080, "attributes": {}, "per_sku_attributes": {
                "S1": {"103": [{"value": "Красный", "dictionary_value_id": 61}]}}})
        self.assertEqual(saved.status_code, 200, saved.text)
        data = saved.json()
        self.assertIn(103, data["missing_by_sku"]["S2"])
        self.assertIn(103, data["compiled"]["required_summary"]["missing_attribute_ids"])

    def test_empty_sku_override_clears_common_instead_of_inheriting(self):
        self.authorize(); _, base = self.product()
        saved = self.client.put(base + "/listing-form", json={"shop": "qa-store", "category_id": 2000001,
            "type_id": 93080, "attributes": {"101": [{"value": 2}]},
            "per_sku_attributes": {"S1": {"101": []}}})
        self.assertEqual(saved.status_code, 200, saved.text)
        data = saved.json()
        self.assertIn(101, data["missing_by_sku"]["S1"])
        self.assertNotIn(101, data["missing_by_sku"]["S2"])
        self.assertIn(101, data["compiled"]["required_summary"]["missing_attribute_ids"])
        self.assertFalse(any(row["attribute_id"] == 101 for row in data["compiled"]["common_attributes"]))
        self.assertEqual(data["compiled"]["attributes_by_sku"]["S2"][0]["value"], 2)
        reloaded = self.client.get(base + "/listing-form").json()
        self.assertEqual(reloaded["per_sku_attributes"]["S1"]["101"], [])

    def test_public_http_foreign_origin_and_validation_cannot_leak_secret(self):
        insecure = TestClient(api.app, base_url="http://public.example")
        denied = insecure.post("/api/workbench/stores/authorize", json=self.payload)
        self.assertEqual(denied.status_code, 403)
        foreign = self.client.post("/api/workbench/stores/authorize", json=self.payload,
                                   headers={"Origin": "https://malicious.example"})
        self.assertEqual(foreign.status_code, 403)
        invalid = self.client.post("/api/workbench/stores/authorize", json={**self.payload,
            "shop_id": self.payload["api_key"] + "/invalid"})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(self.payload["api_key"], invalid.text)
        self.assertEqual(self.transports, [])
        insecure.close()

    def test_loopback_guard_rejects_public_host_even_with_loopback_proxy(self):
        def context(host, peer, origin=None):
            headers = [(b"host", host.encode())]
            if origin: headers.append((b"origin", origin.encode()))
            return api._shop_authorization_context(Request({"type": "http", "method": "POST", "scheme": "http",
                "path": "/api/workbench/stores/authorize", "query_string": b"", "headers": headers,
                "server": ("127.0.0.1", 8766), "client": (peer, 60000)}))
        self.assertTrue(context("127.0.0.1:8766", "127.0.0.1")["can_submit_credentials"])
        self.assertFalse(context("public.example", "127.0.0.1")["can_submit_credentials"])
        self.assertFalse(context("127.0.0.1:8766", "203.0.113.1")["can_submit_credentials"])
        self.assertFalse(context("127.0.0.1:8766", "127.0.0.1", "null")["can_submit_credentials"])

    def test_complex_forged_dictionary_shop_switch_and_legacy_bypass_rejected(self):
        self.authorize(); _, base = self.product()
        for fields in ({"85": [{"value": "伪造", "dictionary_value_id": 909090}]},
                       {"106": [{"value": "不能扁平保存"}]}, {"101": [{"value": "2.5"}]}):
            result = self.client.put(base + "/listing-form", json={"shop": "qa-store", "category_id": 2000001,
                                     "type_id": 93080, "attributes": fields})
            self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(self.client.get(base + "/listing-form?shop=other").status_code, 422)
        legacy = self.client.put(base + "/guided/attributes", json={"attributes": {"85": "自由输入品牌"}})
        self.assertEqual(legacy.status_code, 422)
        self.assertEqual(self.client.get("/api/workbench/products/P999999/listing-form").status_code, 404)

    def test_category_change_clears_only_stale_attribute_confirmations(self):
        self.authorize(); product_id, base = self.product()
        target = self.root / "products" / product_id / "input/human-confirmations.json"
        target.write_text(json.dumps({"material": "硅胶", "attributes": {"101": [{"value": 2}]},
                                      "sku_attributes": {"S1": {"103": [{"value": "旧颜色"}]}}}), encoding="utf-8")
        changed = self.client.put(base + "/ozon-category", json={"shop": "qa-store", "category_id": 2000001, "type_id": 93081})
        self.assertEqual(changed.status_code, 200, changed.text)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["material"], "硅胶")
        self.assertNotIn("101", saved["attributes"])
        self.assertEqual(saved["sku_attributes"], {})
        self.assertEqual(saved["attribute_provenance"]["attributes"]["85"]["source"], "user_requested_default")


if __name__ == "__main__":
    unittest.main()
