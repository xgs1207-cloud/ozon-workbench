"""定价模型测试：卢布固定物流费、成本明细、报价试算 CLI（全离线）。

重点：Ozon 的物流/处理费多数是"每件 + 每公斤"的**卢布固定费**，不是价格百分比。
把它当百分比，会在高价/大件上系统性算错利润。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline.measurements import (  # noqa: E402
    DEFAULT_CONFIG,
    compute_sku_pricing,
    load_pricing_config,
    main as measurements_main,
    quote,
)


def config(**overrides) -> dict:
    payload = dict(DEFAULT_CONFIG)
    payload.update(overrides)
    return payload


def price(cost_cny=20.0, weight_g=500, **overrides) -> dict:
    return compute_sku_pricing(
        sku={"sku_id": "S1", "purchase_price_cny": cost_cny, "weight_g": weight_g},
        sku_id="S1",
        config=config(**overrides),
        dimensions=None,
    )


class FixedLogisticsFeeTests(unittest.TestCase):
    def test_no_fixed_fee_by_default(self):
        row = price()
        self.assertEqual(row["logistics_fee_rub"], 0)
        self.assertEqual(row["logistics_fee_cny"], 0)

    def test_per_item_fee_raises_price_predictably(self):
        base = price()
        with_fee = price(logistics_fee_rub_per_item=116.0)  # 116 RUB = 10 CNY（rub_per_cny=11.6）
        # 成本 +10 CNY，售价 = 成本 /(1-费率-目标利润率) → 涨幅应为 10 / (1-0.225-0.25)
        denominator = 1 - 0.225 - 0.25
        self.assertAlmostEqual(
            with_fee["selling_price_cny"] - base["selling_price_cny"],
            10.0 / denominator,
            places=2,
        )
        self.assertAlmostEqual(with_fee["logistics_fee_cny"], 10.0, places=2)

    def test_per_kg_fee_scales_with_billable_weight(self):
        light = price(weight_g=500, logistics_fee_rub_per_kg=232.0)  # 232 RUB/kg = 20 CNY/kg
        heavy = price(weight_g=2000, logistics_fee_rub_per_kg=232.0)
        self.assertAlmostEqual(light["logistics_fee_cny"], 10.0, places=2)
        self.assertAlmostEqual(heavy["logistics_fee_cny"], 40.0, places=2)
        self.assertGreater(heavy["base_cost_cny"], light["base_cost_cny"])

    def test_processing_fee_counts(self):
        row = price(order_processing_fee_rub=58.0)
        self.assertAlmostEqual(row["logistics_fee_cny"], 5.0, places=2)

    def test_fixed_fee_is_not_price_percentage(self):
        """同样的固定费下，售价越高，固定费占售价比例越低（这与百分比费的本质区别）。"""
        cheap = price(cost_cny=10.0, logistics_fee_rub_per_item=116.0)
        pricey = price(cost_cny=200.0, logistics_fee_rub_per_item=116.0)
        cheap_share = cheap["logistics_fee_cny"] / cheap["selling_price_cny"]
        pricey_share = pricey["logistics_fee_cny"] / pricey["selling_price_cny"]
        self.assertGreater(cheap_share, pricey_share)

    def test_unknown_cost_still_rejects_with_fees(self):
        row = price(cost_cny=0, logistics_fee_rub_per_item=116.0)
        self.assertEqual(row["status"], "REJECT")
        self.assertIsNone(row["selling_price_rub"])


class BreakdownTests(unittest.TestCase):
    def test_breakdown_sums_to_base_cost(self):
        row = price(logistics_fee_rub_per_item=116.0, packing_fee_cny=2.0, other_fixed_cost_cny=3.0)
        breakdown = row["breakdown_cny"]
        total = (
            breakdown["purchase"]
            + breakdown["shipping_to_ozon"]
            + breakdown["packing"]
            + breakdown["other_fixed"]
            + breakdown["ozon_logistics_fixed"]
        )
        self.assertAlmostEqual(total, breakdown["total_cost"], places=2)
        self.assertAlmostEqual(breakdown["total_cost"], row["base_cost_cny"], places=2)
        self.assertAlmostEqual(breakdown["purchase"], 20.0, places=2)
        self.assertAlmostEqual(breakdown["packing"], 2.0, places=2)
        self.assertAlmostEqual(breakdown["other_fixed"], 3.0, places=2)

    def test_breakdown_rub_components_consistent(self):
        row = price(logistics_fee_rub_per_item=116.0)
        rub = row["breakdown_rub"]
        self.assertEqual(rub["selling_price"], row["selling_price_rub"])
        self.assertEqual(rub["profit"], row["estimated_profit_rub"])
        self.assertAlmostEqual(
            rub["selling_price"] - rub["platform_fees"] - rub["cost"],
            rub["profit"],
            places=1,  # nice_price 取整带来的分位差
        )

    def test_margin_matches_profit_over_price(self):
        row = price()
        self.assertAlmostEqual(row["margin_rate"], row["estimated_profit_rub"] / row["selling_price_rub"], places=3)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_example_config_is_valid_and_matches_defaults(self):
        example = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "pricing.example.json"
        loaded = json.loads(example.read_text(encoding="utf-8"))
        for key in loaded:
            if key.startswith("_"):
                continue
            self.assertIn(key, DEFAULT_CONFIG, f"示例配置里有默认配置不认识的键：{key}")
        # 示例里的每个数值键都应当能被默认配置覆盖（即键名可用）
        config_path = self.root / "pricing.json"
        config_path.write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
        resolved, warnings = load_pricing_config(config_path)
        self.assertEqual([item for item in warnings if "解析失败" in item], [])
        self.assertEqual(resolved["rub_per_cny"], loaded["rub_per_cny"])
        self.assertEqual(resolved["config_file"], str(config_path))

    def test_example_config_is_tracked_by_git(self):
        """踩过的坑：config/ 被 .gitignore 排除 → git archive 部署时示例文件根本没进包，
        本地测试却因为"文件就在磁盘上"而通过。所以示例文件必须放在被跟踪的目录，并锁一条测试。"""
        import shutil
        import subprocess

        if not shutil.which("git"):
            self.skipTest("本机没有 git")
        root = pathlib.Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "deploy/pricing.example.json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"deploy/pricing.example.json 未被 git 跟踪：{result.stderr}")
        self.assertFalse((root / "config" / "pricing.example.json").exists(), "示例文件不该放在被忽略的 config/ 里")

    def test_unknown_keys_are_ignored(self):
        path = self.root / "pricing.json"
        path.write_text(json.dumps({"commission_rate": 0.2, "没这个键": 1}, ensure_ascii=False), encoding="utf-8")
        resolved, _ = load_pricing_config(path)
        self.assertEqual(resolved["commission_rate"], 0.2)
        self.assertNotIn("没这个键", resolved)

    def test_missing_file_warns_with_actionable_hint(self):
        _, warnings = load_pricing_config(self.root / "nope.json")
        self.assertTrue(any("建议按自己的物流/佣金实际值配置一份" in item for item in warnings))


class QuoteTests(unittest.TestCase):
    def test_quote_overrides_take_effect(self):
        """提高佣金 → 售价被抬高（仍能保住目标利润率，所以不是 REJECT）。"""
        base = quote(cost_cny=20.0, weight_g=500)
        dearer = quote(cost_cny=20.0, weight_g=500, config_overrides={"commission_rate": 0.30})
        self.assertEqual(base["quote"]["status"], "UPLOAD")
        self.assertEqual(dearer["quote"]["status"], "UPLOAD")
        self.assertGreater(dearer["quote"]["selling_price_rub"], base["quote"]["selling_price_rub"])
        self.assertEqual(dearer["config"]["commission_rate"], 0.30)

    def test_quote_rejects_when_fees_leave_no_room(self):
        report = quote(cost_cny=20.0, weight_g=500, config_overrides={"target_margin_rate": 0.9})
        self.assertEqual(report["quote"]["status"], "REJECT")
        self.assertTrue(any("无法定价" in item for item in report["quote"]["errors"]))

    def test_quote_with_volumetric_dimensions(self):
        report = quote(cost_cny=20.0, weight_g=300, length_mm=300, width_mm=200, height_mm=100)
        row = report["quote"]
        # 体积重 = 300*200*100/6000 = 1000 g > 实重 300 g → 计费重 1000 g
        self.assertEqual(row["billable_weight_g"], 1000)
        self.assertEqual(row["actual_weight_g"], 300)


class CliTests(unittest.TestCase):
    def test_quote_cli_prints_breakdown(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = measurements_main(["--quote", "--cost-cny", "20", "--weight-g", "500"])
        self.assertEqual(code, 0, buffer.getvalue())
        text = buffer.getvalue()
        self.assertIn("建议售价", text)
        self.assertIn("成本构成", text)

    def test_quote_cli_json_and_reject_exit_code(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = measurements_main(
                ["--quote", "--cost-cny", "20", "--weight-g", "500", "--set", "target_margin_rate=0.9", "--json"]
            )
        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["quote"]["status"], "REJECT")

    def test_show_config_cli(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = measurements_main(["--show-config"])
        self.assertEqual(code, 0)
        self.assertIn("commission_rate", buffer.getvalue())

    def test_cli_requires_quote_or_show_config(self):
        with self.assertRaises(SystemExit):
            measurements_main([])
        with self.assertRaises(SystemExit):
            measurements_main(["--quote"])


if __name__ == "__main__":
    unittest.main()
