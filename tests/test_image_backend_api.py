"""Backend selection, one-slot redo and reference provenance; entirely offline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector.ingest import ingest_capture
from models import image_backend_settings, load_image_generator, load_web_image_generator, ModelError
from pipeline.guided_review import slot_fingerprint
from pipeline.image_probe import write_solid_png
from pipeline.listing_form import read_json, write_json
from pipeline.selected_source import selected_source
from tests import test_authorized_forms_api as forms_fixture
from tests import test_listing_flow_api as listing_fixture


class BackendSettingsTests(unittest.TestCase):
    def test_rightapi_safe_status_and_text_provider_is_independent(self):
        env = {"IMAGE_GENERATOR": "rightapi", "RIGHTAPI_API_KEY": "secret-not-to-return",
               "ARK_API_KEY": "other-secret", "ARK_TEXT_MODEL": "text-endpoint"}
        status = image_backend_settings(env)
        self.assertEqual(status["name"], "rightapi")
        self.assertEqual(status["model"], "gpt-image-2.5")
        self.assertEqual(status["image_size"], "2k")
        self.assertEqual(status["aspect_ratio"], "3:4")
        self.assertTrue(status["configured"])
        self.assertNotIn("secret", json.dumps(status))
        self.assertNotIn("RIGHTAPI_BASE_URL", status)
        self.assertEqual(env["ARK_TEXT_MODEL"], "text-endpoint")

    def test_missing_rightapi_key_does_not_fall_back_to_ark(self):
        env = {"IMAGE_GENERATOR": "rightapi", "ARK_API_KEY": "valid-ark-fixture"}
        self.assertFalse(image_backend_settings(env)["configured"])
        with self.assertRaises(ModelError):
            load_web_image_generator(env)

    def test_placeholder_and_unknown_cannot_be_used_for_paid_web_images(self):
        for name in ("placeholder", "off", "typo"):
            with self.subTest(name=name), self.assertRaises(ModelError):
                load_web_image_generator({"IMAGE_GENERATOR": name})

    def test_factory_uses_explicit_env_and_slot_filter(self):
        with patch.dict(os.environ, {"IMAGE_GENERATOR": "doubao"}):
            image = load_image_generator(env={"IMAGE_GENERATOR": "rightapi",
                                              "RIGHTAPI_API_KEY": "offline-fixture"},
                                         slot_filter=["main-S1"])
        self.assertEqual(image.name, "rightapi")
        self.assertEqual(image.slot_filter, {"main-S1"})


class ImageSourceProvenanceTests(unittest.TestCase):
    def test_capture_records_exact_reference_paths_without_sending_ledger_to_text_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = write_solid_png(root / "sample.png", 900, 1200)
            data = reference.read_bytes()
            urls = ["https://cbu01.alicdn.com/img/sample-main.jpg",
                    "https://cbu01.alicdn.com/img/sample-detail.jpg",
                    "https://cbu01.alicdn.com/img/sample-red.jpg"]
            with patch("collector.ingest._download_remote_image", return_value=(data, ".png")):
                result = ingest_capture(root / "products", {
                    "source_url": "https://detail.1688.com/offer/123456789.html",
                    "collection_mode": "all_skus", "main_images": [urls[0]],
                    "detail_images": [urls[1]],
                    "skus": [{"sku_id": "S1", "name": "红色", "image_url": urls[2]}]})
            directory = root / "products" / result["product_id"]
            source = read_json(directory / "input/source.json")
            rows = source["image_sources"]
            self.assertEqual({row["url"] for row in rows}, set(urls))
            for row in rows:
                self.assertTrue((directory / row["path"]).is_file())
                self.assertIn(row["path"], source["stored_images"])
            sku_row = next(row for row in rows if row["role"] == "sku")
            self.assertEqual(sku_row["source_sku_id"], "S1")
            self.assertEqual(sku_row["path"], source["skus"][0]["image_path"])
            projected = selected_source(directory, source)
            self.assertNotIn("image_sources", projected)


class ImageBackendApiTests(unittest.TestCase):
    setUp = forms_fixture.AuthorizedFormsApiTests.setUp
    tearDown = forms_fixture.AuthorizedFormsApiTests.tearDown
    authorize = forms_fixture.AuthorizedFormsApiTests.authorize
    collect = listing_fixture.ListingFlowApiTests.collect
    analyze = listing_fixture.ListingFlowApiTests.analyze
    category_and_keywords = listing_fixture.ListingFlowApiTests.category_and_keywords
    copy_and_plan = listing_fixture.ListingFlowApiTests.copy_and_plan

    def ready_plan(self):
        self.collect()
        self.copy_and_plan()
        result = self.client.post(self.base + "/guided/approve", json={"section": "image_plan"})
        self.assertEqual(result.status_code, 200, result.text)
        plan = read_json(self.directory / "output/image-plan.json")
        return [*plan["main_images"], *plan["detail_images"]]

    def generator(self, name, calls):
        directory = self.directory
        class OfflineGenerator:
            produces_final_images = True
            def generate(self, request):
                calls.append(request.slot)
                plan = read_json(directory / "output/image-plan.json")
                spec = next(row for row in [*plan["main_images"], *plan["detail_images"]]
                            if row["slot"] == request.slot)
                path = write_solid_png(directory / spec["output_path"], 900, 1200)
                return {"generator": name, "model": "offline-model", "final_images": True,
                        "generated": [{"slot": request.slot, "path": spec["output_path"],
                                       "bytes": path.stat().st_size}], "skipped": [], "note": "offline"}
        instance = OfflineGenerator()
        instance.name = name
        return instance

    def test_guided_status_exposes_only_safe_model_settings(self):
        self.collect()
        with patch.dict(os.environ, {"IMAGE_GENERATOR": "rightapi",
                                     "RIGHTAPI_API_KEY": "never-echo-this-fixture"}):
            result = self.client.get(self.base + "/guided")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["image_backend"]["name"], "rightapi")
        self.assertNotIn("never-echo-this-fixture", result.text)
        self.assertNotIn("image_sources", result.json()["source"])

    def test_no_generation_before_approved_plan(self):
        self.collect()
        with patch("models.load_web_image_generator") as loader:
            result = self.client.post(self.base + "/guided/generate-image", json={"slot": "main-S1"})
        self.assertEqual(result.status_code, 409)
        loader.assert_not_called()

    def test_one_slot_generation_keeps_other_real_backend_files_and_forces_new_review(self):
        specs = self.ready_plan()
        files = []
        for spec in specs:
            path = write_solid_png(self.directory / spec["output_path"], 900, 1200)
            files.append({"slot": spec["slot"], "path": spec["output_path"], "bytes": path.stat().st_size,
                          "generator": "doubao", "slot_fingerprint": slot_fingerprint(spec)})
        write_json(self.directory / "output/image-generation-report.json", {
            "generator": "doubao", "final_images": True, "planned_slots": len(specs),
            "generated_slots": len(specs), "files": files})
        calls = []
        generator = self.generator("rightapi", calls)
        with patch("models.load_web_image_generator", return_value=generator) as loader:
            result = self.client.post(self.base + "/guided/generate-image", json={"slot": specs[0]["slot"]})
        self.assertEqual(result.status_code, 200, result.text)
        loader.assert_called_once_with(slot_filter=[specs[0]["slot"]])
        self.assertEqual(calls, [specs[0]["slot"]])
        self.assertEqual(result.json()["generator"], "rightapi")
        report = read_json(self.directory / "output/image-generation-report.json")
        self.assertEqual(report["generator"], "mixed")
        self.assertEqual(report["generated_slots"], len(specs))
        self.assertEqual({row["generator"] for row in report["files"]}, {"rightapi", "doubao"})
        current = self.client.get(self.base + "/guided").json()
        self.assertFalse(current["review"]["sections"]["images"]["approved"])

    def test_missing_configuration_does_not_call_another_generator(self):
        specs = self.ready_plan()
        with patch.dict(os.environ, {"IMAGE_GENERATOR": "rightapi", "RIGHTAPI_API_KEY": "",
                                     "ARK_API_KEY": "offline-other-provider"}):
            result = self.client.post(self.base + "/guided/generate-image", json={"slot": specs[0]["slot"]})
        self.assertEqual(result.status_code, 422, result.text)
        self.assertFalse((self.directory / "output/image-generation-report.json").exists())

    def test_identical_result_and_identical_qc_still_invalidates_previous_image_approval(self):
        specs = self.ready_plan()
        files = []
        for spec in specs:
            path = write_solid_png(self.directory / spec["output_path"], 900, 1200)
            files.append({"slot": spec["slot"], "path": spec["output_path"], "bytes": path.stat().st_size,
                          "generator": "rightapi", "model": "offline-model",
                          "slot_fingerprint": slot_fingerprint(spec)})
        report_path = self.directory / "output/image-generation-report.json"
        write_json(report_path, {"generator": "rightapi", "final_images": True,
                               "planned_slots": len(specs), "generated_slots": len(specs), "files": files})
        generator = self.generator("rightapi", [])
        with patch("models.load_web_image_generator", return_value=generator):
            first = self.client.post(self.base + "/guided/generate-image", json={"slot": specs[0]["slot"]})
            self.assertEqual(first.status_code, 200, first.text)
            approved = self.client.post(self.base + "/guided/approve", json={"section": "images"})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertTrue(approved.json()["review"]["sections"]["images"]["approved"])
            previous_id = read_json(report_path)["generation_id"]
            qc = read_json(self.directory / "output/image-qc-report.json")
            with patch("pipeline.image_qc.run_image_qc", return_value=qc):
                second = self.client.post(self.base + "/guided/generate-image", json={"slot": specs[0]["slot"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(previous_id, read_json(report_path)["generation_id"])
        current = self.client.get(self.base + "/guided").json()
        self.assertFalse(current["review"]["sections"]["images"]["approved"])


if __name__ == "__main__":
    unittest.main()
