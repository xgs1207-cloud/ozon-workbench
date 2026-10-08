"""Selected-SKU stages are local, explicit, cached and never publish anything."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector.ingest import ingest_capture
from models.fake import FakeProvider
from models.http_provider import HttpModelProvider, enrich_facts_from_inputs
from models.base import AnalysisRequest
from pipeline.context import PipelineGateError
from pipeline.guided_workflow import (
    ANALYSIS_FILE, CANDIDATES_FILE, COPY_FILE, STATE_FILE, analyze_selected_product,
    choose_copy_candidate, confirm_analysis, confirm_selected_copy, generate_copy_candidates,
    plan_selected_images, refresh_plan_metadata, workflow_status,
)
from pipeline.handlers import run_single_step
from pipeline.listing_form import read_json, write_json
from pipeline.selection import set_selected_keywords
from pipeline.sku_selection import set_selection


class CountingProvider(FakeProvider):
    def __init__(self):
        self.analysis_requests = []
        self.copy_requests = []
        self.plan_requests = []

    def analyze_product(self, request):
        self.analysis_requests.append(request)
        return super().analyze_product(request)

    def write_copy_ru(self, request):
        self.copy_requests.append(request)
        return super().write_copy_ru(request)

    def plan_images(self, request):
        self.plan_requests.append(request)
        return super().plan_images(request)


class GuidedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        result = ingest_capture(root / "products", {
            "source_url": "https://detail.1688.com/offer/1072823232979.html",
            "title_zh": "保温杯多规格",
            "skus": [
                {"sku_id": "S1", "sku_name": "红色小杯", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 10},
                {"sku_id": "S2", "sku_name": "蓝色大杯", "color_ru": "синий", "capacity": "1000 мл", "purchase_price_cny": 20},
            ],
        })
        self.directory = root / "products" / result["product_id"]
        self.provider = CountingProvider()
        source = read_json(self.directory / "input/source.json")
        source["extra"] = {"all_skus": deepcopy(source["skus"])}
        write_json(self.directory / "input/source.json", source)
        for name in ("main-images", "sku-images", "detail-images"):
            target = self.directory / "input" / name
            target.mkdir(exist_ok=True)
            (target / "01.png").write_bytes(b"fake image")
        set_selection(self.directory, include=["S1"])
        set_selected_keywords(self.directory, [{"keyword": "термокружка", "role": "core"},
                                               {"keyword": "дорожная кружка", "role": "secondary"}])

    def tearDown(self):
        self.temp.cleanup()

    def category(self, type_id=2001):
        write_json(self.directory / "input/category-selection.json", {
            "category_id": 1001, "type_id": type_id, "shop_id": "shop-a",
            "source": "ozon_seller_api", "confirmed_by_user": True,
        })

    def analysis(self):
        result = analyze_selected_product(self.directory, self.provider)
        return confirm_analysis(self.directory, result["input_fingerprint"])

    def candidates(self):
        self.analysis()
        self.category()
        return generate_copy_candidates(self.directory, self.provider)

    def copy(self):
        result = self.candidates()
        chosen = choose_copy_candidate(self.directory, result["copy"]["candidates"][0]["id"])
        return confirm_selected_copy(self.directory, chosen["copy"]["fingerprint"])

    def test_analysis_requires_explicit_selection_and_projects_only_selected_sku(self):
        (self.directory / "input/selected-skus.json").unlink()
        with self.assertRaisesRegex(ValueError, "先确认"):
            analyze_selected_product(self.directory, self.provider)
        set_selection(self.directory, include=["S1"])
        result = analyze_selected_product(self.directory, self.provider)
        self.assertEqual(result["analysis"]["status"], "ready")
        request = self.provider.analysis_requests[0]
        self.assertEqual(request.source["selected_sku_ids"], ["S1"])
        self.assertEqual(len(request.source["skus"]), 1)
        self.assertNotIn("extra", request.source)
        self.assertEqual(result["analysis"]["payload"]["facts"]["skus"][0]["sku_id"], "S1")
        self.assertEqual(result["analysis"]["payload"]["recommendation"]["decision"], "continue")
        self.assertFalse((self.directory / COPY_FILE).exists())

    def test_analysis_cached_and_requires_matching_explicit_confirmation(self):
        result = analyze_selected_product(self.directory, self.provider)
        cached = analyze_selected_product(self.directory, self.provider)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(len(self.provider.analysis_requests), 1)
        with self.assertRaisesRegex(ValueError, "变更"):
            confirm_analysis(self.directory, "bad fingerprint")
        confirmed = confirm_analysis(self.directory, result["input_fingerprint"])
        self.assertTrue(confirmed["analysis"]["confirmed"])
        set_selection(self.directory, include=["S2"])
        self.assertEqual(workflow_status(self.directory)["analysis"]["status"], "stale")
        with self.assertRaisesRegex(ValueError, "变更"):
            confirm_analysis(self.directory, result["input_fingerprint"])

    def test_copy_requires_analysis_confirmation_and_real_confirmed_category(self):
        analyze_selected_product(self.directory, self.provider)
        with self.assertRaisesRegex(ValueError, "确认当前"):
            generate_copy_candidates(self.directory, self.provider)
        self.analysis()
        with self.assertRaisesRegex(ValueError, "真实类目"):
            generate_copy_candidates(self.directory, self.provider)
        self.category(-1)
        with self.assertRaisesRegex(ValueError, "真实类目"):
            generate_copy_candidates(self.directory, self.provider)
        self.assertEqual(len(self.provider.copy_requests), 0)

    def test_three_candidates_cached_and_never_auto_selected(self):
        result = self.candidates()
        self.assertEqual(result["copy"]["status"], "candidates")
        self.assertEqual(len(result["copy"]["candidates"]), 3)
        self.assertEqual({request.extra["candidate_mode"] for request in self.provider.copy_requests},
                         {"search_first", "conversion_first", "differentiation_first"})
        self.assertTrue(all(request.source["selected_sku_ids"] == ["S1"] for request in self.provider.copy_requests))
        self.assertFalse((self.directory / COPY_FILE).exists())
        cached = generate_copy_candidates(self.directory, self.provider)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(len(self.provider.copy_requests), 3)

    def test_choose_edit_hashtags_and_explicit_copy_confirm_then_plan(self):
        result = self.candidates()
        with self.assertRaisesRegex(ValueError, "不存在"):
            choose_copy_candidate(self.directory, "unknown")
        choice = result["copy"]["candidates"][0]
        edited = choose_copy_candidate(self.directory, choice["id"], title_ru="Термокружка для поездок",
                                       hashtags=["#термокружка"])
        self.assertEqual(edited["copy"]["status"], "selected")
        self.assertEqual(edited["copy"]["payload"]["hashtags"], ["#термокружка"])
        with self.assertRaisesRegex(ValueError, "保存和确认"):
            plan_selected_images(self.directory, self.provider)
        confirm_selected_copy(self.directory, edited["copy"]["fingerprint"])
        planned = plan_selected_images(self.directory, self.provider)
        self.assertEqual(planned["plan"]["status"], "ready")
        self.assertEqual(len(planned["plan"]["payload"]["main_images"]), 1)
        self.assertEqual(planned["plan"]["payload"]["main_images"][0]["source_sku_id"], "S1")
        self.assertTrue(plan_selected_images(self.directory, self.provider)["cache_hit"])
        self.assertEqual(len(self.provider.plan_requests), 1)
        with self.assertRaisesRegex(ValueError, "校验"):
            choose_copy_candidate(self.directory, choice["id"], hashtags=["#abc"])
        self.assertTrue(workflow_status(self.directory)["copy"]["confirmed"])

    def test_category_and_keyword_changes_stale_copy_not_analysis(self):
        self.copy()
        plan_selected_images(self.directory, self.provider)
        self.category(2002)
        result = workflow_status(self.directory)
        self.assertTrue(result["analysis"]["confirmed"])
        self.assertEqual(result["copy"]["status"], "stale")
        self.assertEqual(result["plan"]["status"], "stale")
        with self.assertRaisesRegex(ValueError, "过期"):
            choose_copy_candidate(self.directory, result["copy"]["candidates"][0]["id"])
        generate_copy_candidates(self.directory, self.provider)
        set_selected_keywords(self.directory, ["кружка для чая"])
        self.assertTrue(workflow_status(self.directory)["analysis"]["confirmed"])
        self.assertEqual(workflow_status(self.directory)["copy"]["status"], "stale")

    def test_guided_plan_accepts_eleven_skus_without_switching_to_studio_cache_semantics(self):
        source = read_json(self.directory / "input/source.json")
        source["skus"] = [{**source["skus"][0], "sku_id": f"S{index}", "sku_name": f"规格{index}"}
                          for index in range(1, 12)]
        source["extra"]["all_skus"] = deepcopy(source["skus"])
        write_json(self.directory / "input/source.json", source)
        set_selection(self.directory, include=[f"S{index}" for index in range(1, 12)])
        self.copy()
        planned = plan_selected_images(self.directory, self.provider)
        self.assertEqual(planned["plan"]["status"], "ready")
        self.assertEqual(len(planned["plan"]["payload"]["main_images"]), 11)
        self.assertNotIn("studio_mode", planned["plan"]["payload"])
        self.assertTrue(plan_selected_images(self.directory, self.provider)["cache_hit"])
        self.assertEqual(len(self.provider.plan_requests), 1)
        set_selection(self.directory, include=["S2"])
        with self.assertRaisesRegex(ValueError, "变更"):
            refresh_plan_metadata(self.directory)

    def test_pipeline_reuses_current_chosen_copy_and_never_projects_legacy_design(self):
        result = self.copy()
        saved = (self.directory / COPY_FILE).read_bytes()
        write_json(self.directory / "output/ozon-ecommerce-design.json", {"stale": True})
        run_single_step(self.directory, "product_analysis", provider=self.provider)
        run_single_step(self.directory, "russian_copy", provider=self.provider)
        self.assertEqual((self.directory / COPY_FILE).read_bytes(), saved)
        self.assertEqual(len(self.provider.analysis_requests), 1)
        self.assertEqual(len(self.provider.copy_requests), 3)
        set_selection(self.directory, include=["S2"])
        with self.assertRaises(PipelineGateError):
            run_single_step(self.directory, "russian_copy", provider=self.provider)

    def test_local_slot_edits_refresh_metadata_without_model_calls_but_cannot_rescope(self):
        self.copy()
        result = plan_selected_images(self.directory, self.provider)
        plan = result["plan"]["payload"]
        plan["main_images"][0]["prompt"] += " 人工修改背景"
        write_json(self.directory / "output/image-plan.json", plan)
        self.assertEqual(workflow_status(self.directory)["plan"]["status"], "stale")
        self.assertEqual(refresh_plan_metadata(self.directory)["plan"]["status"], "ready")
        self.assertEqual(len(self.provider.plan_requests), 1)
        set_selection(self.directory, include=["S2"])
        with self.assertRaisesRegex(ValueError, "变更"):
            refresh_plan_metadata(self.directory)

    def test_analysis_never_accepts_provider_leaking_unselected_specs(self):
        class LeakyProvider(CountingProvider):
            def analyze_product(self, request):
                result = super().analyze_product(request)
                result["facts"]["skus"].append({**result["facts"]["skus"][0], "sku_id": "S2"})
                return result
        with self.assertRaisesRegex(ValueError, "未选规格"):
            analyze_selected_product(self.directory, LeakyProvider())
        self.assertFalse((self.directory / ANALYSIS_FILE).exists())

    def test_concurrent_same_analysis_makes_one_provider_call(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: analyze_selected_product(self.directory, self.provider), range(2)))
        self.assertEqual(len(self.provider.analysis_requests), 1)
        self.assertEqual(sum(row["cache_hit"] for row in results), 1)

    def test_files_roll_back_if_write_fails_and_published_product_is_not_editable(self):
        before = (self.directory / "input/source.json").read_bytes()
        real_write = write_json
        def failing_write(path, value):
            if path.name == "guided-workflow.json":
                raise OSError("simulated disk full")
            return real_write(path, value)
        with patch("pipeline.guided_workflow.write_json", failing_write):
            with self.assertRaises(OSError):
                analyze_selected_product(self.directory, self.provider)
        self.assertFalse((self.directory / ANALYSIS_FILE).exists())
        self.assertFalse((self.directory / STATE_FILE).exists())
        self.assertEqual((self.directory / "input/source.json").read_bytes(), before)
        write_json(self.directory / "status.json", {"api_write_count": 1})
        with self.assertRaisesRegex(ValueError, "已有 Ozon"):
            analyze_selected_product(self.directory, self.provider)

    def test_http_candidates_use_one_completion_and_cache_batch(self):
        self.analysis()
        self.category()
        fake = FakeProvider()
        from models import CopyRequest
        source = self.provider.analysis_requests[0].source
        bundle = fake.write_copy_ru(CopyRequest(product_id=self.directory.name, product_dir=self.directory,
                                              source=source, selected_keywords=[{"keyword": "термокружка"}]))
        response = {"candidates": [{"mode": mode, "copy_bundle": deepcopy(bundle["copy_bundle"])}
                                   for mode in ("search_first", "conversion_first", "differentiation_first")]}
        class ScriptedTransport:
            name = "offline-scripted"
            model = "offline"
            calls = 0
            def complete(self, **kwargs):
                self.calls += 1
                return json.dumps(response, ensure_ascii=False)
        transport = ScriptedTransport()
        provider = HttpModelProvider(transport, max_attempts=1)
        generated = generate_copy_candidates(self.directory, provider)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(generated["copy"]["candidates"]), 3)
        self.assertTrue(generate_copy_candidates(self.directory, provider)["cache_hit"])
        self.assertEqual(transport.calls, 1)
        snapshot = read_json(self.directory / CANDIDATES_FILE)
        self.assertTrue(snapshot["product"]["facts"])
        self.assertTrue(all(row["claim_evidence"] for row in snapshot["candidates"]))

    def test_unsupported_numbers_and_materials_cannot_be_added_by_edit(self):
        result = self.candidates()
        choice = result["copy"]["candidates"][0]
        for title in ("Термокружка ёмкостью 30 л", "Термокружка из силикона"):
            with self.assertRaisesRegex(ValueError, "未证实"):
                choose_copy_candidate(self.directory, choice["id"], title_ru=title)
        self.assertFalse((self.directory / COPY_FILE).exists())

    def test_tampered_candidate_snapshot_is_not_selectable(self):
        result = self.candidates()
        snapshot = read_json(self.directory / CANDIDATES_FILE)
        snapshot["candidates"][0]["copy_bundle"]["title_ru"] = "Термокружка другой кандидат"
        write_json(self.directory / CANDIDATES_FILE, snapshot)
        self.assertEqual(workflow_status(self.directory)["copy"]["status"], "stale")
        with self.assertRaisesRegex(ValueError, "过期"):
            choose_copy_candidate(self.directory, result["copy"]["candidates"][0]["id"])

    def test_early_enrichment_does_not_guess_unbranded_or_selected_sku_image(self):
        self.category()
        source = self.provider.analysis_requests[0].source if self.provider.analysis_requests else {
            "fact_collection_only": True, "sku_selection_explicit": True, "skus": [{"sku_id": "S1"}]}
        payload = {"facts": {"brand": None, "category_cn": None, "skus": [{"sku_id": "S1", "image_refs": []}]}}
        enrich_facts_from_inputs(payload, AnalysisRequest(product_id=self.directory.name,
                                                         product_dir=self.directory, source=source))
        self.assertIsNone(payload["facts"]["brand"])
        self.assertIsNone(payload["facts"]["category_cn"])
        self.assertEqual(payload["facts"]["skus"][0]["image_refs"], [])

    def test_form_metadata_and_attribute_values_do_not_stale_early_analysis(self):
        self.copy()
        write_json(self.directory / "input/human-confirmations.json", {
            "attributes": {"85": [10001]}, "sku_attributes": {"S1": {"10096": [20002]}},
            "category_form_scope": "new-scope", "category_form_confirmed_at": "2026-10-07T22:00:00Z",
            "category_confirmed_at": "2026-10-07T22:00:00Z",
            "attribute_provenance": {"attributes": {"85": {"source": "manual"}}},
        })
        status = workflow_status(self.directory)
        self.assertTrue(status["analysis"]["confirmed"])
        self.assertTrue(status["copy"]["confirmed"])
        write_json(self.directory / "input/human-confirmations.json", {"material": "硅胶"})
        self.assertEqual(workflow_status(self.directory)["analysis"]["status"], "stale")
        self.assertEqual(workflow_status(self.directory)["copy"]["status"], "stale")

    def test_saving_identical_supplier_fact_is_not_an_analysis_change(self):
        source = read_json(self.directory / "input/source.json")
        source["attributes_zh"] = {"material": "硅胶", "package_quantity": 2}
        write_json(self.directory / "input/source.json", source)
        self.analysis()
        write_json(self.directory / "input/human-confirmations.json", {
            "material": "硅胶", "package_quantity": 2, "listing_details": {"material": "硅胶", "package_quantity": 2}})
        self.assertTrue(workflow_status(self.directory)["analysis"]["confirmed"])
        write_json(self.directory / "input/human-confirmations.json", {"listing_details": {"material": None}})
        self.assertEqual(workflow_status(self.directory)["analysis"]["status"], "stale")

    def test_signed_source_and_video_tokens_are_absent_from_ai_context_and_fingerprint(self):
        source = read_json(self.directory / "input/source.json")
        source["source_url"] += "?share_token=private-share-token#private-fragment"
        source["videos"] = [{"url": "https://video.example/video.mp4?sign=secret-video-token", "role": "product"}]
        source["skus"][0]["image_url"] = "https://images.example/sku.png?sign=private-image-token"
        write_json(self.directory / "input/source.json", source)
        result = self.analysis()
        context = json.dumps(self.provider.analysis_requests[0].source, ensure_ascii=False)
        for token in ("private-share-token", "private-fragment", "secret-video-token", "private-image-token"):
            self.assertNotIn(token, context)
        self.assertNotIn("videos", self.provider.analysis_requests[0].source)
        source["videos"][0]["url"] = "https://video.example/new.mp4?sign=another-video-token"
        source["source_url"] = source["source_url"].replace("private-share-token", "new-shared-token")
        write_json(self.directory / "input/source.json", source)
        self.assertTrue(workflow_status(self.directory)["analysis"]["confirmed"])
        self.assertEqual(workflow_status(self.directory)["analysis"]["fingerprint"], result["analysis"]["fingerprint"])

    def test_input_change_during_analysis_never_commits_new_sku_facts_to_old_fingerprint(self):
        directory = self.directory
        class MutatingProvider(CountingProvider):
            def analyze_product(self, request):
                output = super().analyze_product(request)
                set_selection(directory, include=["S2"])  # legacy writer bypasses lock
                return output
        with self.assertRaisesRegex(ValueError, "生成期间"):
            analyze_selected_product(directory, MutatingProvider())
        self.assertFalse((directory / ANALYSIS_FILE).exists())
        self.assertFalse((directory / STATE_FILE).exists())
        set_selection(directory, include=["S1"])
        self.assertEqual(workflow_status(directory)["analysis"]["status"], "missing")

    def test_input_change_during_candidates_does_not_commit_old_keyword_batch(self):
        self.analysis()
        self.category()
        directory = self.directory
        class MutatingProvider(CountingProvider):
            def write_copy_candidates_ru(self, request):
                output = super().write_copy_candidates_ru(request)
                set_selected_keywords(directory, ["кружка для чая"])
                return output
        with self.assertRaisesRegex(ValueError, "生成期间"):
            generate_copy_candidates(directory, MutatingProvider())
        self.assertFalse((directory / CANDIDATES_FILE).exists())
        self.assertFalse((directory / COPY_FILE).exists())

    def test_input_change_during_plan_never_commits_wrong_scope(self):
        self.copy()
        directory = self.directory
        class MutatingProvider(CountingProvider):
            def plan_images(self, request):
                output = super().plan_images(request)
                set_selection(directory, include=["S2"])
                return output
        with self.assertRaisesRegex(ValueError, "生成期间"):
            plan_selected_images(directory, MutatingProvider())
        self.assertFalse((directory / "output/image-plan.json").exists())

    def test_api_write_started_during_analysis_blocks_local_commit(self):
        directory = self.directory
        class MutatingProvider(CountingProvider):
            def analyze_product(self, request):
                output = super().analyze_product(request)
                write_json(directory / "status.json", {"api_write_count": 1})
                return output
        with self.assertRaisesRegex(ValueError, "已有 Ozon"):
            analyze_selected_product(directory, MutatingProvider())
        self.assertFalse((directory / ANALYSIS_FILE).exists())


if __name__ == "__main__":
    unittest.main()
