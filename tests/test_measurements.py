"""定价与尺寸重量测试：定价数学、只接受确认值、契约合规、与上传门禁的联动。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.context import PipelineGateError, StepContext  # noqa: E402
from pipeline.measurements import (  # noqa: E402
    DEFAULT_CONFIG,
    collect_measurements,
    compute_pricing,
    compute_sku_pricing,
    handle_measurements,
    load_measurements,
    load_pricing_config,
    nice_price,
    volumetric_weight_g,
)
from pipeline.upload import build_upload_payload  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0

OVERRIDES = {
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
}


def source_payload(skus: int = 2) -> dict:
    return {
        "product_id": "P000001",
        "source_url": "https://detail.1688.com/offer/121212121.html",
        "skus": [
            {"sku_id": f"S{index + 1}", "purchase_price_cny": 18.0 + index, "name_zh": f"红色 {index + 1}"}
            for index in range(skus)
        ],
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_when_no_file(self):
        config, warnings = load_pricing_config(self.root / "config" / "pricing.json")
        self.assertEqual(config["rub_per_cny"], DEFAULT_CONFIG["rub_per_cny"])
        self.assertTrue(any("默认费率" in item for item in warnings))

    def test_file_overrides_defaults(self):
        path = self.root / "pricing.json"
        path.write_text(json.dumps({"rub_per_cny": 12.5, "commission_rate": 0.18, "unknown_key": 1}), encoding="utf-8")
        config, warnings = load_pricing_config(path)
        self.assertEqual(config["rub_per_cny"], 12.5)
        self.assertEqual(config["commission_rate"], 0.18)
        self.assertNotIn("unknown_key", config)
        self.assertEqual(warnings, [])

    def test_broken_file_falls_back(self):
        path = self.root / "pricing.json"
        path.write_text("{not json", encoding="utf-8")
        config, warnings = load_pricing_config(path)
        self.assertEqual(config["rub_per_cny"], DEFAULT_CONFIG["rub_per_cny"])
        self.assertTrue(any("解析失败" in item for item in warnings))


class MeasurementCollectionTests(unittest.TestCase):
    def test_confirmed_overrides_are_used(self):
        result = collect_measurements(product_id="P000001", source=source_payload(), overrides=OVERRIDES)
        self.assertEqual(result["source"], "user_confirmed")
        self.assertEqual(result["product"]["length_mm"], 90)
        self.assertEqual(result["package"]["weight_g"], 430)
        self.assertTrue(result["hierarchy_ok"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(set(result["sku_measurements"]), {"S1", "S2"})

    def test_missing_measurements_are_reported_not_invented(self):
        result = collect_measurements(product_id="P000001", source=source_payload(), overrides={})
        self.assertEqual(result["source"], "missing")
        self.assertIsNone(result["product"])
        self.assertTrue(any("缺商品尺寸重量" in item for item in result["warnings"]))

    def test_per_sku_overrides_and_flat_keys(self):
        overrides = {
            "sku_overrides": {
                "S1": {
                    "length_mm": 80,
                    "width_mm": 80,
                    "height_mm": 200,
                    "weight_g": 300,
                    "package_length_mm": 100,
                    "package_width_mm": 100,
                    "package_height_mm": 220,
                    "package_weight_g": 360,
                }
            }
        }
        result = collect_measurements(product_id="P000001", source=source_payload(), overrides=overrides)
        self.assertEqual(result["source"], "mixed")
        self.assertEqual(result["sku_measurements"]["S1"]["product"]["height_mm"], 200)
        self.assertIsNone(result["sku_measurements"]["S2"]["product"])

    def test_hierarchy_violation_is_flagged(self):
        overrides = {
            "product": {
                "product_length_mm": 200,
                "product_width_mm": 200,
                "product_height_mm": 400,
                "product_weight_g": 900,
                "package_length_mm": 100,
                "package_width_mm": 100,
                "package_height_mm": 200,
                "package_weight_g": 400,
            }
        }
        result = collect_measurements(product_id="P000001", source=source_payload(), overrides=overrides)
        self.assertFalse(result["hierarchy_ok"])
        self.assertTrue(any("小于商品本体" in item for item in result["warnings"]))

    def test_legacy_cost_analysis_fallback_normalizes_cm(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            directory = pathlib.Path(tmp.name) / "P000001"
            (directory / "output").mkdir(parents=True)
            (directory / "output" / "cost-analysis.json").write_text(
                json.dumps(
                    {
                        "product_dimensions": {"length": 9, "width": 9, "height": 25, "unit": "cm", "weight_g": 350},
                        "package_dimensions": {"length": 11, "width": 11, "height": 28, "unit": "cm", "weight_g": 430},
                        "measurement_hierarchy": {"valid": True, "rule": "package>=product"},
                    }
                ),
                encoding="utf-8",
            )
            surface = load_measurements(directory)
            self.assertEqual(surface["product"]["length_mm"], 90)
            self.assertEqual(surface["package"]["weight_g"], 430)
            self.assertEqual(surface["source"], "legacy_cost_analysis")
        finally:
            tmp.cleanup()


class PricingMathTests(unittest.TestCase):
    def test_nice_price_only_rounds_up_to_ending(self):
        self.assertEqual(nice_price(1478, ends_with=90, step=10), 1490)
        self.assertEqual(nice_price(1400, ends_with=90, step=10), 1490)
        self.assertEqual(nice_price(1489, ends_with=90, step=10), 1490)

    def test_volumetric_weight(self):
        dims = {"length_mm": 100, "width_mm": 100, "height_mm": 600, "weight_g": 100}
        # 10cm × 10cm × 60cm = 6000 cm³ → /6000 = 1kg
        self.assertEqual(volumetric_weight_g(dims, divisor=6000), 1000)

    def test_pricing_meets_margin_and_price_shape(self):
        row = compute_sku_pricing(
            sku={"sku_id": "S1", "purchase_price_cny": 18.5},
            sku_id="S1",
            config=DEFAULT_CONFIG,
            dimensions={"length_mm": 110, "width_mm": 110, "height_mm": 280, "weight_g": 430},
        )
        self.assertEqual(row["status"], "UPLOAD")
        self.assertIsNotNone(row["selling_price_rub"])
        self.assertEqual(row["selling_price_rub"] % 100, 90)  # 尾数 90
        self.assertGreaterEqual(row["margin_rate"], DEFAULT_CONFIG["min_margin_rate"])
        self.assertGreater(row["selling_price_cny"], row["base_cost_cny"])
        # 计费重取"实际重"与"体积重"的较大者（11×11×28cm → 565g）
        self.assertEqual(row["billable_weight_g"], max(row["actual_weight_g"], row["volumetric_weight_g"]))
        self.assertGreater(row["volumetric_weight_g"], row["actual_weight_g"])

    def test_volumetric_weight_wins_when_larger(self):
        row = compute_sku_pricing(
            sku={"sku_id": "S1", "purchase_price_cny": 10},
            sku_id="S1",
            config=DEFAULT_CONFIG,
            dimensions={"length_mm": 200, "width_mm": 200, "height_mm": 600, "weight_g": 100},
        )
        self.assertGreater(row["volumetric_weight_g"], row["actual_weight_g"])
        self.assertEqual(row["billable_weight_g"], row["volumetric_weight_g"])

    def test_missing_purchase_price_is_reject(self):
        row = compute_sku_pricing(sku={"sku_id": "S1"}, sku_id="S1", config=DEFAULT_CONFIG, dimensions=None)
        self.assertEqual(row["status"], "REJECT")
        self.assertIsNone(row["selling_price_rub"])
        self.assertTrue(any("采购价" in item for item in row["errors"]))

    def test_extreme_fee_config_is_rejected_not_crashed(self):
        config = dict(DEFAULT_CONFIG, target_margin_rate=0.9, commission_rate=0.2, logistics_commission_rate=0.3)
        row = compute_sku_pricing(sku={"sku_id": "S1", "purchase_price_cny": 10}, sku_id="S1", config=config, dimensions=None)
        self.assertEqual(row["status"], "REJECT")
        self.assertTrue(any("无法定价" in item for item in row["errors"]))

    def test_higher_shipping_raises_price_not_lowers_margin(self):
        """成本加成定价的固有性质：成本高就抬价，利润率仍贴着目标值 —— 风险是"没竞争力"而非"亏本"。"""
        base = dict(DEFAULT_CONFIG)
        cheap = compute_sku_pricing(
            sku={"sku_id": "S1", "purchase_price_cny": 10}, sku_id="S1", config=base, dimensions=None
        )
        pricey = compute_sku_pricing(
            sku={"sku_id": "S1", "purchase_price_cny": 10},
            sku_id="S1",
            config=dict(base, shipping_cost_cny_per_kg=300.0),
            dimensions={"length_mm": 300, "width_mm": 300, "height_mm": 300, "weight_g": 5000},
        )
        self.assertGreater(pricey["selling_price_rub"], cheap["selling_price_rub"])
        self.assertGreaterEqual(pricey["margin_rate"], base["min_margin_rate"])

    def test_price_ceiling_downgrades_to_warning(self):
        config = dict(DEFAULT_CONFIG, max_price_rub=3000, shipping_cost_cny_per_kg=300.0)
        row = compute_sku_pricing(
            sku={"sku_id": "S1", "purchase_price_cny": 10},
            sku_id="S1",
            config=config,
            dimensions={"length_mm": 300, "width_mm": 300, "height_mm": 300, "weight_g": 5000},
        )
        self.assertEqual(row["status"], "WARNING")
        self.assertTrue(any("超过上限" in item for item in row["errors"]))

    def test_recommendation_aggregates(self):
        pricing = compute_pricing(
            product_id="P000001",
            source=source_payload(),
            config=DEFAULT_CONFIG,
            measurements=collect_measurements(product_id="P000001", source=source_payload(), overrides=OVERRIDES),
        )
        self.assertEqual(pricing["recommendation"], "UPLOAD")
        self.assertEqual(len(pricing["skus"]), 2)
        self.assertEqual(pricing["pricing_source"], "workbench-pricing-engine")

        broken = source_payload()
        broken["skus"][1].pop("purchase_price_cny")
        pricing_broken = compute_pricing(
            product_id="P000001", source=broken, config=DEFAULT_CONFIG, measurements={"package": None}
        )
        self.assertEqual(pricing_broken["recommendation"], "REJECT")


class MeasurementHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/232323232.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.product_dir = self.products / self.summary["product_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def write_overrides(self) -> None:
        (self.product_dir / "input" / "workbench-sku-overrides.json").write_text(
            json.dumps(OVERRIDES, ensure_ascii=False), encoding="utf-8"
        )

    @unittest.skipUnless(HAS_CONTRACTS, "contracts 尚未拉取")
    def test_handler_writes_contract_valid_artifacts(self):
        self.write_overrides()
        result = handle_measurements(StepContext(product_dir=self.product_dir, step="measurements"))
        self.assertEqual(result["recommendation"], "UPLOAD")
        self.assertEqual(result["priced_skus"], 2)
        self.assertEqual(result["measurement_source"], "user_confirmed")

        pricing = json.loads((self.product_dir / "output" / "pricing-result.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_contract("workbench-pricing-result", pricing), [])
        measurements = json.loads((self.product_dir / "output" / "measurements.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_contract("workbench-measurements", measurements), [])
        profit = json.loads((self.product_dir / "output" / "profit-analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(len(profit["skus"]), 2)

    def test_handler_without_overrides_warns_but_still_prices(self):
        result = handle_measurements(StepContext(product_dir=self.product_dir, step="measurements"))
        self.assertTrue(any("缺包装尺寸重量" in item for item in result["warnings"]))
        self.assertTrue(any("人工确认" in item for item in result["warnings"]))

    @unittest.skipUnless(HAS_CONTRACTS, "contracts 尚未拉取")
    def test_upload_blockers_clear_after_measurements(self):
        create_batch(self.products, batches_root=self.root / "batches", target_store_ids=["shop-a"])
        before = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(any("尺寸" in item or "重量" in item for item in before["production_blockers"]))

        self.write_overrides()
        handle_measurements(StepContext(product_dir=self.product_dir, step="measurements"))
        after = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertFalse(any("尺寸" in item or "重量" in item for item in after["production_blockers"]), after["production_blockers"])
        self.assertFalse(any("售价" in item for item in after["production_blockers"]))
        self.assertEqual(after["sku_measurements"]["package_dimensions"]["weight_g"], 430)
        self.assertTrue(all(variant["price"].endswith(".00") for variant in after["variants"]))

    def test_invalid_measurements_block_handler(self):
        (self.product_dir / "input" / "workbench-sku-overrides.json").write_text(
            json.dumps({"product": {"product_length_mm": -5}}, ensure_ascii=False), encoding="utf-8"
        )
        result = handle_measurements(StepContext(product_dir=self.product_dir, step="measurements"))
        self.assertEqual(result["measurement_source"], "missing")


if __name__ == "__main__":
    unittest.main()
