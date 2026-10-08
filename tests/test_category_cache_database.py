"""Persistent official cache reuse, isolation, expiry and JSON compatibility."""
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline import category_cache
from pipeline.category_form import dictionary_values, invalidate_shop_cache, load_form, validate_attributes
from pipeline.ozon_http import FixtureTransport, OzonClient, PATH_ATTRIBUTES, PATH_ATTRIBUTE_VALUES, PATH_TREE

TREE = {"result": [{"description_category_id": 1, "category_name": "厨具", "children": [
    {"type_id": 2, "type_name": "锅"}, {"type_id": 3, "type_name": "壶"}]}]}
ATTRIBUTES = {"result": [{"id": 85, "name": "品牌", "dictionary_id": 88, "type": "String", "is_required": True}]}


class CategoryCacheDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transport = FixtureTransport({PATH_TREE: TREE, PATH_ATTRIBUTES: ATTRIBUTES,
                                           PATH_ATTRIBUTE_VALUES: {"result": [{"id": 501, "value": "Нет бренда"}]}})
        self.client = OzonClient(self.transport)

    def tearDown(self):
        self.temp.cleanup()

    def form(self, shop="a", type_id=2, **kwargs):
        return load_form(self.root, 1, type_id, shop_id=shop, client=self.client, **kwargs)

    def test_database_reuses_without_json_mirrors_or_network(self):
        original = self.form()
        dictionary_values(self.root, 1, 2, 85, shop_id="a", client=self.client)
        self.assertTrue(category_cache.database_path(self.root).is_file())
        for target in self.root.glob("ozon-*.json"):
            target.unlink()
        before = len(self.transport.calls)
        result = self.form()
        options = dictionary_values(self.root, 1, 2, 85, shop_id="a", client=self.client)
        self.assertTrue(result["cache_hit"])
        self.assertTrue(options["cache_hit"])
        self.assertEqual(result["fields"], original["fields"])
        self.assertEqual(len(self.transport.calls), before)
        self.assertTrue(validate_attributes(result, {"85": [{"dictionary_value_id": 501}]}, cache_root=self.root)["complete"])

    def test_scopes_shop_type_language_refresh_and_credentials_invalidation(self):
        self.form()
        self.form("b")
        self.form(type_id=3)
        self.form(language="RU")
        self.assertEqual(len(self.transport.calls), 7)
        self.form(refresh=True)
        self.assertEqual(len(self.transport.calls), 9)
        dictionary_values(self.root, 1, 2, 85, shop_id="a", client=self.client)
        invalidate_shop_cache(self.root, "a")
        before = len(self.transport.calls)
        self.assertTrue(self.form("b")["cache_hit"])
        self.assertEqual(len(self.transport.calls), before)
        self.assertFalse(self.form()["cache_hit"])
        self.assertEqual(len(self.transport.calls), before + 2)
        with self.assertRaisesRegex(ValueError, "官方字典"):
            validate_attributes(self.form(), {"85": [{"dictionary_value_id": 501}]}, cache_root=self.root)

    def test_db_ttl_and_expired_json_remain_expired(self):
        self.form()
        for target in self.root.glob("ozon-*.json"):
            target.unlink()
        with closing(sqlite3.connect(category_cache.database_path(self.root))) as connection:
            with connection:
                connection.execute("UPDATE ozon_metadata_cache SET stored_at=1")
        self.assertFalse(self.form()["cache_hit"])
        self.assertEqual(len(self.transport.calls), 4)
        target = next(self.root.glob("ozon-category-form-*.json"))
        os.utime(target, (1, 1))
        self.assertFalse(self.form()["cache_hit"])
        self.assertEqual(len(self.transport.calls), 5)

    def test_json_receipt_edit_cannot_be_masked_by_database(self):
        dictionary_values(self.root, 1, 2, 85, shop_id="a", client=self.client)
        target = next(self.root.glob("ozon-dictionary-receipts-*.json"))
        value = json.loads(target.read_text(encoding="utf-8"))
        value["values"]["501"]["observed_at"] = 1
        target.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "官方字典"):
            validate_attributes(self.form(), {"85": [{"dictionary_value_id": 501}]}, cache_root=self.root)

    def test_legacy_json_migrates_without_official_read(self):
        first = self.form()
        category_cache.database_path(self.root).unlink()
        self.assertTrue(self.form()["cache_hit"])
        self.assertEqual(len(self.transport.calls), 2)
        with closing(sqlite3.connect(category_cache.database_path(self.root))) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ozon_metadata_cache").fetchone()[0], 2)
        self.assertEqual(self.form()["fields"], first["fields"])

    def test_authorization_rotation_mid_attributes_request_never_repopulates_stale_cache(self):
        rotated = False

        def attributes(body):
            nonlocal rotated
            if not rotated:
                rotated = True
                invalidate_shop_cache(self.root, "a")
            return ATTRIBUTES

        self.transport.fixtures[PATH_ATTRIBUTES] = attributes
        with self.assertRaisesRegex(ValueError, "授权已变更"):
            self.form()
        self.assertEqual(list(self.root.glob("ozon-category-form-*.json")), [])
        refreshed = self.form()
        self.assertEqual(refreshed["cache_generation"], 1)
        self.assertTrue(self.form()["cache_hit"])

    def test_authorization_rotation_mid_dictionary_request_never_creates_receipts(self):
        self.form()

        def options(body):
            invalidate_shop_cache(self.root, "a")
            return {"result": [{"id": 501, "value": "Нет бренда"}]}

        self.transport.fixtures[PATH_ATTRIBUTE_VALUES] = options
        with self.assertRaisesRegex(ValueError, "授权已变更"):
            dictionary_values(self.root, 1, 2, 85, shop_id="a", client=self.client)
        self.assertEqual(list(self.root.glob("ozon-dictionary-receipts-*.json")), [])
        with self.assertRaisesRegex(ValueError, "官方字典"):
            validate_attributes(self.form(), {"85": [{"dictionary_value_id": 501}]}, cache_root=self.root)


if __name__ == "__main__":
    unittest.main()
