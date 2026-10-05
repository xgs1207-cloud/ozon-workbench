"""唯一字典值强制填 + 上传可行性字段上报 的测试（都来自真实 API 的现场）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from pipeline.attributes import compile_attributes, forced_dictionary_value  # noqa: E402
from pipeline.dossier import collect_dossier, render_dossier  # noqa: E402

#: 真实类目快照片段（床单 17028731/92612）：类型字典只有「床单」；品牌字典被截断
REAL_ATTRIBUTE_SHAPES = [
    {"attribute_id": 8229, "attribute_name": "类型", "required": True, "type": "String",
     "dictionary_id": 1960, "allowed_values": [{"id": 92612, "value": "床单"}], "values_truncated": False},
    {"attribute_id": 85, "attribute_name": "品牌", "required": True, "type": "String",
     "dictionary_id": 28732849, "allowed_values": [{"id": 1, "value": "Prima"}], "values_truncated": True},
    {"attribute_id": 9048, "attribute_name": "型号名称（针对合并为一张商品卡片）", "required": True,
     "type": "String", "dictionary_id": None, "allowed_values": [], "values_truncated": False},
    {"attribute_id": 10097, "attribute_name": "颜色名称", "required": False, "type": "String",
     "dictionary_id": 1000, "allowed_values": [{"id": 61576, "value": "белый"}], "values_truncated": False},
]


class ForcedValueTests(unittest.TestCase):
    def test_single_value_is_forced(self):
        forced = forced_dictionary_value({"allowed_values": [{"id": 92612, "value": "床单"}]})
        self.assertEqual(forced["value"], "床单")
        self.assertEqual(forced["dictionary_value_id"], 92612)

    def test_truncated_single_value_is_refused(self):
        """快照只存了前 N 个值时，看起来"只剩一个"不代表真的只有一个。"""
        self.assertIsNone(
            forced_dictionary_value({"allowed_values": [{"id": 1, "value": "Prima"}], "values_truncated": True})
        )

    def test_multiple_or_empty_values_are_refused(self):
        self.assertIsNone(forced_dictionary_value({"allowed_values": [{"value": "a"}, {"value": "b"}]}))
        self.assertIsNone(forced_dictionary_value({"allowed_values": []}))
        self.assertIsNone(forced_dictionary_value({"allowed_values": [{"id": 1, "value": "  "}]}))


class CompileTests(unittest.TestCase):
    def _compile(self, skus=None, lookups=None, attributes=None, human_attributes=None):
        return compile_attributes(
            product_id="P000004",
            category_snapshot={"attributes": attributes or REAL_ATTRIBUTE_SHAPES},
            fill_input={"skus": skus or [{"sku_id": "S1", "color_ru": "белый"}]},
            dictionary_lookups=lookups,
            human_attributes=human_attributes,
        )

    def test_human_confirmed_attribute_fills_the_last_required_one(self):
        """9048 型号名称：来源与字典都没有，只能人来定 —— 人工确认入口要能把它补上。"""
        compiled = self._compile(human_attributes={"9048": "Bedding-200x200"})
        common = {item["attribute_id"]: item for item in compiled["common_attributes"]}
        self.assertIn(9048, common)
        self.assertEqual(common[9048]["value"], "Bedding-200x200")
        self.assertEqual(common[9048]["source"], "human_confirmation")
        self.assertEqual(common[9048]["confidence"], 1.0)
        # 这个用例没给品牌字典查值 → 仍缺 85；9048 已由人工确认补上
        self.assertEqual(compiled["required_summary"]["missing"], 1)
        self.assertEqual(compiled["required_summary"]["missing_attribute_ids"], [85])
        self.assertEqual(compiled["required_summary"]["filled"], 2)

    def test_human_confirmation_never_overrides_machine_value(self):
        """人工确认只补空缺，不覆盖类目字典/代码推导出来的值。"""
        compiled = self._compile(human_attributes={"8229": "人工乱填的"})
        common = {item["attribute_id"]: item for item in compiled["common_attributes"]}
        self.assertEqual(common[8229]["value"], "床单")
        self.assertEqual(common[8229]["mapping_method"], "single_dictionary_value_forced")

    def test_invalid_human_confirmation_keys_are_ignored(self):
        compiled = self._compile(human_attributes={"品牌": "x", "9048": "  "})
        common = {item["attribute_id"]: item for item in compiled["common_attributes"]}
        self.assertNotIn(9048, common)

    def test_chinese_brand_name_is_recognised_with_lookup(self):
        """真实踩到：属性名是中文「品牌」，而品牌模式表原先只有俄文 → 分支进不去。
        加上中文别名 + 字典查值结果后，品牌必须被填上。"""
        compiled = self._compile(
            lookups={
                "unbranded": {
                    "attribute_id": 85,
                    "attribute_name": "品牌",
                    "query": "Нет бренда",
                    "value": "Нет бренда",
                    "dictionary_value_id": 126745801,
                    "api_endpoint": "/v1/description-category/attribute/values/search",
                }
            }
        )
        common = {item["attribute_id"]: item for item in compiled["common_attributes"]}
        self.assertIn(85, common)
        self.assertEqual(common[85]["value"], "Нет бренда")
        self.assertEqual(common[85]["dictionary_value_id"], 126745801)
        self.assertEqual(common[85]["mapping_method"], "project_unbranded_rule_searched")
        # 类型（唯一字典值）+ 品牌（字典查值）= 2/3，只剩型号名称
        summary = compiled["required_summary"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["filled"], 2)
        self.assertEqual(summary["missing_attribute_ids"], [9048])

    def test_chinese_brand_without_lookup_still_reports_missing(self):
        compiled = self._compile()
        common = {item["attribute_id"]: item for item in compiled["common_attributes"]}
        self.assertNotIn(85, common)
        self.assertIn(85, compiled["required_summary"]["missing_attribute_ids"])
        self.assertTrue(any("品牌" in item for item in compiled["warnings"]), compiled["warnings"])

    def test_chinese_color_attribute_fills_per_sku(self):
        compiled = self._compile(
            attributes=[
                {"attribute_id": 10097, "attribute_name": "颜色名称", "required": False, "type": "String",
                 "dictionary_id": 1000, "allowed_values": [{"id": 61576, "value": "белый"}], "values_truncated": False},
            ],
            skus=[{"sku_id": "S1", "color_ru": "белый"}],
        )
        variants = compiled.get("attributes_by_sku") or {}
        self.assertIn("белый", str(variants))

    def test_forced_type_is_filled_truncated_brand_is_not(self):
        compiled = self._compile()
        common = {item["attribute_id"]: item for item in compiled["common_attributes"]}
        self.assertIn(8229, common)
        self.assertEqual(common[8229]["value"], "床单")
        self.assertEqual(common[8229]["mapping_method"], "single_dictionary_value_forced")
        # 品牌字典被截断 → 不填，仍计入缺失
        self.assertNotIn(85, common)
        self.assertIn(85, compiled["required_summary"]["missing_attribute_ids"])
        summary = compiled["required_summary"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["missing"], 2)  # 类型已强制填上，剩品牌+型号名称
        self.assertEqual(summary["filled"], 1)

    def test_forced_value_recorded_as_warning(self):
        compiled = self._compile()
        self.assertTrue(any("字典只有一个合法值" in item for item in compiled["warnings"]), compiled["warnings"])

    def test_missing_required_still_reported_when_no_dictionary(self):
        compiled = compile_attributes(
            product_id="P000004",
            category_snapshot={"attributes": [REAL_ATTRIBUTE_SHAPES[2]]},
            fill_input={"skus": [{"sku_id": "S1"}]},
        )
        self.assertEqual(compiled["required_summary"]["missing_attribute_ids"], [9048])


class DossierFeasibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        summary = ingest_capture(
            self.root / "products",
            {
                "source_url": "https://detail.1688.com/offer/929292929.html",
                "title_zh": "纯棉床单",
                "category": {"category_id": "17028731", "type_id": "92612"},
                "skus": [{"sku_id": "S1", "color_ru": "белый", "purchase_price_cny": 42.0}],
            },
        )
        self.product_dir = self.root / "products" / summary["product_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _write_feasibility(self, payload: dict) -> None:
        (self.product_dir / "output").mkdir(parents=True, exist_ok=True)
        (self.product_dir / "output" / "upload-feasibility.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_fail_status_and_blocking_checks_are_rendered(self):
        self._write_feasibility(
            {
                "status": "FAIL",
                "blocking_checks": ["image_urls", "required_attributes"],
                "warnings": ["images: 主图不足"],
                "checks": {},
            }
        )
        dossier = collect_dossier(self.product_dir)
        self.assertEqual(dossier["feasibility"]["status"], "FAIL")
        self.assertEqual(dossier["feasibility"]["blocking_checks"], ["image_urls", "required_attributes"])
        text = render_dossier(dossier)
        self.assertIn("status = FAIL", text)
        self.assertIn("未过的检查：image_urls、required_attributes", text)
        self.assertNotIn("✅ 没有阻断项", text)

    def test_pass_status_renders_ok(self):
        self._write_feasibility({"status": "PASS", "blocking_checks": [], "warnings": [], "checks": {}})
        text = render_dossier(collect_dossier(self.product_dir))
        self.assertIn("status = PASS", text)
        self.assertIn("✅ 没有阻断项", text)

    def test_missing_file_says_unknown(self):
        text = render_dossier(collect_dossier(self.product_dir))
        self.assertIn("无法判断", text)


if __name__ == "__main__":
    unittest.main()
