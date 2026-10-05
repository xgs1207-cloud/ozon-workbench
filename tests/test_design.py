"""设计步骤测试：定位、设计文档契约、投影语义、禁止项与中文检查。"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from models import ModelError  # noqa: E402
from models.design import build_design_document, project_copy_from_design  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.image_plan import build_image_plan  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.context import PipelineGateError, StepContext  # noqa: E402
from pipeline.handlers import handle_ecommerce_design, handle_product_positioning, run_single_step  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
CJK = re.compile(r"[\u4e00-\u9fff]")


def capture_payload() -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/676767676.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
        "skus": [
            {"sku_id": "S1", "color_zh": "红色", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
            {"sku_id": "S2", "color_zh": "蓝色", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
        ],
    }


def add_images(product_dir: pathlib.Path) -> None:
    for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
        directory = product_dir / "input" / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + f"{relative}{index}".encode())


class DesignFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]
        add_images(self.product_dir)
        self.provider = FakeProvider()
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        self.write_confirmed_measurements()
        (self.product_dir / "input" / "workbench-sku-overrides.json").write_text(
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
                }
            ),
            encoding="utf-8",
        )
        run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        run_single_step(self.product_dir, "russian_copy", provider=self.provider)
        run_single_step(self.product_dir, "product_positioning", provider=self.provider)

    def tearDown(self):
        self.tmp.cleanup()

    def write_confirmed_measurements(self) -> None:
        return None

    def read(self, relative: str):
        return json.loads((self.product_dir / relative).read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class PositioningTests(DesignFixture):
    def test_positioning_matches_contract_and_stays_honest(self):
        payload = self.read("output/product-positioning.json")
        self.assertEqual(validate_contract("product-positioning", payload), [])
        # 没有证据的字段写 null 并进 unknowns，而不是编一个买家画像
        self.assertIsNone(payload["target_customer"])
        self.assertIn("target_customer", payload["unknowns"])
        self.assertEqual(payload["processing"]["status"], "completed")
        self.assertEqual(payload["processing"]["step"], "product_positioning")
        # 有证据的字段必须带 source_refs
        for item in payload["positioning_evidence"]:
            self.assertTrue(item["source_refs"])

    def test_price_position_follows_pricing_when_available(self):
        from pipeline.measurements import handle_measurements

        handle_measurements(StepContext(product_dir=self.product_dir, step="measurements"))
        run_single_step(self.product_dir, "product_positioning", provider=self.provider)
        payload = self.read("output/product-positioning.json")
        self.assertIn(payload["recommended_price_position"], {"budget", "mass_market", "mid_range", "premium"})
        self.assertTrue(any(item["field"] == "recommended_price_position" for item in payload["positioning_evidence"]))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class DesignDocumentTests(DesignFixture):
    def build(self) -> dict:
        return handle_ecommerce_design(
            StepContext(product_dir=self.product_dir, step="ecommerce_design", provider=self.provider)
        ) and self.read("output/ozon-ecommerce-design.json")

    def test_design_matches_contract(self):
        self.build()
        design = self.read("output/ozon-ecommerce-design.json")
        self.assertEqual(validate_contract("ozon-ecommerce-design", design), [])
        self.assertEqual(len(design["main_images"]), 2)  # 与 SKU 数一致
        self.assertEqual(len(design["detail_images"]), 8)
        self.assertEqual(len(design["decision_trace"]["steps"]), 7)
        self.assertEqual(design["decision_trace"]["compliance_status"], "PASS")
        self.assertEqual(design["decision_trace"]["violations"], [])
        self.assertEqual(design["processing"]["step"], "ecommerce_design")

    def test_image_entries_are_complete_and_typed(self):
        self.build()
        design = self.read("output/ozon-ecommerce-design.json")
        for item in design["main_images"] + design["detail_images"]:
            self.assertGreaterEqual(len(item["prompt"]), 120)
            self.assertTrue(item["russian_text"])
            self.assertGreaterEqual(len(item["overlay_modules"]), 2)
            self.assertGreaterEqual(len(item["must_preserve"]), 3)
            self.assertIn(item["operation"], {"edit_real_image", "compose_from_real_images", "generate_from_reference"})
            self.assertTrue(item["source_references"])
            self.assertTrue(item["art_direction"].get("composition"))

    def test_no_chinese_in_buyer_visible_fields(self):
        self.build()
        design = self.read("output/ozon-ecommerce-design.json")
        listing = design["listing"]
        for value in (listing["seo_title_ru"], listing["short_title_ru"], listing["description_ru"]):
            self.assertIsNone(CJK.search(value), value)
        for item in design["main_images"] + design["detail_images"]:
            for text in item["russian_text"]:
                self.assertIsNone(CJK.search(text), text)

    def test_contract_quirk_is_disclosed_not_hidden(self):
        self.build()
        design = self.read("output/ozon-ecommerce-design.json")
        self.assertEqual(design["processing"]["model_mode"], "connected_codex")  # 契约常量
        self.assertTrue(any("fake" in item for item in design["processing"]["validation_warnings"]))
        provenance = self.read("output/design-provenance.json")
        self.assertEqual(provenance["generated_by"], "fake")
        self.assertIn("契约常量", provenance["contract_note"])

    def test_missing_references_block_design(self):
        for path in (self.product_dir / "input" / "main-images").glob("*.png"):
            path.unlink()
        for path in (self.product_dir / "input" / "sku-images").glob("*.png"):
            path.unlink()
        for path in (self.product_dir / "input" / "detail-images").glob("*.png"):
            path.unlink()
        with self.assertRaises(PipelineGateError) as ctx:
            handle_ecommerce_design(
                StepContext(product_dir=self.product_dir, step="ecommerce_design", provider=self.provider)
            )
        self.assertIn("参考图", str(ctx.exception))

    def test_design_requires_selected_keywords_when_copy_absent(self):
        (self.product_dir / "output" / "copy-ru.json").unlink()
        (self.product_dir / "input" / "selected-keywords.json").unlink()
        with self.assertRaises(PipelineGateError) as ctx:
            handle_ecommerce_design(
                StepContext(product_dir=self.product_dir, step="ecommerce_design", provider=self.provider)
            )
        self.assertIn("选词", str(ctx.exception))

    def test_design_materializes_image_plan_once(self):
        self.build()
        self.assertTrue((self.product_dir / "output" / "image-plan.json").is_file())
        # 之后 image_plan 步骤只是校验物化产物，不重复调模型
        result = run_single_step(self.product_dir, "image_plan", provider=self.provider)
        self.assertTrue(any("物化" in item for item in result["warnings"]))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class CopyProjectionTests(DesignFixture):
    def test_russian_copy_projects_from_design_without_calling_model(self):
        class NoModelProvider(FakeProvider):
            name = "no-model"

            def write_copy_ru(self, request):
                raise ModelError("这个 provider 不允许生成文案")

        handle_ecommerce_design(
            StepContext(product_dir=self.product_dir, step="ecommerce_design", provider=self.provider)
        )
        (self.product_dir / "output" / "copy-ru.json").unlink()
        for name in ("title-ru", "description-ru", "keywords-ru"):
            path = self.product_dir / "output" / f"{name}.json"
            if path.is_file():
                path.unlink()

        result = run_single_step(self.product_dir, "russian_copy", provider=NoModelProvider())
        self.assertTrue(result["projected"])
        for contract_name, filename in (
            ("title-ru", "output/title-ru.json"),
            ("description-ru", "output/description-ru.json"),
            ("keywords-ru", "output/keywords-ru.json"),
        ):
            document = self.read(filename)
            self.assertEqual(validate_contract(contract_name, document), [], contract_name)

    def test_projection_keeps_title_and_hashtags(self):
        handle_ecommerce_design(
            StepContext(product_dir=self.product_dir, step="ecommerce_design", provider=self.provider)
        )
        design = self.read("output/ozon-ecommerce-design.json")
        bundle = project_copy_from_design(
            design=design, product_id=self.product_dir.name, source_refs=["output/ozon-ecommerce-design.json"]
        )
        self.assertEqual(bundle["copy_bundle"]["title_ru"], design["listing"]["seo_title_ru"])
        self.assertEqual(bundle["copy_bundle"]["hashtags"], design["listing"]["hashtags"])
        self.assertEqual(bundle["title_ru"]["title_ru"], design["listing"]["seo_title_ru"])


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class DesignChainTests(DesignFixture):
    def test_two_step_design_chain_via_runner(self):
        create_batch(self.products, batches_root=self.root / "batches", target_store_ids=["shop-a"])
        from pipeline.ozon_http import FixtureTransport, OzonClient
        from pipeline.runner import run_product

        fixtures = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
        report = run_product(
            self.product_dir,
            until="russian_copy",
            provider=self.provider,
            ozon_client=OzonClient(FixtureTransport(directory=fixtures)),
            step_budget=12,
        )
        self.assertIn("product_positioning", report["completed_steps"])
        self.assertIn("ecommerce_design", report["completed_steps"])
        self.assertEqual(report["stop_reason"], "until_reached")


if __name__ == "__main__":
    unittest.main()
