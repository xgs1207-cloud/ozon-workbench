"""Final-image review across supported backends; no model or network calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pipeline.guided_review import approve, problems, slot_fingerprint, status


class ImageBackendReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "output/generated-images").mkdir(parents=True)
        self.slots = [
            {
                "slot": name,
                "prompt": "Show the selected product without invented claims: " + name,
                "reference_image_ids": ["r1"],
                "reference_product_images": ["input/main-images/r1.png"],
                "output_path": f"output/generated-images/{name}.png",
            }
            for name in ("main-S1", "detail-1", "detail-2")
        ]
        for item in self.slots:
            (self.directory / item["output_path"]).write_bytes(b"offline final image bytes")
        self.plan = {"main_images": self.slots[:1], "detail_images": self.slots[1:]}
        self.report = {
            "final_images": True,
            "generator": "doubao",
            "generated_slots": len(self.slots),
            "planned_slots": len(self.slots),
            "files": [
                {
                    "slot": item["slot"],
                    "path": item["output_path"],
                    "generator": "doubao",
                    "slot_fingerprint": slot_fingerprint(item),
                }
                for item in self.slots
            ],
        }
        self.qc = {"decision": "revise", "critical_failures": []}
        self.save()

    def write(self, relative, value):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def save(self):
        self.write("output/image-plan.json", self.plan)
        self.write("output/image-generation-report.json", self.report)
        self.write("output/image-qc-report.json", self.qc)

    def image_problems(self):
        return problems(self.directory, "images")

    def test_existing_doubao_final_set_remains_valid(self):
        self.assertEqual(self.image_problems(), [])

    def test_complete_rightapi_final_set_is_valid(self):
        self.report["generator"] = "rightapi"
        for item in self.report["files"]:
            item.update(generator="rightapi", model="gpt-image-2.5", aspect_ratio="3:4", image_size="2k")
        self.save()
        self.assertEqual(self.image_problems(), [])
        self.assertTrue(approve(self.directory, "images")["sections"]["images"]["approved"])

    def test_mixed_final_set_accepts_known_backends_with_optional_metadata(self):
        self.report["generator"] = "mixed"
        self.report["files"][0]["generator"] = "rightapi"
        self.save()
        self.assertEqual(self.image_problems(), [])

    def test_one_slot_redone_with_rightapi_preserves_other_known_slots(self):
        approved = approve(self.directory, "images")
        self.assertTrue(approved["sections"]["images"]["approved"])
        untouched = deepcopy(self.report["files"][1:])
        self.report["generator"] = "mixed"
        self.report["files"][0].update(generator="rightapi", model="gpt-image-2.5")
        (self.directory / self.slots[0]["output_path"]).write_bytes(b"offline replacement image")
        self.save()
        self.assertEqual(self.report["files"][1:], untouched)
        self.assertEqual(self.image_problems(), [])
        self.assertFalse(status(self.directory)["sections"]["images"]["approved"])
        self.assertTrue(approve(self.directory, "images")["sections"]["images"]["approved"])

    def test_placeholder_slot_cannot_be_laundered_by_rightapi_report(self):
        self.report["generator"] = "rightapi"
        for item in self.report["files"]:
            item["generator"] = "rightapi"
        self.report["files"][1]["generator"] = "local-placeholder"
        self.save()
        self.assertIn("不一致", " ".join(self.image_problems()))
        with self.assertRaises(ValueError):
            approve(self.directory, "images")

    def test_missing_slot_generator_cannot_be_laundered_by_mixed_report(self):
        self.report["generator"] = "mixed"
        self.report["files"][0]["generator"] = "rightapi"
        self.report["files"][1].pop("generator")
        self.save()
        self.assertIn("不一致", " ".join(self.image_problems()))

    def test_unknown_slot_generator_is_blocked(self):
        for generator in ("unverified-backend", None, [], {}):
            with self.subTest(generator=generator):
                self.report["generator"] = "mixed"
                self.report["files"][0]["generator"] = generator
                self.save()
                self.assertIn("不一致", " ".join(self.image_problems()))

    def test_unknown_or_placeholder_report_generator_is_blocked(self):
        for generator in ("unverified-backend", "local-placeholder", "placeholder", None, [], {}):
            with self.subTest(generator=generator):
                self.report["generator"] = generator
                self.save()
                self.assertIn("正式图片", " ".join(self.image_problems()))

    def test_final_images_must_still_be_true(self):
        self.report["generator"] = "rightapi"
        for item in self.report["files"]:
            item["generator"] = "rightapi"
        for final in (False, None, 1, "true"):
            with self.subTest(final=final):
                self.report["final_images"] = final
                self.save()
                self.assertIn("正式图片", " ".join(self.image_problems()))

    def test_summary_backend_must_match_actual_slot_backends(self):
        for summary, generators in (
            ("doubao", ("rightapi", "doubao", "doubao")),
            ("rightapi", ("doubao", "doubao", "doubao")),
            ("mixed", ("rightapi", "rightapi", "rightapi")),
        ):
            with self.subTest(summary=summary, generators=generators):
                self.report["generator"] = summary
                for item, generator in zip(self.report["files"], generators):
                    item["generator"] = generator
                self.save()
                self.assertIn("来源摘要", " ".join(self.image_problems()))

    def test_changed_prompt_requires_only_changed_slot_regeneration(self):
        self.report["generator"] = "mixed"
        self.report["files"][0]["generator"] = "rightapi"
        self.slots[0]["prompt"] += " Keep the real SKU color."
        self.save()
        self.assertIn("不一致", " ".join(self.image_problems()))
        self.report["files"][0]["slot_fingerprint"] = slot_fingerprint(self.slots[0])
        self.save()
        self.assertEqual(self.image_problems(), [])

    def test_changed_reference_and_output_path_are_still_blocked(self):
        for field, value in (
            ("reference_image_ids", ["new-reference"]),
            ("reference_product_images", ["input/main-images/new-reference.png"]),
        ):
            with self.subTest(field=field):
                original = deepcopy(self.slots[0][field])
                self.slots[0][field] = value
                self.save()
                self.assertIn("不一致", " ".join(self.image_problems()))
                self.slots[0][field] = original
        self.report["files"][0]["path"] = self.slots[1]["output_path"]
        self.save()
        self.assertIn("不一致", " ".join(self.image_problems()))

    def test_incomplete_report_still_requires_full_coverage(self):
        self.report["generated_slots"] = 2
        self.report["files"].pop()
        self.save()
        self.assertIn("覆盖整套", " ".join(self.image_problems()))

    def test_missing_or_extra_slot_cannot_bypass_coverage_with_equal_counts(self):
        for slot in (None, "unknown-slot"):
            with self.subTest(slot=slot):
                self.report["files"][-1]["slot"] = slot
                self.save()
                self.assertIn("不一致", " ".join(self.image_problems()))

    def test_missing_file_still_blocks_review(self):
        (self.directory / self.slots[0]["output_path"]).unlink()
        self.assertIn("尚未齐全", " ".join(self.image_problems()))

    def test_critical_qc_failure_or_rejection_still_blocks_known_backends(self):
        self.report["generator"] = "mixed"
        self.report["files"][0]["generator"] = "rightapi"
        for qc in (
            {"decision": "revise", "critical_failures": ["aspect_ratio_mismatch"]},
            {"decision": "reject", "critical_failures": []},
        ):
            with self.subTest(qc=qc):
                self.qc = qc
                self.save()
                self.assertIn("质检未通过", " ".join(self.image_problems()))


if __name__ == "__main__":
    unittest.main()
