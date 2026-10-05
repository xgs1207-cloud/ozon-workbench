"""自建对象存储适配器测试：增量同步、URL 映射、与上传载荷的联动。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.local_image import LocalPlaceholderGenerator  # noqa: E402
from pipeline.context import StepContext  # noqa: E402
from pipeline.handlers import run_single_step  # noqa: E402
from pipeline.image_generation import handle_image_generation  # noqa: E402
from pipeline.image_qc import handle_image_qc  # noqa: E402
from pipeline.oss_local import LocalObjectStorage, planned_slots, sha256_file  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.upload import build_upload_payload, resolve_image_urls  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.static = self.root / "www" / "ozon-images"
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/515151515.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.product_dir = self.products / summary["product_id"]
        for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
            directory = self.product_dir / "input" / relative
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        provider = FakeProvider()
        run_single_step(self.product_dir, "product_analysis", provider=provider)
        run_single_step(self.product_dir, "russian_copy", provider=provider)
        run_single_step(self.product_dir, "image_plan", provider=provider)
        self.storage = LocalObjectStorage(self.static, "https://img.example.com")

    def tearDown(self):
        self.tmp.cleanup()

    def generate_images(self) -> None:
        handle_image_generation(
            StepContext(
                product_dir=self.product_dir,
                step="image_generation",
                image_generator=LocalPlaceholderGenerator(),
            )
        )


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class StorageTests(StorageFixture):
    def test_publishes_every_slot_and_writes_urls(self):
        self.generate_images()
        summary = self.storage.publish_product(self.product_dir)

        self.assertEqual(summary["slots"], 10)  # 2 主图 + 8 详情图
        self.assertEqual(summary["published"], 10)
        self.assertEqual(summary["missing"], [])
        self.assertTrue(summary["https_ok"])
        uuid = self.product_dir.name
        self.assertTrue((self.static / uuid / "main-S1.png").is_file())
        self.assertTrue((self.static / uuid / "detail-008.png").is_file())

        urls = resolve_image_urls(self.product_dir)
        self.assertEqual(len(urls), 10)
        self.assertTrue(all(value.startswith("https://img.example.com/") for value in urls.values()))
        saved = json.loads(
            (self.product_dir / "output" / "image-public-urls.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["storage"], "local-static")
        self.assertEqual(saved["base_url"], "https://img.example.com")

    def test_second_run_is_incremental(self):
        self.generate_images()
        first = self.storage.publish_product(self.product_dir)
        second = self.storage.publish_product(self.product_dir)
        self.assertEqual(first["published"], 10)
        self.assertEqual(second["published"], 0)
        self.assertEqual(second["unchanged"], 10)

    def test_changed_file_is_republished(self):
        self.generate_images()
        self.storage.publish_product(self.product_dir)
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        target = self.product_dir / plan["main_images"][0]["output_path"]
        from pipeline.image_probe import write_solid_png

        write_solid_png(target, 900, 1200)  # 内容变了
        summary = self.storage.publish_product(self.product_dir)
        self.assertEqual(summary["published"], 1)
        self.assertEqual(summary["unchanged"], 9)

    def test_dry_run_writes_nothing(self):
        self.generate_images()
        storage = LocalObjectStorage(self.static, "https://img.example.com", dry_run=True)
        summary = storage.publish_product(self.product_dir)
        self.assertEqual(summary["published"], 0)
        self.assertTrue(all(item["status"] == "would_copy" for item in summary["results"]))
        self.assertIsNone(summary["urls_file"])
        self.assertFalse(self.static.exists())
        self.assertFalse((self.product_dir / "output" / "image-public-urls.json").exists())

    def test_missing_local_file_is_reported(self):
        self.generate_images()
        plan_path = self.product_dir / "output" / "image-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        (self.product_dir / plan["main_images"][0]["output_path"]).unlink()
        summary = self.storage.publish_product(self.product_dir)
        self.assertEqual(len(summary["missing"]), 1)
        self.assertEqual(summary["published"], 9)

    def test_url_base_path_is_prepended(self):
        self.generate_images()
        storage = LocalObjectStorage(
            self.static, "https://cdn.example.com", url_base_path="/ozon"
        )
        summary = storage.publish_product(self.product_dir)
        self.assertTrue(all("/ozon/" in url for url in summary["urls"].values()))

    def test_slot_filter(self):
        self.generate_images()
        summary = self.storage.publish_product(self.product_dir, slots=["main-S1"])
        self.assertEqual(summary["slots"], 1)
        self.assertEqual(list(summary["urls"]), ["main-S1"])

    def test_bad_base_url_and_missing_plan(self):
        with self.assertRaises(ValueError):
            LocalObjectStorage(self.static, "img.example.com")
        with self.assertRaises(ValueError):
            self.storage.publish_product(self.root / "nope")

    def test_upload_payload_uses_published_urls(self):
        self.generate_images()
        self.storage.publish_product(self.product_dir)
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(payload["image_upload_gate"]["passed"])
        self.assertEqual(payload["image_upload_gate"]["missing_slots"], [])
        self.assertTrue(all(image["url"].startswith("https://") for image in payload["images"]))

    def test_cli_dry_run_and_json(self):
        from pipeline.oss_local import main

        self.generate_images()
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--product-dir",
                    str(self.product_dir),
                    "--root",
                    str(self.static),
                    "--base-url",
                    "https://img.example.com",
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["slots"], 10)
        self.assertFalse(self.static.exists())

    def test_cli_reports_error_for_bad_product(self):
        from pipeline.oss_local import main

        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--product-dir",
                    str(self.root / "nope"),
                    "--root",
                    str(self.static),
                    "--base-url",
                    "https://img.example.com",
                ]
            )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_planned_slots_and_hash_helper(self):
        rows = planned_slots(self.product_dir)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["role"], "variant_main")
        self.assertEqual(rows[-1]["role"], "detail")
        first = self.product_dir / rows[0]["output_path"]
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"abc")
        self.assertEqual(sha256_file(first), sha256_file(first))


if __name__ == "__main__":
    unittest.main()
