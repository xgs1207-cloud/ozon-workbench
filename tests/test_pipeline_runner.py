"""干跑 runner 测试：停在正确位置、拒绝上传、门禁失败不推进、不变量成立。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline import status as st  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.steps import PIPELINE_STEPS, step_definition  # noqa: E402

PHASE_A_ONLY = PIPELINE_STEPS[:-1]  # 除 ozon_upload 外的全部步骤


def stub_handler(step):
    """把该步声明的前置产物造出来，模拟"这一步已经做完"。"""

    def handler(ctx):
        artifacts = []
        for relative in step_definition(step).get("outputs", []):
            if relative.endswith((".json", ".jsonl")):
                ctx.write_json(relative, {"stub": True, "step": step})
            else:
                ctx.path(relative).mkdir(parents=True, exist_ok=True)
            artifacts.append(relative)
        return {"warnings": [], "artifacts": artifacts}

    return handler


def category_handler(**overrides):
    payload = {
        "metadata_source": "ozon_seller_api",
        "category_id": 123456,
        "type_id": 789,
        "match_status": "api_confirmed",
    }
    payload.update(overrides)

    def handler(ctx):
        ctx.write_json("output/ozon-category.json", payload)
        ctx.write_json("output/ozon-category-attributes.json", {"attributes": []})
        return {"warnings": [], "artifacts": ["output/ozon-category.json"]}

    return handler


def make_product(
    root: pathlib.Path,
    product_id: str = "P000001",
    *,
    skus: int = 2,
    with_source: bool = True,
    authorize: bool = True,
    completed=None,
    product_status: str = "COLLECTED",
) -> pathlib.Path:
    products = root / "products"
    products.mkdir(parents=True, exist_ok=True)
    directory = products / product_id
    (directory / "input").mkdir(parents=True, exist_ok=True)
    (directory / "output").mkdir(parents=True, exist_ok=True)
    if with_source:
        source = {
            "schema_version": "1.0.0",
            "product_id": product_id,
            "source_url": "https://detail.1688.com/offer/123456789.html",
            "skus": [
                {"sku_id": f"S{index}", "purchase_price_cny": 10 + index} for index in range(skus)
            ],
        }
        (directory / "input" / "source.json").write_text(
            json.dumps(source, ensure_ascii=False), encoding="utf-8"
        )
    payload = st.new_status(product_id, status=product_status)
    payload["completed_steps"] = list(completed) if completed is not None else ["collect_source"]
    payload["task_authorized"] = authorize
    if authorize:
        payload["batch_id"] = "B-TEST"
    st.save_status(directory, payload)
    return directory


class RunnerStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stops_at_until(self):
        directory = make_product(self.root)
        report = run_product(directory, until="product_analysis")
        self.assertEqual(report["stop_reason"], "until_reached")
        self.assertEqual(report["stopped_at"], "product_analysis")
        self.assertEqual([item["step"] for item in report["executed"]], ["validate_source"])
        self.assertEqual(report["completed_steps"], ["collect_source", "validate_source"])
        self.assertEqual(report["product_status"], "PROCESSING")
        self.assertEqual(report["api_write_count"], 0)
        self.assertTrue((directory / "output" / "run-report.json").is_file())

    def test_missing_handler_stops_without_marking_done(self):
        directory = make_product(self.root)
        report = run_product(directory)
        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertEqual(report["stopped_at"], "product_analysis")
        self.assertNotIn("product_analysis", report["completed_steps"])

    def test_missing_input_stops_immediately(self):
        directory = make_product(self.root, with_source=False)
        report = run_product(directory, until="product_analysis")
        self.assertEqual(report["stop_reason"], "missing_inputs")
        self.assertEqual(report["executed"][0]["missing_inputs"], ["input/source.json"])
        self.assertEqual(report["completed_steps"], ["collect_source"])

    def test_step_budget(self):
        directory = make_product(self.root)
        report = run_product(directory, step_budget=1)
        self.assertEqual(report["stop_reason"], "step_budget_exhausted")
        self.assertEqual(report["stopped_at"], "product_analysis")

    def test_unauthorized_product_refused(self):
        directory = make_product(self.root, authorize=False)
        with self.assertRaises(ValueError) as ctx:
            run_product(directory)
        self.assertIn("未授权", str(ctx.exception))


class UploadRefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ready_for_upload(self):
        return make_product(
            self.root,
            completed=["collect_source", *PHASE_A_ONLY],
            product_status="IMAGES_GENERATED",
        )

    def test_dry_run_never_uploads(self):
        directory = self._ready_for_upload()
        report = run_product(directory)
        self.assertEqual(report["stop_reason"], "upload_gate_refused_dry_run")
        self.assertEqual(report["stopped_at"], "ozon_upload")
        self.assertNotIn("ozon_upload", report["completed_steps"])
        self.assertEqual(report["api_write_count"], 0)

    def test_development_mode_never_uploads(self):
        directory = self._ready_for_upload()
        report = run_product(directory, dry_run=False, app_mode="development")
        self.assertEqual(report["stop_reason"], "upload_gate_refused_app_mode")

    def test_production_without_handler_still_refuses(self):
        directory = self._ready_for_upload()
        report = run_product(directory, dry_run=False, app_mode="production")
        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertNotIn("ozon_upload", report["completed_steps"])


class PhaseAEndToEndTests(unittest.TestCase):
    """用真实 handler 跑完阶段 A，验证干跑能端到端停在阶段 B 之前。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _handlers(self, category_overrides=None):
        handlers = {
            "product_analysis": stub_handler("product_analysis"),
            "category_match": category_handler(**(category_overrides or {})),
            "variant_rules": stub_handler("variant_rules"),
            "measurements": stub_handler("measurements"),
        }
        return handlers

    def _attributes_final(self, directory, missing=0):
        (directory / "output").mkdir(parents=True, exist_ok=True)
        (directory / "output" / "ozon-attributes-final.json").write_text(
            json.dumps({"required_summary": {"missing": missing}}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_phase_a_passes_and_stops_before_phase_b(self):
        directory = make_product(self.root)
        self._attributes_final(directory)
        report = run_product(directory, handlers=self._handlers())

        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertEqual(report["stopped_at"], "product_positioning")
        self.assertEqual(
            report["completed_steps"],
            ["collect_source", *PIPELINE_STEPS[:7]],
        )
        payload = json.loads((directory / "output" / "upload-feasibility.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["blocking_checks"], [])
        self.assertEqual(report["api_write_count"], 0)

    def test_category_gate_failure_marks_attention(self):
        directory = make_product(self.root)
        self._attributes_final(directory)
        report = run_product(
            directory, handlers=self._handlers({"metadata_source": "local_translation"})
        )

        self.assertEqual(report["stop_reason"], "gate_failed")
        self.assertEqual(report["stopped_at"], "upload_feasibility")
        saved = st.load_status(directory)
        self.assertEqual(saved["status"], "NEEDS_ATTENTION")
        self.assertEqual(saved["failed_step"], "upload_feasibility")
        self.assertEqual(saved["next_action"], "retry_failed_step")
        self.assertIn("upload_feasibility", saved["pending_steps"])
        payload = json.loads((directory / "output" / "upload-feasibility.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("category", payload["blocking_checks"])
        # 已授权的批次不弹问题（无人值守）
        self.assertFalse((directory / "input" / "pending-question.json").is_file())

    def test_missing_required_attributes_blocks(self):
        directory = make_product(self.root)
        self._attributes_final(directory, missing=3)
        report = run_product(directory, handlers=self._handlers())
        self.assertEqual(report["stop_reason"], "gate_failed")
        payload = json.loads((directory / "output" / "upload-feasibility.json").read_text(encoding="utf-8"))
        self.assertIn("required_attributes", payload["blocking_checks"])


if __name__ == "__main__":
    unittest.main()
