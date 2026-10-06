"""Offline guided review: fake models and local image storage, never Ozon writes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from contracts import available_contracts
from pipeline.guided_review import approve, invalidate_from, problems, slot_fingerprint, status
from pipeline.launch import launch_product
from pipeline.sku_selection import set_selection
from pipeline.upload import DryRunUploader

from tests.test_launch import LaunchFixture


@unittest.skipUnless(available_contracts(), "需要本地 contracts/original")
class GuidedFlowTests(LaunchFixture):
    def test_offline_pipeline_requires_reviews_and_requeues_after_input_edit(self):
        directory = self.product_dir
        set_selection(directory, include=["S1", "S2"])
        (directory / "input" / "manual-pricing-required.json").write_text('{"required":true}', encoding="utf-8")
        (directory / "input" / "manual-prices.json").write_text(json.dumps({"prices": {
            "S1": {"price": 199, "currency": "CNY"},
            "S2": {"price": 209, "currency": "CNY"},
        }}), encoding="utf-8")
        (directory / "input" / "category-selection.json").write_text(json.dumps({
            "category_id": 1001, "type_id": 2001, "category_path_zh": "床单",
            "source": "ozon_seller_api", "confirmed_by_user": True,
        }), encoding="utf-8")
        (directory / "input" / "human-confirmations.json").write_text(json.dumps({
            "attributes": {"9048": "PS-200"},
        }), encoding="utf-8")

        report = launch_product(directory, **self.options(uploader=DryRunUploader(),
                                                          execute_upload=False, app_mode="development"))
        self.assertTrue(report["dry_run"])
        self.assertFalse(status(directory)["ready_to_preflight"])
        initial = status(directory)
        for section in ("grouping", "copy", "image_plan", "fields"):
            self.assertFalse(initial["sections"][section]["problems"], (section, initial, report))
            approve(directory, section)
        self.assertIn("占位图", " ".join(initial["sections"]["images"]["problems"]))
        with self.assertRaises(ValueError):
            approve(directory, "images")
        self.assertFalse(status(directory)["ready_to_preflight"])
        self.assertEqual(json.loads((directory / "output" / "pricing-result.json").read_text())[
            "pricing_source"], "user_manual")
        self.assertEqual(json.loads((directory / "status.json").read_text())["api_write_count"], 0)

        invalidate_from(directory, "measurements")
        self.assertFalse(status(directory)["ready_to_preflight"])
        self.assertIn("measurements", " ".join(status(directory)["blockers"]))


class ImageProvenanceTests(unittest.TestCase):
    def test_old_placeholder_slots_cannot_be_laundered_by_one_doubao_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "output" / "generated-images").mkdir(parents=True)
            slots = [{"slot": name, "prompt": name, "reference_image_ids": ["r1"],
                      "reference_product_images": ["input/main-images/r1.png"],
                      "output_path": f"output/generated-images/{name}.png"}
                     for name in ("main-1", "detail-1")]
            for item in slots:
                (directory / item["output_path"]).write_bytes(b"fake image bytes")
            (directory / "output" / "image-plan.json").write_text(json.dumps({
                "main_images": [slots[0]], "detail_images": [slots[1]],
            }), encoding="utf-8")
            (directory / "output" / "image-qc-report.json").write_text(json.dumps({
                "decision": "revise", "critical_failures": [],
            }), encoding="utf-8")
            report = {"final_images": True, "generator": "doubao", "generated_slots": 2,
                      "planned_slots": 2, "files": [
                          {"slot": "main-1", "path": slots[0]["output_path"],
                           "generator": "doubao", "slot_fingerprint": slot_fingerprint(slots[0])},
                          {"slot": "detail-1", "path": slots[1]["output_path"]},
                      ]}
            target = directory / "output" / "image-generation-report.json"
            target.write_text(json.dumps(report), encoding="utf-8")
            self.assertIn("不一致", " ".join(problems(directory, "images")))
            report["files"][1].update(generator="doubao", slot_fingerprint=slot_fingerprint(slots[1]))
            target.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(problems(directory, "images"), [])


if __name__ == "__main__":
    unittest.main()
