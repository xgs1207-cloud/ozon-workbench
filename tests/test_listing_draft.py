"""Submission boundaries and editor locking, entirely mocked/offline."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pipeline.listing_draft import submit_listing
from pipeline.listing_form import _require_editable, read_json, write_json
from pipeline.product_edit_lock import serialized_product_edit


class ListingDraftSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "P000001"
        self.directory.mkdir()
        write_json(self.directory / "status.json", {"product_id": "P000001", "api_write_count": 0})
        self.patches = [
            patch("pipeline.listing_draft.canonical_listing", return_value={"offline": True}),
            patch("pipeline.guided_review.status", return_value={"ready_to_preflight": True}),
            patch("pipeline.preflight.preflight", return_value={"ok": True}),
            patch("pipeline.ozon_write.OzonWriteUploader", return_value=object()),
            patch("pipeline.ozon_status.confirm_product", return_value={"ok": True, "stores": {}}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_success_records_one_write_and_receipt_prevents_repeat(self):
        response = {"submitted": 1, "api_writes": 1, "stores": {"qa-store": {"task_id": 123}}}
        with patch("pipeline.publications.load_publications", side_effect=[{}, {
            "stores": {"qa-store": {"task_id": 123}}}, {
            "stores": {"qa-store": {"task_id": 123}}}]), patch(
                "pipeline.upload.upload_product", return_value=response) as uploader:
            first = submit_listing(self.directory, shop="qa-store")
            self.assertEqual(first["api_writes"], 1)
            self.assertEqual(read_json(self.directory / "status.json")["api_write_count"], 1)
            self.assertEqual(read_json(self.directory / "runtime/listing-submit-attempt.json")["state"], "processing")
            second = submit_listing(self.directory, shop="qa-store")
            self.assertEqual(second["status"], "already_submitted")
            self.assertEqual(second["api_writes"], 0)
            self.assertEqual(uploader.call_count, 1)
            self.assertEqual(read_json(self.directory / "status.json")["api_write_count"], 1)
        with self.assertRaises(ValueError):
            _require_editable(self.directory)

    def test_ambiguous_failure_is_not_retried_and_attempt_blocks_editing(self):
        with patch("pipeline.publications.load_publications", return_value={}), patch(
                "pipeline.upload.upload_product", side_effect=RuntimeError("offline simulated timeout")) as uploader:
            with self.assertRaisesRegex(ValueError, "结果未确定"):
                submit_listing(self.directory, shop="qa-store")
            self.assertEqual(read_json(self.directory / "runtime/listing-submit-attempt.json")["state"], "unknown_requires_readback")
            with self.assertRaisesRegex(ValueError, "已有提交尝试"):
                submit_listing(self.directory, shop="qa-store")
            self.assertEqual(uploader.call_count, 1)
            self.assertEqual(read_json(self.directory / "status.json").get("api_write_count", 0), 0)
        with self.assertRaises(ValueError):
            _require_editable(self.directory)

    def test_crash_leaves_started_attempt_and_blocks_edit_and_retry(self):
        with patch("pipeline.publications.load_publications", return_value={}), patch(
                "pipeline.upload.upload_product", side_effect=SystemExit("offline simulated crash")) as uploader:
            with self.assertRaises(SystemExit):
                submit_listing(self.directory, shop="qa-store")
            self.assertEqual(read_json(self.directory / "runtime/listing-submit-attempt.json")["state"], "started")
            with self.assertRaises(ValueError):
                _require_editable(self.directory)
            with self.assertRaisesRegex(ValueError, "已有提交尝试"):
                submit_listing(self.directory, shop="qa-store")
            self.assertEqual(uploader.call_count, 1)

    def test_nested_serialized_edit_does_not_reacquire_os_file_lock(self):
        calls = []
        @serialized_product_edit
        def inner(directory):
            calls.append("inner")
            return "saved"
        @serialized_product_edit
        def outer(directory):
            calls.append("outer")
            return inner(directory)
        started = time.monotonic()
        self.assertEqual(outer(self.directory), "saved")
        self.assertEqual(calls, ["outer", "inner"])
        self.assertLess(time.monotonic() - started, 4)
        # A fresh acquisition after both contexts exit must still work.
        self.assertEqual(inner(self.directory), "saved")


if __name__ == "__main__":
    unittest.main()
