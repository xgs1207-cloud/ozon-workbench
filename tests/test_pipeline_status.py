"""商品状态机测试（对齐 status.schema.json 与 pipeline_runtime.py 的语义）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline import status as st  # noqa: E402
from pipeline.steps import MAX_SELECTED_SKUS, PIPELINE_STEPS  # noqa: E402


def make_product(root: pathlib.Path, product_id: str = "P000001", skus: int = 2, **status_fields):
    directory = root / product_id
    (directory / "input").mkdir(parents=True, exist_ok=True)
    (directory / "output").mkdir(parents=True, exist_ok=True)
    source = {
        "schema_version": "1.0.0",
        "product_id": product_id,
        "source_url": "https://detail.1688.com/offer/123456789.html",
        "skus": [{"sku_id": f"S{i}", "purchase_price_cny": 10 + i} for i in range(skus)],
    }
    (directory / "input" / "source.json").write_text(
        json.dumps(source, ensure_ascii=False), encoding="utf-8"
    )
    payload = st.new_status(product_id, status=status_fields.pop("status", "COLLECTED"))
    payload.update(status_fields)
    st.save_status(directory, payload)
    return directory


class NewStatusTests(unittest.TestCase):
    def test_fresh_status_passes_validation(self):
        payload = st.new_status("P000001")
        self.assertEqual(st.validate_status(payload), [])
        self.assertEqual(payload["next_action"], PIPELINE_STEPS[0])
        self.assertFalse(payload["task_authorized"])

    def test_bad_product_id_rejected(self):
        with self.assertRaises(ValueError):
            st.new_status("X000001")


class NormalizeTests(unittest.TestCase):
    def test_non_contiguous_completed_is_truncated(self):
        payload = st.new_status("P000001")
        payload["completed_steps"] = ["collect_source", "validate_source", "category_match"]
        result = st.normalize(payload)
        self.assertEqual(result["completed_steps"], ["collect_source", "validate_source"])
        self.assertEqual(result["next_action"], "product_analysis")
        self.assertNotIn("category_match", result["completed_steps"])

    def test_remote_product_keeps_history_order(self):
        payload = st.new_status("P000001")
        payload["status"] = "UPLOADING"
        payload["api_write_count"] = 1
        payload["completed_steps"] = ["collect_source", "validate_source", "ozon_upload"]
        result = st.normalize(payload)
        self.assertIn("ozon_upload", result["completed_steps"])

    def test_out_of_order_completed_is_sorted(self):
        payload = st.new_status("P000001")
        payload["completed_steps"] = ["collect_source", "validate_source", "product_analysis"]
        result = st.normalize(payload)
        self.assertEqual(result["completed_steps"], ["collect_source", "validate_source", "product_analysis"])


class CompleteStepTests(unittest.TestCase):
    def test_step_advances_status_and_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            result = st.complete_step(directory, "validate_source")
            self.assertEqual(result["status"], "PROCESSING")
            self.assertEqual(result["next_action"], "product_analysis")
            self.assertGreater(result["progress"], 0)
            self.assertEqual(result["steps"][-1]["name"], "validate_source")

    def test_completing_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            st.complete_step(directory, "validate_source")
            result = st.complete_step(directory, "validate_source")
            self.assertEqual(len(result["steps"]), 1)
            self.assertEqual(result["completed_steps"].count("validate_source"), 1)

    def test_unknown_step_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            with self.assertRaises(ValueError):
                st.complete_step(directory, "not_a_step")


class NeedsAttentionTests(unittest.TestCase):
    def test_marks_attention_and_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            result = st.mark_needs_attention(directory, "image_qc", "图片比例不符")
            self.assertEqual(result["status"], "NEEDS_ATTENTION")
            self.assertEqual(result["failed_step"], "image_qc")
            self.assertEqual(result["next_action"], "retry_failed_step")
            self.assertTrue(result["attention_required"])
            self.assertEqual(result["steps"][-1]["status"], "failed")
            self.assertTrue(result["steps"][-1]["error"]["retryable"])

    def test_question_only_for_critical_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            self.assertIsNone(
                st.maybe_create_operator_question(directory, "product_analysis", "图片生成失败")
            )
            question = st.maybe_create_operator_question(
                directory, "variant_rules", "SKU对应关系不明确，线索不一致"
            )
            self.assertIsNotNone(question)
            self.assertTrue((directory / "input" / "pending-question.json").is_file())

    def test_authorized_batch_never_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp), task_authorized=True)
            self.assertIsNone(
                st.maybe_create_operator_question(directory, "variant_rules", "SKU对应不明确")
            )
            self.assertFalse((directory / "input" / "pending-question.json").is_file())


class QueueProductTests(unittest.TestCase):
    def test_fresh_start_resets_and_archives_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(
                pathlib.Path(tmp), warnings=["旧警告"], retry_count_by_step={"image_qc": 3}
            )
            result = st.queue_product(directory, "B-NEW")
            self.assertEqual(result["status"], "QUEUED")
            self.assertEqual(result["current_step"], "queue")
            self.assertTrue(result["task_authorized"])
            self.assertEqual(result["retry_count_by_step"], {})
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["warning_history"][0]["warnings"], ["旧警告"])

    def test_same_batch_resume_keeps_completed_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(
                pathlib.Path(tmp),
                status="QUEUED",
                batch_id="B-KEEP",
                task_authorized=True,
                completed_steps=["collect_source", "validate_source"],
                next_action="product_analysis",
                retry_count_by_step={"product_analysis": 2},
            )
            result = st.queue_product(directory, "B-KEEP")
            self.assertEqual(result["completed_steps"], ["collect_source", "validate_source"])
            self.assertEqual(result["retry_count_by_step"], {"product_analysis": 2})
            self.assertEqual(result["status"], "QUEUED")

    def test_attention_resume_keeps_checkpoint(self):
        """有意偏离原项目：失败后重试必须保住已完成步骤（否则"恢复任务"等于重跑）。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(
                pathlib.Path(tmp),
                status="NEEDS_ATTENTION",
                batch_id="B-OLD",
                task_authorized=True,
                completed_steps=["collect_source", "validate_source", "product_analysis"],
                next_action="category_match",
                attention_required=True,
            )
            result = st.queue_product(directory, "B-NEW")
            self.assertEqual(result["status"], "QUEUED")
            self.assertEqual(result["batch_id"], "B-NEW")
            self.assertFalse(result["attention_required"])
            self.assertEqual(result["completed_steps"][-1], "product_analysis")

    def test_checkpoint_resume_from_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(
                pathlib.Path(tmp),
                status="STOPPED",
                batch_id="B-OLD",
                task_authorized=True,
                completed_steps=["collect_source", "validate_source"],
                next_action="product_analysis",
            )
            result = st.queue_product(directory, "B-NEW")
            self.assertEqual(result["status"], "QUEUED")
            self.assertEqual(result["batch_id"], "B-NEW")
            self.assertEqual(result["completed_steps"], ["collect_source", "validate_source"])

    def test_auto_upload_resume_jumps_to_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(
                pathlib.Path(tmp),
                status="WAITING_MANUAL_REVIEW",
                auto_upload=True,
                manual_confirmation_required=False,
                api_write_count=0,
                next_action="ozon_upload",
                completed_steps=["collect_source", *PIPELINE_STEPS[:-1]],
            )
            result = st.queue_product(directory, "B-UP")
            self.assertEqual(result["next_action"], "ozon_upload")
            self.assertIn("ozon_upload", result["pending_steps"])

    def test_sku_limit_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp), skus=MAX_SELECTED_SKUS + 1)
            with self.assertRaises(ValueError):
                st.queue_product(directory, "B-X")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_written_and_hash_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            first = st.freeze_sku_run_snapshot(directory, "B-1")
            second = st.freeze_sku_run_snapshot(directory, "B-1")
            self.assertEqual(first["dependency_hash"], second["dependency_hash"])
            self.assertEqual(first["selected_sku_count"], 2)
            self.assertTrue((directory / "output" / "sku-run-snapshot.json").is_file())

    def test_snapshot_hash_changes_when_input_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_product(pathlib.Path(tmp))
            first = st.freeze_sku_run_snapshot(directory, "B-1")
            source_path = directory / "input" / "source.json"
            source = json.loads(source_path.read_text(encoding="utf-8"))
            source["skus"][0]["purchase_price_cny"] = 99
            source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            second = st.freeze_sku_run_snapshot(directory, "B-1")
            self.assertNotEqual(first["dependency_hash"], second["dependency_hash"])


class ValidateStatusTests(unittest.TestCase):
    def test_catches_broken_payloads(self):
        payload = st.new_status("P000001")
        payload["status"] = "NOPE"
        payload["progress"] = 200
        problems = st.validate_status(payload)
        self.assertTrue(any("status" in item for item in problems))
        self.assertTrue(any("progress" in item for item in problems))

    def test_uploaded_requires_ozon_uploaded(self):
        payload = st.new_status("P000001")
        payload["status"] = "UPLOADED"
        problems = st.validate_status(payload)
        self.assertTrue(any("upload_status" in item for item in problems))

    def test_snapshot_bounds(self):
        payload = st.new_status("P000001")
        payload["sku_run_snapshot"] = {
            "path": "output/sku-run-snapshot.json",
            "dependency_hash": "a" * 64,
            "frozen_at": st.now_iso(),
            "selected_sku_count": 99,
        }
        problems = st.validate_status(payload)
        self.assertTrue(any("selected_sku_count" in item for item in problems))


if __name__ == "__main__":
    unittest.main()
