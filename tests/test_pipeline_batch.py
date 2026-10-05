"""批次创建测试：选择、去重、门禁、快照与状态回写。"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline import status as st  # noqa: E402
from pipeline.batch import create_batch, load_batch, select_collected  # noqa: E402


def make_product(
    products_root: pathlib.Path,
    product_id: str,
    *,
    skus: int = 2,
    offer: str = "123456789",
    status: str = "COLLECTED",
    source_url: str | None = None,
    **fields,
):
    directory = products_root / product_id
    (directory / "input").mkdir(parents=True, exist_ok=True)
    (directory / "output").mkdir(parents=True, exist_ok=True)
    source = {
        "schema_version": "1.0.0",
        "product_id": product_id,
        "source_url": source_url or f"https://detail.1688.com/offer/{offer}.html",
        "skus": [{"sku_id": f"S{i}", "purchase_price_cny": 10 + i} for i in range(skus)],
    }
    (directory / "input" / "source.json").write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8"
    )
    payload = st.new_status(product_id, status=status)
    payload.update(fields)
    st.save_status(directory, payload)
    return directory


class CreateBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        self.products.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_batch_and_freezes_snapshots(self):
        make_product(self.products, "P000001", offer="111111111")
        make_product(self.products, "P000002", offer="222222222", skus=3)
        batch = create_batch(self.products, batches_root=self.batches)

        self.assertEqual(batch["product_count"], 2)
        self.assertEqual(batch["sku_count"], 5)
        self.assertEqual(batch["status"], "QUEUED")
        self.assertFalse(batch["inventory_submission_enabled"])
        self.assertEqual(batch["review_mode"], "automatic")
        self.assertFalse(batch["manual_upload_required"])
        self.assertTrue((self.batches / batch["batch_id"] / "batch.json").is_file())
        self.assertEqual(len(batch["products"]), 2)

        for product_id in ("P000001", "P000002"):
            saved = st.load_status(self.products / product_id)
            self.assertTrue(saved["task_authorized"])
            self.assertEqual(saved["batch_id"], batch["batch_id"])
            self.assertEqual(saved["status"], "QUEUED")
            self.assertIn("sku_run_snapshot", saved)
            self.assertEqual(
                saved["sku_run_snapshot"]["selected_sku_count"],
                len(json.loads((self.products / product_id / "input" / "source.json").read_text(encoding="utf-8"))["skus"]),
            )
            self.assertTrue((self.products / product_id / "output" / "sku-run-snapshot.json").is_file())

    def test_reload_batch_from_disk(self):
        make_product(self.products, "P000001")
        batch = create_batch(self.products, batches_root=self.batches)
        self.assertEqual(load_batch(self.batches, batch["batch_id"])["batch_id"], batch["batch_id"])

    def test_manual_mode_when_auto_upload_disabled(self):
        make_product(self.products, "P000001")
        batch = create_batch(self.products, batches_root=self.batches, auto_upload=False)
        self.assertEqual(batch["review_mode"], "manual")
        self.assertTrue(batch["manual_upload_required"])

    def test_same_offer_keeps_newest_capture(self):
        older = make_product(self.products, "P000001", offer="333333333")
        newer = make_product(self.products, "P000002", offer="333333333")
        os.utime(older / "status.json", (1_000, 1_000))
        os.utime(newer / "status.json", (2_000, 2_000))
        self.assertEqual([item.name for item in select_collected(self.products)], ["P000002"])
        batch = create_batch(self.products, batches_root=self.batches)
        self.assertEqual(batch["product_count"], 1)
        self.assertEqual(batch["products"][0]["product_id"], "P000002")

    def test_store_override_wins(self):
        make_product(self.products, "P000001")
        batch = create_batch(
            self.products,
            batches_root=self.batches,
            target_store_ids=["shop-a", "shop-b"],
            product_store_overrides={"P000001": ["shop-c"]},
        )
        self.assertEqual(batch["products"][0]["target_store_ids"], ["shop-c"])
        self.assertEqual(batch["products"][0]["publication_count"], 1)
        saved = st.load_status(self.products / "P000001")
        self.assertEqual(saved["target_store_ids"], ["shop-c"])


class BatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        self.products.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_terminal_product(self):
        make_product(self.products, "P000001", status="UPLOADED")
        with self.assertRaises(ValueError) as ctx:
            create_batch(self.products, batches_root=self.batches, product_ids=["P000001"])
        self.assertIn("already terminal", str(ctx.exception))

    def test_allows_attention_product_without_writes(self):
        make_product(self.products, "P000001", status="NEEDS_ATTENTION", api_write_count=0)
        batch = create_batch(self.products, batches_root=self.batches, product_ids=["P000001"])
        self.assertEqual(batch["product_count"], 1)

    def test_rejects_running_product(self):
        make_product(self.products, "P000001", status="QUEUED", batch_id="B-OLD", task_authorized=True)
        with self.assertRaises(ValueError) as ctx:
            create_batch(self.products, batches_root=self.batches, product_ids=["P000001"])
        self.assertIn("already running or queued", str(ctx.exception))

    def test_rejects_non_1688_capture(self):
        make_product(
            self.products,
            "P000001",
            source_url="https://www.ozon.ru/product/123",
        )
        with self.assertRaises(ValueError) as ctx:
            create_batch(self.products, batches_root=self.batches, product_ids=["P000001"])
        self.assertIn("not a 1688 capture", str(ctx.exception))

    def test_rejects_too_many_skus(self):
        make_product(self.products, "P000001", skus=11)
        with self.assertRaises(ValueError) as ctx:
            create_batch(self.products, batches_root=self.batches, product_ids=["P000001"])
        self.assertIn("SKU", str(ctx.exception))

    def test_missing_product_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_batch(self.products, batches_root=self.batches, product_ids=["P999999"])
        self.assertIn("does not exist", str(ctx.exception))

    def test_empty_selection_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            create_batch(self.products, batches_root=self.batches)
        self.assertIn("没有可处理商品", str(ctx.exception))

    def test_collected_without_source_is_ignored(self):
        directory = self.products / "P000001"
        (directory / "input").mkdir(parents=True)
        st.save_status(directory, st.new_status("P000001"))
        self.assertEqual(select_collected(self.products), [])
        with self.assertRaises(ValueError):
            create_batch(self.products, batches_root=self.batches)


if __name__ == "__main__":
    unittest.main()
