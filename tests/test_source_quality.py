"""采集体检测试：真实 1688 采集常见的坑（重复 SKU、offer 撞车、无图、价格离群）全离线。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from pipeline.source_quality import (  # noqa: E402
    check_capture,
    main as quality_main,
    render_report,
    sanitize_sku_id,
    write_quality,
)


def source(skus=None, **overrides) -> dict:
    payload = {
        "product_id": "P000001",
        "source_url": "https://detail.1688.com/offer/121212121.html",
        "title_zh": "316 不锈钢保温杯",
        "skus": skus
        or [
            {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
            {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
        ],
    }
    payload.update(overrides)
    return payload


def with_images(root: pathlib.Path, counts: dict[str, int]) -> pathlib.Path:
    directory = root / "P000001"
    for role, relative in (("main", "main-images"), ("sku", "sku-images"), ("detail", "detail-images")):
        target = directory / "input" / relative
        target.mkdir(parents=True, exist_ok=True)
        for index in range(1, counts.get(role, 0) + 1):
            (target / f"{index:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
    return directory


class CleanCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_capture_has_no_blockers(self):
        directory = with_images(self.root, {"main": 2, "sku": 2, "detail": 3})
        report = check_capture(source(), product_dir=directory)
        self.assertEqual(report["blocking"], [], report)
        self.assertEqual(report["stats"]["images"], {"main": 2, "sku": 2, "detail": 3})

    def test_sanitize_matches_offer_id_rule(self):
        self.assertEqual(sanitize_sku_id("红 500", 1), "-500")
        self.assertEqual(sanitize_sku_id("S1", 1), "S1")
        self.assertEqual(sanitize_sku_id(None, 3), "S3")


class BlockingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_sku_id_blocks(self):
        directory = with_images(self.root, {"main": 2, "detail": 2})
        skus = [
            {"sku_id": "S1", "color_ru": "красный", "purchase_price_cny": 18.5},
            {"sku_id": "S1", "color_ru": "синий", "purchase_price_cny": 19.0},
        ]
        report = check_capture(source(skus), product_dir=directory)
        self.assertTrue(any("重复 sku_id" in item for item in report["blocking"]), report)
        self.assertEqual(report["stats"]["duplicate_sku_ids"], ["S1"])

    def test_offer_id_collision_after_sanitize_blocks(self):
        directory = with_images(self.root, {"main": 2, "detail": 2})
        # 两个不同 sku_id（不同中文颜色、同一规格），清洗后都会变成 "-500" → offer_id 撞车
        skus = [
            {"sku_id": "红 500", "color_ru": "красный", "purchase_price_cny": 18.5},
            {"sku_id": "蓝 500", "color_ru": "синий", "purchase_price_cny": 19.0},
        ]
        report = check_capture(source(skus), product_dir=directory)
        self.assertTrue(any("撞车" in item for item in report["blocking"]), report)
        self.assertEqual(len(report["stats"]["offer_collisions"]), 1)
        self.assertEqual(sanitize_sku_id("红 500", 1), sanitize_sku_id("蓝 500", 2))

    def test_no_images_at_all_warns_but_does_not_block(self):
        """先跑文案后补图是合法流程 → 只提醒；真正的硬门禁在图片步骤自己那里。"""
        directory = self.root / "P000001"
        (directory / "input").mkdir(parents=True)
        report = check_capture(source(), product_dir=directory)
        self.assertEqual(report["blocking"], [], report)
        self.assertTrue(any("一张图都没有" in item for item in report["warnings"]), report)

    def test_without_product_dir_skips_image_checks(self):
        report = check_capture(source())
        self.assertEqual(report["blocking"], [])
        self.assertEqual(report["stats"]["images"], {})


class WarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_odd_price_and_missing_variant_are_warnings(self):
        directory = with_images(self.root, {"main": 2, "detail": 2})
        skus = [
            {"sku_id": "S1", "purchase_price_cny": 0.3},
            {"sku_id": "S2", "purchase_price_cny": 99999},
            {"sku_id": "S3", "color_ru": "синий", "purchase_price_cny": 20},
        ]
        report = check_capture(source(skus), product_dir=directory)
        self.assertEqual(report["blocking"], [], report)
        joined = " ".join(report["warnings"])
        self.assertIn("采购价异常", joined)
        self.assertIn("没有颜色/规格值", joined)

    def test_fewer_main_images_than_skus_warns(self):
        directory = with_images(self.root, {"main": 1, "detail": 2})
        report = check_capture(source(), product_dir=directory)
        self.assertTrue(any("共用参考图" in item for item in report["warnings"]), report)

    def test_long_sku_id_and_short_title_warn(self):
        directory = with_images(self.root, {"main": 2, "detail": 2})
        skus = [
            {"sku_id": "S" * 60, "color_ru": "красный", "purchase_price_cny": 18.5},
            {"sku_id": "S2", "color_ru": "синий", "purchase_price_cny": 19.0},
        ]
        report = check_capture(source(skus, title_zh="杯子"), product_dir=directory)
        joined = " ".join(report["warnings"])
        self.assertIn("超过 40 字符", joined)
        self.assertIn("标题过短", joined)

    def test_active_sku_filter_limits_checks(self):
        directory = with_images(self.root, {"main": 1, "detail": 2})
        skus = [
            {"sku_id": "S1", "color_ru": "красный", "purchase_price_cny": 18.5},
            {"sku_id": "S2", "color_ru": "синий", "purchase_price_cny": 19.0},
            {"sku_id": "S2", "color_ru": "зелёный", "purchase_price_cny": 20.0},  # 重复，但被排除
        ]
        report = check_capture(source(skus), product_dir=directory, active_sku_ids=["S1"])
        self.assertEqual(report["blocking"], [], report)
        self.assertEqual(report["stats"]["skus_active"], 1)


class WiringTests(unittest.TestCase):
    """采集器 → 流水线门禁：体检结果要真的拦住坏数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, skus) -> pathlib.Path:
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/131313131.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": skus,
            },
        )
        directory = self.products / summary["product_id"]
        target = directory / "input" / "main-images"
        target.mkdir(parents=True, exist_ok=True)
        (target / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n a")
        return directory

    def test_write_quality_creates_artifact(self):
        directory = self._ingest(
            [{"sku_id": "S1", "color_ru": "красный", "purchase_price_cny": 18.5}]
        )
        report = write_quality(directory)
        self.assertTrue((directory / "output" / "source-quality.json").is_file())
        self.assertEqual(report["blocking"], [])
        self.assertIn("采集体检", render_report(report))

    def test_cli_json_and_exit_codes(self):
        import io
        from contextlib import redirect_stdout

        directory = self._ingest(
            [{"sku_id": "S1", "color_ru": "красный", "purchase_price_cny": 18.5}]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = quality_main(["--product-dir", str(directory), "--json"])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(json.loads(buffer.getvalue())["blocking"], [])

        # 重复 SKU → 退出码 1
        source_path = directory / "input" / "source.json"
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        payload["skus"] = payload["skus"] + payload["skus"]
        source_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = quality_main(["--product-dir", str(directory), "--json"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(buffer.getvalue())["blocking"])

    def test_cli_missing_product(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = quality_main(["--product-dir", str(self.root / "nope")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
