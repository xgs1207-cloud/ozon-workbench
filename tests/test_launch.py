"""一键编排测试：生图 → 发布图片 → 质检 → 载荷 → 提交（全离线，用假 uploader 与本地发布）。"""

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
from pipeline.batch import create_batch  # noqa: E402
from pipeline.launch import launch_batch, launch_product, main as launch_main, render_report, select_products  # noqa: E402
from pipeline.oss_local import LocalObjectStorage  # noqa: E402
from pipeline.publications import load_publications  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.upload import SimulatedUploader  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
STORE_IDS = ["shop-a", "shop-b"]


def capture(url_suffix: str, keyword: str | None = None) -> dict:
    payload = {
        "source_url": f"https://detail.1688.com/offer/{url_suffix}.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001"},
        "skus": [
            {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
            {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
        ],
    }
    if keyword:
        payload["keywords"] = [keyword]
    return payload


class LaunchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        self.static = self.root / "www"
        self.provider = FakeProvider()
        self.image_generator = LocalPlaceholderGenerator()
        self.product_dir = self._ingest("808080808", ["простынь на резинке 160х200"])

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, suffix: str, keywords: list[str] | None = None) -> pathlib.Path:
        summary = ingest_capture(self.products, capture(suffix))
        directory = self.products / summary["product_id"]
        for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
            target = directory / "input" / relative
            target.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (target / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(directory, keywords or ["простынь на резинке 160х200"])
        # 人工确认的尺寸重量：没有它 measurements 算不出定价，上传门禁会（正确地）拦住
        (directory / "input" / "workbench-sku-overrides.json").write_text(
            json.dumps(
                {
                    "product": {
                        "product_length_mm": 90,
                        "product_width_mm": 90,
                        "product_height_mm": 250,
                        "product_weight_g": 350,
                        "package_length_mm": 110,
                        "package_width_mm": 110,
                        "package_height_mm": 280,
                        "package_weight_g": 430,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return directory

    def ozon_client(self):
        """离线用录制夹具走 Ozon 只读调用（category_match 需要它）。"""
        from pipeline.ozon_http import FixtureTransport, OzonClient

        fixtures = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
        return OzonClient(FixtureTransport(directory=fixtures))

    def publisher(self, *, name: str = "local-static") -> LocalObjectStorage:
        storage = LocalObjectStorage(self.static, "https://img.example.com")
        storage.name = name
        return storage

    def options(self, **overrides) -> dict:
        options = {
            "provider": self.provider,
            "image_generator": self.image_generator,
            "uploader": SimulatedUploader(),
            "publisher": self.publisher(),
            "ozon_client": self.ozon_client(),
            "store_ids": STORE_IDS,
            "execute_upload": True,
            "app_mode": "production",
            "step_budget": 30,
            "batches_root": self.batches,
            "products_root": self.products,
        }
        options.update(overrides)
        return options


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class LaunchProductTests(LaunchFixture):
    def test_full_launch_publishes_images_and_submits(self):
        report = launch_product(self.product_dir, **self.options())

        self.assertTrue(report["ok"], report)
        phases = {item["phase"]: item for item in report["phases"]}
        self.assertEqual(phases["content_and_images"]["stop_reason"], "until_reached")
        self.assertEqual(phases["publish_images"]["urls"], 10)
        self.assertTrue(phases["publish_images"]["https_ok"])
        self.assertEqual(phases["upload"]["stop_reason"], "all_available_steps_done")
        self.assertEqual(phases["upload"]["api_write_count"], 2)

        evidence = report["evidence"]
        self.assertEqual(evidence["image_public_urls"]["count"], 10)
        self.assertTrue(evidence["image_public_urls"]["https_only"])
        self.assertEqual(set(evidence["receipts"]), set(STORE_IDS))
        for receipt in evidence["receipts"].values():
            self.assertNotEqual(receipt["task_id"], "unknown")

    def test_dry_run_never_writes_and_produces_receipts(self):
        from pipeline.upload import DryRunUploader

        report = launch_product(
            self.product_dir,
            **self.options(uploader=DryRunUploader(), execute_upload=False, app_mode="development"),
        )
        phases = {item["phase"]: item for item in report["phases"]}
        self.assertEqual(phases["upload"]["mode"], "dry_run_receipt")
        self.assertEqual(phases["upload"]["api_writes"], 0)
        self.assertTrue(report["dry_run"])
        # 授权阶段会把店铺写进台账（selected=true、无 task_id）：干跑绝不能产生 task_id
        publications = load_publications(self.product_dir)
        for store, entry in (publications.get("stores") or {}).items():
            self.assertFalse(
                any(row.get("task_id") for row in entry.get("sku_publications") or []),
                f"{store} 在干跑后产生了 task_id",
            )

    def test_publish_failure_stops_before_upload(self):
        class BrokenPublisher:
            name = "broken"

            def publish_product(self, product_dir):
                raise RuntimeError("COS 拒绝写入")

        report = launch_product(self.product_dir, **self.options(publisher=BrokenPublisher()))
        self.assertFalse(report["ok"])
        self.assertEqual(report["stopped_phase"], "publish_images")
        self.assertIn("COS 拒绝写入", report["reason"])
        # 没有发布成功就绝不提交
        self.assertFalse((self.product_dir / "output" / "store-runs").exists())

    def test_missing_slot_stops_before_upload(self):
        class PartialPublisher(LocalObjectStorage):
            def publish_product(self, product_dir, **kwargs):
                summary = super().publish_product(product_dir, **kwargs)
                summary["missing"] = [{"slot": "detail-008", "reason": "本地文件不存在"}]
                return summary

        report = launch_product(
            self.product_dir,
            **self.options(publisher=PartialPublisher(self.static, "https://img.example.com")),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["stopped_phase"], "publish_images")
        self.assertIn("没有发布成功", report["reason"])

    def test_stops_early_when_content_phase_fails(self):
        empty = self._ingest("909090909")
        (empty / "input" / "selected-keywords.json").unlink()
        report = launch_product(empty, **self.options())
        self.assertFalse(report["ok"])
        self.assertEqual(report["stopped_phase"], "content_and_images")
        self.assertIn(report["reason"], {"gate_failed", "missing_inputs", "handler_not_implemented"})

    def test_no_publisher_means_no_urls_and_upload_blocked(self):
        report = launch_product(self.product_dir, **self.options(publisher=None))
        self.assertFalse(report["ok"])
        # 第①段能跑完（图片生成/质检），但第③段会被 https 地址门禁拦住
        phases = {item["phase"]: item for item in report["phases"]}
        self.assertEqual(phases["content_and_images"]["stop_reason"], "until_reached")
        self.assertEqual(phases["upload"]["stop_reason"], "gate_failed")
        self.assertEqual(phases["upload"]["stopped_at"], "ozon_upload")
        self.assertEqual(phases["upload"]["api_write_count"], 0)
        self.assertTrue(any("提交失败" in item for item in phases["upload"]["warnings"]))

    def test_uploader_none_reports_skip(self):
        report = launch_product(self.product_dir, **self.options(uploader=None))
        self.assertFalse(report["ok"])
        self.assertEqual(report["stopped_phase"], "upload")
        self.assertIn("uploader", report["reason"])

    def test_render_report_contains_phases(self):
        report = launch_product(self.product_dir, **self.options())
        text = render_report(report)
        self.assertIn("一键跑批次", text)
        self.assertIn("content_and_images", text)
        self.assertIn("publish_images", text)
        self.assertIn("质检", text)
        self.assertIn("店铺回执", text)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class LaunchBatchTests(LaunchFixture):
    def test_select_products_filters_by_status_and_keyword(self):
        second = self._ingest("818181818", ["простынь евро"])
        selected = select_products(self.products)
        self.assertEqual({item.name for item in selected}, {self.product_dir.name, second.name})
        self.assertEqual(
            [item.name for item in select_products(self.products, keywords=["простынь евро"])], [second.name]
        )
        self.assertEqual(select_products(self.products, status="UPLOADING"), [])

    def test_batch_launch_runs_every_product(self):
        second = self._ingest("828282828", ["простынь евро"])
        report = launch_batch(
            self.products,
            STORE_IDS,
            batches_root=self.batches,
            provider=self.provider,
            image_generator=self.image_generator,
            uploader=SimulatedUploader(),
            publisher=self.publisher(),
            ozon_client=self.ozon_client(),
            execute_upload=True,
            app_mode="production",
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["summary"]["products"], 2)
        self.assertTrue(report["batch_id"])
        for item in report["products"]:
            self.assertTrue(item["ok"], item)
            self.assertNotIn(item["product_id"], report["summary"]["failed"])
        self.assertEqual(len(load_publications(second)["stores"]), 2)

    def test_batch_with_no_products_reports_clearly(self):
        empty_root = self.root / "empty-products"
        empty_root.mkdir()
        report = launch_batch(empty_root, STORE_IDS, batches_root=self.batches)
        self.assertFalse(report["ok"])
        self.assertIn("没有符合条件", report["reason"])

    def test_batch_isolates_failures(self):
        second = self._ingest("838383838", ["простынь евро"])
        (second / "input" / "selected-keywords.json").unlink()
        report = launch_batch(
            self.products,
            STORE_IDS,
            batches_root=self.batches,
            provider=self.provider,
            image_generator=self.image_generator,
            uploader=SimulatedUploader(),
            publisher=self.publisher(),
            ozon_client=self.ozon_client(),
            execute_upload=True,
            app_mode="production",
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["failed"], [second.name])
        self.assertEqual(len(report["products"]), 2)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class LaunchCliTests(LaunchFixture):
    def test_cli_single_product_simulated_upload(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = launch_main(
                [
                    "--product-dir",
                    str(self.product_dir),
                    "--store",
                    "shop-a",
                    "--uploader",
                    "simulated",
                    "--execute-upload",
                    "--image-generator",
                    "placeholder",
                    "--ozon-fixture",
                    "contracts/fixtures",
                    "--oss",
                    "local",
                    "--oss-root",
                    str(self.static),
                    "--oss-base-url",
                    "https://img.example.com",
                    "--json",
                ]
            )
        self.assertEqual(code, 0, buffer.getvalue())
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["ok"])
        phases = {item["phase"]: item for item in report["phases"]}
        self.assertEqual(phases["upload"]["mode"], "runner")

    def test_cli_default_is_dry_run(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = launch_main(
                [
                    "--product-dir",
                    str(self.product_dir),
                    "--store",
                    "shop-a",
                    "--image-generator",
                    "placeholder",
                    "--ozon-fixture",
                    "contracts/fixtures",
                    "--oss",
                    "local",
                    "--oss-root",
                    str(self.static),
                    "--oss-base-url",
                    "https://img.example.com",
                    "--json",
                ]
            )
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertTrue(json.loads(buffer.getvalue())["dry_run"])

    def test_cli_real_uploader_requires_confirmation(self):
        with self.assertRaises(SystemExit):
            launch_main(["--product-dir", str(self.product_dir), "--uploader", "ozon-api", "--store", "shop-a"])

    def test_cli_cos_without_credentials_fails_clearly(self):
        import io
        import os
        from contextlib import redirect_stdout

        saved = {key: os.environ.pop(key, None) for key in ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET", "COS_REGION")}
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = launch_main(
                    ["--product-dir", str(self.product_dir), "--store", "shop-a", "--oss", "cos", "--json"]
                )
            self.assertEqual(code, 1)
            self.assertIn("COS 不可用", json.loads(buffer.getvalue())["error"])
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_cli_local_requires_root_and_base_url(self):
        with self.assertRaises(SystemExit):
            launch_main(["--product-dir", str(self.product_dir), "--oss", "local"])

    def test_cli_batch_needs_store(self):
        with self.assertRaises(SystemExit):
            launch_main(["--products-root", str(self.products)])


if __name__ == "__main__":
    unittest.main()
