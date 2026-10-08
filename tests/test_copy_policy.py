"""Keyword-preserving free composition, flexible descriptions and final titles."""
from copy import deepcopy
from unittest.mock import patch
import json
import unittest

from contracts import validate_contract
from models import CopyRequest, ModelError
from models.http_provider import HttpModelProvider
from pipeline.copy_evidence import COPY_EVIDENCE_VERSION, candidate_evidence
from pipeline.guided_workflow import (choose_copy_candidate, confirm_selected_copy, copy_fingerprint,
                                      workflow_status)
from pipeline.listing_document import variant_title
from pipeline.selection import set_selected_keywords
from rules.validate import (guided_copy_bundle_hint, keyword_copy_checks, keyword_phrase_present,
                            official_copy_checks, validate_copy_bundle, validate_guided_copy_bundle)
from tests.test_copy_evidence import Transport
from tests import test_guided_workflow as fixtures


CORE = "антистресс игрушка"
SECONDARY = "подарок на хэллоуин"
KEYWORDS = [{"keyword": CORE, "role": "core"}, {"keyword": SECONDARY, "role": "secondary"}]


def prose():
    return {"title_ru": "Антистресс игрушка в виде розового призрака",
            "description_ru": "✨ Розовый призрак с необычным дизайном.\n\n"
                              "• Антистресс игрушка в форме призрака.\n"
                              "• Подарок на хэллоуин: необычный внешний вид.\n\n"
                              "Выберите подходящую расцветку по фотографии товара.",
            "description_sections": {}, "hashtags": ["#антистресс"],
            "primary_keywords": [CORE], "secondary_keywords": [SECONDARY]}


class CopyPolicyTests(unittest.TestCase):
    def test_phrase_only_normalizes_case_and_whitespace(self):
        self.assertTrue(keyword_phrase_present("АНТИСТРЕСС   игрушка в виде призрака", CORE, at_start=True))
        for title in ("Розовая антистресс игрушка", "Антистресс розовая игрушка", "Игрушка антистресс",
                      "Антистресс игрушками", "Антистресс-игрушка"):
            with self.subTest(title=title):
                self.assertTrue(keyword_copy_checks(prose() | {"title_ru": title}, KEYWORDS))

    def test_title_only_main_phrase_but_description_can_include_secondary(self):
        self.assertEqual(keyword_copy_checks(prose(), KEYWORDS), [])
        coincidence = prose() | {"title_ru": prose()["title_ru"] + ", " + SECONDARY}
        self.assertEqual(keyword_copy_checks(coincidence, KEYWORDS), [])
        self.assertTrue(any("副关键词" in item for item in candidate_evidence(coincidence, [], KEYWORDS)["audit"]["advisory"]))
        self.assertTrue(keyword_copy_checks(prose() | {"primary_keywords": [CORE, SECONDARY]}, KEYWORDS))
        self.assertEqual(keyword_copy_checks(prose(), [*KEYWORDS, {"keyword": "игрушка", "role": "secondary"}]), [])
        self.assertTrue(keyword_copy_checks(prose() | {"description_ru": "Подарком на хэллоуин станет игрушка."}, KEYWORDS))
        self.assertTrue(keyword_copy_checks(prose() | {"title_ru": CORE + ", " + CORE}, KEYWORDS))

    def test_readable_emoji_description_no_fixed_five_sections(self):
        self.assertEqual(validate_guided_copy_bundle(prose()), [])
        self.assertTrue(validate_copy_bundle(prose()), "legacy five-section validation stays isolated")
        self.assertTrue(validate_guided_copy_bundle(prose(), allow_description_emoji=False))
        self.assertTrue(validate_guided_copy_bundle(prose() | {"title_ru": "✨ " + prose()["title_ru"]}))
        self.assertIn("空行", guided_copy_bundle_hint())
        self.assertIn("不改词形", guided_copy_bundle_hint())
        current = prose() | {"copy_policy_version": 3}
        self.assertFalse(any("emoji" in text for text in official_copy_checks(current)["advisory"]))
        excess = current | {"description_ru": current["description_ru"] + " ✨✨✨✨✨"}
        self.assertTrue(any("emoji 较多" in text for text in official_copy_checks(excess)["advisory"]))

    def test_verified_pink_attribute_can_coincide_with_secondary_title_query(self):
        keywords = [{"keyword": CORE, "role": "core"}, {"keyword": "розовый", "role": "secondary"}]
        facts = [{"id": "facts.skus.S1.name_cn", "value": "粉色幽灵", "verified": True,
                  "kind": "sku_descriptor", "allow_numeric": False}]
        copy = prose() | {"title_ru": "Антистресс игрушка — розовый призрак",
                          "secondary_keywords": ["розовый"]}
        report = candidate_evidence(copy, facts, keywords)
        review = report["audit"]["title_keyword_review"][0]
        self.assertEqual(review["status"], "supported_attribute_coincidence")
        self.assertEqual(review["fact_ids"], [facts[0]["id"]])
        self.assertFalse(any("副关键词" in item for item in report["audit"]["advisory"]))
        self.assertTrue(report["audit"]["core_coverage"])

    def test_malformed_keyword_arrays_report_errors_instead_of_bypassing_repair(self):
        for field in ("primary_keywords", "secondary_keywords"):
            for value in (12, None, {}, [False]):
                with self.subTest(field=field, value=value):
                    self.assertTrue(keyword_copy_checks(prose() | {field: value}, KEYWORDS))
                    with self.assertRaisesRegex(ValueError, "字符串数组"):
                        candidate_evidence(prose() | {field: value}, [], KEYWORDS)

    def test_current_description_contract_accepts_sparse_sections_legacy_does_not(self):
        document = {"product_id": "P000001", "description_ru": prose()["description_ru"],
                    "sections": {}, "section_evidence": []}
        self.assertTrue(validate_contract("description-ru", document))
        self.assertEqual(validate_contract("description-ru", document | {"copy_policy_version": 3}), [])

    def test_evidence_checks_main_keyword_and_spacing_for_usage(self):
        copy = prose() | {"title_ru": "АНТИСТРЕСС  игрушка в виде призрака"}
        report = candidate_evidence(copy, [], KEYWORDS)
        self.assertTrue(report["audit"]["core_coverage"])
        self.assertEqual(report["audit"]["copy_policy_version"], COPY_EVIDENCE_VERSION)
        with self.assertRaisesRegex(ValueError, "完整短语"):
            candidate_evidence(copy | {"title_ru": "Игрушка антистресс в виде призрака"}, [], KEYWORDS)

    def test_final_title_does_not_append_colour_or_use_raw_source_name(self):
        title = prose()["title_ru"]
        self.assertEqual(variant_title(title, {"color_ru": "зелёный", "name_ru": "Случайное название"},
                                       core_keyword=CORE), title)
        self.assertEqual(variant_title(title, {"listing_title_ru": "Случайное название",
                                               "listing_title_confirmed_by_user": True}, core_keyword=CORE), title)
        self.assertEqual(variant_title(title, {}, core_keyword=CORE,
                                       confirmed_title_ru="Антистресс игрушка в виде призрака"),
                         "Антистресс игрушка в виде призрака")
        with self.assertRaisesRegex(ValueError, "пол|主关键词"):
            variant_title(title, {}, core_keyword=CORE, confirmed_title_ru="Игрушка в виде призрака")

    def test_single_completion_prompt_and_current_shape(self):
        rows = [{"mode": mode, "copy_bundle": deepcopy(prose())}
                for mode in ("search_first", "conversion_first", "differentiation_first")]
        transport = Transport([{"candidates": rows}])
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            request = CopyRequest(product_id="P000001", product_dir=Path(temp), source={}, selected_keywords=KEYWORDS,
                                  extra={"verified_facts": [], "copy_policy_version": 3})
            result = HttpModelProvider(transport, max_attempts=1).write_copy_candidates_ru(request)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(result["candidates"]), 3)
        prompt = transport.calls[0]["user"]
        for text in ("三个候选标题都", "不换序", "空行", "emoji", "不编造算法权重"):
            self.assertIn(text, prompt)
        self.assertNotIn("五个 description_sections 每项", prompt)

    def test_compact_truncation_repair_does_not_restore_five_section_script(self):
        transport = Transport([])
        responses = ['{"candidates":[{"mode":"search_first"', json.dumps({"ok": True})]
        with patch.object(transport, "complete", side_effect=responses) as complete:
            provider = HttpModelProvider(transport, max_attempts=2)
            provider._call_json(task="russian_copy_candidates", user="u", validate=lambda data: [])
        prompt = complete.call_args_list[1].kwargs["user"]
        self.assertIn("description_sections 可为 {}", prompt)
        self.assertIn("不要截断主关键词", prompt)
        self.assertNotIn("1200–2000", prompt)


class CopyPolicyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GuidedWorkflowTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_edited_main_keyword_and_secondary_title_cannot_bypass_policy(self):
        set_selected_keywords(self.fixture.directory, KEYWORDS)
        result = self.fixture.candidates()
        candidate = result["copy"]["candidates"][0]
        for title in ("Розовая антистресс игрушка", "Антистресс розовая игрушка"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                choose_copy_candidate(self.fixture.directory, candidate["id"], title_ru=title)
        choice = choose_copy_candidate(self.fixture.directory, candidate["id"], title_ru=prose()["title_ru"],
                                        description_ru=prose()["description_ru"])
        confirmed = confirm_selected_copy(self.fixture.directory, choice["copy"]["fingerprint"])
        self.assertTrue(confirmed["copy"]["confirmed"])

    def test_policy_version_changes_fingerprint_and_stales_old_candidates(self):
        self.fixture.candidates()
        before = copy_fingerprint(self.fixture.directory)
        with patch("pipeline.guided_workflow.COPY_EVIDENCE_VERSION", COPY_EVIDENCE_VERSION + 1):
            self.assertNotEqual(copy_fingerprint(self.fixture.directory), before)
            self.assertEqual(workflow_status(self.fixture.directory)["copy"]["status"], "stale")


if __name__ == "__main__":
    unittest.main()
