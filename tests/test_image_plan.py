"""图片规划测试：契约合规、N+8 结构、参考图边界、提示词建议、门禁。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from models import ImagePlanRequest, ImageRequest, ModelError  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.image_plan import build_image_plan, render_plan_brief  # noqa: E402
from pipeline import status as st  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.context import PipelineGateError  # noqa: E402
from pipeline.handlers import run_single_step  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.steps import step_definition  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def capture_payload(*, skus: int = 2) -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/555000111.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
        "skus": [
            {
                "sku_id": f"S{index + 1}",
                "color_ru": ["красный", "синий", "зеленый"][index % 3],
                "capacity": "500 мл",
                "purchase_price_cny": 18.0 + index,
            }
            for index in range(skus)
        ],
    }


def add_images(product_dir: pathlib.Path, *, main: int = 2, sku: int = 2, detail: int = 3) -> None:
    for relative, count in (("main-images", main), ("sku-images", sku), ("detail-images", detail)):
        directory = product_dir / "input" / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + f"{relative}{index}".encode())


class ImagePlanContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]
        add_images(self.product_dir)
        self.provider = FakeProvider()

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, copy_bundle=None, **kwargs):
        source = json.loads((self.product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        return build_image_plan(
            product_dir=self.product_dir,
            source=source,
            source_refs=["input/source.json"],
            copy_bundle=(
                copy_bundle
                if copy_bundle is not None
                else {"core_keyword": "термос 500 мл", "title_ru": "Термос 500 мл", "hashtags": ["#термос"]}
            ),
            analysis={"recommendation": {"decision": "continue"}},
            **kwargs,
        )

    @unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
    def test_plan_passes_contract(self):
        plan = self._plan()
        self.assertEqual(validate_contract("image-plan", plan), [])

    def test_plan_structure_is_n_plus_8(self):
        plan = self._plan()
        self.assertEqual(len(plan["main_images"]), 2)
        self.assertEqual(len(plan["detail_images"]), 8)
        self.assertEqual(len(plan["main_images"]) + len(plan["detail_images"]), 10)
        self.assertEqual(plan["variant_image_strategy"]["variant_main_count"], 2)
        self.assertEqual(plan["variant_image_strategy"]["shared_detail_count"], 8)
        self.assertEqual(plan["generator_contract"]["exact_shared_detail_count"], 8)
        self.assertEqual(plan["generator_contract"]["aspect_ratio"], "3:4")
        self.assertFalse(plan["generator_contract"]["raw_1688_image_direct_upload_forbidden"] is False)

    def test_main_images_follow_selected_sku_order(self):
        plan = self._plan()
        slots = [item["slot"] for item in plan["main_images"]]
        self.assertEqual(slots, ["main-S1", "main-S2"])
        self.assertTrue(all(item["shared_across_variants"] is False for item in plan["main_images"]))
        self.assertTrue(all(item["shared_across_variants"] is True for item in plan["detail_images"]))

    def test_single_sku_skips_comparison_slot(self):
        summary = ingest_capture(self.products, capture_payload(skus=1), allow_new_version=True)
        directory = self.products / summary["product_id"]
        add_images(directory)
        source = json.loads((directory / "input" / "source.json").read_text(encoding="utf-8"))
        plan = build_image_plan(
            product_dir=directory,
            source=source,
            source_refs=["input/source.json"],
            copy_bundle={"core_keyword": "термос 500 мл"},
        )
        self.assertEqual(len(plan["main_images"]), 1)
        layout_types = [item["layout_type"] for item in plan["detail_images"]]
        self.assertNotIn("sku_comparison", layout_types)

    def test_main_and_details_bind_only_the_selected_sku_identity(self):
        from models.image_plan import _references_for_sku
        references = [{"id": "sku-001", "role": "sku", "path": "input/sku-images/001-S1.jpg"},
                      {"id": "sku-002", "role": "sku", "path": "input/sku-images/002-S10.jpg"}]
        self.assertEqual([ref["id"] for ref in _references_for_sku({"sku_id": "S1"}, references)], ["sku-001"])
        source = json.loads((self.product_dir / "input/source.json").read_text(encoding="utf-8"))
        source["skus"] = [source["skus"][0]]
        source["skus"][0]["image_path"] = "input/sku-images/01.png"
        plan = build_image_plan(product_dir=self.product_dir, source=source, source_refs=["input/source.json"],
                                copy_bundle={"core_keyword": "термос"})
        self.assertEqual(plan["main_images"][0]["reference_image_ids"], ["sku-001"])
        for slot in plan["detail_images"]:
            self.assertEqual(slot["reference_image_ids"], ["sku-001"])

    def test_unknown_sku_image_order_is_not_identity_evidence(self):
        plan = self._plan()
        for slot in plan["main_images"]:
            self.assertEqual(slot["reference_image_ids"], [])
            self.assertEqual(slot["operation"], "needs_human_input")

    def test_reference_images_only_from_input(self):
        plan = self._plan()
        roles = {item["role"] for item in plan["reference_images"]}
        self.assertEqual(roles, {"main", "sku", "detail"})
        for item in plan["reference_images"]:
            self.assertTrue(item["path"].startswith("input/"))
            self.assertTrue(item["usable"])

    def test_missing_sku_references_require_human(self):
        empty = self.products / "P000099"
        (empty / "input").mkdir(parents=True)
        source = {
            "product_id": "P000099",
            "collection_id": "COL-TEST000001",
            "source_kind": "workbench_collection",
            "skus": [{"sku_id": "S1", "purchase_price_cny": 10}],
        }
        plan = build_image_plan(
            product_dir=empty,
            source=source,
            source_refs=["input/source.json"],
            copy_bundle={"core_keyword": "термос"},
        )
        self.assertEqual(plan["main_images"][0]["operation"], "needs_human_input")
        self.assertEqual(plan["main_images"][0]["status"], "needs_review")
        self.assertTrue(any(risk["area"] == "reference" for risk in plan["risks"]))

    def test_plan_without_copy_is_rejected(self):
        """图片上的文字只能来自文案 —— 没有文案时不给计划，而不是编一段俄文。"""
        with self.assertRaises(ValueError) as ctx:
            self._plan(copy_bundle={})
        self.assertIn("文案", str(ctx.exception))

    def test_plan_requires_collection_id_and_skus(self):
        with self.assertRaises(ValueError):
            build_image_plan(product_dir=self.product_dir, source={"skus": [{"sku_id": "S1"}]}, source_refs=[])
        with self.assertRaises(ValueError):
            build_image_plan(product_dir=self.product_dir, source={"collection_id": "COL-XXXXXXXX"}, source_refs=[])

    def test_no_chinese_in_buyer_visible_text_or_prompts(self):
        """叠字与生图提示词里不能有中文（颜色走俄语映射；提示词给生图模型）。"""
        plan = self._plan()
        cjk = __import__("re").compile(r"[\u4e00-\u9fff]")
        for item in plan["main_images"] + plan["detail_images"]:
            for text in item["russian_text"]:
                self.assertIsNone(cjk.search(text), f"{item['slot']} 叠字含中文: {text}")
            self.assertIsNone(cjk.search(str(item["prompt"])), f"{item['slot']} 提示词含中文")
        # 中文颜色名要能映射成俄语（红色 → красный）
        colors = [item["variant_value"] for item in plan["main_images"]]
        self.assertTrue(all(value and not cjk.search(value) for value in colors), colors)

    def test_brief_lists_every_slot_and_prompt(self):
        plan = self._plan()
        brief = render_plan_brief(plan)
        for item in plan["main_images"] + plan["detail_images"]:
            self.assertIn(item["slot"], brief)
        self.assertIn("提示词建议", brief)
        self.assertIn("禁止出现", brief)
        self.assertIn("3:4", brief)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class ImagePlanHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]
        add_images(self.product_dir)
        self.provider = FakeProvider()
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_step_writes_plan_and_brief(self):
        run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        run_single_step(self.product_dir, "russian_copy", provider=self.provider)
        result = run_single_step(self.product_dir, "image_plan", provider=self.provider)

        self.assertEqual(result["main_images"], 2)
        self.assertEqual(result["detail_images"], 8)
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_contract("image-plan", plan), [])
        brief = (self.product_dir / "output" / "image-plan-brief.md").read_text(encoding="utf-8")
        self.assertIn("main-S1", brief)
        self.assertIn("detail-008", brief)

    def _prepare_copy(self):
        """图片文字来自文案，所以两个 gate 测试都要先真正生成文案。"""
        run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        run_single_step(self.product_dir, "russian_copy", provider=self.provider)

    def test_handler_rejects_wrong_detail_count(self):
        class BadPlanProvider(FakeProvider):
            name = "bad-plan"

            def plan_images(self, request):
                plan = super().plan_images(request)
                plan["detail_images"] = plan["detail_images"][:7]
                return plan

        self._prepare_copy()
        with self.assertRaises(PipelineGateError) as ctx:
            run_single_step(self.product_dir, "image_plan", provider=BadPlanProvider())
        # 详情图必须是 8 张：7 张会先在契约层被拦下（契约 minItems=maxItems=8）
        self.assertIn("契约", str(ctx.exception))
        self.assertFalse((self.product_dir / "output" / "image-plan.json").is_file())

    def test_handler_requires_copy_first(self):
        with self.assertRaises(PipelineGateError) as ctx:
            run_single_step(self.product_dir, "image_plan", provider=self.provider)
        self.assertIn("文案", str(ctx.exception))

    def test_handler_rejects_main_count_mismatch(self):
        class TwoMainProvider(FakeProvider):
            name = "two-main"

            def plan_images(self, request):
                plan = super().plan_images(request)
                plan["main_images"] = plan["main_images"] + [dict(plan["main_images"][0], slot="main-extra")]
                return plan

        self._prepare_copy()
        with self.assertRaises(PipelineGateError) as ctx:
            run_single_step(self.product_dir, "image_plan", provider=TwoMainProvider())
        self.assertIn("与要上架的 SKU 数", str(ctx.exception))

    def test_pipeline_reaches_image_plan(self):
        """阶段 A 用桩、文案与图片规划用真实 handler，看能否一路走到 image_generation。"""
        create_batch(self.products, batches_root=self.root / "batches", target_store_ids=["shop-a"])
        (self.product_dir / "output").mkdir(parents=True, exist_ok=True)
        (self.product_dir / "output" / "ozon-attributes-final.json").write_text(
            json.dumps({"required_summary": {"missing": 0}}), encoding="utf-8"
        )
        (self.product_dir / "output" / "attribute-fill-input.json").write_text("{}", encoding="utf-8")

        def stub(step):
            def handler(ctx):
                for relative in step_definition(step).get("outputs", []):
                    if relative.endswith((".json", ".jsonl")):
                        ctx.write_json(relative, {"stub": True})
                    else:
                        ctx.path(relative).mkdir(parents=True, exist_ok=True)
                return {"warnings": [], "artifacts": []}

            return handler

        def category(ctx):
            ctx.write_json(
                "output/ozon-category.json",
                {
                    "metadata_source": "ozon_seller_api",
                    "category_id": 123456,
                    "type_id": 789,
                    "match_status": "api_confirmed",
                },
            )
            ctx.write_json("output/ozon-category-attributes.json", {"attributes": []})
            return {"warnings": [], "artifacts": []}

        handlers = {
            "category_match": category,
            "variant_rules": stub("variant_rules"),
            "measurements": stub("measurements"),
            "field_completion": stub("field_completion"),
        }
        source_file = self.product_dir / "input/source.json"
        source = json.loads(source_file.read_text(encoding="utf-8"))
        for index, sku in enumerate(source["skus"], 1):
            sku["image_path"] = f"input/sku-images/{index:02d}.png"
        source_file.write_text(json.dumps(source), encoding="utf-8")
        report = run_product(self.product_dir, handlers=handlers, provider=self.provider, step_budget=20)

        self.assertIn("image_plan", report["completed_steps"])
        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertEqual(report["stopped_at"], "image_generation")
        self.assertEqual(report["api_write_count"], 0)

    def test_generate_image_is_still_unimplemented(self):
        with self.assertRaises(ModelError):
            self.provider.generate_image(
                ImageRequest(product_id="P000001", product_dir=self.product_dir, source={})
            )


if __name__ == "__main__":
    unittest.main()
