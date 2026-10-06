"""Automatic Ozon submission tests use fakes only; never send API writes."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from market_intelligence.auto_publish import process_ready
from market_intelligence.sessions import _db, list_publish_attempts, set_auto_publish


class AutoPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "market.sqlite3"
        self.root = Path(self.temp.name) / "products"
        with closing(_db(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO research_sessions"
                "(id,category_key,primary_keyword,secondary_keywords_json,config_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("R-TEST", "床单", "простыня", "[]", "{}", "2026-10-06T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO research_session_products(session_id,product_id,attached_at) VALUES (?,?,?)",
                ("R-TEST", "P000001", "2026-10-06T00:00:00Z"),
            )
        set_auto_publish(self.db, session_id="R-TEST", enabled=True, target_store_id="shop-a")

    def test_server_arm_is_required_before_any_work(self):
        with patch.dict("os.environ", {"WORKBENCH_AUTO_PUBLISH_ARMED": "0"}):
            with patch("market_intelligence.auto_publish.upload_product") as write:
                with self.assertRaisesRegex(ValueError, "总开关"):
                    process_ready(self.db, self.root, session_id="R-TEST")
                write.assert_not_called()
        self.assertEqual(list_publish_attempts(self.db, "R-TEST"), [])

    def test_one_ready_product_gets_only_one_automatic_write_attempt(self):
        publisher = Mock()
        publisher.publish_product.return_value = {"missing": [], "https_ok": True}
        preflight = Mock(return_value={"ok": True})
        uploader = object()
        mocks = (
            patch("market_intelligence.auto_publish.ensure_registry", return_value=object()),
            patch("market_intelligence.auto_publish.list_shops", return_value=[
                {"id": "shop-a", "enabled": True, "default_currency_code": "CNY"}
            ]),
            patch("market_intelligence.auto_publish.review_status", return_value={"ready_to_preflight": True}),
            patch("market_intelligence.auto_publish.build_upload_payload", return_value={"items": []}),
            patch("market_intelligence.auto_publish.payload_problems", return_value=[]),
            patch("market_intelligence.auto_publish.write_json"),
            patch("market_intelligence.auto_publish.upload_product", return_value={"submitted": 1}),
        )
        with patch.dict("os.environ", {"WORKBENCH_AUTO_PUBLISH_ARMED": "1"}):
            with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], mocks[5], mocks[6] as write:
                first = process_ready(self.db, self.root, session_id="R-TEST", preflight_fn=preflight,
                                      uploader=uploader, publisher=publisher)
                second = process_ready(self.db, self.root, session_id="R-TEST", preflight_fn=preflight,
                                       uploader=uploader, publisher=publisher)
                self.assertEqual(first["results"][0]["status"], "submitted")
                self.assertEqual(second["results"][0]["status"], "already_attempted")
                write.assert_called_once()
                preflight.assert_called_once()
                publisher.publish_product.assert_called_once()
        attempts = list_publish_attempts(self.db, "R-TEST")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "submitted")


if __name__ == "__main__":
    unittest.main()
