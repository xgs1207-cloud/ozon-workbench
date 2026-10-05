"""模型层 fake 的测试：产物必须过真实契约，且行为确定。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from contracts import available_contracts, validate_contract  # noqa: E402
from models import (  # noqa: E402
    AnalysisRequest,
    CopyRequest,
    ImagePlanRequest,
    ImageRequest,
    ModelError,
    existing_source_refs,
    load_provider,
)
from models.fake import FakeProvider  # noqa: E402
from rules.validate import validate_copy_bundle  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0


def make_product(root: pathlib.Path) -> pathlib.Path:
    directory = root / "P000001"
    (directory / "input").mkdir(parents=True)
    source = {
        "schema_version": "1.0.0",
        "product_id": "P000001",
        "source_url": "https://detail.1688.com/offer/123456789.html",
        "title_zh": "316 不锈钢保温杯",
        "selected_category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
        "skus": [
            {"sku_id": "S1", "color_zh": "红色", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
            {"sku_id": "S2", "color_zh": "蓝色", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
        ],
    }
    (directory / "input" / "source.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    return directory


def analysis_request(directory: pathlib.Path) -> AnalysisRequest:
    source = json.loads((directory / "input" / "source.json").read_text(encoding="utf-8"))
    return AnalysisRequest(
        product_id=directory.name,
        product_dir=directory,
        source=source,
        source_refs=existing_source_refs(directory, ("input/source.json",)),
    )


class ProviderLoadingTests(unittest.TestCase):
    def test_default_is_fake(self):
        self.assertEqual(load_provider().name, "fake")
        self.assertEqual(load_provider("fake").name, "fake")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ModelError):
            load_provider("codex-cli")

    def test_image_planning_needs_real_inputs_and_generation_is_unimplemented(self):
        provider = FakeProvider()
        # 空 source 时规划必须报错（缺 SKU / collection_id），不编造计划
        with self.assertRaises(ValueError):
            provider.plan_images(
                ImagePlanRequest(product_id="P000001", product_dir=pathlib.Path("."), source={})
            )
        with self.assertRaises(ModelError):
            provider.generate_image(
                ImageRequest(product_id="P000001", product_dir=pathlib.Path("."), source={})
            )


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class FakeOutputContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = make_product(pathlib.Path(self.tmp.name))
        self.provider = FakeProvider()

    def tearDown(self):
        self.tmp.cleanup()

    def test_analysis_passes_contract(self):
        payload = self.provider.analyze_product(analysis_request(self.directory))
        self.assertEqual(validate_contract("product-analysis", payload), [])
        self.assertEqual(payload["recommendation"]["decision"], "continue")
        self.assertEqual(len(payload["facts"]["skus"]), 2)
        # 没有证据的字段必须进 unknowns，而不是编造
        unknown_fields = {item["field"] for item in payload["unknowns"]}
        self.assertIn("materials", unknown_fields)

    def test_missing_category_is_blocking(self):
        source_path = self.directory / "input" / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["selected_category"] = None
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

        payload = self.provider.analyze_product(analysis_request(self.directory))
        self.assertEqual(payload["recommendation"]["decision"], "needs_human_input")
        self.assertTrue(any(item["blocking"] for item in payload["risks"]))

    def test_copy_passes_contracts_and_rules(self):
        analysis = self.provider.analyze_product(analysis_request(self.directory))
        source = json.loads((self.directory / "input" / "source.json").read_text(encoding="utf-8"))
        bundle = self.provider.write_copy_ru(
            CopyRequest(
                product_id=self.directory.name,
                product_dir=self.directory,
                source=source,
                source_refs=["input/source.json", "input/selected-keywords.json"],
                analysis=analysis,
                selected_keywords=[
                    {"keyword": "термос 500 мл", "score": 0.47},
                    {"keyword": "термос для чая", "score": 0.31},
                    {"keyword": "термос подарочный", "score": 0.12},
                ],
            )
        )
        for contract_name, key in (("title-ru", "title_ru"), ("description-ru", "description_ru"), ("keywords-ru", "keywords_ru")):
            self.assertEqual(validate_contract(contract_name, bundle[key]), [], contract_name)
        self.assertEqual(validate_copy_bundle(bundle["copy_bundle"]), [])
        self.assertTrue(bundle["copy_bundle"]["hashtags"])
        self.assertIn("500мл", bundle["copy_bundle"]["title_ru"])

    def test_copy_without_keywords_is_rejected(self):
        source = json.loads((self.directory / "input" / "source.json").read_text(encoding="utf-8"))
        with self.assertRaises(ModelError):
            self.provider.write_copy_ru(
                CopyRequest(product_id=self.directory.name, product_dir=self.directory, source=source)
            )

    def test_output_is_deterministic_except_timestamps(self):
        first = self.provider.analyze_product(analysis_request(self.directory))
        second = self.provider.analyze_product(analysis_request(self.directory))
        for payload in (first, second):
            payload["processing"]["started_at"] = "X"
            payload["processing"]["finished_at"] = "X"
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
