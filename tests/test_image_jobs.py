"""Offline durable single-image studio regression coverage; no model calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from collector.ingest import ingest_capture
from models.base import ModelError
from pipeline import image_jobs
from pipeline.guided_review import approve, problems, slot_fingerprint, update_plan_slot
from pipeline.image_probe import write_solid_png
from pipeline.listing_form import read_json, write_json
from pipeline.media_selection import selected_image_specs
from pipeline.sku_selection import set_selection


class OfflineGenerator:
    name = "rightapi"
    produces_final_images = True

    def __init__(self, *, entered=None, resume=None, fail=None, color=(200, 20, 30)):
        self.entered, self.resume, self.fail, self.color = entered, resume, fail, color
        self.calls = []

    def generate(self, request):
        self.calls.append((request.slot, request.product_dir))
        if self.entered:
            self.entered.set()
        if self.resume:
            if not self.resume.wait(5):
                raise RuntimeError("fixture timed out")
        if self.fail:
            raise ModelError(self.fail)
        plan = read_json(request.product_dir / "output/image-plan.json")
        spec = [*plan["main_images"], *plan["detail_images"]][0]
        path = write_solid_png(request.product_dir / spec["output_path"], 900, 1200, rgb=self.color)
        return {"generator": self.name, "model": "offline-only", "final_images": True,
                "generated": [{"slot": request.slot, "path": spec["output_path"], "bytes": path.stat().st_size}], "skipped": []}


class ImageJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        image = write_solid_png(root / "original.png", 900, 1200).read_bytes()
        with patch("collector.ingest._download_remote_image", return_value=(image, ".png")):
            captured = ingest_capture(root / "products", {
                "source_url": "https://detail.1688.com/offer/123456789.html", "collection_mode": "all_skus",
                "title_zh": "测试商品", "skus": [{"sku_id": "S1", "name": "粉色", "purchase_price_cny": 2.5,
                                                     "image_url": "https://cbu01.alicdn.com/img/test.jpg"}]})
        self.directory = root / "products" / captured["product_id"]
        set_selection(self.directory, include=["S1"])
        self.ref = "sku-001"

    def slot(self, prompt="用柔和光线近景展示所选商品真实细节", role="detail"):
        return image_jobs.add_image_slot(self.directory, prompt=prompt, reference_ids=[self.ref], role=role)["slot"]

    def enqueue(self, slot, generator=None):
        gen = generator or OfflineGenerator()
        job = image_jobs.enqueue_image(self.directory, slot, generator=gen, dispatch=False)
        return job, gen

    def generate_slot(self, slot, generator=None):
        job, gen = self.enqueue(slot, generator)
        image_jobs.run_image_job(self.directory, job["id"], gen)
        return next(row for row in image_jobs.list_image_jobs(self.directory) if row["id"] == job["id"])

    def test_single_image_without_copy_or_whole_plan_approval(self):
        slot = self.slot()
        self.assertFalse((self.directory / "output/copy-ru.json").exists())
        job = self.generate_slot(slot)
        self.assertEqual(job["status"], "completed", job)
        image_jobs.select_images(self.directory, [slot])
        self.assertEqual(problems(self.directory, "images"), [])
        review = approve(self.directory, "images")
        self.assertTrue(review["sections"]["images"]["approved"])
        self.assertTrue(review["sections"]["image_plan"]["approved"])

    def test_independent_concurrent_slots_merge_and_leave_lock_available(self):
        first, second = self.slot(), self.slot()
        entered, resume = threading.Event(), threading.Event()
        slow = OfflineGenerator(entered=entered, resume=resume)
        job, _ = self.enqueue(first, slow)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(image_jobs.run_image_job, self.directory, job["id"], slow)
            self.assertTrue(entered.wait(3))
            other = self.generate_slot(second)
            self.assertEqual(other["status"], "completed", other)
            self.slot("另一个图位可继续编辑")
            resume.set()
            future.result(5)
        files = read_json(self.directory / image_jobs.REPORT_FILE)["files"]
        self.assertEqual({row["slot"] for row in files}, {first, second})
        self.assertNotEqual(slow.calls[0][1], self.directory)

    def test_same_slot_duplicate_rejected_without_second_call(self):
        slot = self.slot()
        job, generator = self.enqueue(slot)
        with self.assertRaises(image_jobs.ImageJobConflict):
            self.enqueue(slot)
        image_jobs.run_image_job(self.directory, job["id"], generator)
        self.assertEqual(len(generator.calls), 1)

    def test_failed_redo_keeps_current_and_other_good_images_selectable(self):
        first, second = self.slot(), self.slot()
        self.generate_slot(first); self.generate_slot(second)
        plan = image_jobs.select_images(self.directory, [first, second])
        before_files = deepcopy(read_json(self.directory / image_jobs.REPORT_FILE)["files"])
        before_bytes = {row["slot"]: (self.directory / row["path"]).read_bytes() for row in before_files}
        update_plan_slot(self.directory, slot=first, prompt="新的构图草稿", reference_ids=[self.ref])
        job = self.generate_slot(first, OfflineGenerator(fail="RightAPI 生图返回 HTTP 502；未自动重试，请先核对调用记录"))
        self.assertEqual(job["status"], "unknown", job)
        self.assertEqual(read_json(self.directory / image_jobs.REPORT_FILE)["files"], before_files)
        for row in before_files:
            self.assertEqual((self.directory / row["path"]).read_bytes(), before_bytes[row["slot"]])
        plan = image_jobs.select_images(self.directory, [second, first])
        adopted = selected_image_specs(plan)
        self.assertEqual([row["slot"] for row in adopted], [second, first])
        self.assertEqual(adopted[1]["prompt"], "用柔和光线近景展示所选商品真实细节")
        self.assertEqual(problems(self.directory, "images"), [])

    def test_successful_redo_changes_only_one_and_keeps_history(self):
        first, second = self.slot(), self.slot()
        self.generate_slot(first); self.generate_slot(second)
        before = {row["slot"]: row for row in read_json(self.directory / image_jobs.REPORT_FILE)["files"]}
        update_plan_slot(self.directory, slot=first, prompt="改变角度并放大真实纹理", reference_ids=[self.ref])
        job = self.generate_slot(first, OfflineGenerator(color=(20, 90, 170)))
        self.assertEqual(job["status"], "completed", job)
        after = {row["slot"]: row for row in read_json(self.directory / image_jobs.REPORT_FILE)["files"]}
        self.assertEqual(before[second], after[second])
        self.assertNotEqual(before[first]["sha256"], after[first]["sha256"])
        self.assertEqual(len(list((self.directory / "output/image-history" / first).glob("*.png"))), 1)

    def test_stale_queued_request_does_not_call_provider(self):
        slot = self.slot()
        job, gen = self.enqueue(slot)
        update_plan_slot(self.directory, slot=slot, prompt="更新提示词", reference_ids=[self.ref])
        image_jobs.run_image_job(self.directory, job["id"], gen)
        self.assertEqual(gen.calls, [])
        self.assertEqual(image_jobs.list_image_jobs(self.directory)[0]["status"], "stale")

    def test_edit_during_generation_does_not_overwrite_good_result(self):
        slot = self.slot()
        self.generate_slot(slot)
        before = read_json(self.directory / image_jobs.REPORT_FILE)
        entered, resume = threading.Event(), threading.Event()
        slow = OfflineGenerator(entered=entered, resume=resume, color=(10, 20, 30))
        job, _ = self.enqueue(slot, slow)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(image_jobs.run_image_job, self.directory, job["id"], slow)
            self.assertTrue(entered.wait(3))
            update_plan_slot(self.directory, slot=slot, prompt="生成期间修改的新提示词", reference_ids=[self.ref])
            resume.set(); future.result(5)
        self.assertEqual(read_json(self.directory / image_jobs.REPORT_FILE), before)
        self.assertEqual(image_jobs.list_image_jobs(self.directory)[-1]["status"], "stale")

    def test_reference_bytes_changed_while_running_cannot_be_adopted(self):
        slot = self.slot()
        entered, resume = threading.Event(), threading.Event()
        slow = OfflineGenerator(entered=entered, resume=resume)
        job, _ = self.enqueue(slot, slow)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(image_jobs.run_image_job, self.directory, job["id"], slow)
            self.assertTrue(entered.wait(3))
            reference = read_json(self.directory / image_jobs.PLAN_FILE)["detail_images"][0]["reference_product_images"][0]
            write_solid_png(self.directory / reference, 900, 1200, rgb=(5, 6, 7))
            resume.set(); future.result(5)
        self.assertEqual(image_jobs.list_image_jobs(self.directory)[0]["status"], "stale")
        self.assertFalse((self.directory / image_jobs.REPORT_FILE).exists())

    def test_same_job_cannot_dispatch_twice_after_success(self):
        slot = self.slot()
        job, generator = self.enqueue(slot)
        image_jobs.run_image_job(self.directory, job["id"], generator)
        image_jobs.run_image_job(self.directory, job["id"], generator)
        self.assertEqual(len(generator.calls), 1)

    def test_queued_owner_restart_does_not_dispatch_model(self):
        slot = self.slot()
        job, generator = self.enqueue(slot)
        journal = read_json(self.directory / image_jobs.JOBS_FILE)
        journal["jobs"][0]["instance"] = "previous-process"
        write_json(self.directory / image_jobs.JOBS_FILE, journal)
        with image_jobs._GUARD:
            image_jobs._ACTIVE.discard(job["id"])
        self.assertEqual(image_jobs.list_image_jobs(self.directory)[0]["status"], "failed")
        image_jobs.run_image_job(self.directory, job["id"], generator)
        self.assertEqual(generator.calls, [])

    def test_restart_reports_unknown_and_never_replays_paid_job(self):
        slot = self.slot()
        job, gen = self.enqueue(slot)
        journal = read_json(self.directory / image_jobs.JOBS_FILE)
        journal["jobs"][0].update(status="running", started_at="2026-10-08", instance="previous-process")
        write_json(self.directory / image_jobs.JOBS_FILE, journal)
        with image_jobs._GUARD:
            image_jobs._ACTIVE.discard(job["id"])
        rows = image_jobs.list_image_jobs(self.directory)
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(gen.calls, [])

    def test_zero_selected_images_is_draft_not_fake_qc(self):
        image_jobs.select_images(self.directory, [])
        self.assertFalse((self.directory / "output/image-qc-report.json").exists())
        self.assertEqual(problems(self.directory, "images"), [])
        self.assertTrue(approve(self.directory, "images")["sections"]["images"]["approved"])

    def test_one_selected_slot_ignores_many_unused_empty_slots(self):
        good = self.slot()
        self.generate_slot(good)
        for _ in range(10):
            self.slot()
        image_jobs.select_images(self.directory, [good])
        self.assertEqual(problems(self.directory, "images"), [])

    def test_unused_draft_edit_and_completed_job_do_not_invalidate_adopted_review(self):
        chosen, unused = self.slot(), self.slot()
        self.generate_slot(chosen)
        image_jobs.select_images(self.directory, [chosen])
        self.assertTrue(approve(self.directory, "images")["sections"]["images"]["approved"])
        update_plan_slot(self.directory, slot=unused, prompt="另一个未选择图片的草稿", reference_ids=[self.ref])
        from pipeline.guided_review import status
        self.assertTrue(status(self.directory)["sections"]["images"]["approved"])
        job = self.generate_slot(unused)
        self.assertEqual(job["status"], "completed", job)
        self.assertTrue(status(self.directory)["sections"]["images"]["approved"])

    def test_failed_selected_draft_redo_does_not_invalidate_adopted_review(self):
        chosen = self.slot()
        self.generate_slot(chosen)
        image_jobs.select_images(self.directory, [chosen])
        self.assertTrue(approve(self.directory, "images")["sections"]["images"]["approved"])
        update_plan_slot(self.directory, slot=chosen, prompt="失败时仍可以使用之前的已确认图片", reference_ids=[self.ref])
        self.generate_slot(chosen, OfflineGenerator(fail="RightAPI 生图返回 HTTP 502；未自动重试"))
        from pipeline.guided_review import status
        self.assertTrue(status(self.directory)["sections"]["images"]["approved"])

    def test_four_references_rejected_not_silently_trimmed(self):
        with self.assertRaisesRegex(ValueError, "1–3"):
            image_jobs.add_image_slot(self.directory, prompt="展示真实商品", reference_ids=["sku-001", "fake2", "fake3", "fake4"])

    def other_sku_reference(self):
        source = read_json(self.directory / "input/source.json")
        relative = "input/sku-images/002-S2.png"
        write_solid_png(self.directory / relative, 900, 1200, rgb=(20, 80, 200))
        source["stored_images"].append(relative)
        source["skus"].append({"sku_id": "S2", "name": "蓝色", "purchase_price_cny": 2.5, "image_path": relative})
        source.setdefault("image_sources", []).append({"path": relative, "role": "sku", "source_sku_id": "S2",
                                                       "url": "https://cbu01.alicdn.com/img/blue.jpg"})
        write_json(self.directory / "input/source.json", source)
        manifest = read_json(self.directory / "input/source-manifest.json")
        files = {row["path"]: row for row in manifest["files"]}
        for item in (relative, "input/source.json"):
            path = self.directory / item
            files[item] = {"path": item, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest["files"] = list(files.values())
        write_json(self.directory / "input/source-manifest.json", manifest)

    def test_known_unselected_sku_reference_rejected_before_provider_loaded(self):
        self.other_sku_reference()
        with patch("models.load_web_image_generator") as factory:
            with self.assertRaisesRegex(ValueError, "未选上架规格"):
                image_jobs.add_image_slot(self.directory, prompt="显示蓝色规格", reference_ids=["sku-002"], source_sku_id="S1")
        factory.assert_not_called()
        self.assertFalse((self.directory / image_jobs.JOBS_FILE).exists())

    def test_known_other_selected_sku_reference_cannot_be_assigned_to_current_slot(self):
        self.other_sku_reference()
        set_selection(self.directory, include=["S1", "S2"])
        with self.assertRaisesRegex(ValueError, "图位规格不一致"):
            image_jobs.add_image_slot(self.directory, prompt="显示粉色规格", reference_ids=["sku-002"], source_sku_id="S1")
        correct = image_jobs.add_image_slot(self.directory, prompt="显示真实蓝色规格", reference_ids=["sku-002"], source_sku_id="S2")
        self.assertEqual(correct["image_plan"]["detail_images"][0]["source_sku_id"], "S2")

    def test_shared_reference_is_not_bound_by_file_position(self):
        source = read_json(self.directory / "input/source.json")
        source["skus"].append({"sku_id": "S2", "name": "另一规格", "purchase_price_cny": 2.5})
        relative = "input/detail-images/shared.png"
        write_solid_png(self.directory / relative, 900, 1200)
        source["stored_images"].append(relative)
        source["image_sources"].append({"path": relative, "role": "detail", "url": "https://cbu01.alicdn.com/img/shared.jpg"})
        write_json(self.directory / "input/source.json", source)
        set_selection(self.directory, include=["S1", "S2"])
        shared = image_jobs.add_image_slot(self.directory, prompt="展示商品共同细节，不推断未知规格参数", reference_ids=["detail-001"])
        spec = shared["image_plan"]["detail_images"][0]
        self.assertNotIn("source_sku_id", spec)
        self.assertEqual(spec["variant_scope"], "shared")

    def test_empty_studio_does_not_claim_publish_readiness_with_all_other_approvals(self):
        image_jobs.select_images(self.directory, [])
        from pipeline import guided_review
        write_json(self.directory / guided_review.REVIEW_FILE, {
            "approved": {name: {"sha256": "proof"} for name in guided_review.DEPENDENCIES}})
        with patch.object(guided_review, "digest", return_value="proof"), \
             patch.object(guided_review, "problems", return_value=[]), \
             patch.object(guided_review, "manual_prices_complete", return_value=True), \
             patch("pipeline.guided_workflow.workflow_status", return_value={"analysis": {"confirmed": True}}), \
             patch("pipeline.listing_draft.card_ready", return_value=True):
            result = guided_review.status(self.directory)
        self.assertTrue(all(row["approved"] for row in result["sections"].values()))
        self.assertTrue(result["facts_ready"])
        self.assertTrue(result["manual_prices_ready"])
        self.assertFalse(result["ready_to_preflight"])
        self.assertEqual(len(result["blockers"]), 1)
        self.assertIn("零张可确认草稿", result["blockers"][0])

    def test_recycled_pid_owner_becomes_unknown_without_paid_replay(self):
        slot = self.slot()
        job, generator = self.enqueue(slot)
        journal = read_json(self.directory / image_jobs.JOBS_FILE)
        journal["jobs"][0].update(status="running", started_at="2026-10-08", instance="previous-worker",
                                    pid=424242, process_birth="old-starttime")
        write_json(self.directory / image_jobs.JOBS_FILE, journal)
        with image_jobs._GUARD:
            image_jobs._ACTIVE.discard(job["id"])
        with patch.object(image_jobs, "_pid_alive", return_value=True), \
             patch.object(image_jobs, "_process_birth", return_value="new-starttime"):
            result = image_jobs.list_image_jobs(self.directory)
        self.assertEqual(result[0]["status"], "unknown")
        image_jobs.run_image_job(self.directory, job["id"], generator)
        self.assertEqual(generator.calls, [])

    def test_safe_errors_do_not_echo_keys_or_signed_urls(self):
        message = image_jobs._safe_error(ModelError("API Bearer token-secret sk-live-secret https://example.com/img?secret=x"))
        self.assertNotIn("token-secret", message)
        self.assertNotIn("sk-live-secret", message)
        self.assertNotIn("secret=x", message)


if __name__ == "__main__":
    unittest.main()
