"""Sparse selected-SKU evidence and fee-safe repair/cache regressions (offline)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from models import CopyRequest, ModelError
from models.fake import FakeProvider
from models.http_provider import HttpModelProvider
from pipeline.copy_evidence import candidate_evidence, safe_evidence_problems, verified_copy_facts
from pipeline.guided_workflow import (CANDIDATES_FILE, _copy_source, copy_generation_status,
                                      generate_copy_candidates)
from pipeline.listing_form import read_json, write_json
from pipeline.selection import set_selected_keywords
from tests import test_guided_workflow as fixtures


MODES = ("search_first", "conversion_first", "differentiation_first")


def sparse_analysis():
    return {"facts": {"title_cn": "硅胶玩具 99 克 安全儿童玩具", "materials": [], "dimensions": None,
                      "weight": None, "skus": [{"sku_id": "6281570506082", "name_cn": "粉色幽灵",
                                                "properties": {}, "price_cny": 3.2, "image_refs": []}]}}


def real_category():
    return {"source": "ozon_seller_api", "confirmed_by_user": True, "type_name": "抗压玩具",
            "category_id": 17028973, "type_id": 92811}


def safe_bundle(directory):
    request = CopyRequest(product_id=directory.name, product_dir=directory,
                          source={"selected_category": {"category_name_ru": "Игрушка антистресс"}},
                          extra={"allow_category_only_copy": True})
    bundle = FakeProvider().write_copy_ru(request)["copy_bundle"]
    bundle["title_ru"] = "Игрушка антистресс в форме розового привидения"
    bundle["claim_evidence"] = [{"claim": "Игрушка антистресс", "fact_ids": ["category.type"]},
                               {"claim": "розового привидения", "fact_ids": ["facts.skus.6281570506082.name_cn"]}]
    return bundle


class Transport:
    name = "offline-evidence"
    model = "offline-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response, ensure_ascii=False)


class CopyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "P000001"
        self.directory.mkdir()
        self.facts = verified_copy_facts(sparse_analysis(), {"selected_category": real_category()})

    def tearDown(self):
        self.temp.cleanup()

    def request(self, **extra):
        return CopyRequest(product_id=self.directory.name, product_dir=self.directory,
                           source={"selected_category": real_category()}, analysis=sparse_analysis(),
                           extra={"verified_facts": self.facts, "allow_category_only_copy": True, **extra})

    def response(self):
        bundle = safe_bundle(self.directory)
        return {"candidates": [{"mode": mode, "copy_bundle": deepcopy(bundle)} for mode in MODES]}

    def test_sparse_facts_include_restricted_type_and_selected_label_not_price_or_title(self):
        self.assertEqual({row["id"] for row in self.facts},
                         {"facts.skus.6281570506082.name_cn", "category.type"})
        self.assertTrue(all(row["allow_numeric"] is False for row in self.facts))
        self.assertFalse(any("硅胶" in str(row["value"]) or row["value"] == 3.2 for row in self.facts))
        result = candidate_evidence(safe_bundle(self.directory), self.facts, [])
        self.assertEqual(len(result["claim_evidence"]), 2)

    def test_unconfirmed_category_does_not_become_verified_type(self):
        category = real_category()
        category["confirmed_by_user"] = False
        facts = verified_copy_facts(sparse_analysis(), {"selected_category": category})
        self.assertNotIn("category.type", {row["id"] for row in facts})

    def test_raw_label_numbers_and_materials_cannot_launder_claims(self):
        analysis = sparse_analysis()
        analysis["facts"]["skus"][0]["name_cn"] = "粉色硅胶幽灵 99克"
        facts = verified_copy_facts(analysis, {"selected_category": real_category()})
        for title, error in (("Игрушка антистресс 99 г", "未证实"), ("Игрушка антистресс из силикона", "材质")):
            copy = safe_bundle(self.directory)
            copy["title_ru"], copy["claim_evidence"] = title, []
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, error):
                candidate_evidence(copy, facts, [])

    def test_malformed_ids_and_nonliteral_claim_get_useful_validation_errors(self):
        for ids in ("category.type", {"id": "category.type"}, [False], [], [["category.type"]]):
            copy = safe_bundle(self.directory)
            copy["claim_evidence"] = [{"claim": "Игрушка антистресс", "fact_ids": ids}]
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, "fact_ids"):
                candidate_evidence(copy, self.facts, [])
        copy["claim_evidence"] = [{"claim": "антистресс игрушка", "fact_ids": ["category.type"]}]
        with self.assertRaisesRegex(ValueError, "逐字"):
            candidate_evidence(copy, self.facts, [])

    def test_reference_errors_use_existing_bounded_repair_not_postpaid_workflow_exception(self):
        invalid = self.response()
        invalid["candidates"][0]["copy_bundle"]["claim_evidence"][0]["fact_ids"] = ["product_type"]
        transport = Transport([invalid, self.response()])
        provider = HttpModelProvider(transport, max_attempts=2)
        result = provider.write_copy_candidates_ru(self.request())
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("未知 fact_ids=product_type", transport.calls[1]["user"])
        self.assertIn('"category.type"', transport.calls[0]["user"])

    def test_failed_response_cached_and_force_retry_explicit(self):
        invalid = self.response()
        invalid["candidates"][0]["copy_bundle"]["claim_evidence"][0]["fact_ids"] = ["not-real"]
        transport = Transport([invalid, self.response()])
        provider = HttpModelProvider(transport, max_attempts=1)
        with self.assertRaisesRegex(ModelError, "not-real"):
            provider.write_copy_candidates_ru(self.request())
        diagnostic = read_json(self.directory / "output/copy-generation-diagnostic.json")
        self.assertEqual(diagnostic["status"], "invalid")
        self.assertEqual(diagnostic["payload"], invalid)
        with self.assertRaisesRegex(ModelError, "未重复收费"):
            provider.write_copy_candidates_ru(self.request())
        self.assertEqual(len(transport.calls), 1)
        provider.write_copy_candidates_ru(self.request(force_new_model_call=True))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(read_json(self.directory / "output/copy-generation-diagnostic.json")["status"], "valid")

    def test_transport_failure_during_repair_keeps_first_paid_response(self):
        invalid = self.response()
        invalid["candidates"][0]["copy_bundle"]["claim_evidence"][0]["fact_ids"] = ["not-real"]
        transport = Transport([invalid, ModelError("offline network failed")])
        provider = HttpModelProvider(transport, max_attempts=2)
        with self.assertRaisesRegex(ModelError, "network failed"):
            provider.write_copy_candidates_ru(self.request())
        self.assertEqual(read_json(self.directory / "output/copy-generation-diagnostic.json")["payload"], invalid)

    def test_free_revalidation_cache_miss_or_model_change_never_calls(self):
        transport = Transport([self.response()])
        provider = HttpModelProvider(transport, max_attempts=1)
        with self.assertRaisesRegex(ModelError, "未调用模型"):
            provider.write_copy_candidates_ru(self.request(revalidate_only=True))
        self.assertEqual(len(transport.calls), 0)
        invalid = self.response()
        invalid["candidates"][0]["copy_bundle"]["claim_evidence"][0]["fact_ids"] = ["not-real"]
        transport.responses = [invalid]
        with self.assertRaises(ModelError):
            provider.write_copy_candidates_ru(self.request())
        transport.model = "new-model"
        with self.assertRaisesRegex(ModelError, "未调用模型"):
            provider.write_copy_candidates_ru(self.request(revalidate_only=True))
        self.assertEqual(len(transport.calls), 1)
        with self.assertRaisesRegex(ModelError, "不能同时"):
            provider.write_copy_candidates_ru(self.request(revalidate_only=True, force_new_model_call=True))
        self.assertEqual(len(transport.calls), 1)

    def test_malformed_modes_and_bundles_enter_repair_and_preserve_response(self):
        for mutation in (lambda rows: rows[0].update(mode=["search_first"]),
                         lambda rows: rows.__setitem__(0, False),
                         lambda rows: rows[0]["copy_bundle"].update(hashtags=23),
                         lambda rows: rows[0]["copy_bundle"].update(description_sections=[]),
                         lambda rows: rows[0].update(copy_bundle=[])):
            invalid = self.response()
            mutation(invalid["candidates"])
            transport = Transport([invalid, self.response()])
            provider = HttpModelProvider(transport, max_attempts=1)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ModelError):
                    provider.write_copy_candidates_ru(self.request(force_new_model_call=True))
                self.assertEqual(read_json(self.directory / "output/copy-generation-diagnostic.json")["payload"], invalid)
                with self.assertRaisesRegex(ModelError, "未重复收费"):
                    provider.write_copy_candidates_ru(self.request(revalidate_only=True))
                self.assertEqual(len(transport.calls), 1)
                provider.write_copy_candidates_ru(self.request(force_new_model_call=True))
                self.assertEqual(len(transport.calls), 2)

    def test_numeric_and_material_failures_also_enter_repair(self):
        for suffix in (" 99 г", " из силикона"):
            invalid = self.response()
            invalid["candidates"][0]["copy_bundle"]["title_ru"] += suffix
            transport = Transport([invalid, self.response()])
            provider = HttpModelProvider(transport, max_attempts=2)
            result = provider.write_copy_candidates_ru(self.request(force_new_model_call=True))
            self.assertEqual(len(transport.calls), 2)
            self.assertNotIn(suffix, result["candidates"][0]["documents"]["copy_bundle"]["title_ru"])

    def test_diagnostic_redacts_urls_credentials_and_control_characters(self):
        text = safe_evidence_problems(["bad sk-secretxxx https://host/path?key=secret \n Bearer abc eyJabc.def.ghi"])[0]
        for secret in ("sk-secretxxx", "https://host", "Bearer abc", "eyJabc", "\n"):
            self.assertNotIn(secret, text)

    def test_malformed_or_unselected_keywords_are_repairable_not_type_errors(self):
        for keywords in ("not-an-array", [False], [{"keyword": "wrong"}], ["invented"]):
            invalid = self.response()
            invalid["candidates"][0]["copy_bundle"]["secondary_keywords"] = keywords
            transport = Transport([invalid, self.response()])
            provider = HttpModelProvider(transport, max_attempts=2)
            with self.subTest(keywords=keywords):
                provider.write_copy_candidates_ru(self.request(force_new_model_call=True))
                self.assertEqual(len(transport.calls), 2)

    def test_only_invalid_generic_cached_tag_revalidated_without_any_new_rpc(self):
        cached_response = self.response()
        for row in cached_response["candidates"]:
            row["copy_bundle"]["hashtags"] = ["#антистресс", "#подарок", "#привидение", "#антистресс"]
        transport = Transport([cached_response])
        provider = HttpModelProvider(transport, max_attempts=1)
        # Reproduce the receipt written by the previous deployed validator.
        with patch("models.http_provider._normalize_copy_candidate_hashtags", lambda value: (deepcopy(value), [])):
            with self.assertRaisesRegex(ModelError, "подарок"):
                provider.write_copy_candidates_ru(self.request())
        receipt_path = self.directory / "output/copy-generation-diagnostic.json"
        previous = read_json(receipt_path)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(previous["payload"], cached_response)
        result = provider.write_copy_candidates_ru(self.request(revalidate_only=True))
        self.assertEqual(len(transport.calls), 1)  # No responses remain, so any extra RPC would also fail.
        receipt = read_json(receipt_path)
        self.assertEqual(receipt["input_key"], previous["input_key"])
        self.assertEqual(receipt["payload"], cached_response)  # Original private response remains auditable.
        self.assertEqual(receipt["status"], "valid")
        self.assertEqual(receipt["original_attempts"], 1)
        self.assertTrue(any("подарок" in note for note in receipt["normalization"]))
        for raw, row in zip(cached_response["candidates"], result["candidates"]):
            copy = row["documents"]["copy_bundle"]
            self.assertEqual(copy["hashtags"], ["#антистресс", "#привидение"])
            for field in ("title_ru", "description_ru", "claim_evidence", "description_sections"):
                self.assertEqual(copy[field], raw["copy_bundle"][field])
            self.assertTrue(any("已移除" in warning and "подарок" in warning for warning in copy["warnings"]))

    def test_fresh_tag_cleanup_uses_same_normalizer_and_preserves_raw_receipt(self):
        response = self.response()
        response["candidates"][0]["copy_bundle"]["hashtags"] = ["#антистресс", "#подарок", "#антистресс"]
        transport = Transport([response])
        provider = HttpModelProvider(transport, max_attempts=1)
        result = provider.write_copy_candidates_ru(self.request())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["candidates"][0]["documents"]["copy_bundle"]["hashtags"], ["#антистресс"])
        receipt = read_json(self.directory / "output/copy-generation-diagnostic.json")
        self.assertEqual(receipt["payload"], response)
        self.assertTrue(receipt["normalization"])

    def test_tag_cleanup_cannot_hide_cached_numbers_materials_or_invalid_fact_ids(self):
        for mutation, message in (
            (lambda copy: copy.update(title_ru=copy["title_ru"] + " 99 г"), "数值"),
            (lambda copy: copy.update(title_ru=copy["title_ru"] + " из силикона"), "材质"),
            (lambda copy: copy["claim_evidence"][0].update(fact_ids=["fabricated_fact"]), "fabricated_fact"),
        ):
            response = self.response()
            copy = response["candidates"][0]["copy_bundle"]
            copy["hashtags"] = ["#антистресс", "#подарок"]
            mutation(copy)
            transport = Transport([response])
            provider = HttpModelProvider(transport, max_attempts=1)
            with self.subTest(message=message):
                with patch("models.http_provider._normalize_copy_candidate_hashtags", lambda value: (deepcopy(value), [])):
                    with self.assertRaises(ModelError):
                        provider.write_copy_candidates_ru(self.request(force_new_model_call=True))
                with self.assertRaisesRegex(ModelError, message):
                    provider.write_copy_candidates_ru(self.request(revalidate_only=True))
                self.assertEqual(len(transport.calls), 1)
                receipt = read_json(self.directory / "output/copy-generation-diagnostic.json")
                self.assertEqual(receipt["status"], "invalid")
                self.assertEqual(receipt["payload"], response)


class CopyEvidenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GuidedWorkflowTests("runTest")
        self.fixture.setUp()
        self.directory = self.fixture.directory

    def tearDown(self):
        self.fixture.tearDown()

    def sparse_confirmed(self):
        source = read_json(self.directory / "input/source.json")
        source["skus"][0] = {"sku_id": "S1", "sku_name": "粉色幽灵", "purchase_price_cny": 3.2}
        write_json(self.directory / "input/source.json", source)
        self.fixture.analysis()
        self.fixture.category()
        write_json(self.directory / "input/category-form.json", {"source": "ozon_seller_api", "shop_id": "shop-a",
                                                                "category_id": 1001, "type_id": 2001,
                                                                "category_name": "抗压玩具", "fields": []})
        set_selected_keywords(self.directory, [], allow_empty=True)

    def test_official_type_loaded_only_from_matching_form(self):
        self.sparse_confirmed()
        self.assertEqual(_copy_source(self.directory)["selected_category"]["type_name"], "抗压玩具")
        form = read_json(self.directory / "input/category-form.json")
        form["type_id"] = 9001
        write_json(self.directory / "input/category-form.json", form)
        self.assertNotIn("type_name", _copy_source(self.directory)["selected_category"])

    def test_sparse_category_only_batch_and_status_cache_without_autoselection(self):
        self.sparse_confirmed()
        bundle = safe_bundle(self.directory)
        bundle["claim_evidence"][1]["fact_ids"] = ["facts.skus.S1.name_cn"]
        valid = {"candidates": [{"mode": mode, "copy_bundle": deepcopy(bundle)} for mode in MODES]}
        invalid = deepcopy(valid)
        invalid["candidates"][0]["copy_bundle"]["claim_evidence"][0]["fact_ids"] = ["not-real"]
        transport = Transport([invalid, valid])
        provider = HttpModelProvider(transport, max_attempts=1)
        with self.assertRaises(ModelError):
            generate_copy_candidates(self.directory, provider)
        status = copy_generation_status(self.directory)
        self.assertEqual(status["status"], "invalid_response")
        self.assertTrue(status["current"])
        self.assertEqual(status["model_calls"], 0)
        self.assertNotIn("payload", status)
        with self.assertRaisesRegex(ModelError, "未重复收费"):
            generate_copy_candidates(self.directory, provider)
        self.assertEqual(len(transport.calls), 1)
        result = generate_copy_candidates(self.directory, provider, force=True)
        self.assertEqual(result["copy"]["status"], "candidates")
        self.assertFalse(result["copy"]["selected"])
        self.assertEqual(copy_generation_status(self.directory)["status"], "none")
        snapshot = read_json(self.directory / CANDIDATES_FILE)
        self.assertEqual(snapshot["keyword_plan"], [])
        self.assertTrue(all(not row["copy_bundle"]["primary_keywords"] for row in snapshot["candidates"]))
        self.assertTrue(generate_copy_candidates(self.directory, provider)["cache_hit"])
        self.assertEqual(len(transport.calls), 2)

    def test_old_failure_is_stale_after_input_change(self):
        self.sparse_confirmed()
        write_json(self.directory / "output/copy-generation-diagnostic.json", {
            "status": "invalid", "context": {"input_fingerprint": "old", "evidence_version": 2},
            "payload": {"private": "never returned"}, "problems": ["old error"]})
        status = copy_generation_status(self.directory)
        self.assertEqual(status["status"], "stale")
        self.assertFalse(status["current"])
        self.assertEqual(status["errors"], [])


if __name__ == "__main__":
    unittest.main()
