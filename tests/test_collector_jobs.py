"""Capture snapshots remain isolated after browser navigation/closure."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from collector.ingest import CaptureValidationError, ingest_capture
from collector.jobs import CaptureJobs
from workbench_collection_jobs_api import create_router


def capture(offer="1053070588316", colour="红色/内白"):
    return {"source_url": f"https://detail.1688.com/offer/{offer}.html", "title_cn": "铸铁珐琅锅",
            "collection_mode": "all_skus", "captured_at": "2026-10-08T02:00:00Z",
            "skus": [{"sku_id": f"sku-{offer}", "sku_name": f"{colour} 24cm/3.5L",
                      "option_values": [{"name_cn": "颜色", "value_cn": colour},
                                        {"name_cn": "规格", "value_cn": "24cm/3.5L"}], "purchase_price": 110}]}


class CaptureJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = CaptureJobs(self.root / "products", self.root / "jobs.sqlite3")

    def tearDown(self):
        self.jobs.close()
        self.temp.cleanup()

    def wait(self, request_id):
        for _ in range(200):
            result = self.jobs.read(request_id, "test-device-1234")
            if result["state"] not in {"queued", "running"}:
                return result
            time.sleep(.01)
        self.fail("collector job never completed")

    def test_page_navigation_after_acceptance_cannot_change_queued_snapshot(self):
        gate, entered = threading.Event(), threading.Event()
        original = capture()
        from collector.ingest import _copy_images
        def delayed(*args, **kwargs):
            entered.set()
            gate.wait(2)
            return _copy_images(*args, **kwargs)
        with patch("collector.ingest._copy_images", delayed):
            accepted = self.jobs.enqueue("immutable-request-001", "test-device-1234", original)
            self.assertEqual(accepted["state"], "queued")
            self.assertTrue(entered.wait(1))
            # Simulate the popup disappearing and the same tab showing another offer.
            original["source_url"] = "https://detail.1688.com/offer/9999999999999.html"
            original["skus"][0]["sku_name"] = "另一件商品"
            gate.set()
            result = self.wait("immutable-request-001")
        self.assertEqual(result["state"], "completed")
        source = json.loads((self.jobs.products_root / result["result"]["product_id"] / "input/source.json").read_text("utf-8"))
        self.assertIn("1053070588316", source["source_url"])
        self.assertEqual(source["skus"][0]["sku_name"], "红色/内白 24cm/3.5L")
        self.assertTrue(source["sku_selection_required"])

    def test_two_offers_download_concurrently_without_product_id_collision(self):
        barrier = threading.Barrier(2)
        from collector.ingest import _copy_images
        def overlap(*args, **kwargs):
            barrier.wait(2)
            return _copy_images(*args, **kwargs)
        with patch("collector.ingest._copy_images", overlap):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda offer: ingest_capture(self.jobs.products_root, capture(offer)),
                                        ["1053070588316", "1053070588317"]))
        self.assertEqual({row["product_id"] for row in results}, {"P000001", "P000002"})
        self.assertEqual(len(list(self.jobs.products_root.glob("P*/status.json"))), 2)

    def test_replay_new_version_request_is_exactly_once(self):
        self.jobs.enqueue("repeat-version-request1", "test-device-1234", capture(), True)
        first = self.wait("repeat-version-request1")
        replay = self.jobs.enqueue("repeat-version-request1", "test-device-1234", capture(), True)
        self.assertEqual(first["result"]["product_id"], replay["result"]["product_id"])
        self.assertEqual(len(list(self.jobs.products_root.glob("P*/status.json"))), 1)

    def test_replay_cannot_switch_payload_device_or_version_choice(self):
        self.jobs.enqueue("bound-device-request1", "test-device-1234", capture())
        self.wait("bound-device-request1")
        for payload, device, new_version in [(capture("1053070588317"), "test-device-1234", False),
                                             (capture(), "other-device-123", False),
                                             (capture(), "test-device-1234", True)]:
            with self.assertRaises(CaptureValidationError):
                self.jobs.enqueue("bound-device-request1", device, payload, new_version)
        with self.assertRaises(KeyError):
            self.jobs.read("bound-device-request1", "other-device-123")

    def test_ingest_request_id_is_bound_to_exact_payload(self):
        payload = {**capture(), "capture_request_id": "direct-request-bound1"}
        first = ingest_capture(self.jobs.products_root, payload, allow_new_version=True)
        second = ingest_capture(self.jobs.products_root, payload, allow_new_version=True)
        self.assertEqual(first["product_id"], second["product_id"])
        with self.assertRaises(CaptureValidationError):
            ingest_capture(self.jobs.products_root, {**payload, "title_cn": "changed"}, allow_new_version=True)

    def test_duplicate_is_a_visible_terminal_state_and_never_overwrites(self):
        self.jobs.enqueue("duplicate-request-first", "test-device-1234", capture())
        self.wait("duplicate-request-first")
        self.jobs.enqueue("duplicate-request-second", "test-device-1234", capture())
        second = self.wait("duplicate-request-second")
        self.assertEqual(second["state"], "duplicate")
        self.assertEqual(second["result"]["duplicate_of"], "P000001")

    def test_dead_worker_completed_request_is_reconciled_without_reposting(self):
        self.jobs.enqueue("recover-completed-job1", "test-device-1234", capture())
        self.wait("recover-completed-job1")
        with self.jobs._connect() as connection:
            connection.execute("UPDATE captures SET state='running', worker_pid=99999999, worker_birth='dead'")
        other = CaptureJobs(self.jobs.products_root, self.jobs.db_path)
        try:
            with patch("collector.jobs.ingest_capture", side_effect=AssertionError("must not repost")):
                result = other.read("recover-completed-job1", "test-device-1234")
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["result"]["product_id"], "P000001")
        finally:
            other.close()

    def test_another_live_worker_is_not_marked_interrupted(self):
        self.jobs.enqueue("preserve-live-worker1", "test-device-1234", capture())
        self.wait("preserve-live-worker1")
        with self.jobs._connect() as connection:
            connection.execute("UPDATE captures SET state='running', worker_pid=1234, worker_birth='real'")
        other = CaptureJobs(self.jobs.products_root, self.jobs.db_path)
        try:
            with patch("collector.jobs._pid_alive", return_value=True), patch("collector.jobs._process_birth", return_value="real"):
                self.assertEqual(other.read("preserve-live-worker1", "test-device-1234")["state"], "running")
        finally:
            other.close()

    def test_worker_error_does_not_expose_signed_urls(self):
        with patch("collector.jobs.ingest_capture", side_effect=RuntimeError("https://video.example/mp4?sign=SECRET")):
            self.jobs.enqueue("failed-private-job1", "test-device-1234", capture())
            result = self.wait("failed-private-job1")
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_router_resolves_fixture_paths_after_registration(self):
        paths = {"root": self.root / "old", "db": self.root / "old.sqlite3"}
        app = FastAPI()
        app.include_router(create_router(lambda: paths["root"], lambda: paths["db"]))
        paths.update(root=self.root / "isolated", db=self.root / "isolated.sqlite3")
        with TestClient(app) as client:
            response = client.post("/api/collector/jobs", headers={"X-Factory-Device-Id": "test-device-1234"},
                                   json={"request_id": "router-fixture-request", "capture": capture()})
            self.assertEqual(response.status_code, 202)
            for _ in range(200):
                body = client.get("/api/collector/jobs/router-fixture-request",
                                  headers={"X-Factory-Device-Id": "test-device-1234"}).json()
                if body["state"] == "completed":
                    break
                time.sleep(.01)
            self.assertEqual(body["state"], "completed")
        self.assertFalse((self.root / "old").exists())
        self.assertTrue((self.root / "isolated" / "P000001" / "status.json").is_file())
