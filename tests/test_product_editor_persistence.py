"""Local editor saves preserve previous data on error and serialize workers."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from collector.ingest import ingest_capture
from pipeline.product_editor import read_listing_details, save_listing_details
from pipeline.listing_form import write_json
from pipeline.product_edit_lock import product_edit_lock


class ProductEditorPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "products"
        captured = ingest_capture(root, {"source_url": "https://detail.1688.com/offer/838383838.html",
            "title_zh": "测试商品", "skus": [{"sku_id": "S1", "purchase_price_cny": 1}]})
        self.directory = root / captured["product_id"]

    def snapshot(self):
        return {str(path.relative_to(self.directory)): path.read_bytes()
                for path in self.directory.rglob("*") if path.is_file()}

    def test_write_failure_restores_status_details_and_confirmations(self):
        before = self.snapshot()
        def failing_write(path, value):
            if Path(path).name == "human-confirmations.json":
                raise OSError("simulated disk failure")
            write_json(path, value)
        with patch("pipeline.product_editor.write_json", side_effect=failing_write):
            with self.assertRaises(OSError):
                save_listing_details(self.directory, {"material": "硅胶", "product_height_mm": 100})
        self.assertEqual(self.snapshot(), before)

    def test_legacy_aliases_are_visible_until_explicitly_cleared(self):
        write_json(self.directory / "input/human-confirmations.json", {
            "material_zh": "棉", "product_weight_g": 100,
            "product_dimensions_mm": {"length": 70, "width": 60, "height": 100}})
        read = read_listing_details(self.directory)
        self.assertEqual(read["details"]["material"], "棉")
        self.assertEqual(read["details"]["product_length_mm"], 70)
        save_listing_details(self.directory, {"material": None, "product_weight_g": None})
        self.assertIsNone(read_listing_details(self.directory)["details"]["material"])
        self.assertIsNone(read_listing_details(self.directory)["details"]["product_weight_g"])

    def test_two_partial_saves_merge_without_lost_updates(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(save_listing_details, self.directory, {"material": "棉"})
            second = pool.submit(save_listing_details, self.directory, {"package_quantity": 2})
            first.result(); second.result()
        details = read_listing_details(self.directory)["details"]
        self.assertEqual(details["material"], "棉")
        self.assertEqual(details["package_quantity"], 2)

    def test_thread_lock_orders_product_writes(self):
        entered = threading.Event()
        events = []
        def first():
            with product_edit_lock(self.directory):
                events.append("first entered")
                entered.set()
                time.sleep(.08)
                events.append("first leaving")
        def second():
            entered.wait(1)
            with product_edit_lock(self.directory):
                events.append("second entered")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(first), pool.submit(second)]
            for result in futures: result.result()
        self.assertEqual(events, ["first entered", "first leaving", "second entered"])


if __name__ == "__main__":
    unittest.main()
