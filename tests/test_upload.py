"""上传链路测试：载荷契约、production 阻断项、干跑不写 task_id、多店幂等与失败隔离。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from pipeline import status as st  # noqa: E402
from pipeline.attributes import build_attribute_fill_input, compile_attributes, evaluate_variant_rules  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.handlers import run_single_step  # noqa: E402
from pipeline.publications import load_publications, store_has_task  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.steps import step_definition  # noqa: E402
from pipeline.upload import (  # noqa: E402
    DryRunUploader,
    SimulatedUploader,
    build_upload_payload,
    payload_problems,
    upload_product,
)

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def capture_payload(*, colors=("красный", "синий"), capacities=("500 мл", "500 мл")) -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/606060606.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001"},
        "skus": [
            {
                "sku_id": f"S{index + 1}",
                "color_ru": colors[index % len(colors)],
                "capacity": capacities[index % len(capacities)],
                "purchase_price_cny": 18.0 + index,
            }
            for index in range(len(colors))
        ],
    }


def category_snapshot(*, with_capacity: bool = True) -> dict:
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
    return {
        "schema_version": "1.0.0",
        "product_id": "P000001",
        "fetched_at": "2026-10-05T12:00:00+08:00",
        "api_endpoint": "/v1/description-category/attribute",
        "category_id": 1001,
        "category_name": "Термосы",
        "type_id": 2001,
        "attributes": attributes,
        "warnings": [],
    }


def add_images(product_dir: pathlib.Path, *, main: int = 2, sku: int = 2, detail: int = 3) -> None:
    for relative, count in (("main-images", main), ("sku-images", sku), ("detail-images", detail)):
        directory = product_dir / "input" / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + f"{relative}{index}".encode())


def artifact_stub(step):
    """把该步声明的产物造出来；目录类产物也会放一个占位文件（空目录不算"已产出"）。"""

    def handler(ctx):
        for relative in step_definition(step).get("outputs", []):
            if relative.endswith((".json", ".jsonl")):
                ctx.write_json(relative, {"stub": True})
            else:
                directory = ctx.path(relative)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "stub.png").write_bytes(IMAGE_BYTES)
        return {"warnings": [], "artifacts": []}

    return handler


class UploadFixture(unittest.TestCase):
    """准备一个"除生图外全部就绪"的商品（含类目、属性、定价、图片公网地址）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        self.provider = FakeProvider()

        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]
        add_images(self.product_dir)
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])

        output = self.product_dir / "output"
        output.mkdir(parents=True, exist_ok=True)
        snapshot = category_snapshot()
        (output / "ozon-category-attributes.json").write_text(json.dumps(snapshot), encoding="utf-8")
        (output / "ozon-category.json").write_text(
            json.dumps(
                {
                    "metadata_source": "ozon_seller_api",
                    "category_id": 1001,
                    "type_id": 2001,
                    "match_status": "api_confirmed",
                }
            ),
            encoding="utf-8",
        )
        (output / "ozon-ecommerce-design.json").write_text("{}", encoding="utf-8")

        # 变体规则 + 属性编译（真实逻辑）
        source = json.loads((self.product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        grouping = evaluate_variant_rules(skus=source["skus"], category_attributes=snapshot["attributes"])
        (output / "platform-grouping-result.json").write_text(json.dumps(grouping), encoding="utf-8")

        fill_input = build_attribute_fill_input(source=source)
        compiled = compile_attributes(
            product_id=self.product_dir.name, category_snapshot=snapshot, fill_input=fill_input
        )
        (output / "ozon-attributes-final.json").write_text(json.dumps(compiled), encoding="utf-8")
        self.attributes = compiled

        (output / "pricing-result.json").write_text(
            json.dumps({"skus": [{"sku_id": "S1", "selling_price_rub": 1290}, {"sku_id": "S2", "selling_price_rub": 1390}]}),
            encoding="utf-8",
        )
        # 尺寸重量：只写"采集/确认过"的值（uploader 拒绝编造）
        (output / "cost-analysis.json").write_text(
            json.dumps(
                {
                    "product_dimensions": {"length_mm": 90, "width_mm": 90, "height_mm": 250, "weight_g": 350},
                    "package_dimensions": {"length_mm": 110, "width_mm": 110, "height_mm": 280, "weight_g": 430},
                }
            ),
            encoding="utf-8",
        )

        # 文案与图片计划（真实 handler）
        run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        run_single_step(self.product_dir, "russian_copy", provider=self.provider)
        run_single_step(self.product_dir, "image_plan", provider=self.provider)

        # 生图（本地占位图）与技术质检（都真跑）
        from models.local_image import LocalPlaceholderGenerator
        from pipeline.context import StepContext
        from pipeline.image_generation import handle_image_generation
        from pipeline.image_qc import handle_image_qc

        handle_image_generation(
            StepContext(
                product_dir=self.product_dir,
                step="image_generation",
                image_generator=LocalPlaceholderGenerator(),
            )
        )
        handle_image_qc(StepContext(product_dir=self.product_dir, step="image_qc"))

        # 对象存储 adapter 的产物：slot → https URL
        plan = json.loads((output / "image-plan.json").read_text(encoding="utf-8"))
        urls = {
            str(item["slot"]): f"https://cdn.example.com/{self.product_dir.name}/{item['slot']}.png"
            for item in (plan["main_images"] + plan["detail_images"])
        }
        (output / "image-public-urls.json").write_text(
            json.dumps({"schema_version": "1.0.0", "urls": urls}), encoding="utf-8"
        )
        self.image_urls = urls

    def tearDown(self):
        self.tmp.cleanup()


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class PayloadTests(UploadFixture):
    def test_payload_is_contract_valid_without_blockers(self):
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertEqual(validate_contract("ozon-upload-payload", payload), [])
        self.assertEqual(payload["production_blockers"], [], payload["production_blockers"])
        self.assertEqual(payload["upload_mode"], "dry-run")
        self.assertFalse(payload["api_writes_performed"])
        self.assertEqual(payload["product_group"]["variant_mapping_status"], "MAPPED")
        self.assertEqual([item["slot"] for item in payload["images"]][:2], ["main-S1", "main-S2"])
        self.assertEqual(len(payload["variants"]), 2)
        self.assertTrue(all(item["price"].endswith(".00") for item in payload["variants"]))
        # 载荷里不允许出现库存字段
        self.assertEqual(payload_problems(payload), [])

    def test_search_text_has_no_inventory_words(self):
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertFalse(
            __import__("re").search(r'"(stock|stocks|inventory|warehouse|warehouses)"\s*:', json.dumps(payload))
        )
        self.assertFalse(payload["api_request_template"]["inventory_fields_included"])
        self.assertFalse(payload["api_request_template"]["inventory_submission_enabled"])

    def test_missing_pricing_is_a_blocker(self):
        (self.product_dir / "output" / "pricing-result.json").unlink()
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(any("卢布售价" in item for item in payload["production_blockers"]))
        self.assertFalse(payload["product_group"]["upload_allowed"])

    def test_missing_image_url_is_a_blocker(self):
        payload = build_upload_payload(
            self.product_dir, shop_name="shop-a", image_urls={"main-S1": "https://cdn.example.com/a.png"}
        )
        self.assertTrue(any("https 公网地址" in item for item in payload["production_blockers"]))
        self.assertFalse(payload["image_upload_gate"]["passed"])

    def test_missing_required_attributes_is_a_blocker(self):
        (self.product_dir / "output" / "ozon-attributes-final.json").write_text(
            json.dumps({"required_summary": {"total": 2, "filled": 1, "missing": 1, "missing_attribute_ids": [10097]}}),
            encoding="utf-8",
        )
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(any("必需属性仍缺" in item for item in payload["production_blockers"]))

    def test_rule_required_variant_mapping_is_a_blocker(self):
        grouping = json.loads(
            (self.product_dir / "output" / "platform-grouping-result.json").read_text(encoding="utf-8")
        )
        grouping["upload_strategy"] = "rule_required"
        grouping["reason"] = "无法映射容量差异"
        (self.product_dir / "output" / "platform-grouping-result.json").write_text(
            json.dumps(grouping), encoding="utf-8"
        )
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertEqual(payload["product_group"]["variant_mapping_status"], "RULE_REQUIRED")
        self.assertTrue(any("人工确认" in item for item in payload["production_blockers"]))

    def test_missing_measurements_is_a_blocker(self):
        """没有任何尺寸重量数据时不允许提交（Ozon 真实导入必需，且我们不编造）。"""
        (self.product_dir / "output" / "cost-analysis.json").unlink()
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        blockers = " ".join(payload["production_blockers"])
        self.assertIn("缺少商品尺寸", blockers)
        self.assertIn("缺少包装重量", blockers)
        self.assertEqual(payload["sku_measurements"], {})

    def test_measurements_are_carried_into_payload(self):
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertEqual(payload["sku_measurements"]["package_dimensions"]["weight_g"], 430)

    def test_production_mode_refuses_blockers(self):
        (self.product_dir / "output" / "pricing-result.json").unlink()
        payload = build_upload_payload(
            self.product_dir, shop_name="shop-a", upload_mode="production"
        )
        problems = payload_problems(payload, upload_mode="production")
        self.assertTrue(any("阻断项" in item for item in problems), problems)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class UploadFlowTests(UploadFixture):
    def test_dry_run_upload_writes_receipts_but_no_task_id(self):
        summary = upload_product(self.product_dir, ["shop-a", "shop-b"], DryRunUploader())
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["api_writes"], 0)
        for store in ("shop-a", "shop-b"):
            self.assertFalse(store_has_task(self.product_dir, store))
            receipt_path = self.product_dir / "output" / "store-runs" / store / "ozon-result.json"
            self.assertTrue(receipt_path.is_file())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(validate_contract("ozon-result", receipt), [])
            self.assertEqual(receipt["status"], "skipped")
            self.assertEqual(receipt["task_id"], "unknown")
            self.assertTrue((self.product_dir / "output" / "store-runs" / store / "payload.json").is_file())
        payload = load_publications(self.product_dir)
        self.assertEqual(payload["stores"]["shop-a"]["status"], "skipped")

    def test_simulated_upload_records_task_and_is_idempotent(self):
        first = upload_product(self.product_dir, ["shop-a"], SimulatedUploader())
        self.assertEqual(first["submitted"], 1)
        self.assertEqual(first["api_writes"], 1)
        self.assertTrue(store_has_task(self.product_dir, "shop-a"))

        second = upload_product(self.product_dir, ["shop-a"], SimulatedUploader())
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["submitted"], 0)
        self.assertIn("task_id", second["stores"]["shop-a"]["reason"])

    def test_one_store_failure_does_not_block_others(self):
        summary = upload_product(
            self.product_dir,
            ["shop-a", "shop-b"],
            SimulatedUploader(fail_stores=["shop-b"]),
        )
        self.assertEqual(summary["submitted"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["stores"]["shop-a"]["status"], "submitted")
        self.assertEqual(summary["stores"]["shop-b"]["status"], "failed")
        publications = load_publications(self.product_dir)
        self.assertEqual(publications["stores"]["shop-a"]["status"], "submitted")
        self.assertEqual(publications["stores"]["shop-b"]["status"], "failed")

    def test_disabled_store_is_skipped(self):
        summary = upload_product(
            self.product_dir, ["shop-a", "shop-b"], SimulatedUploader(), enabled_store_ids=["shop-a"]
        )
        self.assertEqual(summary["stores"]["shop-b"]["status"], "skipped")
        self.assertIn("未启用", summary["stores"]["shop-b"]["reason"])

    def test_pipeline_reaches_ozon_upload_with_uploader(self):
        create_batch(self.products, batches_root=self.batches, target_store_ids=["shop-a"])

        def noop(ctx):
            return {"warnings": [], "artifacts": []}

        handlers = {
            "category_match": noop,
            "measurements": noop,
            "product_positioning": artifact_stub("product_positioning"),
            "image_generation": noop,
            "image_qc": noop,
        }
        report = run_product(
            self.product_dir,
            handlers=handlers,
            provider=self.provider,
            uploader=SimulatedUploader(),
            dry_run=False,
            app_mode="production",
            step_budget=25,
        )
        self.assertIn("ozon_upload", report["completed_steps"])
        saved = st.load_status(self.product_dir)
        self.assertEqual(saved["api_write_count"], 1)
        self.assertTrue((self.product_dir / "output" / "upload-summary.json").is_file())

    def test_dry_run_uploader_is_refused_in_production_mode(self):
        """危险配置保护：干跑 uploader 不允许用在 production 模式。"""
        with self.assertRaises(ValueError) as ctx:
            upload_product(self.product_dir, ["shop-a"], DryRunUploader(), upload_mode="production")
        self.assertIn("不能用于 production", str(ctx.exception))

    def test_pipeline_refuses_dry_run_uploader_in_production(self):
        create_batch(self.products, batches_root=self.batches, target_store_ids=["shop-a"])

        def noop(ctx):
            return {"warnings": [], "artifacts": []}

        handlers = {
            "category_match": noop,
            "measurements": noop,
            "product_positioning": artifact_stub("product_positioning"),
            "image_generation": noop,
            "image_qc": noop,
        }
        report = run_product(
            self.product_dir,
            handlers=handlers,
            provider=self.provider,
            uploader=DryRunUploader(),
            dry_run=False,
            app_mode="production",
            step_budget=25,
        )
        self.assertEqual(report["stop_reason"], "gate_failed")
        self.assertEqual(report["stopped_at"], "ozon_upload")
        # 一次写请求都没有发生
        self.assertEqual(report["api_write_count"], 0)
        self.assertEqual(st.load_status(self.product_dir)["api_write_count"], 0)
        self.assertNotIn("ozon_upload", report["completed_steps"])


if __name__ == "__main__":
    unittest.main()
