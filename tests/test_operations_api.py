"""Complete operations route story, with isolated storage and fixture transports."""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from workbench_operations_api import register_operations_routes


class AdvertisingFixture:
    authorizations = []

    def __init__(self, root):
        self.root = root

    def public_status(self, shop):
        return {"shop": shop, "configured": False, "connection_status": "not_configured"}

    def authorize(self, shop, client_id, client_secret):
        self.authorizations.append((shop, client_id, client_secret))
        return {"shop": shop, "configured": True, "connection_status": "connected"}

    def list_campaigns(self, shop, **kwargs):
        return {"items": [{"id": "10", "title": "Fixture campaign", "state": "INACTIVE"}], **kwargs}

    def request_report(self, shop, campaigns, date_from, date_to):
        return {"uuid": "test-owned-report", "state": "IN_PROGRESS"}

    def poll_report(self, shop, report_id):
        return {"uuid": report_id, "state": "OK"}

    def download_report(self, shop, report_id):
        return {"rows": [{"clicks": 3}], "provenance": {"source": "fixture"}}

    def list_reports(self, shop, limit=30):
        return []


class OperationsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = FastAPI()
        self.calls = []
        AdvertisingFixture.authorizations = []
        rows = [{"shop": "a", "offer_id": "xzj.jp.10.8.1", "product_id": "P000007",
                 "source_sku_id": "red24", "source_url": "https://detail.1688.com/offer/123.html",
                 "source_note": "红色/内白 24cm/3.5L", "ozon_product_id": "101", "import_status": "imported"},
                {"shop": "b", "offer_id": "xzj.jp.10.8.1", "product_id": "P000008",
                 "source_note": "绿色", "ozon_product_id": "102"}]

        def transport(shop):
            self.calls.append(shop)
            raise RuntimeError("fixture-network-disabled")

        def secure(request):
            if request.url.scheme != "https":
                raise HTTPException(403, "请通过 HTTPS 授权")

        register_operations_routes(self.app, runtime_root=lambda: self.root, seller_transport=transport,
                                   shop_rows=lambda: [{"id": "a", "name": "A", "enabled": True},
                                                      {"id": "b", "name": "B", "enabled": False}],
                                   credential_context=lambda req: {"can_submit_credentials": req.url.scheme == "https"},
                                   require_credentials=secure, publication_rows=lambda: rows,
                                   performance_factory=AdvertisingFixture, start_worker=False)
        self.client = TestClient(self.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_discover_cached_read_and_shop_isolation(self):
        result = self.client.get("/api/operations/config")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["advertising_write_enabled"])
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.post("/api/operations/discover", json={}).status_code, 200)
        first = self.client.get("/api/operations/products", params={"shop": "a"}).json()
        self.assertEqual(first["total"], 1)
        detail = self.client.get("/api/operations/product", params={"shop": "a", "offer_id": first["items"][0]["offer_id"]})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["product"]["source_note"], "红色/内白 24cm/3.5L")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.get("/api/operations/products", params={"shop": "not-bound"}).status_code, 404)

    def test_manual_job_durable_and_schedule_off_by_default(self):
        self.client.post("/api/operations/discover", json={})
        payload = {"shop": "a", "offer_id": "xzj.jp.10.8.1", "days": 7, "include_traffic": False}
        result = self.client.post("/api/operations/sync", json=payload)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get("/api/operations/jobs?shop=a").json()["items"][0]["id"], result.json()["job"]["id"])
        self.assertEqual(self.calls, [])  # enqueue doesn't directly access Ozon
        self.assertFalse(self.client.get("/api/operations/schedule?shop=a").json()["enabled"])
        changed = self.client.put("/api/operations/schedule", json={"shop": "a", "enabled": True, "days": 7, "interval_hours": 24})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(self.client.get("/api/operations/schedule?shop=a").json()["enabled"])
        self.client.put("/api/operations/schedule", json={"shop": "a", "enabled": False})
        self.assertFalse(self.client.get("/api/operations/schedule?shop=a").json()["enabled"])
        payload["shop"] = "b"
        self.assertEqual(self.client.post("/api/operations/sync", json=payload).status_code, 409)

    def test_csrf_credentials_and_read_only_ad_report_story(self):
        secret = "never-return-this-fixture-secret"
        payload = {"shop": "a", "client_id": "fixture@advertising.performance.ozon.ru", "client_secret": secret}
        result = self.client.post("/api/operations/advertising/authorize", json=payload, headers={"origin": "https://evil.invalid"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(AdvertisingFixture.authorizations, [])
        with TestClient(self.app, base_url="http://testserver") as insecure:
            self.assertEqual(insecure.post("/api/operations/advertising/authorize", json=payload).status_code, 403)
        result = self.client.post("/api/operations/advertising/authorize", json=payload)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(secret, result.text)
        malformed = self.client.post("/api/operations/advertising/authorize", json={**payload, "shop": "bad/id", "extra": secret})
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn(secret, malformed.text)
        self.assertEqual(self.client.get("/api/operations/advertising/campaigns?shop=a").json()["items"][0]["id"], "10")
        result = self.client.post("/api/operations/advertising/reports", json={"shop": "a", "campaigns": ["10"], "date_from": "2026-10-01", "date_to": "2026-10-07"})
        self.assertEqual(result.json()["status"], "IN_PROGRESS")
        result = self.client.get("/api/operations/advertising/reports/test-owned-report?shop=a")
        self.assertEqual(result.json()["rows"], [{"clicks": 3}])

    def test_forbidden_write_intents_and_bad_values(self):
        self.assertEqual(self.client.post("/api/operations/advertising/activate", json={"shop": "a"}).status_code, 404)
        self.assertEqual(self.client.post("/api/operations/sync", json={"shop": "a", "offer_id": "x", "days": 365}).status_code, 422)
        self.assertEqual(self.client.put("/api/operations/schedule", json={"shop": "a", "enabled": True, "interval_hours": 1}).status_code, 422)
        self.assertEqual(self.client.get("/api/operations/products?shop=../x").status_code, 422)


class OperationsWorkerTests(unittest.TestCase):
    def test_disabled_shop_does_not_contact_provider(self):
        from pipeline.operations import Store
        from pipeline.operations_worker import OperationsWorker
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "operations.sqlite3")
            store.discover([{"shop": "a", "offer_id": "x", "product_id": "P000007"}])
            store.enqueue("a", "x")
            contacts = []
            worker = OperationsWorker(store=lambda: store, shop_enabled=lambda _: False,
                                      seller_transport=lambda shop: contacts.append(shop))
            self.assertTrue(worker.tick())
            job = store.jobs()[0]
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error_code"], "shop_disabled")
            self.assertEqual(contacts, [])

    def test_failures_are_bounded_durable_and_redacted(self):
        from pipeline.operations import Store
        from pipeline.operations_worker import OperationsWorker
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "operations.sqlite3")
            store.discover([{"shop": "a", "offer_id": "x", "product_id": "P000007"}])
            store.enqueue("a", "x")
            worker = OperationsWorker(store=lambda: store, shop_enabled=lambda _: True,
                                      seller_transport=lambda _: (_ for _ in ()).throw(RuntimeError("secret-provider-error")))
            worker.tick()
            self.assertNotIn("secret-provider-error", json.dumps(store.jobs()))


if __name__ == "__main__":
    unittest.main()
