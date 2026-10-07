"""Studio selection/publication regressions, fake local media only."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contracts import validate_contract
from pipeline.image_probe import write_solid_png
from pipeline.image_qc import run_image_qc
from pipeline.listing_form import read_json, write_json
from pipeline.media_selection import selected_image_specs, selected_media_version
from pipeline.oss_cos import CosObjectStorage, image_publication_binding, image_publication_version
from pipeline.oss_local import LocalObjectStorage
from pipeline.ozon_write import OzonWriteError, build_import_request
from pipeline.upload import build_upload_payload, payload_problems
from pipeline.publish_urls import build_url_map
from tests.test_oss_cos import FakeCosClient
from tests.test_ozon_write import sample_payload
from tests.test_upload import UploadFixture


def studio_plan(*, chosen=None):
    return {"studio_mode": True, "selected_slots": ["detail-b", "main-S1"] if chosen is None else chosen,
            "main_images": [{"slot": "main-S1", "source_sku_id": "S1", "prompt": "TEST ONLY",
                             "reference_image_ids": ["test-ref"], "output_path": "output/generated-images/main-S1.png"},
                            {"slot": "main-S2", "source_sku_id": "S2", "prompt": "TEST ONLY unmade",
                             "reference_image_ids": ["test-ref"], "output_path": "output/generated-images/unmade.png"}],
            "detail_images": [{"slot": "detail-b", "prompt": "TEST ONLY", "reference_image_ids": ["test-ref"],
                               "output_path": "output/generated-images/detail-b.png"}]}


class SelectionTests(unittest.TestCase):
    def test_adopted_version_cannot_redirect_sku_or_output_identity(self):
        for field, replacement in (("source_sku_id", "S2"), ("output_path", "output/generated-images/other.png")):
            plan = studio_plan(chosen=["main-S1"])
            plan["main_images"][0]["generated_spec"] = {"slot": "main-S1", field: replacement}
            with self.subTest(field=field), self.assertRaises(ValueError):
                selected_image_specs(plan)

    def test_explicit_order_and_legacy_complete_set(self):
        plan = studio_plan()
        self.assertEqual([row["slot"] for row in selected_image_specs(plan)], ["detail-b", "main-S1"])
        plan.pop("studio_mode")
        self.assertEqual([row["slot"] for row in selected_image_specs(plan)], ["main-S1", "main-S2", "detail-b"])

    def test_empty_draft_not_unknown_duplicates_or_invalid_selection(self):
        self.assertEqual(selected_image_specs(studio_plan(chosen=[])), [])
        for chosen in (["missing"], ["main-S1", "main-S1"], [1], None):
            plan = studio_plan()
            plan["selected_slots"] = chosen
            with self.subTest(chosen=chosen), self.assertRaises(ValueError):
                selected_image_specs(plan)

    def test_failed_redo_keeps_adopted_spec_without_rewriting_draft(self):
        plan = studio_plan(chosen=["main-S1"])
        row = plan["main_images"][0]
        row["generated_spec"] = {"slot": "main-S1", "prompt": "TEST ONLY accepted previous prompt"}
        before = deepcopy(plan)
        self.assertEqual(selected_image_specs(plan)[0]["prompt"], "TEST ONLY accepted previous prompt")
        self.assertEqual(plan, before)
        adopted_version = selected_media_version(plan)
        row.update(prompt="TEST ONLY next draft", job_id="TEST ONLY failed redo", status="failed")
        self.assertEqual(selected_media_version(plan), adopted_version)
        row["generated_spec"]["slot"] = "other"
        with self.assertRaises(ValueError):
            selected_image_specs(plan)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "P000999"
        self.directory.mkdir()
        self.plan = studio_plan()
        write_json(self.directory / "output/image-plan.json", self.plan)
        for row in selected_image_specs(self.plan):
            write_solid_png(self.directory / row["output_path"], 900, 1200)
        self.client = FakeCosClient()
        self.storage = CosObjectStorage(self.client, bucket="test-only-123456", region="test-only",
                                       public_base_url="https://test-only.invalid", sleep=lambda _: None)

    def test_qc_skips_unused_missing_files_but_rejects_selected_missing_file(self):
        report = run_image_qc(self.directory)
        self.assertEqual([row["slot"] for row in report["images_checked"]], ["detail-b", "main-S1"])
        self.assertEqual(report["critical_failures"], [])
        self.plan["selected_slots"].append("main-S2")
        write_json(self.directory / "output/image-plan.json", self.plan)
        self.assertIn("image_file_unreadable", run_image_qc(self.directory)["critical_failures"])

    def test_local_and_mapping_adapters_obey_same_selection(self):
        storage = LocalObjectStorage(Path(self.temp.name) / "test-only-www", "https://test-only.invalid")
        copied = storage.publish_product(self.directory, write_urls=False)
        self.assertEqual(list(copied["urls"]), ["detail-b", "main-S1"])
        self.assertEqual(copied["missing"], [])
        mapped = build_url_map(self.directory, "https://test-only.invalid")
        self.assertEqual(list(mapped["urls"]), ["detail-b", "main-S1"])
        self.assertEqual(mapped["skipped"], [])

    def test_cos_upload_and_bind_only_selected_order_and_versions(self):
        with patch("pipeline.guided_review.digest", return_value="test-only-review"), patch(
                "pipeline.guided_review.status", return_value={"sections": {"images": {"approved": True}}}):
            summary = self.storage.publish_product(self.directory)
            self.assertEqual(summary["slots"], 2)
            self.assertEqual(list(summary["urls"]), ["detail-b", "main-S1"])
            manifest = read_json(self.directory / "output/image-public-urls.json")
            self.assertTrue(image_publication_binding(self.directory, manifest)["ok"])
            version = image_publication_version(self.directory)
            self.plan["main_images"][1]["prompt"] = "TEST ONLY unused changed"
            write_json(self.directory / "output/image-plan.json", self.plan)
            self.assertEqual(image_publication_version(self.directory), version)
            self.assertTrue(image_publication_binding(self.directory, manifest)["ok"])
            self.plan["selected_slots"].reverse()
            write_json(self.directory / "output/image-plan.json", self.plan)
            self.assertFalse(image_publication_binding(self.directory, manifest)["ok"])


class CompileTests(unittest.TestCase):
    def payload(self):
        payload = sample_payload()
        payload["studio_mode"] = True
        payload["images"] = [
            {"role": "detail", "url": "https://test-only.invalid/first.png"},
            {"role": "variant_main", "source_sku_id": "S2", "url": "https://test-only.invalid/blue.png"},
            {"role": "variant_main", "source_sku_id": "S1", "url": "https://test-only.invalid/red.png"},
            {"role": "detail", "url": "https://test-only.invalid/last.png"},
        ]
        return payload

    def test_gallery_follows_order_without_another_variant_main(self):
        red, blue = build_import_request(self.payload())["items"]
        self.assertEqual(red["primary_image"], "https://test-only.invalid/first.png")
        self.assertEqual(red["images"], ["https://test-only.invalid/red.png", "https://test-only.invalid/last.png"])
        self.assertEqual(blue["images"], ["https://test-only.invalid/blue.png", "https://test-only.invalid/last.png"])

    def test_one_image_works_empty_or_other_variant_only_does_not(self):
        payload = self.payload()
        payload["variants"] = payload["variants"][:1]
        payload["images"] = [payload["images"][2]]
        item = build_import_request(payload)["items"][0]
        self.assertEqual(item["primary_image"], "https://test-only.invalid/red.png")
        self.assertEqual(item["images"], [])
        for images in ([], [{"role": "variant_main", "source_sku_id": "S2", "url": "https://test-only.invalid/blue.png"}]):
            payload["images"] = images
            with self.assertRaises(OzonWriteError):
                build_import_request(payload)

    def test_snapshot_endpoint_cap_is_per_sku_and_never_silently_truncates(self):
        payload = self.payload()
        payload["variants"] = payload["variants"][:1]
        payload["images"] = [{"role": "detail", "url": f"https://test-only.invalid/{index}.png"} for index in range(50)]
        self.assertEqual(len(build_import_request(payload)["items"][0]["images"]), 49)
        payload["images"].append({"role": "detail", "url": "https://test-only.invalid/extra.png"})
        with self.assertRaises(OzonWriteError):
            build_import_request(payload)


class PayloadTests(UploadFixture):
    def test_studio_contract_one_detail_or_empty_preserves_legacy_count_validation(self):
        legacy = read_json(self.product_dir / "output/image-plan.json")
        legacy["detail_images"] = legacy["detail_images"][:1]
        self.assertTrue(validate_contract("image-plan", legacy))
        legacy.update(studio_mode=True, selected_slots=[legacy["detail_images"][0]["slot"]])
        legacy["main_images"] = []
        legacy["detail_images"][0]["overlay_plan"] = []
        legacy["variant_image_strategy"].update(variant_main_count=0, shared_detail_count=1)
        legacy["generator_contract"]["exact_shared_detail_count"] = 1
        self.assertEqual(validate_contract("image-plan", legacy), [])
        legacy["detail_images"] = []
        legacy["selected_slots"] = []
        legacy["variant_image_strategy"]["shared_detail_count"] = 0
        legacy["generator_contract"]["exact_shared_detail_count"] = 0
        self.assertEqual(validate_contract("image-plan", legacy), [])

    def test_payload_uses_only_selected_slots_and_empty_draft_blocks_live_submit(self):
        plan = read_json(self.product_dir / "output/image-plan.json")
        detail = plan["detail_images"][0]["slot"]
        plan.update(studio_mode=True, selected_slots=[detail])
        write_json(self.product_dir / "output/image-plan.json", plan)
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertEqual([row["slot"] for row in payload["images"]], [detail])
        self.assertEqual(payload_problems(payload), [])
        plan["selected_slots"] = ["main-S1"]
        write_json(self.product_dir / "output/image-plan.json", plan)
        missing_variant = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(any("SKU S2" in problem for problem in missing_variant["production_blockers"]))
        plan["selected_slots"] = []
        write_json(self.product_dir / "output/image-plan.json", plan)
        empty = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertFalse(empty["image_upload_gate"]["passed"])
        self.assertTrue(any("未选择上架图片" in problem for problem in empty["production_blockers"]))


if __name__ == "__main__":
    unittest.main()
