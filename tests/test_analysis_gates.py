import unittest
from unittest.mock import patch

from pipeline.analysis_gates import risk_gates
from pipeline.guided_workflow import analyze_selected_product, confirm_analysis, generate_copy_candidates, workflow_status
from pipeline.listing_form import read_json, write_json
from tests.test_guided_workflow import GuidedWorkflowTests


class AnalysisGateTests(unittest.TestCase):
    def test_missing_parameters_do_not_block_preparation(self):
        payload = {"risks": [
            {"area": "product_info", "blocking": True, "message": "Отсутствуют ключевые параметры товара: материал"},
            {"area": "logistics", "blocking": True, "message": "Нет данных о весе и размерах товара"},
            {"area": "compliance", "blocking": True, "message": "Отсутствуют данные о сертификатах безопасности"}]}
        gates = risk_gates(payload)
        self.assertEqual(gates["preparation"], [])
        self.assertEqual(len(gates["deferred"]), 3)
        self.assertEqual(gates["publication"], [payload["risks"][2]["message"]])

    def test_known_noncompliance_and_rejection_remain_blocking(self):
        for payload in [
            {"risks": [{"area": "compliance", "blocking": True, "message": "已确认存在虚假认证"}]},
            {"risks": [{"area": "identity", "blocking": True, "message": "商品身份与类目矛盾"}]},
            {"recommendation": {"decision": "reject"}}]:
            self.assertTrue(risk_gates(payload)["preparation"])

    def test_only_missing_category_information_can_defer(self):
        missing = {"risks": [{"area": "category", "blocking": True, "message": "尚未确认 Ozon 类目"}]}
        self.assertFalse(risk_gates(missing)["preparation"])
        for message in ["已选抗压玩具类目与实际商品不符", "Selected category does not match the product"]:
            gates = risk_gates({"risks": [{"area": "category", "blocking": True, "message": message}]})
            self.assertEqual(gates["preparation"], [message])
            self.assertEqual(gates["publication"], [message])

    def test_publication_gate_is_enforced_before_card_or_live_write(self):
        from pathlib import Path
        from pipeline.listing_draft import canonical_listing
        with patch("pipeline.listing_draft._modern_ready", return_value={"publication_blockers": ["认证待补充"]}):
            with self.assertRaisesRegex(ValueError, "商品合规风险"):
                canonical_listing(Path("offline-unused"), shop="offline")


class CategoryOnlyCopyTests(unittest.TestCase):
    setUp = GuidedWorkflowTests.setUp
    tearDown = GuidedWorkflowTests.tearDown
    category = GuidedWorkflowTests.category

    def test_analysis_preserves_known_category_mismatch(self):
        original = self.provider.analyze_product
        def mismatch(request):
            value = original(request)
            value["risks"].append({"area": "category", "level": "high", "blocking": True,
                                   "message": "所选类目与真实商品身份不符"})
            return value
        with patch.object(self.provider, "analyze_product", side_effect=mismatch):
            generated = analyze_selected_product(self.directory, self.provider)
        risk = read_json(self.directory / "output/product-analysis.json")["risks"][-1]
        self.assertTrue(risk["blocking"])
        with self.assertRaisesRegex(ValueError, "阻断性风险"):
            confirm_analysis(self.directory, generated["input_fingerprint"])

    def test_confirm_missing_compliance_then_create_truthful_draft_but_not_publish(self):
        original = self.provider.analyze_product
        def pending(request):
            value = original(request)
            value["risks"].extend([
                {"area": "logistics", "level": "medium", "blocking": True, "message": "缺少包装尺寸重量"},
                {"area": "compliance", "level": "critical", "blocking": True, "message": "未提供所需认证信息"}])
            return value
        with patch.object(self.provider, "analyze_product", side_effect=pending):
            generated = analyze_selected_product(self.directory, self.provider)
        confirmed = confirm_analysis(self.directory, generated["input_fingerprint"])
        self.assertTrue(confirmed["analysis"]["confirmed"])
        self.assertEqual(confirmed["publication_blockers"], ["未提供所需认证信息"])
        self.assertTrue(read_json(self.directory / "output/product-analysis.json")["risks"][-1]["blocking"])

    def test_real_category_without_library_queries_generates_three_candidates(self):
        (self.directory / "input/selected-keywords.json").unlink()
        result = analyze_selected_product(self.directory, self.provider)
        confirm_analysis(self.directory, result["input_fingerprint"])
        self.category()
        result = generate_copy_candidates(self.directory, self.provider)
        self.assertEqual(len(result["copy"]["candidates"]), 3)
        self.assertEqual(read_json(self.directory / "output/copy-candidates.json")["keyword_plan"], [])
        self.assertTrue(all(not request.selected_keywords for request in self.provider.copy_requests))
        self.assertTrue(all(row["audit"]["core_coverage"] is None for row in result["copy"]["candidates"]))
