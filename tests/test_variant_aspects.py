"""变体属性（is_aspect）测试：中文属性名 + Ozon 权威 is_aspect → 颜色差异能正确合并。

背景（真实踩到）：Ozon 属性按 ZH_HANS 拉取时名字是中文（"颜色名称"/"商品颜色"），
而变体模式表原先只有俄文 → 匹配不到 → 两个颜色被拆成两张商品卡。
修法：category_match 旁挂 `output/ozon-aspect-attributes.json`（Ozon 用 is_aspect 亲自标记的），
`evaluate_variant_rules` 优先用它，并在没有它时退回老的按名字匹配。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from pipeline.attributes import aspect_kind_for, evaluate_variant_rules  # noqa: E402
from pipeline.category import category_handlers  # noqa: E402
from pipeline.context import StepContext  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKUS = [
    {"sku_id": "S1", "color_ru": "белый", "capacity": "200х200 см"},
    {"sku_id": "S2", "color_ru": "серый", "capacity": "200х200 см"},
]
CHINESE_ATTRIBUTES = [
    {"attribute_id": 8229, "attribute_name": "类型", "dictionary_id": None, "allowed_values": []},
    {"attribute_id": 10096, "attribute_name": "商品颜色", "dictionary_id": 1000, "allowed_values": [{"value": "белый"}]},
    {"attribute_id": 10097, "attribute_name": "颜色名称", "dictionary_id": 1000, "allowed_values": [{"value": "белый"}]},
]


class AspectKindTests(unittest.TestCase):
    def test_known_ids_win(self):
        self.assertEqual(aspect_kind_for({"attribute_id": 10097, "attribute_name": "随便什么名"}), "color")
        self.assertEqual(aspect_kind_for({"attribute_id": 10096, "attribute_name": "x"}), "color")
        self.assertEqual(aspect_kind_for({"attribute_id": 6771}), "size_or_measurement")

    def test_chinese_and_russian_names(self):
        self.assertEqual(aspect_kind_for({"attribute_name": "颜色名称"}), "color")
        self.assertEqual(aspect_kind_for({"attribute_name": "Название цвета"}), "color")
        self.assertEqual(aspect_kind_for({"attribute_name": "纸张尺寸"}), "size_or_measurement")

    def test_unknown_attribute(self):
        self.assertIsNone(aspect_kind_for({"attribute_id": 8229, "attribute_name": "类型"}))
        self.assertIsNone(aspect_kind_for({}))


class VariantRuleTests(unittest.TestCase):
    def test_chinese_names_alone_now_map(self):
        """加了中文模式后，即使没有旁挂文件，中文属性名也能映射（原 bug 的直接修复）。"""
        result = evaluate_variant_rules(skus=SKUS, category_attributes=CHINESE_ATTRIBUTES)
        self.assertTrue(result["platform_can_merge"])
        self.assertEqual(result["upload_strategy"], "merged_variants")
        self.assertTrue(all(item["kind"] == "color" for item in result["allowed_aspect_attributes"]))
        self.assertIn("来源：name_hint", result["reason"])

    def test_unrecognisable_attribute_cannot_merge(self):
        """名字认不出、又没有 is_aspect 信息时，宁可不合并（安全默认）。"""
        attributes = [{"attribute_id": 999999, "attribute_name": "某种变体维度"}]
        result = evaluate_variant_rules(skus=SKUS, category_attributes=attributes)
        self.assertFalse(result["platform_can_merge"])
        self.assertEqual(result["upload_strategy"], "rule_required")
        self.assertEqual(result["allowed_aspect_attributes"], [])

    def test_aspect_info_enables_merge(self):
        aspect_attributes = [
            {"attribute_id": 10096, "attribute_name": "商品颜色", "dictionary_id": 1000},
            {"attribute_id": 10097, "attribute_name": "颜色名称", "dictionary_id": 1000},
        ]
        result = evaluate_variant_rules(
            skus=SKUS, category_attributes=CHINESE_ATTRIBUTES, aspect_attributes=aspect_attributes
        )
        self.assertTrue(result["platform_can_merge"])
        self.assertEqual(result["upload_strategy"], "merged_variants")
        self.assertEqual(result["platform_card_count"], 1)
        self.assertEqual(len(result["mapped_aspect_attributes"]), 1)
        self.assertEqual(result["mapped_aspect_attributes"][0]["kind"], "color")
        self.assertIn(result["mapped_aspect_attributes"][0]["attribute_id"], (10096, 10097))
        self.assertIn("ozon_is_aspect", result["reason"])

    def test_unknown_aspect_is_listed_but_not_used(self):
        aspect_attributes = [{"attribute_id": 999999, "attribute_name": "某种变体维度"}]
        result = evaluate_variant_rules(
            skus=SKUS, category_attributes=CHINESE_ATTRIBUTES, aspect_attributes=aspect_attributes
        )
        self.assertEqual([item["kind"] for item in result["allowed_aspect_attributes"]], ["unknown_aspect"])
        self.assertFalse(result["platform_can_merge"])

    def test_russian_names_still_work_without_aspect_info(self):
        """老行为不能坏：俄文名字 + 无旁挂文件时仍能映射并合并。"""
        attributes = [
            {"attribute_id": 10097, "attribute_name": "Название цвета", "dictionary_id": 1000},
        ]
        result = evaluate_variant_rules(skus=SKUS, category_attributes=attributes)
        self.assertTrue(result["platform_can_merge"])
        self.assertEqual(result["mapped_aspect_attributes"][0]["kind"], "color")

    def test_result_matches_upstream_contract(self):
        """不能多塞字段：上游 platform-grouping-result 契约是 additionalProperties=false。"""
        from contracts import validate_contract

        result = evaluate_variant_rules(
            skus=SKUS,
            category_attributes=CHINESE_ATTRIBUTES,
            aspect_attributes=[{"attribute_id": 10097, "attribute_name": "颜色名称"}],
        )
        self.assertEqual(validate_contract("platform-grouping-result", result), [])


class StubClient:
    """最小 Ozon 客户端桩：只实现 category_match 用到的两个方法。"""

    def __init__(self, tree: dict, attributes: dict) -> None:
        self._tree = tree
        self._attributes = attributes

    def fetch_category_tree(self, *, language: str = "ZH_HANS") -> dict:
        return self._tree

    def fetch_category_attributes(self, *, category_id: int, type_id: int, language: str = "ZH_HANS") -> dict:
        return self._attributes

    def fetch_attribute_values(
        self, *, attribute_id: int, category_id: int, type_id: int, limit: int = 1000, language: str = "ZH_HANS"
    ) -> dict:
        return {"result": [], "has_next": False}


class CategoryMatchSideFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/919191919.html",
                "title_zh": "纯棉床单",
                "category": {"category_id": "17028731", "type_id": "92612"},
                "skus": [{"sku_id": "S1", "color_ru": "белый", "purchase_price_cny": 42.0}],
            },
        )
        self.product_dir = self.products / summary["product_id"]
        # 用录制夹具，并给其中一个属性补上真实接口会返回的 is_aspect
        self.fixtures = self.root / "fixtures"
        shutil.copytree(ROOT / "contracts" / "fixtures", self.fixtures)
        attributes_path = self.fixtures / "ozon-category-attributes.json"
        payload = json.loads(attributes_path.read_text(encoding="utf-8"))
        result = payload.get("result") or []
        if result:
            result[0]["is_aspect"] = True
        attributes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.payload = payload

    def tearDown(self):
        self.tmp.cleanup()

    def test_side_file_written_with_aspect_ids(self):
        tree = json.loads((ROOT / "contracts" / "fixtures" / "ozon-category-tree.json").read_text(encoding="utf-8"))
        client = StubClient(tree, self.payload)
        handler = category_handlers(client)["category_match"]
        context = StepContext(
            product_dir=self.product_dir,
            step="category_match",
            dry_run=True,
            app_mode="development",
            status={},
            provider=None,
            uploader=None,
            image_generator=None,
            ozon_client=client,
        )
        handler(context)

        aspect_path = self.product_dir / "output" / "ozon-aspect-attributes.json"
        self.assertTrue(aspect_path.is_file())
        payload = json.loads(aspect_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "ozon_seller_api")
        self.assertEqual(payload["api_endpoint"], "/v1/description-category/attribute")
        self.assertEqual(len(payload["aspect_attributes"]), 1)
        self.assertEqual(payload["aspect_attribute_ids"], [payload["aspect_attributes"][0]["attribute_id"]])
        # 上游快照仍然合规（没往里面塞 is_aspect）
        snapshot = json.loads(
            (self.product_dir / "output" / "ozon-category-attributes.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("is_aspect", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
