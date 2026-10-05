"""上架 SKU 选择测试：模块本身 + 对流水线各环节的约束（全离线）。"""

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
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.sku_selection import (  # noqa: E402
    SkuSelectionError,
    active_skus,
    main as sku_main,
    selection_state,
    set_selection,
    source_skus,
)
from pipeline.upload import build_upload_payload, SimulatedUploader  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
SKUS = ("S1", "S2", "S3")


def capture() -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/606060606.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001"},
        "skus": [
            {"sku_id": sku_id, "color_ru": color, "capacity": "500 мл", "purchase_price_cny": 18.0 + index}
            for index, (sku_id, color) in enumerate(
                zip(SKUS, ("красный", "синий", "зелёный"))
            )
        ],
    }


class SelectionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        summary = ingest_capture(self.products, capture())
        self.product_dir = self.products / summary["product_id"]
        for relative, count in (("main-images", 3), ("sku-images", 3), ("detail-images", 3)):
            target = self.product_dir / "input" / relative
            target.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (target / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(
            self.product_dir,
            ["термос 500 мл"],
            category={"category_id": "1001", "type_id": "2001"},
        )
        self.write_overrides(SKUS)

    def tearDown(self):
        self.tmp.cleanup()

    def write_overrides(self, sku_ids) -> None:
        payload = {
            "product": {
                "product_length_mm": 90,
                "product_width_mm": 90,
                "product_height_mm": 250,
                "product_weight_g": 350,
                "package_length_mm": 110,
                "package_width_mm": 110,
                "package_height_mm": 280,
                "package_weight_g": 430,
            },
            "sku_overrides": {
                sku_id: {
                    "product_length_mm": 90,
                    "product_width_mm": 90,
                    "product_height_mm": 250,
                    "product_weight_g": 350,
                    "package_length_mm": 110,
                    "package_width_mm": 110,
                    "package_height_mm": 280,
                    "package_weight_g": 430,
                }
                for sku_id in sku_ids
            },
        }
        (self.product_dir / "input" / "workbench-sku-overrides.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def run_chain(self):
        from pipeline.ozon_http import FixtureTransport, OzonClient

        fixtures = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
        create_batch(
            self.products,
            batches_root=self.batches,
            product_ids=[self.product_dir.name],
            target_store_ids=["shop-a"],
        )
        return run_product(
            self.product_dir,
            until="ozon_upload",
            dry_run=True,
            provider=FakeProvider(),
            image_generator=LocalPlaceholderGenerator(),
            uploader=SimulatedUploader(),
            ozon_client=OzonClient(FixtureTransport(directory=fixtures)),
        )


class ModuleTests(SelectionFixture):
    def test_no_selection_means_all_skus(self):
        self.assertEqual([row["sku_id"] for row in active_skus(self.product_dir, source_skus(self.product_dir))], list(SKUS))
        state = selection_state(self.product_dir)
        self.assertFalse(state["has_selection"])
        self.assertEqual(state["active_count"], 3)

    def test_include_writes_file_and_filters(self):
        result = set_selection(self.product_dir, include=["S1", "S3"], reason="只上架两个颜色")
        self.assertTrue(result["ok"])
        self.assertEqual(result["selected"], ["S1", "S3"])
        self.assertEqual([row["sku_id"] for row in active_skus(self.product_dir, source_skus(self.product_dir))], ["S1", "S3"])
        state = selection_state(self.product_dir)
        self.assertTrue(state["has_selection"])
        self.assertEqual(state["active_count"], 2)
        self.assertEqual(state["excluded"], [{"sku_id": "S2", "reason": "只上架两个颜色"}])

    def test_exclude_is_the_complement(self):
        set_selection(self.product_dir, exclude=["S2"])
        state = selection_state(self.product_dir)
        self.assertEqual(state["selected"], ["S1", "S3"])
        self.assertIn("人工排除", state["excluded"][0]["reason"])

    def test_unknown_sku_is_rejected(self):
        with self.assertRaises(SkuSelectionError) as ctx:
            set_selection(self.product_dir, include=["S9"])
        self.assertIn("不在采集数据里", str(ctx.exception))

    def test_include_and_exclude_overlap_is_rejected(self):
        with self.assertRaises(SkuSelectionError):
            set_selection(self.product_dir, include=["S1"], exclude=["S1"])

    def test_excluding_everything_is_rejected(self):
        with self.assertRaises(SkuSelectionError) as ctx:
            set_selection(self.product_dir, exclude=list(SKUS))
        self.assertIn("至少要保留 1 个", str(ctx.exception))

    def test_clear_selection_restores_all(self):
        set_selection(self.product_dir, include=["S1"])
        self.assertTrue((self.product_dir / "input" / "selected-skus.json").is_file())
        clear = __import__("pipeline.sku_selection", fromlist=["clear_selection"]).clear_selection(self.product_dir)
        self.assertTrue(clear["removed"])
        self.assertEqual(len(active_skus(self.product_dir, source_skus(self.product_dir))), 3)

    def test_state_reports_unknown_entries_in_file(self):
        set_selection(self.product_dir, include=["S1"])
        path = self.product_dir / "input" / "selected-skus.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["selected"] = ["S1", "GONE"]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        state = selection_state(self.product_dir)
        self.assertEqual(state["unknown_in_selection"], ["GONE"])
        self.assertEqual([row["sku_id"] for row in active_skus(self.product_dir, source_skus(self.product_dir))], ["S1"])

    def test_cli_modes(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sku_main(["--product-dir", str(self.product_dir), "--include", "S2"])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(json.loads(buffer.getvalue())["selected"], ["S2"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sku_main(["--product-dir", str(self.product_dir), "--list"])
        self.assertEqual(code, 0)
        self.assertIn("上架", buffer.getvalue())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sku_main(["--product-dir", str(self.product_dir), "--exclude", "S9"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sku_main(["--product-dir", str(self.product_dir), "--all"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["ok"])


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class IntegrationTests(SelectionFixture):
    def test_without_selection_all_skus_flow_through(self):
        report = self.run_chain()
        self.assertEqual(report["stop_reason"], "until_reached")
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([item["slot"] for item in plan["main_images"]], ["main-S1", "main-S2", "main-S3"])
        pricing = json.loads((self.product_dir / "output" / "pricing-result.json").read_text(encoding="utf-8"))
        self.assertEqual([row["sku_id"] for row in pricing["skus"]], list(SKUS))

    def test_selection_limits_images_pricing_and_payload(self):
        set_selection(self.product_dir, include=["S1", "S3"], reason="只上架两个颜色")
        self.write_overrides(["S1", "S3"])  # S2 不上架，就不需要它的尺寸重量
        report = self.run_chain()
        self.assertEqual(report["stop_reason"], "until_reached", report)

        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([item["slot"] for item in plan["main_images"]], ["main-S1", "main-S3"])

        pricing = json.loads((self.product_dir / "output" / "pricing-result.json").read_text(encoding="utf-8"))
        self.assertEqual([row["sku_id"] for row in pricing["skus"]], ["S1", "S3"])

        measurements = json.loads((self.product_dir / "output" / "measurements.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(measurements["sku_measurements"]), ["S1", "S3"])

        payload = build_upload_payload(
            self.product_dir,
            shop_name="shop-a",
            image_urls={
                slot: f"https://cdn.example.com/P000001/{slot}.png"
                for slot in ("main-S1", "main-S3", "detail-001")
            },
        )
        self.assertEqual([item["source_sku_id"] for item in payload["variants"]], ["S1", "S3"])
        self.assertNotIn("S2", json.dumps(payload["variants"], ensure_ascii=False))

    def test_selection_makes_excluded_missing_dimensions_irrelevant(self):
        """没上架的 SKU 缺尺寸重量，不该把整单卡住（这正是选择功能的意义）。"""
        set_selection(self.product_dir, include=["S1"])
        self.write_overrides(["S1"])  # 只确认 S1 的尺寸
        report = self.run_chain()
        self.assertEqual(report["stop_reason"], "until_reached", report)
        measurements = json.loads((self.product_dir / "output" / "measurements.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(measurements["sku_measurements"]), ["S1"])

    def test_attribute_variants_filtered_by_selection(self):
        set_selection(self.product_dir, include=["S2"])
        self.write_overrides(["S2"])
        report = self.run_chain()
        self.assertEqual(report["stop_reason"], "until_reached", report)
        fill_input = json.loads(
            (self.product_dir / "output" / "attribute-fill-input.json").read_text(encoding="utf-8")
        )
        self.assertEqual([str(item.get("sku_id")) for item in fill_input["skus"]], ["S2"])


if __name__ == "__main__":
    unittest.main()
