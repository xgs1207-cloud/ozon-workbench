"""类目/属性本地 handler 测试：变体规则判定与属性编译（含与上传可行性门禁的联动）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from pipeline.attributes import (  # noqa: E402
    build_attribute_fill_input,
    compile_attributes,
    detect_sku_differences,
    evaluate_variant_rules,
)
from pipeline.batch import create_batch  # noqa: E402
from pipeline.context import PipelineGateError, StepContext  # noqa: E402
from pipeline.runner import handler_upload_feasibility, run_product  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def add_images(product_dir: pathlib.Path, *, main: int = 2, sku: int = 2, detail: int = 3) -> None:
    for relative, count in (("main-images", main), ("sku-images", sku), ("detail-images", detail)):
        directory = product_dir / "input" / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + f"{relative}{index}".encode())


def capture_payload(*, skus: int = 2, colors=("красный", "синий"), capacities=("500 мл", "500 мл")) -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/321321321.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001"},
        "skus": [
            {
                "sku_id": f"S{index + 1}",
                "color_ru": colors[index % len(colors)],
                "capacity": capacities[index % len(capacities)],
                "purchase_price_cny": 18.0 + index,
            }
            for index in range(skus)
        ],
    }


def category_snapshot(*, with_capacity: bool = True, with_certificate: bool = False) -> dict:
    attributes = [
        {
            "attribute_id": 85,
            "attribute_name": "Бренд",
            "required": True,
            "type": "String",
            "dictionary_id": 28732,
            "complex_id": 0,
            "is_collection": False,
            "allowed_values": [{"id": 126745801, "value": "Нет бренда"}],
            "values_truncated": False,
        },
        {
            "attribute_id": 10097,
            "attribute_name": "Название цвета",
            "required": True,
            "type": "String",
            "dictionary_id": None,
            "complex_id": 0,
            "is_collection": False,
            "allowed_values": [],
            "values_truncated": False,
        },
    ]
    if with_capacity:
        attributes.append(
            {
                "attribute_id": 10096,
                "attribute_name": "Объём, мл",
                "required": False,
                "type": "String",
                "dictionary_id": None,
                "complex_id": 0,
                "is_collection": False,
                "allowed_values": [],
                "values_truncated": False,
            }
        )
    if with_certificate:
        attributes.append(
            {
                "attribute_id": 9999,
                "attribute_name": "Сертификат",
                "required": True,
                "type": "String",
                "dictionary_id": None,
                "complex_id": 0,
                "is_collection": False,
                "allowed_values": [],
                "values_truncated": False,
            }
        )
    return {
        "schema_version": "1.0.0",
        "product_id": "P000001",
        "fetched_at": "2026-10-05T12:00:00+08:00",
        "api_endpoint": "/v1/description-category/attribute",
        "category_id": 123456,
        "category_name": "Термосы",
        "type_id": 789,
        "attributes": attributes,
        "warnings": [],
    }


class VariantRuleTests(unittest.TestCase):
    def test_single_sku_is_single_sku_strategy(self):
        result = evaluate_variant_rules(
            skus=[{"sku_id": "S1", "color_ru": "красный"}],
            category_attributes=category_snapshot()["attributes"],
        )
        self.assertEqual(result["upload_strategy"], "single_sku")
        self.assertFalse(result["platform_can_merge"])

    def test_color_difference_maps_to_category_attribute(self):
        result = evaluate_variant_rules(
            skus=[{"sku_id": "S1", "color_ru": "красный"}, {"sku_id": "S2", "color_ru": "синий"}],
            category_attributes=category_snapshot()["attributes"],
        )
        self.assertEqual(result["upload_strategy"], "merged_variants")
        self.assertTrue(result["platform_can_merge"])
        self.assertEqual(result["platform_card_count"], 1)
        self.assertEqual(result["mapped_aspect_attributes"][0]["attribute_id"], 10097)

    def test_difference_without_category_attribute_requires_rule(self):
        result = evaluate_variant_rules(
            skus=[{"sku_id": "S1", "capacity": "500 мл"}, {"sku_id": "S2", "capacity": "1000 мл"}],
            category_attributes=category_snapshot(with_capacity=False)["attributes"],
        )
        self.assertEqual(result["upload_strategy"], "rule_required")
        self.assertFalse(result["platform_can_merge"])
        self.assertIn("无法映射", result["reason"])

    def test_no_difference_does_not_merge(self):
        result = evaluate_variant_rules(
            skus=[{"sku_id": "S1", "color_ru": "красный"}, {"sku_id": "S2", "color_ru": "красный"}],
            category_attributes=category_snapshot()["attributes"],
        )
        self.assertEqual(result["upload_strategy"], "separate_cards")
        self.assertEqual(result["detected_sku_differences"], [])

    def test_detect_differences_from_chinese_colors(self):
        differences = detect_sku_differences(
            [{"sku_id": "S1", "color_zh": "红色"}, {"sku_id": "S2", "color_zh": "蓝色"}]
        )
        self.assertEqual(differences[0]["kind"], "color")
        self.assertEqual(set(differences[0]["values"]), {"красный", "синий"})


class CompileAttributesTests(unittest.TestCase):
    def test_brand_color_capacity_filled_and_missing_reported(self):
        snapshot = category_snapshot(with_certificate=True)
        fill_input = build_attribute_fill_input(
            source={"product_id": "P000001", "skus": capture_payload()["skus"]}
        )
        compiled = compile_attributes(
            product_id="P000001", category_snapshot=snapshot, fill_input=fill_input
        )
        self.assertEqual(compiled["schema_source"], "ozon_seller_api")
        brand = [item for item in compiled["common_attributes"] if item["attribute_id"] == 85]
        self.assertEqual(brand[0]["value"], "Нет бренда")
        self.assertEqual(brand[0]["dictionary_value_id"], 126745801)
        self.assertEqual(set(compiled["attributes_by_sku"]), {"S1", "S2"})
        colors = [item["value"] for item in compiled["attributes_by_sku"]["S1"] if item["attribute_id"] == 10097]
        self.assertEqual(colors, ["красный"])
        summary = compiled["required_summary"]
        self.assertEqual(summary["total"], 3)  # Бренд / Название цвета / Сертификат
        self.assertEqual(summary["filled"], 2)
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["missing_attribute_ids"], [9999])

    def test_multi_sku_color_attribute_goes_to_by_sku(self):
        fill_input = build_attribute_fill_input(source={"skus": capture_payload()["skus"]})
        compiled = compile_attributes(
            product_id="P000001", category_snapshot=category_snapshot(), fill_input=fill_input
        )
        self.assertEqual(compiled["attributes_by_sku"]["S1"][0]["scope"], "sku")
        self.assertTrue(all(item["scope"] in {"common", "sku"} for item in compiled["attributes"]))

    def test_chinese_material_is_flagged_not_used(self):
        fill_input = build_attribute_fill_input(
            source={
                "skus": capture_payload()["skus"],
                "extra": {"attributes": [{"name": "材质", "value": "304 不锈钢"}]},
            }
        )
        self.assertTrue(fill_input["product_level"]["materials_need_translation"])
        self.assertEqual(fill_input["product_level"]["materials"][0].startswith("NEEDS_TRANSLATION:"), True)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class CatalogHandlerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]
        add_images(self.product_dir)
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        (self.product_dir / "output").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_category(self, snapshot: dict) -> None:
        (self.product_dir / "output" / "ozon-category-attributes.json").write_text(
            json.dumps(snapshot), encoding="utf-8"
        )
        (self.product_dir / "output" / "ozon-category.json").write_text(
            json.dumps(
                {
                    "metadata_source": "ozon_seller_api",
                    "category_id": 123456,
                    "type_id": 789,
                    "match_status": "api_confirmed",
                }
            ),
            encoding="utf-8",
        )

    def _stub(self, step):
        from pipeline.steps import step_definition

        def handler(ctx):
            for relative in step_definition(step).get("outputs", []):
                if relative.endswith((".json", ".jsonl")):
                    ctx.write_json(relative, {"stub": True})
                else:
                    ctx.path(relative).mkdir(parents=True, exist_ok=True)
            return {"warnings": [], "artifacts": []}

        return handler

    def _noop(self):
        """类目元数据由 _write_category 预先写好，这里只让步骤完成、不覆盖文件。"""

        def handler(ctx):
            return {"warnings": [], "artifacts": []}

        return handler

    def _run_phase_a_and_b(self, snapshot: dict):
        from models.fake import FakeProvider

        self._write_category(snapshot)
        create_batch(self.products, batches_root=self.batches, target_store_ids=["shop-a"])
        handlers = {
            "category_match": self._noop(),
            "measurements": self._stub("measurements"),
        }
        return run_product(self.product_dir, handlers=handlers, provider=FakeProvider(), step_budget=20)

    def test_variant_rules_and_field_completion_reach_upload(self):
        report = self._run_phase_a_and_b(category_snapshot())
        self.assertIn("variant_rules", report["completed_steps"])
        self.assertIn("field_completion", report["completed_steps"])
        self.assertIn("upload_feasibility", report["completed_steps"])
        self.assertIn("image_plan", report["completed_steps"])
        # 生图还没实现，必须如实停下
        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertEqual(report["stopped_at"], "image_generation")

        grouping = json.loads(
            (self.product_dir / "output" / "platform-grouping-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_contract("platform-grouping-result", grouping), [])
        self.assertEqual(grouping["upload_strategy"], "merged_variants")

        attributes = json.loads(
            (self.product_dir / "output" / "ozon-attributes-final.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_contract("ozon-attributes-final", attributes), [])
        self.assertEqual(attributes["required_summary"]["missing"], 0)

    def test_missing_required_attribute_is_reported_and_blocks_final_gate(self):
        self._run_phase_a_and_b(category_snapshot(with_certificate=True))
        attributes = json.loads(
            (self.product_dir / "output" / "ozon-attributes-final.json").read_text(encoding="utf-8")
        )
        # 编译器如实报告缺 1 个必需属性，不编造值
        self.assertEqual(attributes["required_summary"]["missing"], 1)
        self.assertEqual(attributes["required_summary"]["missing_attribute_ids"], [9999])

        # 提交前的硬门禁：此时最终属性已存在，直接跑一次 upload_feasibility 必须失败
        context = StepContext(self.product_dir, "upload_feasibility", True, "development", {}, None)
        with self.assertRaises(PipelineGateError) as ctx:
            handler_upload_feasibility(context)
        self.assertIn("required_attributes", ctx.exception.details.get("blocking_checks", []))

    def test_upload_feasibility_warns_without_attribute_file(self):
        """阶段 A 早期预检：两个属性文件都没有时应是 WARN（不是 FAIL），否则流程走不到属性编译。"""
        (self.product_dir / "output" / "ozon-category.json").write_text(
            json.dumps(
                {
                    "metadata_source": "ozon_seller_api",
                    "category_id": 123456,
                    "type_id": 789,
                    "match_status": "api_confirmed",
                }
            ),
            encoding="utf-8",
        )
        context = StepContext(self.product_dir, "upload_feasibility", True, "development", {}, None)
        result = handler_upload_feasibility(context)
        payload = json.loads(
            (self.product_dir / "output" / "upload-feasibility.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["checks"]["required_attributes"]["status"], "WARN")
        self.assertTrue(any("阶段 A" in item for item in result["warnings"]))

    def test_field_completion_rejects_non_api_category(self):
        from pipeline.catalog import handle_field_completion

        (self.product_dir / "output" / "ozon-category-attributes.json").write_text(
            json.dumps(category_snapshot()), encoding="utf-8"
        )
        (self.product_dir / "output" / "ozon-category.json").write_text(
            json.dumps({"metadata_source": "local_translation", "match_status": "api_confirmed"}),
            encoding="utf-8",
        )
        context = StepContext(self.product_dir, "field_completion", True, "development", {}, None)
        with self.assertRaises(PipelineGateError) as ctx:
            handle_field_completion(context)
        self.assertIn("Ozon Seller API", str(ctx.exception))

    def test_field_completion_rejects_snapshot_without_category_id(self):
        from pipeline.catalog import handle_field_completion

        broken = category_snapshot()
        broken.pop("category_id")
        (self.product_dir / "output" / "ozon-category-attributes.json").write_text(
            json.dumps(broken), encoding="utf-8"
        )
        (self.product_dir / "output" / "ozon-category.json").write_text(
            json.dumps({"metadata_source": "ozon_seller_api", "match_status": "api_confirmed"}),
            encoding="utf-8",
        )
        context = StepContext(self.product_dir, "field_completion", True, "development", {}, None)
        with self.assertRaises(PipelineGateError) as ctx:
            handle_field_completion(context)
        self.assertIn("category_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
