"""Offline shop catalogue browsing/selection; no public API writes or capture."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pipeline import operations_catalog as catalog
from pipeline.operations import Store
from pipeline.ozon_http import OzonHttpError


class CatalogSeller:
    def __init__(self):
        self.credentials = SimpleNamespace(shop_id="qa", client_id="123", api_key="never-show-secret")
        self.calls = []
        self.listing = {"result": {"items": [
            {"offer_id": "existing.1", "product_id": 1001, "sku": 7001, "archived": False},
            {"offer_id": "existing.2", "product_id": 1002, "sku": 7002, "archived": False}],
            "total_items": 4, "total": 999, "last_id": "cursor-A"}}
        self.details = {"items": [
            {"offer_id": "existing.1", "id": 1001, "sku": 7001, "name": "Зеленая игрушка", "price": "10.5",
             "currency_code": "CNY", "primary_image": ["https://cdn.ozon.ru/image/1.jpg"], "is_archived": False,
             "statuses": {"status": "price_sent", "moderate_status": "approved"}},
            {"offer_id": "existing.2", "id": 1002, "sku": 7002, "name": "Красная игрушка", "price": "0",
             "currency_code": "RUB", "is_autoarchived": False, "statuses": {"status": "price_sent"}}]}
        self.fail = {}

    def post(self, path, body):
        self.calls.append((path, deepcopy(body)))
        if path in self.fail:
            raise self.fail[path]
        if path == catalog.LIST_PATH:
            return deepcopy(self.listing)
        if path == catalog.INFO_PATH:
            return deepcopy(self.details)
        raise AssertionError("Only read paths may be called: " + path)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc).timestamp()
        self.store = Store(self.root / "operations.sqlite3", clock=lambda: self.now)
        self.seller = CatalogSeller()

    def browse(self, **kwargs):
        return catalog.browse_catalog("qa", self.seller, store=self.store, limit=2, **kwargs)

    def test_browse_authoritative_fields_read_only_and_total_items_priority(self):
        page = self.browse()
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["total_source"], "total_items")
        self.assertTrue(page["has_more"])
        self.assertEqual(page["items"][0]["ozon_product_id"], "1001")
        self.assertEqual(page["items"][0]["ozon_sku"], "7001")
        self.assertEqual(page["items"][0]["price"], 10.5)
        self.assertEqual(page["items"][1]["price"], 0)
        self.assertFalse(page["api_writes_performed"])
        self.assertEqual(self.seller.calls, [
            (catalog.LIST_PATH, {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 2}),
            (catalog.INFO_PATH, {"product_id": ["1001", "1002"]})])
        self.assertEqual(self.store.list_products()["total"], 0)
        self.assertFalse((self.root / "products").exists())

    def test_current_page_query_casefold_no_unsupported_server_filter(self):
        page = self.browse(query="ЗЕЛЕНАЯ")
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["filtered_count"], 1)
        self.assertEqual(page["search_scope"], "current_page")
        self.assertIn("search_current_page_only", page["warning_codes"])
        self.assertEqual(self.seller.calls[0][1]["filter"], {"visibility": "ALL"})
        with self.assertRaises(catalog.CatalogError):
            self.store.import_catalog_rows("qa", page["page_token"], ["existing.2"])

    def test_empty_filter_result_can_still_continue_unfiltered_cursor(self):
        page = self.browse(query="not-present")
        self.assertEqual(page["items"], [])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["page_item_count"], 2)

    def test_cached_page_ownership_expiry_and_arbitrary_row_selection(self):
        page = self.browse()
        self.assertEqual(self.store.catalog_page("qa", page["page_token"])["seen_cursors"], [""])
        with self.assertRaises(catalog.CatalogError) as wrong_shop:
            self.store.catalog_page("other", page["page_token"])
        self.assertEqual(wrong_shop.exception.code, "page_not_found")
        with self.assertRaises(catalog.CatalogError):
            self.store.import_catalog_rows("qa", page["page_token"], ["forged-offer"])
        with self.assertRaises(catalog.CatalogError):
            self.store.import_catalog_rows("qa", page["page_token"], [{"offer_id": "existing.1"}])
        self.now += 3600
        with self.assertRaises(catalog.CatalogError) as expired:
            self.store.import_catalog_rows("qa", page["page_token"], ["existing.1"])
        self.assertEqual(expired.exception.code, "page_expired")
        self.assertEqual(self.store.list_products()["total"], 0)

    def test_import_idempotent_no_supplier_directory_warehouse_or_auto_jobs(self):
        page = self.browse()
        result = self.store.import_catalog_rows("qa", page["page_token"], ["existing.1", "existing.1"])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["jobs_enqueued"], 0)
        product = self.store.product("qa", "existing.1")
        self.assertEqual(product["source"], "shop_readonly_import")
        for field in ("product_id", "source_sku_id", "source_url", "source_note", "warehouse_id", "warehouse_name"):
            self.assertNotIn(field, product)
        self.assertEqual(self.store.import_catalog_rows("qa", page["page_token"], ["existing.1"])["existing"], 1)
        self.assertEqual(self.store.list_products()["total"], 1)
        self.assertEqual(self.store.jobs(), [])
        self.assertFalse((self.root / "products").exists())

    def test_existing_local_binding_and_observations_survive_catalog_import(self):
        self.store.discover([{"shop": "qa", "offer_id": "existing.1", "ozon_product_id": "1001", "product_id": "P000007",
            "source_sku_id": "red", "source_note": "红色 24cm", "source_url": "https://detail.1688.com/offer/123.html",
            "warehouse_id": "12", "warehouse_name": "real warehouse"}])
        self.store._observe_product("qa", "existing.1", {"name": "Newer observation", "price": 99, "ozon_sku": "7001"})
        before = self.store.product("qa", "existing.1")
        page = self.browse()
        self.store.import_catalog_rows("qa", page["page_token"], ["existing.1"])
        after = self.store.product("qa", "existing.1")
        for field, value in before.items():
            self.assertEqual(after[field], value, field)
        self.assertEqual(after["source"], "local_listing")

    def test_existing_identity_conflict_rolls_back_whole_selection(self):
        self.store.discover([{"shop": "qa", "offer_id": "existing.2", "ozon_product_id": "999", "product_id": "P000008"}])
        page = self.browse()
        with self.assertRaises(catalog.CatalogError) as conflict:
            self.store.import_catalog_rows("qa", page["page_token"], ["existing.1", "existing.2"])
        self.assertEqual(conflict.exception.code, "identity_conflict")
        self.assertEqual(self.store.list_products()["total"], 1)
        self.assertEqual(self.store.product("qa", "existing.2")["ozon_product_id"], "999")

    def test_reverse_local_discovery_keeps_remote_id_binding(self):
        page = self.browse()
        self.store.import_catalog_rows("qa", page["page_token"], ["existing.1"])
        result = self.store.discover([{"shop": "qa", "offer_id": "existing.1", "ozon_product_id": "999", "product_id": "P000007"}])
        self.assertEqual(result["conflicts"], 1)
        self.assertNotIn("product_id", self.store.product("qa", "existing.1"))
        self.store.discover([{"shop": "qa", "offer_id": "existing.1", "ozon_product_id": "1001", "product_id": "P000007"}])
        self.assertEqual(self.store.product("qa", "existing.1")["source"], "local_listing")

    def test_partial_details_leave_unknown_fields_none(self):
        self.seller.details["items"].pop()
        page = self.browse()
        missing = page["items"][1]
        self.assertIn("details_missing", page["warning_codes"])
        self.assertFalse(missing["details_available"])
        self.assertIsNone(missing["price"])
        self.assertIsNone(missing["name"])
        self.assertEqual(missing["ozon_product_id"], "1002")
        self.assertEqual(self.store.import_catalog_rows("qa", page["page_token"], ["existing.2"])["imported"], 1)

    def test_wrong_detail_offer_product_or_sku_cannot_enter_cache(self):
        for field, value in (("offer_id", "foreign"), ("id", 9999), ("sku", 8888)):
            seller = CatalogSeller()
            seller.details["items"][0][field] = value
            with self.assertRaises(catalog.CatalogError) as failure:
                catalog.browse_catalog("qa", seller, store=self.store, limit=2)
            self.assertEqual(failure.exception.code, "identity_conflict")
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM catalog_pages").fetchone()[0], 0)

    def test_duplicate_list_and_extra_detail_ids_fail_closed(self):
        self.seller.listing["result"]["items"][1]["offer_id"] = "existing.1"
        with self.assertRaises(catalog.CatalogError):
            self.browse()
        self.seller = CatalogSeller()
        self.seller.details["items"][1] = deepcopy(self.seller.details["items"][0])
        with self.assertRaises(catalog.CatalogError):
            self.browse()

    def test_empty_total_zero_is_preserved_and_no_detail_request(self):
        self.seller.listing = {"result": {"items": [], "total_items": 0, "total": 100, "last_id": "stale"}}
        page = self.browse()
        self.assertEqual(page["total"], 0)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(len(self.seller.calls), 1)

    def test_legacy_total_fallback_and_unknown_total_not_fake_zero(self):
        del self.seller.listing["result"]["total_items"]
        page = self.browse()
        self.assertEqual(page["total"], 999)
        self.assertEqual(page["total_source"], "total")
        del self.seller.listing["result"]["total"]
        self.assertIsNone(self.browse()["total"])

    def test_cursor_self_loop_and_previous_loop_stop_with_warning(self):
        self.seller.listing["result"]["total_items"] = 99
        first = self.browse()
        self.seller.listing["result"]["last_id"] = "cursor-A"
        same = self.browse(last_id=first["next_cursor"], seen_cursors=first["seen_cursors"])
        self.assertFalse(same["has_more"])
        self.assertIn("cursor_loop", same["warning_codes"])
        self.seller.listing["result"]["last_id"] = "cursor-B"
        second = self.browse(last_id="cursor-A", seen_cursors=[""])
        self.seller.listing["result"]["last_id"] = "cursor-A"
        third = self.browse(last_id="cursor-B", seen_cursors=second["seen_cursors"])
        self.assertFalse(third["has_more"])
        self.assertIn("cursor_loop", third["warning_codes"])

    def test_exact_final_full_page_uses_total_and_trace_to_stop(self):
        first = self.browse()
        self.seller.listing["result"]["last_id"] = "cursor-B"
        second = self.browse(last_id=first["next_cursor"], seen_cursors=first["seen_cursors"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])

    def test_trace_limit_invalid_cursor_and_malformed_page_rejected(self):
        for kwargs in ({"last_id": "bad\n"}, {"seen_cursors": ["x"] * 200}, {"visibility": "GUESS"}, {"query": "x" * 101}):
            with self.assertRaises(catalog.CatalogError):
                self.browse(**kwargs)
        self.assertEqual(self.seller.calls, [])
        self.seller.listing["result"]["items"][0]["product_id"] = 0
        with self.assertRaises(catalog.CatalogError):
            self.browse()

    def test_http_400_403_429_failures_safe_no_cache_or_import(self):
        for path in (catalog.LIST_PATH, catalog.INFO_PATH):
            for status in (400, 403, 429):
                self.seller = CatalogSeller()
                self.seller.fail[path] = OzonHttpError("never-show-secret", status=status, body="never-show-secret")
                with self.assertRaises(catalog.CatalogError) as error:
                    self.browse()
                self.assertEqual(error.exception.http_status, status)
                self.assertNotIn("never-show-secret", str(error.exception))
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM catalog_pages").fetchone()[0], 0)

    def test_thumbnail_https_no_auth_and_no_download(self):
        item = self.seller.details["items"][0]
        item["primary_image"] = ["http://cdn.ozon.ru/1.jpg", "https://account:secret@cdn.ozon.ru/1.jpg", "javascript:bad",
                                 "https://cdn.ozon.ru/private.jpg?api_key=never-show-secret"]
        item["images"] = ["https://cdn.ozon.ru/valid.jpg"]
        page = self.browse()
        self.assertEqual(page["items"][0]["thumbnail"], "https://cdn.ozon.ru/valid.jpg")
        self.assertEqual(len(self.seller.calls), 2)

    def test_selection_count_and_forged_receipt_cannot_bind(self):
        page = self.browse()
        for selected in ([], ["existing.1"] * 101, "existing.1"):
            with self.assertRaises(catalog.CatalogError):
                self.store.import_catalog_rows("qa", page["page_token"], selected)
        with self.assertRaises(catalog.CatalogError):
            self.store.import_catalog_rows("qa", "0" * 32, ["existing.1"])
        self.assertEqual(self.store.list_products()["total"], 0)

    def test_transport_shop_mismatch_rejected_before_network(self):
        self.seller.credentials.shop_id = "other"
        with self.assertRaises(catalog.CatalogError):
            self.browse()
        self.assertEqual(self.seller.calls, [])

    def test_same_offer_in_other_shop_remains_independent(self):
        page = self.browse()
        self.store.import_catalog_rows("qa", page["page_token"], ["existing.1"])
        self.seller.credentials.shop_id = "other"
        self.seller.listing["result"]["items"][0]["product_id"] = 2222
        self.seller.details["items"][0]["id"] = 2222
        other = catalog.browse_catalog("other", self.seller, store=self.store, limit=2)
        self.store.import_catalog_rows("other", other["page_token"], ["existing.1"])
        self.assertEqual(self.store.product("qa", "existing.1")["ozon_product_id"], "1001")
        self.assertEqual(self.store.product("other", "existing.1")["ozon_product_id"], "2222")


if __name__ == "__main__":
    unittest.main()
