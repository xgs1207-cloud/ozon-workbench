"""Real listing safety cases reproduced with injected, offline transports."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_upload import UploadFixture
from test_ozon_status import RecordingTransport, imported, info_response, sample_payload
from test_ozon_verify import build_product, FixtureTransport, ozon_item

from pipeline.context import read_json, write_json
from pipeline.listing_draft import submission_editable, submit_listing
from pipeline.ozon_http import OzonHttpError
from pipeline.ozon_status import confirm_product, parse_import_info
from pipeline.ozon_verify import verify_submitted
from pipeline.ozon_write import OzonWriteUploader, PATH_IMPORT, PATH_IMPORT_INFO
from pipeline.publications import load_publications
from pipeline import stores
from pipeline.upload import upload_product


class ImportCompletenessTests(unittest.TestCase):
    def test_missing_offer_is_not_a_terminal_success(self):
        response = info_response(imported("P000001-S1"))
        response["result"]["total"] = 2
        parsed = parse_import_info(response, payload=sample_payload())
        self.assertFalse(parsed["terminal"])
        self.assertEqual(parsed["missing_offers"], ["P000001-S2"])

    def test_imported_error_does_not_count_as_success(self):
        item = imported("P000001-S1")
        item["errors"] = [{"code": "INVALID_VALUE", "message": "wrong"}]
        parsed = parse_import_info(info_response(item, imported("P000001-S2")), payload=sample_payload())
        self.assertTrue(parsed["terminal"])
        self.assertEqual(parsed["counts"]["failed"], 1)
        self.assertEqual(parsed["counts"]["imported"], 1)

    def test_known_warning_is_preserved_not_promoted_to_fatal(self):
        item = imported("P000001-S1")
        item["errors"] = [{"code": "WARN", "message": "notice", "level": "warning"}]
        parsed = parse_import_info(info_response(item, imported("P000001-S2")), payload=sample_payload())
        self.assertTrue(parsed["terminal"])
        self.assertEqual(parsed["counts"]["imported"], 2)
        self.assertEqual(parsed["errors"][0]["level"], "warning")

    def test_duplicate_offers_and_missing_identity_are_not_success(self):
        parsed = parse_import_info(info_response(imported("P000001-S1"), imported("P000001-S1")),
                                   payload=sample_payload())
        self.assertFalse(parsed["terminal"])
        self.assertEqual(parsed["duplicate_offers"], ["P000001-S1"])
        no_id = imported("P000001-S1", 0)
        self.assertFalse(parse_import_info(info_response(no_id))["terminal"])


class TaskReceiptRecoveryTests(UploadFixture):
    def setUp(self):
        super().setUp()
        self.registry = self.root / "config/shops.json"
        registry = stores.example_registry()
        registry["shops"][0].update(enabled=True, default_currency_code="RUB")
        stores.save_registry(registry, self.registry)
        self.registry_patch = patch("pipeline.stores.ensure_registry", return_value=registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)

    def uploader(self, transport):
        return OzonWriteUploader(transport_factory=lambda credentials: transport, registry_path=self.registry,
            env={"OZON_DEFAULT_CLIENT_ID": "fixture", "OZON_DEFAULT_API_KEY": "fixture"}, sleep=lambda _: None)

    def test_task_only_response_is_saved_before_readback_failure(self):
        transport = RecordingTransport([{"result": {"task_id": 777}}, OzonHttpError("offline outage", status=503)])
        summary = upload_product(self.product_dir, ["default"], self.uploader(transport), upload_mode="production")
        self.assertEqual(summary["api_writes"], 1)
        ledger = load_publications(self.product_dir)["stores"]["default"]
        self.assertEqual(ledger["task_id"], "777")
        self.assertEqual({row["offer_id"] for row in ledger["sku_publications"]}, {"P000001-S1", "P000001-S2"})
        self.assertTrue(all(row["task_id"] == "777" for row in ledger["sku_publications"]))
        receipt = read_json(self.product_dir / "output/store-runs/default/submission-receipt.json")
        self.assertEqual(receipt["task_id"], "777")
        recovered = RecordingTransport([info_response(imported("P000001-S1"), imported("P000001-S2"))])
        report = confirm_product(self.product_dir, "default", registry_path=self.registry,
            transport_factory=lambda credentials: recovered, max_attempts=1, interval_seconds=0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["stores"]["default"]["status"], "created")
        self.assertEqual([call["path"] for call in recovered.calls], [PATH_IMPORT_INFO])

    def test_independent_receipt_recovers_a_missing_ledger(self):
        run_dir = self.product_dir / "output/store-runs/default"
        write_json(run_dir / "payload.json", sample_payload())
        write_json(run_dir / "submission-receipt.json", {"task_id": "888"})
        transport = RecordingTransport([info_response(imported("P000001-S1"), imported("P000001-S2"))])
        report = confirm_product(self.product_dir, "default", registry_path=self.registry,
            transport_factory=lambda credentials: transport, max_attempts=1, interval_seconds=0)
        self.assertTrue(report["ok"])
        self.assertEqual(transport.calls[0]["body"]["task_id"], 888)

    def test_timeout_is_unknown_and_a_400_is_an_explicit_rejection(self):
        # First test a deterministic HTTP rejection, then the unknown outcome;
        # unknown deliberately forbids a later write in this same ledger.
        for error, expected, retry in [(OzonHttpError("bad request", status=400), "rejected", True),
                                       (OzonHttpError("offline timeout", status=None), "unknown_requires_readback", False)]:
            with self.subTest(status=error.status):
                transport = RecordingTransport([error])
                summary = upload_product(self.product_dir, ["default"], self.uploader(transport), upload_mode="production")
                self.assertFalse(summary["ok"])
                self.assertEqual(summary["submitted"], 0)
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(summary["stores"]["default"]["outcome"], expected)
                self.assertIs(summary["stores"]["default"]["safe_to_retry"], retry)
                self.assertEqual([call["path"] for call in transport.calls], [PATH_IMPORT])
        blocked_transport = RecordingTransport([{"result": {"task_id": 999}}])
        blocked = upload_product(self.product_dir, ["default"], self.uploader(blocked_transport), upload_mode="production")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["api_writes"], 0)
        self.assertEqual(blocked_transport.calls, [])

    def test_crash_after_send_intent_does_not_allow_another_write(self):
        uploader = self.uploader(RecordingTransport([]))
        with patch.object(uploader, "submit", side_effect=SystemExit("offline process crash")):
            with self.assertRaises(SystemExit):
                upload_product(self.product_dir, ["default"], uploader, upload_mode="production")
        entry = load_publications(self.product_dir)["stores"]["default"]
        self.assertEqual(entry["submission_outcome"], "started")
        blocked = RecordingTransport([{"result": {"task_id": 999}}])
        report = upload_product(self.product_dir, ["default"], self.uploader(blocked), upload_mode="production")
        self.assertFalse(report["ok"])
        self.assertEqual(report["api_writes"], 0)
        self.assertEqual(blocked.calls, [])


class GuidedRejectedRetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "P000001"
        self.directory.mkdir()
        write_json(self.directory / "status.json", {"product_id": "P000001", "api_write_count": 0})
        self.addCleanup(self.temporary.cleanup)
        self.payload = {"offline": True, "title": "first"}
        self.patches = [patch("pipeline.listing_draft.canonical_listing", side_effect=lambda *a, **k: dict(self.payload)),
            patch("pipeline.guided_review.status", return_value={"ready_to_preflight": True}),
            patch("pipeline.preflight.preflight", return_value={"ok": True}),
            patch("pipeline.ozon_write.OzonWriteUploader", return_value=object())]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_explicit_400_rejection_keeps_receipt_and_requires_changed_payload(self):
        response = {"ok": False, "submitted": 0, "failed": 1, "api_writes": 1,
            "stores": {"default": {"outcome": "rejected", "safe_to_retry": True, "task_id": None}}}
        with patch("pipeline.upload.upload_product", return_value=response) as upload:
            report = submit_listing(self.directory, shop="default")
            self.assertFalse(report["ok"])
            self.assertEqual(report["state"], "rejected")
            self.assertTrue(submission_editable(self.directory))
            with self.assertRaisesRegex(ValueError, "已有提交尝试"):
                submit_listing(self.directory, shop="default")
            with self.assertRaisesRegex(ValueError, "尚未改变"):
                submit_listing(self.directory, shop="default", retry_rejected=True)
            self.assertEqual(upload.call_count, 1)
            self.payload["title"] = "corrected"
            submit_listing(self.directory, shop="default", retry_rejected=True)
            self.assertEqual(upload.call_count, 2)
            self.assertTrue(list((self.directory / "runtime/listing-submit-history").glob("*.json")))

    def test_unknown_is_not_editable_or_retryable(self):
        response = {"ok": False, "submitted": 0, "failed": 1, "api_writes": 1,
            "stores": {"default": {"outcome": "unknown_requires_readback", "safe_to_retry": False}}}
        with patch("pipeline.upload.upload_product", return_value=response) as upload:
            report = submit_listing(self.directory, shop="default")
            self.assertFalse(report["ok"])
            self.assertFalse(submission_editable(self.directory))
            self.payload["title"] = "changed"
            with self.assertRaises(ValueError):
                submit_listing(self.directory, shop="default", retry_rejected=True)
            self.assertEqual(upload.call_count, 1)

    def test_known_task_reconciles_without_a_second_write(self):
        write_json(self.directory / "output/store-publications.json", {
            "stores": {"default": {"task_id": "777", "sku_publications": []}}})
        with (patch("pipeline.ozon_status.confirm_product", return_value={"ok": False, "stores": {}}) as readback,
              patch("pipeline.upload.upload_product") as upload):
            report = submit_listing(self.directory, shop="default")
            self.assertFalse(report["ok"])
            self.assertEqual(report["api_writes"], 0)
            readback.assert_called_once()
            upload.assert_not_called()


class ExactVariantVerificationTests(unittest.TestCase):
    def test_swapped_colors_fail_per_offer(self):
        with tempfile.TemporaryDirectory() as temporary:
            product = build_product(Path(temporary))
            transport = FixtureTransport({"result": [ozon_item("P1-S1", color="серый"),
                                                       ozon_item("P1-S2", color="белый")]})
            result = verify_submitted(product, store_id="default", transport=transport)
            self.assertFalse(result["ok"])
            self.assertIn("attribute[P1-S1][10096]", result["failed"])
            self.assertIn("attribute[P1-S2][10096]", result["failed"])

    def test_separate_cards_do_not_require_a_shared_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            product = build_product(Path(temporary))
            write_json(product / "output/platform-grouping-result.json", {"upload_strategy": "separate_cards"})
            transport = FixtureTransport({"result": [ozon_item("P1-S1", color="белый", model_id=1, count=1),
                ozon_item("P1-S2", color="серый", model_id=2, count=1)]})
            self.assertTrue(verify_submitted(product, store_id="default", transport=transport)["ok"])


if __name__ == "__main__":
    unittest.main()
