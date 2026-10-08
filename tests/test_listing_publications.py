"""Offline warehouse/import/stock boundaries; no external requests or models."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pipeline.context import read_json, write_json
from pipeline import listing_publications as service
from pipeline.publications import record_publication


WAREHOUSES = {"warehouses": [
    {"warehouse_id": 11, "name": "FBS", "status": "created", "is_rfbs": False, "pause_at": None},
    {"warehouse_id": 22, "name": "rFBS", "status": "created", "is_rfbs": True, "pause_at": ""},
    {"warehouse_id": 33, "name": "暂停", "status": "created", "is_rfbs": True, "pause_at": "2026-10-01T00:00:00Z"},
    {"warehouse_id": 44, "name": "未知", "status": "new_unknown", "is_rfbs": False}],
    "has_next": False, "cursor": ""}


class StockTransport:
    def __init__(self, offers):
        self.offers = dict(offers)
        self.ids = {offer: 1001 + index for index, offer in enumerate(self.offers.values())}
        self.calls, self.amounts = [], {}
        self.import_state = "imported"
        self.stage = "price_sent"
        self.mode = "success"
        self.read_mode = "success"

    def post(self, path, body):
        self.calls.append({"path": path, "body": deepcopy(body)})
        if path == service.WAREHOUSE_PATH:
            return deepcopy(WAREHOUSES)
        if path == "/v1/product/import/info":
            return {"result": {"items": [{"offer_id": offer, "product_id": self.ids[offer] if self.import_state == "imported" else 0,
                "status": self.import_state, "errors": []} for offer in self.offers.values()]}}
        if path == service.INFO_PATH:
            return {"items": [{"offer_id": offer, "id": self.ids[offer],
                "statuses": {"status": self.stage, "is_created": True}, "price": "99.00",
                "visibility_details": {"has_price": True}, "errors": []} for offer in body["offer_id"]]}
        if path == service.STOCK_READ_PATH:
            if self.read_mode == "missing":
                return {"products": [], "has_next": False}
            if self.read_mode == "malformed":
                return {"products": []}
            if self.read_mode == "after_failed" and any(call["path"] == service.STOCK_PATH for call in self.calls):
                raise ValueError("readback unavailable")
            return {"products": [{"offer_id": offer, "product_id": self.ids[offer], "warehouse_id": 22,
                "present": self.amounts.get(offer, 0) + 7, "reserved": 7, "free_stock": self.amounts.get(offer, 0)}
                for offer in body["offer_id"]], "has_next": False}
        if path == service.STOCK_PATH:
            if self.mode == "timeout":
                raise TimeoutError("request may have reached server")
            results = []
            for index, row in enumerate(body["stocks"]):
                rejected = self.mode == "partial" and index == 1
                if not rejected:
                    self.amounts[row["offer_id"]] = row["stock"]
                results.append({"offer_id": row["offer_id"], "product_id": self.ids[row["offer_id"]],
                    "warehouse_id": row["warehouse_id"], "updated": not rejected,
                    "errors": [{"code": "SKU_NOT_READY"}] if rejected else []})
            if self.mode == "missing":
                results.pop()
            if self.mode == "duplicate":
                results.append(deepcopy(results[0]))
            return {"result": results}
        raise AssertionError("Unexpected external route: " + path)


class ListingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / "products/P000001"
        self.db = self.root / "runtime/listing-publications.sqlite3"
        self.registry = self.root / "config/shops.json"
        write_json(self.registry, {"shops": [{"id": "qa", "enabled": True, "client_id_env": "QA_CLIENT", "api_key_env": "QA_KEY"}]})
        self.env = patch.dict(os.environ, {"WORKBENCH_PUBLICATION_DB_PATH": str(self.db),
                                         "WORKBENCH_SHOP_REGISTRY_PATH": str(self.registry)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.offers = {"S1": "qa.10.8.1", "S2": "qa.10.8.2"}
        write_json(self.directory / "input/source.json", {"source_url": "http://detail.1688.com/offer/123456.html?share_token=secret",
            "skus": [{"sku_id": sku, "offer_id": offer} for sku, offer in self.offers.items()]})
        write_json(self.directory / "input/selected-skus.json", {"selected": ["S1", "S2"]})
        self.payload = {"product_id": self.directory.name, "variants": [{"source_sku_id": sku, "offer_id": offer} for sku, offer in self.offers.items()]}
        self.transport = StockTransport(self.offers)

    def configured(self):
        service.refresh_warehouses("qa", transport=self.transport)
        return service.save_config(self.directory, shop="qa", warehouse_id=22, stock=100,
                                   stock_by_sku={"S2": 80}, source_note="供应商链接已核对")

    def submitted(self):
        self.configured()
        service.seal_config(self.directory, shop="qa", payload=self.payload)
        write_json(self.directory / "output/store-runs/qa/payload.json", self.payload)
        service.record_import_attempt(self.directory, "qa", self.payload, state="started")
        service.record_import_attempt(self.directory, "qa", self.payload, state="processing", task_id=123)
        for sku, offer in self.offers.items():
            record_publication(self.directory, "qa", sku_id=sku, offer_id=offer, task_id="123", status="submitted")

    def run_stocks(self, **kwargs):
        return service.continue_stocks(self.directory, shop="qa", confirm="UPDATE_STOCK", transport=self.transport,
                                       registry=self.registry, **kwargs)

    def test_empty_gets_no_db_creation_and_default_100_only_local(self):
        self.assertEqual(service.list_publications()["items"], [])
        self.assertFalse(service.read_warehouses("qa")["cached"])
        config = service.read_config(self.directory, shop="qa")
        self.assertEqual(config["stock"], 100)
        self.assertIsNone(config["warehouse_id"])
        self.assertFalse(config["saved"])
        self.assertFalse(self.db.exists())
        self.assertEqual(self.transport.calls, [])

    def test_source_notes_are_complete_supplier_specifications_per_sku_and_offer(self):
        source = read_json(self.directory / "input/source.json")
        source["skus"][0].update(name_cn="红色/内白", option_values=[
            {"name_cn": "颜色", "value_cn": "红色/内白"},
            {"name_cn": "规格", "value_cn": "24cm/3.5L"}])
        source["skus"][1].update(name_cn="南瓜橘/内白 24cm/3.5L", option_values=[
            {"name_cn": "颜色", "value_cn": "南瓜橘/内白"},
            {"name_cn": "规格", "value_cn": "24cm/3.5L"}])
        write_json(self.directory / "input/source.json", source)
        config = service.read_config(self.directory, shop="qa")
        self.assertTrue(config["source_note_auto"])
        self.assertEqual(config["source_note_by_sku"], {"S1": "红色/内白 24cm/3.5L", "S2": "南瓜橘/内白 24cm/3.5L"})
        self.submitted()
        records = service.list_publications(shop="qa")["items"]
        self.assertEqual({row["source_sku_id"]: row["source_note"] for row in records}, config["source_note_by_sku"])
        self.assertEqual({row["offer_id"] for row in records}, set(self.offers.values()))

    def test_missing_source_specification_does_not_invent_one_or_erase_legacy_note(self):
        self.assertEqual(service.source_specifications(self.directory), {})
        self.configured()
        config = service.read_config(self.directory, shop="qa")
        self.assertFalse(config["source_note_auto"])
        self.assertEqual(config["source_note"], "供应商链接已核对")

    def test_sku_without_specification_does_not_inherit_another_skus_note(self):
        source = read_json(self.directory / "input/source.json")
        source["skus"][0]["name_cn"] = "红色/内白 24cm/3.5L"
        write_json(self.directory / "input/source.json", source)
        self.submitted()
        records = {row["source_sku_id"]: row for row in service.list_publications(shop="qa")["items"]}
        self.assertEqual(records["S1"]["source_note"], "红色/内白 24cm/3.5L")
        self.assertEqual(records["S2"]["source_note"], "")

    def test_warehouse_cache_has_real_ids_and_blocks_pause_unknown(self):
        result = service.refresh_warehouses("qa", transport=self.transport)
        self.assertEqual([row["warehouse_id"] for row in result["items"] if row["eligible"]], ["11", "22"])
        for bad in (33, 44, 999):
            with self.assertRaises(ValueError):
                service.save_config(self.directory, shop="qa", warehouse_id=bad)
        self.assertEqual(len(self.transport.calls), 1)

    def test_bad_refresh_retains_previous_complete_cache(self):
        self.configured()
        with self.assertRaises(ValueError):
            service.refresh_warehouses("qa", transport=type("Malformed", (), {"post": lambda self, path, body: {"warehouses": []}})())
        self.assertEqual(len(service.read_warehouses("qa")["items"]), 4)

    def test_save_stock_is_independent_of_copy_card_and_source_url_has_no_secrets(self):
        from pipeline.listing_draft import card_fingerprint
        before = card_fingerprint(self.directory)
        config = self.configured()
        self.assertEqual(config["source_url"], "https://detail.1688.com/offer/123456.html")
        self.assertEqual(card_fingerprint(self.directory), before)
        self.assertFalse(any(row["path"] == service.STOCK_PATH for row in self.transport.calls))
        with self.assertRaises(ValueError):
            service.save_config(self.directory, shop="qa", warehouse_id=22, source_note="x" * 501)
        with self.assertRaises(ValueError):
            service.save_config(self.directory, shop="qa", warehouse_id=22, stock=True)

    def test_selection_change_invalidates_saved_config_without_writing(self):
        self.configured()
        write_json(self.directory / "input/selected-skus.json", {"selected": ["S1"]})
        before = (self.directory / service.CONFIG_FILE).read_bytes()
        config = service.read_config(self.directory, shop="qa")
        self.assertTrue(config["stale"])
        self.assertFalse(config["saved"])
        self.assertEqual((self.directory / service.CONFIG_FILE).read_bytes(), before)

    def test_no_submit_snapshot_cannot_update_stock(self):
        self.configured()
        with self.assertRaises(ValueError):
            self.run_stocks()
        self.assertFalse(any(row["path"] == service.STOCK_PATH for row in self.transport.calls))

    def test_frozen_after_seal_and_tampered_intent_blocks(self):
        self.submitted()
        self.assertTrue(service.read_config(self.directory, shop="qa")["frozen"])
        with self.assertRaises(ValueError):
            service.save_config(self.directory, shop="qa", warehouse_id=11)
        intent = read_json(self.directory / service.INTENT_FILE)
        intent["shops"]["qa"]["stock"] = 900
        write_json(self.directory / service.INTENT_FILE, intent)
        with self.assertRaises(ValueError):
            self.run_stocks()

    def test_pending_import_never_writes_and_never_reimports(self):
        self.submitted()
        self.transport.import_state = "pending"
        result = self.run_stocks()
        self.assertEqual(result["api_writes"], 0)
        self.assertEqual(result["status"], "stock_pending")
        self.assertTrue(all(row["path"] not in {service.STOCK_PATH, "/v3/product/import"} for row in self.transport.calls))

    def test_price_not_ready_never_writes(self):
        self.submitted()
        self.transport.stage = "imported"
        result = self.run_stocks()
        self.assertEqual(result["api_writes"], 0)
        self.assertTrue(all(row["stock_status"] == "pending_price" for row in result["items"]))

    def test_later_sale_and_stock_sent_states_accept_actual_price_proof(self):
        for stage in ("price_sent", "stock_sent", "sale", "active"):
            self.assertTrue(service._price_ready({"statuses": {"status": stage, "is_created": True},
                "price": "12", "visibility_details": {"has_price": True}}))
        for extra in ({"price": "NaN"}, {"is_archived": True}, {"errors": [{"code": "bad"}]}, {"visibility_details": {"has_price": False}}):
            item = {"statuses": {"status": "sale", "is_created": True}, "price": "12", "visibility_details": {"has_price": True}}
            item.update(extra)
            self.assertFalse(service._price_ready(item))

    def test_complete_stock_writes_after_price_reserved_read_and_persists_actual(self):
        self.submitted()
        result = self.run_stocks(epoch=100)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stock_complete")
        self.assertEqual(result["api_writes"], 1)
        call = next(row for row in self.transport.calls if row["path"] == service.STOCK_PATH)
        self.assertEqual({row["offer_id"]: row["stock"] for row in call["body"]["stocks"]}, {self.offers["S1"]: 100, self.offers["S2"]: 80})
        paths = [row["path"] for row in self.transport.calls]
        self.assertLess(paths.index(service.INFO_PATH), paths.index(service.STOCK_PATH))
        self.assertLess(paths.index(service.STOCK_READ_PATH), paths.index(service.STOCK_PATH))
        self.assertEqual(paths.count(service.STOCK_READ_PATH), 2)
        for row in result["items"]:
            self.assertEqual(row["stock_readback"]["reserved"], 7)
            self.assertEqual(row["stock_readback"]["present"], row["stock"] + 7)
            self.assertTrue(row["stock_readback_matches"])
            self.assertGreaterEqual(len(row["attempts"]), 6)
        again = self.run_stocks(epoch=150)
        self.assertEqual(again["api_writes"], 0)
        self.assertEqual(paths.count(service.STOCK_PATH), 1)

    def test_partial_failures_keep_success_and_retry_only_failed_pairs(self):
        self.submitted()
        self.transport.mode = "partial"
        result = self.run_stocks(epoch=100)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "stock_partial")
        self.transport.mode = "success"
        again = self.run_stocks(epoch=140)
        self.assertTrue(again["ok"])
        calls = [row for row in self.transport.calls if row["path"] == service.STOCK_PATH]
        self.assertEqual(len(calls[1]["body"]["stocks"]), 1)
        self.assertEqual(calls[1]["body"]["stocks"][0]["offer_id"], self.offers["S2"])

    def test_timeout_no_automatic_stock_retry_or_reimport(self):
        self.submitted()
        self.transport.mode = "timeout"
        result = self.run_stocks(epoch=100)
        self.assertEqual(result["status"], "stock_unknown")
        self.assertEqual(result["api_writes"], 1)
        self.transport.mode = "success"
        self.assertEqual(self.run_stocks(epoch=140)["api_writes"], 0)
        self.assertEqual(self.run_stocks(epoch=120, retry_unknown=True)["api_writes"], 0)
        self.assertTrue(self.run_stocks(epoch=141, retry_unknown=True)["ok"])
        paths = [row["path"] for row in self.transport.calls]
        self.assertEqual(paths.count(service.STOCK_PATH), 2)
        self.assertNotIn("/v3/product/import", paths)

    def test_unknown_matching_readback_resolves_without_paid_write_retry(self):
        self.submitted()
        self.transport.mode = "timeout"
        self.run_stocks(epoch=100)
        self.transport.amounts = {self.offers["S1"]: 100, self.offers["S2"]: 80}
        result = self.run_stocks(epoch=140)
        self.assertEqual(result["api_writes"], 0)
        self.assertTrue(result["ok"])
        self.assertTrue(all(row["stock_status"] == "readback_confirmed" for row in result["items"]))

    def test_duplicate_response_is_unknown_not_partial_success(self):
        self.submitted()
        self.transport.mode = "duplicate"
        result = self.run_stocks(epoch=100)
        self.assertEqual(result["status"], "stock_unknown")
        self.assertFalse(result["ok"])

    def test_complete_absent_first_stock_query_does_not_invent_zero_and_allows_first_assignment(self):
        self.submitted()
        self.transport.read_mode = "missing"
        result = self.run_stocks()
        self.assertEqual(result["api_writes"], 1)
        self.assertEqual(result["status"], "stock_acknowledged")
        self.assertTrue(all(row["stock_readback"]["absence"] for row in result["items"]))
        self.assertTrue(all("reserved" not in row["stock_readback"] for row in result["items"]))

    def test_incomplete_reserved_stock_read_prevents_write(self):
        self.submitted()
        self.transport.read_mode = "malformed"
        with self.assertRaises(ValueError):
            self.run_stocks()
        self.assertNotIn(service.STOCK_PATH, [row["path"] for row in self.transport.calls])

    def test_updated_receipt_without_actual_readback_is_not_stock_complete(self):
        self.submitted()
        self.transport.read_mode = "after_failed"
        result = self.run_stocks()
        self.assertEqual(result["status"], "stock_acknowledged")
        self.assertFalse(result["ok"])
        self.transport.read_mode = "success"
        second = self.run_stocks()
        self.assertEqual(second["api_writes"], 0)
        self.assertTrue(second["ok"])

    def test_history_search_pagination_and_shop_binding(self):
        self.submitted()
        service.record_import_attempt(self.directory, "other", self.payload, state="started")
        all_rows = service.list_publications(limit=1)
        self.assertEqual(all_rows["total"], 4)
        self.assertEqual(all_rows["next_offset"], 1)
        self.assertEqual(service.list_publications(shop="other", q=".8.1")["total"], 1)
        sibling = self.root / "products/P000002"
        with self.assertRaises(ValueError):
            service.record_import_attempt(sibling, "qa", self.payload, state="started")
        self.assertEqual(service.list_publications(shop="qa")["total"], 2)

    def test_legacy_import_observation_is_pure_import_not_default_stock(self):
        record_publication(self.directory, "qa", sku_id="S1", offer_id=self.offers["S1"], task_id="123", status="submitted")
        row = service.list_publications(shop="qa")["items"][0]
        self.assertEqual(row["stock_status"], "not_configured")
        self.assertNotIn("stock", row)
        self.assertNotIn("warehouse_id", row)
        with self.assertRaises(ValueError):
            self.run_stocks()

    def test_running_pid_birth_reuse_is_unknown_and_never_auto_replayed(self):
        self.submitted()
        rows = service.list_publications(shop="qa")["items"]
        request_id, allowed = service._begin_stock(self.db, "qa", rows, retry_unknown=False, epoch=100)
        self.assertTrue(request_id)
        self.assertEqual(len(allowed), 2)
        with patch.object(service, "_process_birth", return_value="different-process-birth"):
            request, allowed = service._begin_stock(self.db, "qa", rows, retry_unknown=False, epoch=140)
        self.assertIsNone(request)
        self.assertEqual(allowed, [])
        self.assertTrue(all(row["stock_status"] == "unknown" for row in service.list_publications(shop="qa")["items"]))

    def test_seller_rate_limit_is_global_not_per_product(self):
        self.submitted()
        connection = service._connect(self.db)
        with connection:
            for index in range(80):
                connection.execute("INSERT INTO stock_requests VALUES (?,?,?,?,?)", (f"old-{index}", "qa", 99, "complete", "{}"))
        connection.close()
        result = self.run_stocks(epoch=100)
        self.assertEqual(result["api_writes"], 0)
        self.assertNotIn(service.STOCK_PATH, [row["path"] for row in self.transport.calls])

    def test_production_legacy_write_boundary_records_before_timeout(self):
        from pipeline.upload import upload_product
        owner = self
        class TimeoutUploader:
            performs_api_writes = True
            name = "isolated-fixture"
            registry_path = owner.registry
            def submit(self, payload, *, store_id):
                current = service.list_publications(shop=store_id)["items"]
                owner.assertEqual(len(current), 2)
                owner.assertTrue(all(row["import_status"] == "started" for row in current))
                raise TimeoutError("fixture ambiguous import")
        with patch("pipeline.upload.build_upload_payload", return_value=self.payload), patch("pipeline.upload.payload_problems", return_value=[]):
            result = upload_product(self.directory, ["qa"], TimeoutUploader(), upload_mode="production", enabled_store_ids=["qa"])
        self.assertEqual(result["failed"], 1)
        rows = service.list_publications(shop="qa")["items"]
        self.assertTrue(all(row["import_status"] == "unknown" for row in rows))
        self.assertTrue(all(row["stock_status"] == "not_configured" for row in rows))
        self.assertTrue(all(len(row["attempts"]) == 2 for row in rows))

    def test_running_lease_is_bounded_even_with_same_process_birth(self):
        self.submitted()
        rows = service.list_publications(shop="qa")["items"]
        first, _ = service._begin_stock(self.db, "qa", rows, retry_unknown=False, epoch=100)
        self.assertTrue(first)
        second, _ = service._begin_stock(self.db, "qa", rows, retry_unknown=False, epoch=150)
        self.assertIsNone(second)
        expired, _ = service._begin_stock(self.db, "qa", rows, retry_unknown=False, epoch=800)
        self.assertIsNone(expired)
        self.assertTrue(all(row["stock_status"] == "unknown" for row in service.list_publications(shop="qa")["items"]))
        manual, allowed = service._begin_stock(self.db, "qa", rows, retry_unknown=True, epoch=810)
        self.assertTrue(manual)
        self.assertEqual(len(allowed), 2)

    def test_duplicate_offer_seller_account_is_rate_scoped_across_shop_aliases(self):
        self.submitted()
        connection = service._connect(self.db)
        with connection:
            for index in range(80):
                connection.execute("INSERT INTO stock_requests VALUES (?,?,?,?,?)", (f"alias-{index}", "other", 99, "complete", json.dumps({"account_scope": "same-seller"})))
        connection.close()
        request, allowed = service._begin_stock(self.db, "qa", service.list_publications(shop="qa")["items"], retry_unknown=False, epoch=100, account_scope="same-seller")
        self.assertIsNone(request)
        self.assertEqual(allowed, [])


class PublicationApiTests(unittest.TestCase):
    setUp = ListingPublicationTests.setUp
    configured = ListingPublicationTests.configured
    submitted = ListingPublicationTests.submitted

    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import workbench_publication_api as routes
        app = FastAPI()
        app.include_router(routes.router)
        product = patch.object(routes, "directory_for", lambda product_id, edit=True: self.directory)
        product.start()
        self.addCleanup(product.stop)
        transport = patch.object(service, "transport_for_shop", lambda *args, **kwargs: self.transport)
        transport.start()
        self.addCleanup(transport.stop)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_gets_are_cached_and_unknown_shop_rejected(self):
        client = self.client()
        self.assertEqual(client.get("/api/workbench/warehouses?shop=qa").status_code, 200)
        self.assertEqual(client.get("/api/workbench/publications").json()["items"], [])
        self.assertEqual(client.get("/api/workbench/products/P000001/publication-config?shop=qa").json()["config"]["stock"], 100)
        self.assertEqual(client.get("/api/workbench/warehouses?shop=unknown").status_code, 422)
        self.assertEqual(client.get("/api/workbench/publications?limit=101").status_code, 422)
        self.assertFalse(self.db.exists())
        self.assertEqual(self.transport.calls, [])

    def test_refresh_save_confirm_stock_lifecycle_and_frozen_edit(self):
        client = self.client()
        refreshed = client.post("/api/workbench/warehouses/refresh", json={"shop": "qa"})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        config = client.put("/api/workbench/products/P000001/publication-config", json={"shop": "qa", "warehouse_id": "22", "source_url": "https://evil.invalid/private"})
        self.assertEqual(config.status_code, 200, config.text)
        self.assertTrue(config.json()["config"]["saved"])
        self.assertEqual(config.json()["config"]["source_url"], "https://detail.1688.com/offer/123456.html")
        self.submitted()
        self.assertEqual(client.put("/api/workbench/products/P000001/publication-config", json={"shop": "qa", "warehouse_id": 22}).status_code, 422)
        invalid = client.post("/api/workbench/products/P000001/publications/continue", json={"shop": "qa", "confirm": "anything"})
        self.assertEqual(invalid.status_code, 422)
        result = client.post("/api/workbench/products/P000001/publications/continue", json={"shop": "qa", "confirm": "UPDATE_STOCK"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "stock_complete")
        self.assertEqual(read_json(self.directory / "status.json")["api_write_count"], 1)
        self.assertEqual(client.get("/api/workbench/publications?shop=qa&q=.8.1").json()["total"], 1)

    def test_note_boolean_and_inventory_shape_validated_before_any_write(self):
        client = self.client()
        for changes in ({"source_note": "x" * 501}, {"stock": True}, {"stock": -1}, {"stock_by_sku": {"unknown": 1}}):
            reply = client.put("/api/workbench/products/P000001/publication-config", json={"shop": "qa", "warehouse_id": 22, **changes})
            self.assertEqual(reply.status_code, 422, reply.text)
        self.assertNotIn(service.STOCK_PATH, [row["path"] for row in self.transport.calls])

    def test_actual_api_app_legacy_route_preserved_and_new_listing_publications_not_shadowed(self):
        from fastapi.testclient import TestClient
        import api
        self.submitted()
        write_json(self.directory / "status.json", {"status": "IMPORTED", "api_write_count": 1})
        with patch.object(api, "PRODUCTS_ROOT", self.root / "products"), \
             patch.object(api, "MARKET_DB_PATH", self.root / "runtime/market.sqlite3"), \
             TestClient(api.app, base_url="https://testserver") as client:
            legacy = client.get("/api/workbench/products/P000001/publications")
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertEqual(set(legacy.json()), {"product_id", "publications", "plan"})
            self.assertEqual(legacy.json()["product_id"], "P000001")
            self.assertIn("qa", legacy.json()["publications"]["stores"])
            actual = client.get("/api/workbench/products/P000001/listing-publications", params={"shop": "qa", "q": ".8.1"})
            self.assertEqual(actual.status_code, 200, actual.text)
            self.assertEqual(actual.json()["total"], 1)
            self.assertEqual(actual.json()["items"][0]["offer_id"], self.offers["S1"])
            self.assertEqual(actual.json()["items"][0]["source_url"], "https://detail.1688.com/offer/123456.html")
            self.assertNotIn("publications", actual.json())
            self.assertTrue(all(row["path"] != service.STOCK_PATH for row in self.transport.calls))


if __name__ == "__main__":
    unittest.main()
