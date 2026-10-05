"""契约校验器测试：用真实 schema 验证，并记录我们自己的状态文件与契约的差异。"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from contracts import CONTRACTS_DIR, available_contracts, format_problems, validate_contract  # noqa: E402
from pipeline import status as st  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取（运行 contracts/fetch_contracts.ps1）")
class TitleContractTests(unittest.TestCase):
    def test_valid_title_passes(self):
        payload = {
            "schema_version": "1.0.0",
            "product_id": "P000001",
            "source_refs": ["input/source.json", "output/product-analysis.json"],
            "title_ru": "Термос из нержавеющей стали 500 мл для чая и кофе",
            "short_title_ru": "Термос 500 мл",
            "core_keyword": "термос",
        }
        self.assertEqual(validate_contract("title-ru", payload), [])

    def test_extra_field_is_rejected(self):
        payload = {
            "product_id": "P000001",
            "title_ru": "Термос из нержавеющей стали 500 мл",
            "unexpected": True,
        }
        problems = validate_contract("title-ru", payload)
        self.assertTrue(any("unexpected" in item for item in problems), problems)

    def test_length_and_required_are_enforced(self):
        self.assertTrue(any("minLength" in item for item in validate_contract("title-ru", {"product_id": "P000001", "title_ru": "коротко"})))
        self.assertTrue(any("product_id" in item for item in validate_contract("title-ru", {"title_ru": "Термос из нержавеющей стали 500 мл"})))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class DescriptionAndAnalysisTests(unittest.TestCase):
    def test_sections_min_length(self):
        payload = {
            "product_id": "P000001",
            "description_ru": "х" * 90,
            "sections": {
                "product_value": "коротко",
                "usage_scenarios": "Достаточно длинный текст про сценарии использования.",
                "core_advantages": "Достаточно длинный текст про преимущества товара.",
                "usage_method": "Достаточно длинный текст про способ применения.",
                "notices": "Достаточно длинный текст про меры предосторожности.",
            },
        }
        problems = validate_contract("description-ru", payload)
        self.assertTrue(any("product_value" in item for item in problems), problems)

    def test_source_refs_need_three(self):
        payload = {
            "product_id": "P000001",
            "description_ru": "х" * 90,
            "source_refs": ["a", "b"],
        }
        problems = validate_contract("description-ru", payload)
        self.assertTrue(any("minItems" in item for item in problems), problems)

    def test_product_analysis_requires_facts(self):
        payload = {
            "schema_version": "1.0.0",
            "product_id": "P000001",
            "source_refs": ["input/source.json"],
            "selling_points": [],
            "inferences": [],
            "unknowns": [],
            "risks": [],
            "recommendation": {"decision": "continue", "reason": "ok"},
            "processing": {
                "step": "product_analysis",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+08:00",
                "finished_at": "2026-01-01T00:00:01+08:00",
                "error": None,
            },
        }
        problems = validate_contract("product-analysis", payload)
        self.assertTrue(any("facts" in item for item in problems), problems)


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class OurStatusDeviationsTests(unittest.TestCase):
    """我们的 status.json 是上游契约的超集：必填齐全，但多两个字段。"""

    def test_status_is_superset_of_upstream_contract(self):
        payload = st.new_status("P000001")
        strict = validate_contract("status", payload)
        extra = [item for item in strict if "契约未定义的字段" in item]
        self.assertTrue(any("collection_id" in item for item in extra), strict)
        self.assertTrue(any("source_url" in item for item in extra), extra)

        relaxed = validate_contract("status", payload, allow_extra=True)
        self.assertEqual(relaxed, [], format_problems(relaxed))

    def test_uploaded_rule_still_applies_when_relaxed(self):
        payload = st.new_status("P000001")
        payload["status"] = "UPLOADED"
        problems = validate_contract("status", payload, allow_extra=True)
        self.assertTrue(any("upload_status" in item for item in problems), problems)


class FormatProblemsTests(unittest.TestCase):
    def test_truncates(self):
        text = format_problems([f"p{i}" for i in range(12)], limit=3)
        self.assertIn("另有 9 条", text)


if __name__ == "__main__":
    unittest.main()
