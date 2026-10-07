"""Offline workspace, provenance and explicit-fee recovery regression tests."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from collector.ingest import ingest_capture
from contracts import validate_contract
from pipeline import image_jobs
from pipeline.captured_images import validate_captured_image
from pipeline.guided_review import approve, problems, status
from pipeline.image_qc import run_image_qc
from pipeline.listing_form import read_json, write_json
from pipeline.media_selection import selected_image_specs
from pipeline.media_workspace import adopt_original, create_set, generate_set, media_state, retry_image
from pipeline.oss_cos import CosError, CosObjectStorage, image_publication_binding
from pipeline.oss_local import LocalObjectStorage
from pipeline.sku_selection import set_selection
from tests import test_image_jobs as fixtures
from tests import test_oss_cos as cos_fixtures


class MediaWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImageJobTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = self.fixture.directory
        self.ref = self.fixture.ref
        def clear_pending():
            with image_jobs._GUARD:
                for row in image_jobs._journal(self.directory)["jobs"]:
                    image_jobs._ACTIVE.discard(row["id"])
        self.addCleanup(clear_pending)

    def complete(self, slot, generator=None):
        job = image_jobs.enqueue_image(self.directory, slot, generator=generator or fixtures.OfflineGenerator(), dispatch=False)
        image_jobs.run_image_job(self.directory, job["id"], generator or fixtures.OfflineGenerator())
        return next(row for row in image_jobs.list_image_jobs(self.directory) if row["id"] == job["id"])

    def make_set(self, count=3, **kwargs):
        return create_set(self.directory, count=count, reference_ids=[self.ref], **kwargs)

    def collect_format(self, *, format="JPEG", size=(600, 600)):
        from PIL import Image
        data = io.BytesIO()
        Image.new("RGB", size, (190, 100, 80)).save(data, format=format)
        suffix = {"JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}[format]
        with patch("collector.ingest._download_remote_image", return_value=(data.getvalue(), suffix)):
            result = ingest_capture(self.directory.parent, {"source_url": "https://detail.1688.com/offer/987654321.html",
                "title_zh": "真实原图商品", "collection_mode": "all_skus",
                "skus": [{"sku_id": "S1", "name": "粉色", "purchase_price_cny": 2,
                          "image_url": "https://cbu01.alicdn.com/img/real.jpg"}]})
        directory = self.directory.parent / result["product_id"]
        set_selection(directory, include=["S1"])
        return directory, data.getvalue()

    def test_set_counts_and_separate_workspaces_preserve_selected_single(self):
        single = self.fixture.slot()
        self.complete(single)
        image_jobs.select_images(self.directory, [single])
        approve(self.directory, "images")
        old = deepcopy(read_json(self.directory / image_jobs.REPORT_FILE))
        first, second = self.make_set(count=1), self.make_set(count=12)
        state = media_state(self.directory)
        self.assertEqual(state["workspaces"]["single"], [single])
        self.assertEqual(len(state["workspaces"]["set"]), 13)
        self.assertEqual(state["selected_slots"], [single])
        self.assertEqual(state["image_generation"], old)
        self.assertTrue(status(self.directory)["sections"]["images"]["approved"])
        self.assertEqual({row["total"] for row in state["sets"]}, {1, 12})
        self.assertNotEqual(first["set_id"], second["set_id"])
        self.assertEqual(validate_contract("image-plan", state["image_plan"]), [])

    def test_fifty_slots_can_queue_and_repeat_set_does_not_recharge(self):
        created = self.make_set(count=50)
        generators = {}
        def factory(slot):
            generators[slot] = fixtures.OfflineGenerator()
            return generators[slot]
        response = generate_set(self.directory, created["set_id"], generator_factory=factory, dispatch=False)
        self.assertEqual(len(response["accepted"]), 50)
        self.assertEqual(response["rejected"], [])
        self.assertEqual(response["queue_capacity"], 256)
        self.assertEqual(media_state(self.directory)["sets"][0]["queued"], 50)
        self.assertTrue(all(len(generator.calls) == 0 for generator in generators.values()))
        again = generate_set(self.directory, created["set_id"], generator_factory=factory, dispatch=False)
        self.assertEqual(again["model_requests_queued"], 0)
        self.assertEqual(len(again["skipped"]), 50)

    def test_set_first_main_and_distinct_editable_prompts_preserve_user_base(self):
        base = "请用清新自然的风格展示商品"
        created = self.make_set(count=3, prompt=base)
        plan = created["image_plan"]
        rows = [row for row in image_jobs._slots(plan) if row["set_id"] == created["set_id"]]
        self.assertEqual(rows[0]["image_type"], "main")
        self.assertEqual(rows[0]["source_sku_id"], "S1")
        self.assertEqual([row["image_type"] for row in rows[1:]], ["detail", "detail"])
        self.assertEqual(created["set"]["base_prompt"], base)
        self.assertTrue(all(row["prompt"].startswith(base) for row in rows))
        self.assertEqual(len({row["prompt"] for row in rows}), 3)
        self.assertTrue(all(row["purpose"] in row["prompt"] for row in rows))

    def test_queue_capacity_rejects_whole_unstarted_batch_without_rpc(self):
        created = self.make_set()
        with patch.object(image_jobs, "_ACTIVE", {str(index) for index in range(255)}):
            response = generate_set(self.directory, created["set_id"], generator_factory=lambda slot: self.fail("no paid request"), dispatch=False)
        self.assertEqual(response["accepted"], [])
        self.assertEqual(len(response["rejected"]), 3)
        self.assertEqual(response["model_requests_queued"], 0)

    def test_set_progress_is_true_count_and_skips_completed_failed_unknown(self):
        created = self.make_set()
        first, second, third = created["slots"]
        self.complete(first)
        self.complete(second, fixtures.OfflineGenerator(fail="RightAPI 生图返回 HTTP 502；未自动重试"))
        self.complete(third, fixtures.OfflineGenerator(fail="fixture ordinary failure"))
        response = generate_set(self.directory, created["set_id"], generator_factory=lambda slot: self.fail("must skip"), dispatch=False)
        self.assertEqual(response["jobs"], [])
        self.assertEqual(len(response["skipped"]), 3)
        progress = media_state(self.directory)["sets"][0]
        self.assertEqual((progress["completed"], progress["unknown"], progress["failed"]), (1, 1, 1))
        self.assertNotIn("percentage", progress)

    def test_failed_retry_requires_new_charge_ack_and_retains_other_images(self):
        created = self.make_set(count=2)
        first, second = created["slots"]
        self.complete(first)
        self.complete(second, fixtures.OfflineGenerator(fail="RightAPI 生图返回 HTTP 502；未自动重试"))
        first_receipt = read_json(self.directory / image_jobs.REPORT_FILE)["files"][0]
        first_bytes = (self.directory / first_receipt["path"]).read_bytes()
        generator = fixtures.OfflineGenerator()
        with self.assertRaisesRegex(ValueError, "付费"):
            retry_image(self.directory, second, confirm_new_charge=False, generator=generator, dispatch=False)
        self.assertEqual(generator.calls, [])
        retried = retry_image(self.directory, second, confirm_new_charge=True, generator=generator, dispatch=False)
        self.assertTrue(retried["retry_of"])
        image_jobs.run_image_job(self.directory, retried["id"], generator)
        self.assertEqual((self.directory / first_receipt["path"]).read_bytes(), first_bytes)
        self.assertEqual(media_state(self.directory)["sets"][0]["completed"], 2)

    def test_download_stage_reports_actual_activity_not_fake_percent(self):
        slot = self.fixture.slot()
        generator = fixtures.OfflineGenerator()
        observed = []
        class Transport:
            def _download(inner, *args):
                job = image_jobs.list_image_jobs(self.directory)[-1]
                observed.append(job["stage"])
                return b"offline"
        generator.transport = Transport()
        original_generate = generator.generate
        def generate(request):
            generator.transport._download("offline://no-network")
            return original_generate(request)
        generator.generate = generate
        job = image_jobs.enqueue_image(self.directory, slot, generator=generator, dispatch=False)
        stages = []
        original_mark = image_jobs._mark
        def record_mark(*args, **kwargs):
            if "stage" in kwargs:
                stages.append(kwargs["stage"])
            return original_mark(*args, **kwargs)
        with patch.object(image_jobs, "_mark", side_effect=record_mark):
            image_jobs.run_image_job(self.directory, job["id"], generator)
        self.assertEqual(observed, ["downloading"])
        self.assertTrue({"provider_request", "downloading", "verifying", "committing", "complete"}.issubset(stages))
        self.assertNotIn("percentage", image_jobs.list_image_jobs(self.directory)[-1])

    def test_count_validation_and_atomic_set_creation_failure(self):
        old = read_json(self.directory / image_jobs.PLAN_FILE)
        for count in (0, 51, True, 1.5):
            with self.assertRaises(ValueError):
                self.make_set(count=count)
        original = image_jobs.add_image_slot
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("offline injected failure")
            return original(*args, **kwargs)
        with patch.object(image_jobs, "add_image_slot", side_effect=fail_second), self.assertRaises(ValueError):
            self.make_set()
        self.assertEqual(read_json(self.directory / image_jobs.PLAN_FILE), old)
        self.assertFalse((self.directory / image_jobs.REPORT_FILE).exists())

    def test_original_adoption_zero_model_calls_has_distinct_provenance(self):
        with patch("models.load_web_image_generator", side_effect=AssertionError("no model calls")):
            result = adopt_original(self.directory, reference_id=self.ref)
        receipt = result["adopted"]
        proof = receipt["capture_receipt"]
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(receipt["generator"], "captured-original")
        self.assertIsNone(receipt["model"])
        self.assertEqual(proof["source_sku_ids"], ["S1"])
        self.assertEqual((self.directory / receipt["path"]).read_bytes(), (self.directory / proof["source_path"]).read_bytes())
        self.assertEqual(hashlib.sha256((self.directory / receipt["path"]).read_bytes()).hexdigest(), receipt["sha256"])
        plan = image_jobs.select_images(self.directory, [result["slot"]])
        self.assertEqual(validate_captured_image(self.directory, selected_image_specs(plan)[0], receipt), [])
        self.assertEqual(problems(self.directory, "images"), [])
        self.assertFalse(status(self.directory)["sections"]["images"]["approved"])
        self.assertTrue(approve(self.directory, "images")["sections"]["images"]["approved"])

    def test_mix_original_and_ai_order_human_review_and_cos_binding(self):
        original = adopt_original(self.directory, reference_id=self.ref)
        ai_slot = self.fixture.slot()
        self.complete(ai_slot)
        plan = image_jobs.select_images(self.directory, [ai_slot, original["slot"]])
        self.assertEqual([row.get("origin") for row in selected_image_specs(plan)], ["ai", "captured"])
        approve(self.directory, "images")
        client = cos_fixtures.FakeCosClient()
        storage = CosObjectStorage(client, bucket="test-1250000000", region="ap-hongkong", max_attempts=1)
        response = storage.publish_product(self.directory)
        self.assertEqual(response["uploaded"], 2)
        manifest = read_json(self.directory / "output/image-public-urls.json")
        self.assertEqual(manifest["files"][original["slot"]]["origin"], "captured")
        self.assertTrue(image_publication_binding(self.directory, manifest)["ok"])
        manifest["files"][original["slot"]].pop("capture_receipt")
        self.assertFalse(image_publication_binding(self.directory, manifest)["ok"])

    def test_original_jpeg_square_is_adoptable_and_not_forced_to_regenerate(self):
        directory, raw = self.collect_format()
        result = adopt_original(directory, reference_id="sku-001")
        self.assertTrue(result["adopted"]["path"].endswith(".jpg"))
        image_jobs.select_images(directory, [result["slot"]])
        qc = run_image_qc(directory, produces_final_images=True)
        self.assertEqual(qc["critical_failures"], [])
        self.assertFalse(qc["regenerate_needed"])
        self.assertEqual(qc["technical_checks"][0]["format"], "jpeg")
        self.assertTrue(any("非强制" in text for text in qc["suggestions"]))
        approve(directory, "images")
        storage = LocalObjectStorage(directory.parent / "static", "https://img.example.test")
        response = storage.publish_product(directory)
        url = response["urls"][result["slot"]]
        self.assertTrue(url.endswith(".jpg"), url)
        self.assertIn(hashlib.sha256(raw).hexdigest(), url)
        self.assertEqual(Path(response["results"][0]["path"]).read_bytes(), raw)
        self.assertTrue(image_publication_binding(directory, read_json(directory / "output/image-public-urls.json"))["ok"])

    def test_webp_original_enters_library_but_actual_api_format_blocks_publish_review(self):
        directory, _ = self.collect_format(format="WEBP")
        result = adopt_original(directory, reference_id="sku-001")
        image_jobs.select_images(directory, [result["slot"]])
        qc = run_image_qc(directory, produces_final_images=True)
        self.assertIn("unsupported_format", qc["critical_failures"])
        with self.assertRaises(ValueError):
            approve(directory, "images")

    def test_tampered_original_copy_or_seal_is_never_publishable(self):
        result = adopt_original(self.directory, reference_id=self.ref)
        image_jobs.select_images(self.directory, [result["slot"]])
        approve(self.directory, "images")
        copy = self.directory / result["adopted"]["path"]
        copy.write_bytes(copy.read_bytes() + b"tampered")
        with self.assertRaises(ValueError):
            image_jobs.select_images(self.directory, [result["slot"]])
        self.assertTrue(problems(self.directory, "images"))
        client = cos_fixtures.FakeCosClient()
        with self.assertRaises(CosError):
            CosObjectStorage(client, bucket="test-1250000000", region="ap-hongkong").publish_product(self.directory)
        self.assertFalse(any(action == "put_object" for action, _ in client.calls))

    def test_modified_captured_source_is_rejected_before_adoption(self):
        reference = media_state(self.directory)["captured_reference_images"][0]
        path = self.directory / reference["path"]
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "封存"):
            adopt_original(self.directory, reference_id=self.ref)
        self.assertFalse((self.directory / image_jobs.REPORT_FILE).exists())

    def test_original_cannot_be_overwritten_by_paid_generator(self):
        result = adopt_original(self.directory, reference_id=self.ref)
        gen = fixtures.OfflineGenerator()
        with self.assertRaisesRegex(ValueError, "另建"):
            image_jobs.enqueue_image(self.directory, result["slot"], generator=gen, dispatch=False)
        self.assertEqual(gen.calls, [])
        with self.assertRaises(ValueError):
            adopt_original(self.directory, reference_id=self.ref, workspace="set", set_id="missing")

    def test_sku_owned_original_infers_owner_not_shared_and_unselected_owner_rejects(self):
        reference = next(row for row in media_state(self.directory)["captured_reference_images"] if row["id"] == self.ref)
        data = (self.directory / reference["path"]).read_bytes()
        from PIL import Image
        blue = io.BytesIO()
        Image.new("RGB", (900, 1200), (10, 20, 220)).save(blue, format="PNG")
        with patch("collector.ingest._download_remote_image", side_effect=lambda url: (blue.getvalue() if "blue" in url else data, ".png")):
            result = ingest_capture(self.directory.parent, {"source_url": "https://detail.1688.com/offer/11223344.html",
                "title_zh": "多规格商品", "collection_mode": "all_skus", "skus": [
                    {"sku_id": "S1", "name": "红色", "purchase_price_cny": 2, "image_url": "https://cbu01.alicdn.com/img/red.png"},
                    {"sku_id": "S2", "name": "蓝色", "purchase_price_cny": 2, "image_url": "https://cbu01.alicdn.com/img/blue.png"}]})
        directory = self.directory.parent / result["product_id"]
        set_selection(directory, include=["S1", "S2"])
        result = adopt_original(directory, reference_id="sku-002", role="variant_main")
        self.assertEqual(result["adopted"]["source_sku_id"], "S2")
        row = next(row for row in image_jobs._slots(result["image_plan"]) if row["slot"] == result["slot"])
        self.assertFalse(row["shared_across_variants"])
        self.assertEqual(row["sku_identity"], "S2")
        self.assertEqual(row["capture_receipt"]["source_sku_ids"], ["S2"])
        set_selection(directory, include=["S1"])
        with self.assertRaisesRegex(ValueError, "规格"):
            adopt_original(directory, reference_id="sku-002")

    def test_four_or_duplicate_refs_are_not_silently_trimmed(self):
        for refs in ([self.ref] * 4, [self.ref] * 2):
            with self.assertRaises(ValueError):
                image_jobs.add_image_slot(self.directory, prompt="真实商品图片", reference_ids=refs)

    def test_old_full_planner_cannot_clear_single_and_set_workspaces(self):
        self.fixture.slot()
        before = (self.directory / image_jobs.PLAN_FILE).read_bytes()
        from pipeline.guided_workflow import plan_selected_images
        class Provider:
            def plan_images(inner, request):
                self.fail("must not make paid planner call")
        with self.assertRaisesRegex(ValueError, "不能.*覆盖"):
            plan_selected_images(self.directory, Provider(), force=True)
        self.assertEqual((self.directory / image_jobs.PLAN_FILE).read_bytes(), before)

    def test_failed_slot_state_survives_more_than_one_hundred_later_jobs(self):
        rows = [{"id": "failed-before", "slot": "old", "status": "unknown"}]
        rows += [{"id": f"later-{i}", "slot": "new", "status": "completed"} for i in range(110)]
        write_json(self.directory / image_jobs.JOBS_FILE, {"jobs": rows})
        self.assertTrue(any(row["id"] == "failed-before" for row in image_jobs.list_image_jobs(self.directory)))


class MediaApiTests(unittest.TestCase):
    setUp = MediaWorkspaceTests.setUp
    def test_router_create_set_and_original_without_paid_calls(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import workbench_media_api
        app = FastAPI()
        app.include_router(workbench_media_api.router)
        with patch.object(workbench_media_api, "directory_for", return_value=self.directory), TestClient(app) as client:
            base = f"/api/workbench/products/{self.directory.name}/guided/media"
            with patch("models.load_web_image_generator", side_effect=AssertionError("no paid calls")):
                result = client.post(base + "/sets", json={"count": 2, "reference_ids": [self.ref]})
                self.assertEqual(result.status_code, 200, result.text)
                result = client.post(base + "/adopt", json={"reference_id": self.ref})
                self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(client.get(base).json()["sets"][0]["total"], 2)
            self.assertEqual(client.post(base + "/slots/unknown/retry", json={}).status_code, 422)
            self.assertEqual(client.post(base + "/sets", json={"count": 0, "reference_ids": [self.ref]}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
