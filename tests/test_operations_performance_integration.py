"""Real FastAPI -> PerformanceAccess -> fake HTTP wire -> encrypted SQLite story.

Only the HTTP transport is substituted. No PerformanceAccess class mocking and
no real Ozon requests, campaign updates, keyword writes or model calls.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pipeline.performance_access import PerformanceAccess, WireResponse
from workbench_operations_api import register_operations_routes

CLIENT_ID = "integration-fixture@advertising.performance.ozon.ru"
CLIENT_SECRET = "integration-fixture-secret-never-output"
TOKEN = "integration-fixture-token-never-output"
REPORT_UUID = "c2d233ca-ed34-4f3c-8613-ac1ed15bad61"


class FakeWire:
    def __init__(self):
        self.calls = []
        self.poll_count = 0

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if (method, path) == ("POST", "/api/client/token"):
            self.assert_credentials(kwargs["body"])
            value = {"access_token": TOKEN, "expires_in": 1800, "token_type": "Bearer"}
        elif (method, path) == ("GET", "/api/client/campaign"):
            if kwargs["token"] != TOKEN:
                raise AssertionError("token not used")
            value = {"list": [{"id": "19", "title": "Offline fixture campaign", "dailyBudget": "100000000", "state": "CAMPAIGN_STATE_RUNNING"}]}
        elif (method, path) == ("POST", "/api/client/statistics"):
            value = {"UUID": REPORT_UUID}
        elif (method, path) == ("GET", "/api/client/statistics/" + REPORT_UUID):
            self.poll_count += 1
            value = {"UUID": REPORT_UUID, "state": "IN_PROGRESS" if self.poll_count == 1 else "OK",
                     "link": "https://unsafe.invalid/ignored?token=" + TOKEN}
        elif (method, path) == ("GET", "/api/client/statistics/report"):
            if kwargs["query"] != {"UUID": REPORT_UUID}:
                raise AssertionError("wrong report")
            return WireResponse("; Кампания 19\nsku;Название товара;Показы;Клики;Расход, Р, с НДС\n777;Игрушка;100;5;5,00\n".encode("utf-8"), "text/csv; charset=UTF-8")
        else:
            raise AssertionError("unsupported endpoint")
        return WireResponse(json.dumps(value).encode("utf-8"))

    @staticmethod
    def assert_credentials(payload):
        if payload != {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials"}:
            raise AssertionError("incorrect token exchange body")


class OperationsPerformanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wire = FakeWire()
        self.now = 1791403200.0
        self.app = FastAPI()
        self.seller_calls = []

        def no_seller_requests(shop):
            self.seller_calls.append(shop)
            raise AssertionError("advertising must not use Seller credentials")

        def secure(request):
            if request.url.scheme != "https":
                raise HTTPException(403, "广告凭据需要 HTTPS")

        register_operations_routes(
            self.app, runtime_root=lambda: self.root,
            seller_transport=no_seller_requests,
            shop_rows=lambda: [{"id": "a", "enabled": True, "name": "A"}, {"id": "b", "enabled": True, "name": "B"}],
            credential_context=lambda req: {"can_submit_credentials": req.url.scheme == "https"},
            require_credentials=secure, publication_rows=lambda: [], start_worker=False,
            performance_factory=lambda root: PerformanceAccess(root, transport=self.wire, clock=lambda: self.now),
        )
        self.client = TestClient(self.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def payload(self, **extra):
        return {"shop": "a", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **extra}

    def authorize(self):
        response = self.client.post("/api/operations/advertising/authorize", json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["configured"])
        self.assert_secrets_absent(response.text)

    def assert_secrets_absent(self, text):
        for secret in (CLIENT_ID, CLIENT_SECRET, TOKEN):
            self.assertNotIn(secret, text)

    def test_full_readonly_performance_story_with_persistent_real_service(self):
        before = self.client.get("/api/operations/advertising/status?shop=a")
        self.assertEqual(before.status_code, 200)
        self.assertFalse(before.json()["configured"])
        self.assertEqual(self.wire.calls, [])
        self.authorize()
        self.assertTrue(self.client.get("/api/operations/advertising/status?shop=a").json()["ready"])
        vault = self.root / "performance"
        self.assertTrue((vault / "performance-access.sqlite3").exists())
        self.assertTrue((vault / "master.key").exists())
        for path in vault.iterdir():
            if path.is_file():
                self.assert_secrets_absent(path.read_bytes().decode("utf-8", errors="ignore"))
        campaigns = self.client.get("/api/operations/advertising/campaigns?shop=a")
        self.assertEqual(campaigns.status_code, 200)
        self.assertEqual(campaigns.json()["items"][0]["id"], "19")
        self.assertEqual(campaigns.json()["budget_divisor"], 1_000_000)
        before_reports = self.client.get("/api/operations/advertising/reports?shop=a")
        self.assertEqual(before_reports.json()["items"], [])

        payload = {"shop": "a", "campaigns": ["19"], "date_from": "2026-10-01", "date_to": "2026-10-07"}
        created = self.client.post("/api/operations/advertising/reports", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        local_id = created.json()["report_id"]
        self.assertEqual(created.json()["status"], "NOT_STARTED")
        body = next(kwargs["body"] for _, path, kwargs in self.wire.calls if path == "/api/client/statistics")
        self.assertEqual(body, {"campaigns": ["19"], "dateFrom": "2026-10-01", "dateTo": "2026-10-07", "groupBy": "DATE"})
        reports = self.client.get("/api/operations/advertising/reports?shop=a")
        self.assertEqual(reports.status_code, 200)
        self.assertEqual(reports.json()["items"][0]["report_id"], local_id)
        self.assertEqual(self.client.get("/api/operations/advertising/reports/" + local_id + "?shop=b").status_code, 422)

        polled = self.client.get("/api/operations/advertising/reports/" + local_id + "?shop=a")
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.json()["status"], "IN_PROGRESS")
        self.now += 6
        ready = self.client.get("/api/operations/advertising/reports/" + local_id + "?shop=a")
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json()["status"], "OK")
        self.assertEqual(ready.json()["rows"][0]["derived"]["ctr_percent"], 5)
        self.assertEqual(ready.json()["rows"][0]["raw"]["Расход, Р, с НДС"], "5,00")
        self.assertFalse(ready.json()["ad_writes_performed"])
        self.assertFalse(ready.json()["library_writes_performed"])
        self.assert_secrets_absent(ready.text)
        self.assertNotIn("unsafe.invalid", ready.text)
        count = len(self.wire.calls)
        cached = self.client.get("/api/operations/advertising/reports/" + local_id + "?shop=a")
        self.assertEqual(cached.json()["rows"], ready.json()["rows"])
        self.assertEqual(len(self.wire.calls), count)
        self.assertTrue(self.client.get("/api/operations/advertising/reports?shop=a").json()["items"][0]["downloaded"])
        self.assertEqual(self.seller_calls, [])
        for response in (before, before_reports, campaigns, reports, created, polled, ready, cached):
            self.assertEqual(response.headers.get("cache-control"), "private, no-store")
        self.assertTrue(all(path in {"/api/client/token", "/api/client/campaign", "/api/client/statistics", "/api/client/statistics/" + REPORT_UUID, "/api/client/statistics/report"} for _, path, _ in self.wire.calls))

    def test_secret_validation_security_boundary_and_disabled_mutation_routes(self):
        for data in (self.payload(shop="../other"), self.payload(extra=CLIENT_SECRET), {"shop": "a", "client_id": CLIENT_ID}):
            response = self.client.post("/api/operations/advertising/authorize", json=data)
            self.assertEqual(response.status_code, 422)
            self.assert_secrets_absent(response.text)
        response = self.client.post("/api/operations/advertising/authorize", json=self.payload(), headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(response.status_code, 403)
        with TestClient(self.app, base_url="http://testserver") as insecure:
            self.assertEqual(insecure.post("/api/operations/advertising/authorize", json=self.payload()).status_code, 403)
        self.assertEqual(self.wire.calls, [])
        for suffix in ("activate", "budget", "bid"):
            self.assertEqual(self.client.post("/api/operations/advertising/" + suffix, json={"shop": "a"}).status_code, 404)
        self.assertFalse(self.root.joinpath("performance").exists())


if __name__ == "__main__":
    unittest.main()
