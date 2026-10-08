"""Offline post-listing monitoring contracts, tenancy, quotas and recovery."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error

from pipeline import operations as op
from pipeline.ozon_http import OzonCredentials, OzonHttpError, UrllibTransport


class ErrorWithSecret(RuntimeError):
    def __init__(self, status=403):
        super().__init__("provider error Api-Key=super-secret client_id=123")
        self.status = status


class Seller:
    def __init__(self, clock, offer="xzj.jp.10.8.1", account="123"):
        self.clock = clock
        self.offer = offer
        self.credentials = SimpleNamespace(client_id=account, api_key="super-secret")
        self.calls = []
        self.fail = {}
        self.sku = 7654321
        self.info = {"items": [{"offer_id": offer, "id": 9876, "sku": self.sku,
            "name": "Игрушка", "currency_code": "RUB", "price": "199.00",
            "is_archived": False, "is_autoarchived": False, "errors": [],
            "statuses": {"is_created": True, "moderate_status": "approved", "status": "active"},
            "stocks": {"has_stock": True}, "visibility_details": {"has_price": True}}]}
        self.query_response = {"queries": [{"sku": self.sku, "query": "игрушка антистресс", "order_count": 1,
            "unique_search_users": 15, "gmv": 199, "currency": "RUB"}], "total": 1, "page_count": 1}

    def post(self, path, body):
        self.calls.append((path, deepcopy(body)))
        if path in self.fail:
            raise self.fail[path]
        if path == op.INFO_PATH:
            return deepcopy(self.info)
        if path == op.METRICS_PATH:
            return {"result": {"data": [{"dimensions": [{"id": str(self.sku)}, {"id": body["date_to"]}],
                "metrics": [199, 1] + [None] * (len(body["metrics"]) - 2)}], "totals": [199, 1]}}
        if path == op.QUERIES_PATH:
            return deepcopy(self.query_response)
        raise AssertionError("Read-only path whitelist violated: " + path)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "runtime/operations.sqlite3"
        self.now = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc).timestamp()
        self.clock = lambda: self.now
        self.store = op.Store(self.path, clock=self.clock)
        self.offer = "xzj.jp.10.8.1"
        self.rows = [{"shop": "qa", "offer_id": self.offer, "product_id": "P000007", "source_sku_id": "S1",
            "ozon_product_id": "9876", "source_url": "https://detail.1688.com/offer/123.html", "source_note": "红色/内白 24cm/3.5L"}]
        self.store.discover(self.rows)
        self.transport = Seller(self.clock, self.offer)

    def test_discovery_is_scoped_immutable_and_projection_only(self):
        self.store.discover([{**self.rows[0], "api_key": "forbidden", "attempts": [{"secret": "forbidden"}]}])
        product = self.store.product("qa", self.offer)
        self.assertNotIn("api_key", product)
        self.assertNotIn("attempts", product)
        result = self.store.discover([{**self.rows[0], "product_id": "P000008", "source_note": "wrong"}])
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(self.store.product("qa", self.offer)["source_note"], self.rows[0]["source_note"])
        self.store.discover([{**self.rows[0], "shop": "other", "product_id": "P000008"}])
        self.assertEqual(self.store.list_products(shop="qa")["total"], 1)
        self.assertEqual(self.store.product("other", self.offer)["product_id"], "P000008")

    def test_search_matches_offer_product_spec_and_name_literally(self):
        self.store._observe_product("qa", self.offer, {"name": "Игрушка 100%_A"})
        for term in ("xzj.jp", "P000007", "24cm/3.5L", "Игрушка", "100%_A"):
            self.assertEqual(self.store.list_products(shop="qa", q=term)["total"], 1, term)
        for term in ("source_note", "name", "%_B", "runtime"):
            self.assertEqual(self.store.list_products(shop="qa", q=term)["total"], 0, term)
        self.store.discover([{**self.rows[0], "shop": "other", "source_note": "独有规格"}])
        self.assertEqual(self.store.list_products(shop="qa", q="独有规格")["total"], 0)
        self.assertEqual(self.store.list_products(shop="other", q="独有规格")["total"], 1)

    def test_fresh_health_analytics_and_query_dates_are_distinct(self):
        result = op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertFalse(result["partial"])
        detail = self.store.detail("qa", self.offer)
        metrics = next(r for r in detail["snapshots"] if r["kind"] == "metrics")
        queries = detail["queries"]
        self.assertEqual((metrics["date_from"], metrics["date_to"]), ("2026-10-01", "2026-10-07"))
        self.assertEqual((queries["date_from"], queries["date_to"]), ("2026-09-30", "2026-10-06"))
        self.assertIsNone(queries["items"][0]["unique_view_users"])
        self.assertIsNone(metrics["data"]["totals"]["hits_view_search"])
        self.assertFalse(metrics["data"]["missing_days_are_zero"])
        self.assertEqual(detail["product"]["ozon_sku"], str(self.transport.sku))
        request = next(b for p, b in self.transport.calls if p == op.QUERIES_PATH)
        self.assertEqual(request["page"], 0)
        self.assertEqual(request["page_size"], 100)
        self.assertEqual(request["limit_by_sku"], 15)
        self.assertEqual({p for p, _ in self.transport.calls}, {op.INFO_PATH, op.METRICS_PATH, op.QUERIES_PATH})

    def test_basic_metrics_can_recover_membership_restrictions(self):
        op.sync_product(self.store, "qa", self.offer, self.transport, include_traffic=False)
        body = next(b for p, b in self.transport.calls if p == op.METRICS_PATH)
        self.assertEqual(body["metrics"], ["revenue", "ordered_units"])

    def test_permission_and_provider_text_never_become_metrics_or_secret(self):
        self.transport.fail[op.METRICS_PATH] = ErrorWithSecret()
        result = op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertTrue(result["partial"])
        detail = self.store.detail("qa", self.offer)
        metrics = next(r for r in detail["snapshots"] if r["kind"] == "metrics")
        self.assertEqual(metrics["status"], "permission_required")
        self.assertTrue(all(v is None for v in metrics["data"]["totals"].values()))
        self.assertNotIn("super-secret", json.dumps(detail))
        self.assertEqual(detail["queries"]["status"], "available")

    def test_missing_sku_blocks_queries_and_analytics_even_if_old_binding_exists(self):
        self.store._observe_product("qa", self.offer, {"ozon_sku": "123456"})
        del self.transport.info["items"][0]["sku"]
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.detail("qa", self.offer)["queries"]["status"], "not_ready")

    def test_unique_source_sku_can_recover_but_ambiguous_sources_cannot(self):
        item = self.transport.info["items"][0]
        del item["sku"]
        item["sources"] = [{"sku": self.transport.sku}, {"sku": self.transport.sku}]
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertEqual(self.store.product("qa", self.offer)["ozon_sku"], str(self.transport.sku))
        item["sources"] = [{"sku": self.transport.sku}, {"sku": 8888888}]
        self.transport.calls.clear()
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertEqual(len(self.transport.calls), 1)

    def test_wrong_offer_response_cannot_supply_other_sku(self):
        self.transport.info["items"][0]["offer_id"] = "foreign"
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertNotIn("ozon_sku", self.store.product("qa", self.offer))

    def test_product_id_binding_conflict_fails_closed(self):
        self.transport.info["items"][0]["id"] = 111
        result = op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertTrue(result["partial"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.product("qa", self.offer)["ozon_product_id"], "9876")

    def test_empty_response_is_no_data_not_zero(self):
        original = self.transport.post
        def post(path, body):
            if path == op.METRICS_PATH:
                return {"result": {"data": [], "totals": []}}
            return original(path, body)
        self.transport.post = post
        self.transport.query_response = {"queries": [], "page_count": 0}
        op.sync_product(self.store, "qa", self.offer, self.transport)
        snapshots = self.store.detail("qa", self.offer)["snapshots"]
        self.assertEqual(next(s for s in snapshots if s["kind"] == "metrics")["status"], "no_data")
        self.assertEqual(self.store.detail("qa", self.offer)["queries"]["status"], "no_data")

    def test_cross_sku_query_response_is_not_persisted(self):
        self.transport.query_response["queries"][0]["sku"] = 999
        op.sync_product(self.store, "qa", self.offer, self.transport)
        queries = self.store.detail("qa", self.offer)["queries"]
        self.assertEqual(queries["status"], "error")
        self.assertEqual(queries["items"], [])

    def test_query_page_prose_limits_and_pagination_are_bounded(self):
        self.transport.query_response["page_count"] = 1000000
        self.transport.query_response["queries"] = [{"sku": self.transport.sku, "query": "term" + str(i)} for i in range(20)]
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertEqual(len(self.store.detail("qa", self.offer)["queries"]["items"]), 15)
        self.assertTrue(self.store.detail("qa", self.offer)["queries"]["partial"])
        self.assertEqual(sum(p == op.QUERIES_PATH for p, _ in self.transport.calls), 1)

    def test_throttle_is_per_client_across_shops_and_restarts(self):
        self.assertTrue(self.store.reserve_analytics("shared") ["allowed"])
        restarted = op.Store(self.path, clock=self.clock)
        self.assertFalse(restarted.reserve_analytics("shared")["allowed"])
        self.assertTrue(restarted.reserve_analytics("other")["allowed"])
        self.now += 61
        self.assertTrue(restarted.reserve_analytics("shared")["allowed"])
        with self.path.open("rb") as file:
            self.assertNotIn(b"shared", file.read())

    def test_daily_quota_and_utc_rollover(self):
        for _ in range(50):
            self.assertTrue(self.store.reserve_analytics("account")["allowed"])
            self.now += 61
        blocked = self.store.reserve_analytics("account")
        self.assertEqual(blocked["code"], "daily_quota_guard")
        self.now += 24 * 3600
        self.assertTrue(self.store.reserve_analytics("account")["allowed"])

    def test_unknown_account_identity_does_not_send_analytics(self):
        self.transport.credentials = None
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertFalse(any(p == op.METRICS_PATH for p, _ in self.transport.calls))
        metrics = next(r for r in self.store.detail("qa", self.offer)["snapshots"] if r["kind"] == "metrics")
        self.assertEqual(metrics["error_code"], "account_identity_unavailable")

    def test_queue_deduplication_and_cross_shop_separation(self):
        job = self.store.enqueue("qa", self.offer)
        self.assertEqual(self.store.enqueue("qa", self.offer)["id"], job["id"])
        self.store.discover([{**self.rows[0], "shop": "other"}])
        self.assertNotEqual(self.store.enqueue("other", self.offer)["id"], job["id"])
        self.assertEqual(len(self.store.jobs(shop="qa")), 1)

    def test_lease_recovery_disallows_old_owner_completion(self):
        job = self.store.enqueue("qa", self.offer)
        claimed = self.store.claim_job("old", lease_seconds=30)
        self.assertEqual(claimed["id"], job["id"])
        self.assertIsNone(self.store.claim_job("new"))
        self.now += 31
        replacement = self.store.claim_job("new")
        self.assertEqual(replacement["attempts"], 2)
        with self.assertRaises(ValueError):
            self.store.complete_job(job["id"], "old")
        self.store.complete_job(job["id"], "new", {"partial": False})
        self.assertEqual(self.store.jobs()[0]["status"], "completed")

    def test_retry_attempts_and_error_are_bounded(self):
        job = self.store.enqueue("qa", self.offer)
        for attempt in range(3):
            claimed = self.store.claim_job("worker")
            self.assertEqual(claimed["attempts"], attempt + 1)
            self.store.fail_job(job["id"], "worker", "network_unavailable", True)
            self.now += 200
        self.assertIsNone(self.store.claim_job("worker"))
        self.assertEqual(self.store.jobs()[0]["status"], "failed")

    def test_run_job_and_deferred_resume_without_sleep(self):
        self.store.reserve_analytics("123")
        job = self.store.enqueue("qa", self.offer)
        claimed = self.store.claim_job("worker")
        result = op.run_job(self.store, claimed, self.transport)
        self.assertTrue(result["partial"])
        deferred = self.store.jobs()[0]
        self.assertEqual(deferred["status"], "retry_wait")
        self.assertEqual(deferred["attempts"], 0)
        self.assertIsNone(self.store.claim_job("worker"))
        self.now += 61
        op.run_job(self.store, self.store.claim_job("worker"), self.transport)
        self.assertEqual(self.store.jobs()[0]["status"], "completed")
        self.assertEqual(self.store.jobs()[0]["id"], job["id"])

    def test_opt_in_schedule_is_disabled_until_saved(self):
        self.assertFalse(self.store.schedule("qa")["enabled"])
        self.now += 2 * 24 * 3600
        self.assertEqual(self.store.enqueue_due()["enqueued"], 0)
        self.store.save_schedule("qa", True, interval_hours=1)
        self.now += 3601
        self.assertEqual(self.store.enqueue_due()["enqueued"], 1)
        self.assertEqual(self.store.enqueue_due()["enqueued"], 0)
        self.store.save_schedule("qa", False)
        self.now += 2 * 24 * 3600
        self.assertEqual(self.store.enqueue_due()["enqueued"], 0)

    def test_health_priority_and_no_claimed_causal_keyword_conclusion(self):
        self.transport.info["items"][0]["stocks"]["has_stock"] = False
        op.sync_product(self.store, "qa", self.offer, self.transport)
        notes = self.store.detail("qa", self.offer)["diagnostics"]
        self.assertEqual(notes[0]["code"], "no_stock")
        self.assertTrue(any(n["code"] == "query_sales_observed" for n in notes))
        self.assertNotIn("标题没覆盖", json.dumps(notes, ensure_ascii=False))

    def test_moderation_rejection_is_a_blocker_even_without_errors(self):
        self.transport.info["items"][0]["statuses"]["moderate_status"] = "declined"
        op.sync_product(self.store, "qa", self.offer, self.transport)
        self.assertEqual(self.store.detail("qa", self.offer)["diagnostics"][0]["code"], "listing_not_ready")

    def test_schedule_queue_and_advance_are_atomic_and_deduplicated(self):
        self.store.save_schedule("qa", True, interval_hours=1)
        self.now += 3601
        self.store.enqueue_due()
        restarted = op.Store(self.path, clock=self.clock)
        self.assertEqual(restarted.enqueue_due()["enqueued"], 0)
        self.assertEqual(len(restarted.jobs()), 1)

    def test_invalid_parameters_and_untracked_offer_fail_before_network(self):
        with self.assertRaises(ValueError):
            op.sync_product(self.store, "other", self.offer, self.transport)
        with self.assertRaises(ValueError):
            self.store.enqueue("qa", self.offer, days=365)
        with self.assertRaises(ValueError):
            self.store.save_schedule("qa", True, interval_hours=0)
        self.assertEqual(self.transport.calls, [])

    def test_metrics_currency_does_not_copy_listing_currency(self):
        op.sync_product(self.store, "qa", self.offer, self.transport)
        data = next(r for r in self.store.detail("qa", self.offer)["snapshots"] if r["kind"] == "metrics")["data"]
        self.assertIsNone(data["currency"])
        self.assertEqual(data["price_currency"], "RUB")
        self.assertFalse(data["revenue_currency_verified"])

    def test_thirty_day_query_period_explicitly_excludes_two_immature_days(self):
        op.sync_product(self.store, "qa", self.offer, self.transport, period_days=30)
        queries = self.store.detail("qa", self.offer)["queries"]
        self.assertEqual(queries["requested_days"], 30)
        self.assertEqual(queries["effective_days"], 28)
        self.assertEqual((queries["date_from"], queries["date_to"]), ("2026-09-09", "2026-10-06"))

    def test_provider_transient_failure_automatically_retries_but_is_bounded(self):
        self.transport.fail[op.INFO_PATH] = ErrorWithSecret(status=503)
        self.store.enqueue("qa", self.offer)
        for _ in range(3):
            job = self.store.claim_job("worker")
            op.run_job(self.store, job, self.transport)
            self.now += 300
        self.assertEqual(self.store.jobs()[0]["status"], "failed")
        self.assertEqual(self.store.jobs()[0]["error_code"], "provider_unavailable")
        self.assertNotIn("super-secret", json.dumps(self.store.detail("qa", self.offer)))

    def test_query_transient_failure_automatically_retries_without_losing_snapshots(self):
        self.transport.fail[op.QUERIES_PATH] = ErrorWithSecret(status=429)
        self.store.enqueue("qa", self.offer)
        result = op.run_job(self.store, self.store.claim_job("worker"), self.transport)
        self.assertEqual(result["retryable_error"], "rate_limited")
        self.assertEqual(self.store.jobs()[0]["status"], "retry_wait")
        self.assertEqual(len(self.store.detail("qa", self.offer)["snapshots"]), 3)
        self.assertIsNone(self.store.claim_job("worker"))

    def test_quota_deferrals_are_bounded_not_an_endless_loop(self):
        self.store.enqueue("qa", self.offer)
        for _ in range(4):
            # Another shop sharing the same client owns the current minute slot.
            self.assertTrue(self.store.reserve_analytics("123")["allowed"])
            op.run_job(self.store, self.store.claim_job("worker"), self.transport)
            self.now += 61
        self.assertEqual(self.store.jobs()[0]["status"], "partial")
        self.assertTrue(self.store.jobs()[0]["result"]["automatic_retry_exhausted"])
        self.assertIsNone(self.store.claim_job("worker"))

    def test_real_transport_wrapped_url_timeout_is_retryable_and_redacted(self):
        def offline_timeout(request, timeout):
            raise urllib.error.URLError(TimeoutError("network error super-secret"))
        transport = UrllibTransport(OzonCredentials(client_id="123", api_key="super-secret"), urlopen=offline_timeout)
        with self.assertRaises(OzonHttpError) as raised:
            transport.post(op.INFO_PATH, {"offer_id": [self.offer]})
        self.assertIsInstance(raised.exception.__cause__, urllib.error.URLError)
        self.assertEqual(op.safe_error(raised.exception), {"code": "network_unavailable", "http_status": None, "retryable": True})
        self.store.enqueue("qa", self.offer)
        result = op.run_job(self.store, self.store.claim_job("worker"), transport)
        self.assertEqual(result["retryable_error"], "network_unavailable")
        self.assertEqual(self.store.jobs()[0]["status"], "retry_wait")
        self.assertNotIn("super-secret", json.dumps(self.store.detail("qa", self.offer)))

    def test_top_http_status_has_priority_over_nested_network_error(self):
        denied = OzonHttpError("redacted", status=403)
        denied.__cause__ = urllib.error.URLError(TimeoutError("private"))
        result = op.safe_error(denied)
        self.assertEqual(result["code"], "permission_required")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["http_status"], 403)
        outer = RuntimeError("redacted")
        outer.__cause__ = urllib.error.HTTPError("https://api-seller.ozon.ru", 503, "redacted", {}, None)
        self.assertEqual(op.safe_error(outer)["code"], "provider_unavailable")

    def test_exception_chain_is_bounded_and_cycle_safe(self):
        first, second = RuntimeError("redacted"), RuntimeError("redacted")
        first.__cause__, second.__cause__ = second, first
        self.assertEqual(op.safe_error(first)["code"], "request_failed")
        error = TimeoutError("redacted")
        for _ in range(7):
            outer = RuntimeError("redacted")
            outer.__cause__ = error
            error = outer
        self.assertEqual(op.safe_error(error)["code"], "request_failed")
        malformed = json.JSONDecodeError("redacted", "", 0)
        outer = OzonHttpError("redacted")
        outer.__cause__ = malformed
        self.assertFalse(op.safe_error(outer)["retryable"])

    def test_disabled_shop_safe_code_is_preserved_without_retry(self):
        job = self.store.enqueue("qa", self.offer)
        self.store.claim_job("worker")
        self.store.fail_job(job["id"], "worker", "shop_disabled", False)
        self.assertEqual(self.store.jobs()[0]["error_code"], "shop_disabled")
        self.assertEqual(self.store.jobs()[0]["status"], "failed")
        self.assertIsNone(self.store.claim_job("worker"))


if __name__ == "__main__":
    unittest.main()
