"""选词 + 模型 handler 的测试：M0 关键词库 → M2 文案的完整链路。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from keyword_library import store as keyword_store  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from pipeline import status as st  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.context import PipelineGateError, StepContext  # noqa: E402
from pipeline.handlers import handle_product_analysis, run_single_step  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import (  # noqa: E402
    load_selected_keywords,
    select_from_library,
    selected_keyword_texts,
    set_selected_keywords,
)
from pipeline.steps import step_definition  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0

SOURCE_URL = "https://detail.1688.com/offer/123456789.html"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def add_images(product_dir: pathlib.Path, *, main: int = 2, sku: int = 2, detail: int = 3) -> None:
    for relative, count in (("main-images", main), ("sku-images", sku), ("detail-images", detail)):
        directory = product_dir / "input" / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + f"{relative}{index}".encode())


def capture_payload() -> dict:
    return {
        "source_url": SOURCE_URL,
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
        "skus": [
            {"sku_id": "S1", "color_zh": "红色", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5, "image_path": "input/sku-images/01.png"},
            {"sku_id": "S2", "color_zh": "蓝色", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0, "image_path": "input/sku-images/02.png"},
        ],
    }


def stub_handler(step):
    """把该步声明的产物造出来（用于 phase A 里需要 Ozon 网络的步骤）。"""

    def handler(ctx):
        for relative in step_definition(step).get("outputs", []):
            if relative.endswith((".json", ".jsonl")):
                ctx.write_json(relative, {"stub": True})
            else:
                ctx.path(relative).mkdir(parents=True, exist_ok=True)
        return {"warnings": [], "artifacts": []}

    return handler


def category_handler():
    """合法的 Ozon 类目元数据桩：metadata_source 必须是 ozon_seller_api，且快照要能支撑属性编译。"""

    def handler(ctx):
        ctx.write_json(
            "output/ozon-category.json",
            {
                "metadata_source": "ozon_seller_api",
                "category_id": 123456,
                "type_id": 789,
                "match_status": "api_confirmed",
            },
        )
        ctx.write_json(
            "output/ozon-category-attributes.json",
            {
                "schema_version": "1.0.0",
                "product_id": ctx.product_dir.name,
                "fetched_at": "2026-10-05T12:00:00+08:00",
                "api_endpoint": "/v1/description-category/attribute",
                "category_id": 123456,
                "category_name": "Термосы",
                "type_id": 789,
                "attributes": [
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
                ],
                "warnings": [],
            },
        )
        return {"warnings": [], "artifacts": []}

    return handler


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_selected_keywords_normalizes(self):
        record = set_selected_keywords(
            self.product_dir,
            ["термос 500 мл", "ТЕРМОС 500 МЛ", "x", {"keyword": "термос для чая", "score": 0.31}],
        )
        texts = [item["keyword"] for item in record["keywords"]]
        self.assertEqual(texts, ["термос 500 мл", "термос для чая"])  # 去重 + 丢掉过短的
        self.assertEqual(selected_keyword_texts(self.product_dir), texts)
        self.assertIsNotNone(load_selected_keywords(self.product_dir))

    def test_empty_selection_rejected(self):
        with self.assertRaises(ValueError):
            set_selected_keywords(self.product_dir, ["", "x"])

    def test_select_from_library(self):
        library = self.root / "keyword-library"
        keyword_store.upsert(
            library,
            [
                {"keyword": "термос 500 мл", "category_id": "1001", "type_id": "2001", "search_volume": 12400, "competitor_count": 830},
                {"keyword": "термос для чая", "category_id": "1001", "type_id": "2001", "search_volume": 3100, "competitor_count": 120},
                {"keyword": "термос стальной", "category_id": "1001", "type_id": "2001", "search_volume": 8800, "competitor_count": 1900},
            ],
        )
        record = select_from_library(self.product_dir, library, limit=2)
        self.assertEqual(record["source"], "library:in_library,qualified")
        # 这个 3 词样本里只有 1 个词同时满足"热度进前 40% + 竞争落前 60%"（默认门槛偏严）
        self.assertEqual(len(record["keywords"]), 1)
        self.assertEqual(record["keywords"][0]["keyword"], "термос 500 мл")

    def test_select_from_empty_library_fails(self):
        with self.assertRaises(ValueError):
            select_from_library(self.product_dir, self.root / "empty-library", limit=3)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / self.summary["product_id"]
        self.provider = FakeProvider()
        add_images(self.product_dir)
        # upload_feasibility 要求 final 属性文件存在且 missing == 0；
        # attribute-fill-input 是 ecommerce_design 的前置产物（真实流水线由属性填值步骤生成）
        (self.product_dir / "output").mkdir(parents=True, exist_ok=True)
        (self.product_dir / "output" / "ozon-attributes-final.json").write_text(
            json.dumps({"required_summary": {"missing": 0}}, ensure_ascii=False), encoding="utf-8"
        )
        (self.product_dir / "output" / "attribute-fill-input.json").write_text(
            json.dumps({"stub": True}, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def phase_a_handlers(self):
        return {
            "category_match": category_handler(),
            "variant_rules": stub_handler("variant_rules"),
            "measurements": stub_handler("measurements"),
        }

    def test_analysis_then_copy_single_steps(self):
        analysis = run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        self.assertEqual(analysis["decision"], "continue")
        self.assertTrue((self.product_dir / "output" / "product-analysis.json").is_file())

        with self.assertRaises(PipelineGateError) as ctx:
            run_single_step(self.product_dir, "russian_copy", provider=self.provider)
        self.assertIn("选词", str(ctx.exception))

        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        result = run_single_step(self.product_dir, "russian_copy", provider=self.provider)
        self.assertTrue(result["title_ru"])

        title_doc = json.loads((self.product_dir / "output" / "title-ru.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_contract("title-ru", title_doc), [])

    def test_full_chain_reaches_image_generation(self):
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая", "термос подарочный"])
        create_batch(self.products, batches_root=self.root / "batches", target_store_ids=["shop-a"])
        report = run_product(
            self.product_dir, handlers=self.phase_a_handlers(), provider=self.provider, step_budget=20
        )

        # 文案、属性编译、图片规划都是真实 handler；生图未实现 → 如实停在 image_generation
        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertEqual(report["stopped_at"], "image_generation")
        for step in ("product_analysis", "russian_copy", "field_completion", "image_plan"):
            self.assertIn(step, report["completed_steps"])
        self.assertNotIn("image_generation", report["completed_steps"])
        self.assertEqual(report["api_write_count"], 0)

        copy = json.loads((self.product_dir / "output" / "copy-ru.json").read_text(encoding="utf-8"))
        self.assertEqual(copy["generated_by"], "fake")
        self.assertTrue(copy["hashtags"])
        for contract_name, filename in (
            ("title-ru", "output/title-ru.json"),
            ("description-ru", "output/description-ru.json"),
            ("keywords-ru", "output/keywords-ru.json"),
        ):
            document = json.loads((self.product_dir / filename).read_text(encoding="utf-8"))
            self.assertEqual(validate_contract(contract_name, document), [], contract_name)

    def test_copy_gate_failure_marks_attention_and_writes_nothing(self):
        set_selected_keywords(self.product_dir, ["термос 500 мл"])
        create_batch(self.products, batches_root=self.root / "batches", target_store_ids=["shop-a"])

        class BadProvider(FakeProvider):
            name = "bad"

            def write_copy_ru(self, request):
                bundle = super().write_copy_ru(request)
                bundle["title_ru"]["title_ru"] = "коротко"
                bundle["copy_bundle"]["title_ru"] = "коротко"
                bundle["copy_bundle"]["hashtags"] = ["#termos"]
                bundle["keywords_ru"]["primary_keywords"] = []
                return bundle

        report = run_product(
            self.product_dir, handlers=self.phase_a_handlers(), provider=BadProvider(), step_budget=20
        )
        self.assertEqual(report["stop_reason"], "gate_failed")
        # 上游语义：文案由设计步骤产出，所以坏文案在设计步骤就被拦住（不会污染后续产物）
        self.assertEqual(report["stopped_at"], "ecommerce_design")
        saved = st.load_status(self.product_dir)
        self.assertEqual(saved["status"], "NEEDS_ATTENTION")
        self.assertFalse((self.product_dir / "output" / "title-ru.json").is_file())
        self.assertFalse((self.product_dir / "output" / "copy-ru.json").is_file())

    def test_analysis_without_category_is_gated(self):
        source_path = self.product_dir / "input" / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["selected_category"] = None
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        # 采集时选的类目文件也要清掉：只要它还写着类目，分析步骤就应当（正确地）采用它
        selection = self.product_dir / "input" / "category-selection.json"
        if selection.is_file():
            selection.unlink()

        with self.assertRaises(PipelineGateError) as ctx:
            run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        self.assertIn("人工确认", str(ctx.exception))

    def test_analysis_accepts_category_from_selection_file(self):
        """采集时漏选类目、之后补在 category-selection.json 里：分析步骤应当能用它继续。"""
        source_path = self.product_dir / "input" / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["selected_category"] = None
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        (self.product_dir / "input" / "category-selection.json").write_text(
            json.dumps({"category_id": "1001", "type_id": "2001"}), encoding="utf-8"
        )

        result = run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        self.assertEqual(result["decision"], "continue")

    def test_context_without_provider_reports_gate(self):
        context = StepContext(self.product_dir, "product_analysis", True, "development", {}, None)
        with self.assertRaises(PipelineGateError) as error:
            handle_product_analysis(context)
        self.assertIn("模型层", str(error.exception))


if __name__ == "__main__":
    unittest.main()
