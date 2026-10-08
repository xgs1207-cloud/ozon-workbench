"""Performance authorization/report contract: offline, no real ads or credentials."""
from __future__ import annotations

import io
import json
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from pipeline.performance_access import (
    PerformanceAccess, PerformanceAccessError, ReadonlyPerformanceTransport,
    WireResponse, _NoRedirect, _allowed_request, parse_report,
)

CLIENT = "offline@advertising.performance.ozon.ru"
SECRET = "offline-performance-secret-123456"
TOKEN = "offline-performance-token-abcdefghijk"
REPORT_UUID = "c2d233ca-ed34-4f3c-8613-ac1ed15bad61"
CSV = "; Кампания 19\nsku;Название товара;Показы;Клики;Расход, Р, с НДС;Выручка, Р\n777;Игрушка;200;10;15,50;45,00\n778;Игрушка 2;0;0;0;0\n".encode("utf-8")


def json_response(value):
    return WireResponse(json.dumps(value, ensure_ascii=False).encode())


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.states = ["IN_PROGRESS", "OK"]
        self.campaigns = [{"id": "19", "title": "Test campaign", "state": "CAMPAIGN_STATE_RUNNING", "dailyBudget": "200000000", "secret": SECRET}]
        self.token = TOKEN
        self.expiry = 1800
        self.report_uuid = REPORT_UUID
        self.content = WireResponse(CSV, "text/csv; charset=UTF-8")
        self.custom_status = None
        self.mutex = threading.Lock()

    def request(self, method, path, **kwargs):
        with self.mutex:
            self.calls.append((method, path, kwargs))
        if callable(self.failure):
            self.failure(method, path, kwargs)
        elif self.failure:
            raise self.failure
        if path == "/api/client/token":
            return json_response({"access_token": self.token, "expires_in": self.expiry, "token_type": "Bearer"})
        if path == "/api/client/campaign":
            query = kwargs.get("query", {})
            selected = query.get("campaignIds")
            rows = [row for row in self.campaigns if selected is None or row["id"] in selected]
            size = query.get("pageSize", 50)
            page = query.get("page", 1)
            return json_response({"list": rows[(page - 1) * size:page * size]})
        if path == "/api/client/statistics":
            return json_response({"UUID": self.report_uuid})
        if path == "/api/client/statistics/report":
            return self.content
        if path == "/api/client/statistics/" + self.report_uuid:
            return json_response(self.custom_status or {"UUID": self.report_uuid, "state": self.states.pop(0) if len(self.states) > 1 else self.states[0], "link": "https://attacker.invalid/secret?token=" + TOKEN, "error": SECRET})
        raise RuntimeError("unsupported fake request " + SECRET)


class PerformanceAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "performance"
        self.now = 1791403200.0  # 2026-10-08 UTC
        self.transport = FakeTransport()
        self.access = PerformanceAccess(self.root, transport=self.transport, clock=lambda: self.now)

    def tearDown(self):
        self.temporary.cleanup()

    def authorize(self, shop="shop-a", client=CLIENT, secret=SECRET):
        return self.access.authorize(shop, client, secret)

    def report(self, shop="shop-a"):
        return self.access.request_report(shop, ["19"], "2026-10-01", "2026-10-07")

    def ready_report(self):
        self.authorize()
        row = self.report()
        self.assertEqual(self.access.poll_report("shop-a", row["report_id"])["state"], "IN_PROGRESS")
        self.now += 6
        self.assertEqual(self.access.poll_report("shop-a", row["report_id"])["state"], "OK")
        return row

    def assert_no_secrets(self, value):
        text = json.dumps(value, ensure_ascii=False)
        for secret in (CLIENT, SECRET, TOKEN):
            self.assertNotIn(secret, text)

    def test_status_before_authorization_is_readonly_and_creates_nothing(self):
        status = self.access.public_status("shop-a")
        self.assertFalse(status["configured"])
        self.assertFalse(status["ad_writes_enabled"])
        self.assertEqual(status["scope"], "read_only")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.transport.calls, [])

    def test_authorization_only_token_and_campaigns_persists_ciphertext(self):
        result = self.authorize()
        self.assertTrue(result["configured"])
        self.assert_no_secrets(result)
        self.assertEqual([(method, path) for method, path, _ in self.transport.calls], [
            ("POST", "/api/client/token"), ("GET", "/api/client/campaign")])
        for target in self.root.iterdir():
            content = target.read_bytes()
            for secret in (CLIENT, SECRET, TOKEN):
                self.assertNotIn(secret.encode(), content)
        conn = sqlite3.connect(self.access.path)
        encrypted = conn.execute("SELECT ciphertext FROM credentials WHERE shop='shop-a'").fetchone()[0]
        conn.close()
        decoded = json.loads(self.access._cipher(create=False).decrypt(encrypted))
        self.assertEqual(decoded["client_id"], CLIENT)
        self.assertEqual(decoded["client_secret"], SECRET)

    def test_authorize_invalid_credentials_never_calls_network(self):
        for client, secret in (("5776003", SECRET), (CLIENT, "short"), (CLIENT, "secret\nheader-injection")):
            with self.assertRaises(PerformanceAccessError):
                self.authorize(client=client, secret=secret)
        self.assertEqual(self.transport.calls, [])

    def test_shop_path_traversal_rejected(self):
        for shop in ("../other", "/root", "", ".", "..", "x" * 65, "a/b", "中文"):
            with self.assertRaises(PerformanceAccessError):
                self.access.public_status(shop)
        self.assertFalse(self.root.exists())

    def test_bad_authorization_keeps_previous_valid_ciphertext(self):
        self.authorize()
        original = self.access.path.read_bytes()
        self.transport.failure = RuntimeError(CLIENT + SECRET + TOKEN)
        with self.assertRaises(PerformanceAccessError) as raised:
            self.authorize(secret="replacement-secret-12345")
        self.assert_no_secrets(str(raised.exception))
        self.assertEqual(self.access.path.read_bytes(), original)
        self.assertTrue(self.access.public_status("shop-a")["configured"])

    def test_missing_master_key_never_overwrites_old_ciphertext(self):
        self.authorize()
        original = self.access.path.read_bytes()
        (self.root / "master.key").unlink()
        self.assertEqual(self.access.public_status("shop-a")["connection_status"], "credential_error")
        with self.assertRaises(PerformanceAccessError) as raised:
            self.authorize("shop-b")
        self.assertEqual(raised.exception.code, "missing_key")
        self.assertFalse((self.root / "master.key").exists())
        self.assertEqual(self.access.path.read_bytes(), original)

    def test_cross_shop_ciphertext_swap_is_rejected(self):
        self.authorize()
        self.authorize("shop-b")
        conn = sqlite3.connect(self.access.path)
        with conn:
            source = conn.execute("SELECT ciphertext FROM credentials WHERE shop='shop-a'").fetchone()[0]
            conn.execute("UPDATE credentials SET ciphertext=? WHERE shop='shop-b'", (source,))
        conn.close()
        with self.assertRaises(PerformanceAccessError):
            self.access.list_campaigns("shop-b")

    def test_remove_unconfigures_without_upstream_mutation(self):
        self.authorize()
        calls = len(self.transport.calls)
        result = self.access.remove("shop-a")
        self.assertFalse(result["configured"])
        self.assertEqual(len(self.transport.calls), calls)
        with self.assertRaises(PerformanceAccessError):
            self.access.list_campaigns("shop-a")

    def test_token_cached_across_instances_refreshes_on_expiry(self):
        self.authorize()
        other = PerformanceAccess(self.root, transport=self.transport, clock=lambda: self.now)
        other.list_campaigns("shop-a")
        count = sum(path == "/api/client/token" for _, path, _ in self.transport.calls)
        self.assertEqual(count, 1)
        self.now += 1800
        other.list_campaigns("shop-a")
        self.assertEqual(sum(path == "/api/client/token" for _, path, _ in self.transport.calls), 2)

    def test_rotating_credentials_invalidates_old_token_generation(self):
        self.authorize()
        self.transport.token = "replacement-token-abcdefghijk"
        self.authorize(secret="replacement-secret-12345")
        self.access.list_campaigns("shop-a")
        token = self.transport.calls[-1][2]["token"]
        self.assertEqual(token, "replacement-token-abcdefghijk")

    def test_same_account_secret_rotation_keeps_report_resumable(self):
        self.authorize()
        row = self.report()
        self.authorize(secret="replacement-secret-12345")
        self.assertEqual(self.access.list_reports("shop-a")["items"][0]["report_id"], row["report_id"])
        self.assertEqual(self.access.poll_report("shop-a", row["report_id"])["state"], "IN_PROGRESS")
        self.now += 6
        self.assertEqual(self.access.poll_report("shop-a", row["report_id"])["state"], "OK")
        self.transport.report_uuid = str(uuid.uuid4())
        self.assertEqual(self.report()["state"], "NOT_STARTED")

    def test_opaque_client_ids_are_not_case_normalized_for_report_ownership(self):
        self.authorize()
        row = self.report()
        self.authorize(client=CLIENT.replace("offline@", "Offline@"))
        self.assertEqual(self.access.list_reports("shop-a")["items"], [])
        with self.assertRaises(PerformanceAccessError) as raised:
            self.access.poll_report("shop-a", row["report_id"])
        self.assertEqual(raised.exception.code, "report_not_owned")

    def test_nonpositive_token_expiry_rejected_before_campaign_request(self):
        for expiry in (0, -1, "-1", "NaN", "Infinity"):
            self.transport.expiry = expiry
            with self.assertRaises(PerformanceAccessError) as raised:
                self.authorize()
            self.assertEqual(raised.exception.code, "invalid_token")
        self.assertFalse(any(path == "/api/client/campaign" for _, path, _ in self.transport.calls))

    def test_401_reauth_is_bounded_to_one_retry(self):
        self.authorize()
        fired = False

        def fail_once(method, path, kwargs):
            nonlocal fired
            if path == "/api/client/campaign" and not fired:
                fired = True
                raise PerformanceAccessError("expired", http_status=401)

        self.transport.failure = fail_once
        self.access.list_campaigns("shop-a")
        self.assertEqual(sum(path == "/api/client/token" for _, path, _ in self.transport.calls), 2)

    def test_campaign_page_limits_and_safe_fields(self):
        self.authorize()
        self.transport.campaigns[0]["title"] = SECRET + TOKEN + CLIENT
        result = self.access.list_campaigns("shop-a")
        self.assert_no_secrets(result)
        self.assertNotIn("secret", result["items"][0])
        self.assertEqual(result["budget_divisor"], 1_000_000)
        for page, size in ((0, 10), (1, 101), (1, 0), (True, 10)):
            with self.assertRaises(PerformanceAccessError):
                self.access.list_campaigns("shop-a", page=page, page_size=size)

    def test_report_payload_uses_official_date_fields_and_grouping(self):
        self.authorize()
        result = self.report()
        self.assertEqual(result["state"], "NOT_STARTED")
        body = next(kwargs["body"] for _, path, kwargs in self.transport.calls if path == "/api/client/statistics")
        self.assertEqual(body, {"campaigns": ["19"], "dateFrom": "2026-10-01", "dateTo": "2026-10-07", "groupBy": "DATE"})
        self.assert_no_secrets(result)

    def test_report_limits_and_ownership_checked_before_post(self):
        self.authorize()
        for campaigns, start, end in (([], "2026-10-01", "2026-10-07"), (["19"] * 2, "2026-10-01", "2026-10-07"), (["20"], "2026-10-01", "2026-10-07"), (["19"], "2026-01-01", "2026-10-07"), (["19"], "2026-10-01", "2026-10-12"), (["19"], "2026-10-05", "2026-10-04"), (["19"], "10/01/2026", "2026-10-07")):
            with self.assertRaises(PerformanceAccessError):
                self.access.request_report("shop-a", campaigns, start, end)
        self.assertFalse(any(path == "/api/client/statistics" for _, path, _ in self.transport.calls))

    def test_persistent_account_export_lock_cross_shop_cross_instance(self):
        self.authorize()
        self.authorize("shop-b")
        row = self.report()
        other = PerformanceAccess(self.root, transport=self.transport, clock=lambda: self.now)
        with self.assertRaises(PerformanceAccessError) as raised:
            other.request_report("shop-b", ["19"], "2026-10-01", "2026-10-07")
        self.assertEqual(raised.exception.code, "export_busy")
        self.assertEqual(other.list_reports("shop-a")["items"][0]["report_id"], row["report_id"])

    def test_concurrent_export_submits_exactly_once(self):
        self.authorize()

        def submit():
            try:
                return self.report()["state"]
            except PerformanceAccessError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertCountEqual(results, ["NOT_STARTED", "export_busy"])
        self.assertEqual(sum(path == "/api/client/statistics" for _, path, _ in self.transport.calls), 1)

    def test_poll_cross_shop_or_account_rejected_before_network(self):
        self.authorize()
        row = self.report()
        self.authorize("shop-b")
        calls = len(self.transport.calls)
        for identifier in (row["report_id"], row["uuid"], str(uuid.uuid4())):
            with self.assertRaises(PerformanceAccessError) as raised:
                self.access.poll_report("shop-b", identifier)
            self.assertEqual(raised.exception.code, "report_not_owned")
        self.assertEqual(len(self.transport.calls), calls)
        self.authorize(client="other@advertising.performance.ozon.ru")
        with self.assertRaises(PerformanceAccessError):
            self.access.poll_report("shop-a", row["report_id"])

    def test_resume_owned_uuid_after_restart_throttle_and_release_lock(self):
        self.authorize()
        row = self.report()
        other = PerformanceAccess(self.root, transport=self.transport, clock=lambda: self.now)
        polled = other.poll_report("shop-a", row["uuid"])
        self.assertEqual(polled["state"], "IN_PROGRESS")
        calls = len(self.transport.calls)
        other.poll_report("shop-a", row["uuid"])
        self.assertEqual(len(self.transport.calls), calls)
        self.now += 6
        polled = other.poll_report("shop-a", row["uuid"])
        self.assertEqual(polled["state"], "OK")
        self.transport.report_uuid = str(uuid.uuid4())
        self.assertEqual(other.request_report("shop-a", ["19"], "2026-10-01", "2026-10-07")["state"], "NOT_STARTED")

    def test_report_error_does_not_persist_reflected_error_or_link(self):
        self.authorize()
        row = self.report()
        self.transport.custom_status = {"UUID": row["uuid"], "state": "ERROR", "error": SECRET, "link": "https://evil.invalid/" + TOKEN}
        result = self.access.poll_report("shop-a", row["report_id"])
        self.assertEqual(result["state"], "ERROR")
        self.assert_no_secrets(result)
        self.assertNotIn(SECRET.encode(), self.access.path.read_bytes())

    def test_invalid_status_uuid_cannot_release_pending_export(self):
        self.authorize()
        row = self.report()
        self.transport.custom_status = {"UUID": str(uuid.uuid4()), "state": "OK"}
        with self.assertRaises(PerformanceAccessError):
            self.access.poll_report("shop-a", row["report_id"])
        with self.assertRaises(PerformanceAccessError) as raised:
            self.report()
        self.assertEqual(raised.exception.code, "export_busy")

    def test_submission_timeout_remains_uncertain_no_duplicate_post(self):
        self.authorize()

        def timeout(method, path, kwargs):
            if path == "/api/client/statistics":
                raise RuntimeError("response lost " + SECRET + TOKEN)

        self.transport.failure = timeout
        with self.assertRaises(PerformanceAccessError) as raised:
            self.report()
        self.assertEqual(raised.exception.code, "submission_uncertain")
        self.assert_no_secrets(str(raised.exception))
        items = self.access.list_reports("shop-a")["items"]
        self.assertEqual(items[0]["state"], "SUBMISSION_UNCERTAIN")
        self.assertTrue(items[0]["recovery_required"])
        with self.assertRaises(PerformanceAccessError) as raised:
            self.report()
        self.assertEqual(raised.exception.code, "export_busy")
        self.assertEqual(sum(path == "/api/client/statistics" for _, path, _ in self.transport.calls), 1)

    def test_known_submission_rejection_releases_lock(self):
        self.authorize()

        def rejected(method, path, kwargs):
            if path == "/api/client/statistics":
                raise PerformanceAccessError("quota", http_status=429)

        self.transport.failure = rejected
        with self.assertRaises(PerformanceAccessError):
            self.report()
        self.assertEqual(self.access.list_reports("shop-a")["items"][0]["state"], "ERROR")
        self.transport.failure = None
        self.assertEqual(self.report()["state"], "NOT_STARTED")

    def test_download_requires_ready_owned_report(self):
        self.authorize()
        row = self.report()
        with self.assertRaises(PerformanceAccessError) as raised:
            self.access.download_report("shop-a", row["report_id"])
        self.assertEqual(raised.exception.code, "report_not_ready")
        self.authorize("shop-b")
        with self.assertRaises(PerformanceAccessError):
            self.access.download_report("shop-b", row["report_id"])

    def test_csv_download_cached_with_units_provenance_no_library_side_effect(self):
        row = self.ready_report()
        result = self.access.download_report("shop-a", row["report_id"])
        self.assertEqual(result["rows"][0]["derived"]["ctr_percent"], 5)
        self.assertIsNone(result["rows"][1]["derived"]["ctr_percent"])
        self.assertEqual(result["rows"][0]["raw"]["Расход, Р, с НДС"], "15,50")
        self.assertNotIn("acos", result["rows"][0]["derived"])
        self.assertFalse(result["ad_writes_performed"])
        self.assertFalse(result["library_writes_performed"])
        self.assertEqual(result["provenance"]["source"], "Ozon Performance API")
        calls = len(self.transport.calls)
        self.assertEqual(self.access.download_report("shop-a", row["report_id"]), result)
        self.assertEqual(len(self.transport.calls), calls)
        self.assertFalse(any(path.startswith("https:") for _, path, _ in self.transport.calls))

    def test_csv_credential_reflection_is_removed_before_storage(self):
        row = self.ready_report()
        self.transport.content = WireResponse(CSV.replace("Игрушка".encode(), (CLIENT + SECRET + TOKEN).encode()), "text/csv")
        result = self.access.download_report("shop-a", row["report_id"])
        self.assert_no_secrets(result)
        self.assertNotIn(SECRET.encode(), self.access.path.read_bytes())


class PerformanceParsingTests(unittest.TestCase):
    def zip_content(self, filename="19.csv", content=CSV):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(filename, content)
        return buffer.getvalue()

    def test_safe_zip_is_parsed_without_disk_extraction(self):
        parsed = parse_report(self.zip_content(), "application/zip", campaigns=["19"])
        self.assertEqual(parsed["files"][0]["name"], "19.csv")
        self.assertEqual(len(parsed["rows"]), 2)

    def test_zip_traversal_unowned_file_compression_bomb_rejected(self):
        for filename, content in (("../19.csv", CSV), ("20.csv", CSV), ("19.csv", b"0" * 100_000)):
            with self.assertRaises(PerformanceAccessError):
                parse_report(self.zip_content(filename, content), "application/zip", campaigns=["19"])

    def test_duplicate_csv_headings_and_malformed_rows_rejected(self):
        for data in ("sku;Показы;Клики;Клики\n1;2;3;4", "sku;Показы;Клики\n1;2", "arbitrary;column\n1;2", "<html>error</html>"):
            with self.assertRaises(PerformanceAccessError):
                parse_report(data.encode(), "text/csv", campaigns=["19"])

    def test_csv_decimal_comma_does_not_change_semicolon_delimiter(self):
        result = parse_report("sku;Показы;Клики;Расход, Р;Выручка, Р\n1;100;2;3,00;6,00".encode(), "text/csv", campaigns=["19"])
        self.assertEqual(result["rows"][0]["derived"]["ctr_percent"], 2)

    def test_csv_formula_stays_inert_string(self):
        parsed = parse_report("sku;Показы;Клики;Название\n1;20;1;=HYPERLINK(\"unsafe\")".encode(), "text/csv", campaigns=["19"])
        self.assertTrue(parsed["rows"][0]["raw"]["Название"].startswith("=HYPERLINK"))

    def test_readonly_whitelist_forbids_mutating_get_and_external_urls(self):
        for method, path in (("GET", "/api/client/campaign/all_sku_promo/activate"), ("POST", "/api/client/campaign/1/activate"), ("PATCH", "/api/client/campaign/1"), ("GET", "https://evil.invalid/report"), ("GET", "//evil.invalid/report"), ("GET", "/api/client/statistics/../campaign/1/activate")):
            with self.assertRaises(PerformanceAccessError):
                _allowed_request(method, path, {})
        with self.assertRaises(PerformanceAccessError):
            _allowed_request("GET", "/api/client/statistics/report", {"UUID": REPORT_UUID, "redirect": "https://evil.invalid"})
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", None, "https://evil.invalid"))

    def test_transport_rejects_external_and_mutating_paths_before_opener(self):
        transport = ReadonlyPerformanceTransport()
        with patch("pipeline.performance_access.request.build_opener") as opener:
            for path in ("https://evil.invalid", "/api/client/campaign/all_sku_promo/activate"):
                with self.assertRaises(PerformanceAccessError):
                    transport.request("GET", path, token=TOKEN)
            opener.assert_not_called()

    def test_transport_network_error_contains_no_secrets(self):
        transport = ReadonlyPerformanceTransport()
        with patch("pipeline.performance_access.request.build_opener") as opener:
            opener.return_value.open.side_effect = RuntimeError(CLIENT + SECRET + TOKEN)
            with self.assertRaises(PerformanceAccessError) as raised:
                transport.request("POST", "/api/client/token", body={"client_id": CLIENT, "client_secret": SECRET})
        for secret in (CLIENT, SECRET, TOKEN):
            self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
