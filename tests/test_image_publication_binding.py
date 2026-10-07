"""Offline media-version/preflight regression tests; never publish test facts."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from pipeline.guided_review import approve, slot_fingerprint
from pipeline.image_probe import write_solid_png
from pipeline.listing_form import read_json, write_json
from pipeline.oss_cos import CosError, CosObjectStorage, image_publication_binding
from pipeline.preflight import preflight
from tests.test_oss_cos import FakeCosClient


class ImagePublicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "P000999"
        self.directory.mkdir()
        write_json(self.directory / "status.json", {"product_id": self.directory.name, "api_write_count": 0})
        self.client = FakeCosClient()
        self.storage = CosObjectStorage(self.client, bucket="test-only-123456", region="test-only",
                                       public_base_url="https://test-only.invalid", sleep=lambda _: None)
        self.make_images()

    def make_images(self, count=1):
        slots = [{"slot": f"main-S{index}", "source_sku_id": f"S{index}",
                  "output_path": f"output/generated-images/main-S{index}.png", "prompt": "TEST ONLY fixture",
                  "reference_image_ids": ["test-only-ref"], "reference_product_images": ["input/test-only.png"],
                  "russian_text": []} for index in range(1, count + 1)]
        write_json(self.directory / "output/image-plan.json", {"main_images": slots, "detail_images": []})
        for row in slots:
            write_solid_png(self.directory / row["output_path"], 900, 1200)
        # These are isolated synthetic image receipts, not genuine AI output.
        write_json(self.directory / "output/image-generation-report.json", {
            "generator": "rightapi", "model": "TEST_ONLY", "final_images": True,
            "planned_slots": count, "generated_slots": count,
            "files": [{"slot": row["slot"], "path": row["output_path"], "generator": "rightapi",
                       "slot_fingerprint": slot_fingerprint(row)} for row in slots]})
        write_json(self.directory / "output/image-qc-report.json", {"decision": "revise", "critical_failures": []})
        approve(self.directory, "images")
        self.slots = slots

    def anonymous_get(self, request, timeout=None):
        key = unquote(urlsplit(request.full_url).path.lstrip("/"))
        response = io.BytesIO(self.client.objects[key])
        response.status = 200
        return response

    def preflight(self, *, opener=None):
        urls = read_json(self.directory / "output/image-public-urls.json").get("urls") or {}
        payload = {"production_blockers": [], "attributes": [], "variants": [{"currency_code": "CNY"}],
                   "images": [{"url": url} for url in urls.values()], "image_upload_gate": {"passed": True}}
        # Isolate the new binding gate, without mocking it or the image review.
        with patch("pipeline.preflight.ensure_registry", return_value={}), patch(
                "pipeline.preflight.list_shops", return_value=[{"id": "test-only", "enabled": True,
                                                                "default_currency_code": "CNY"}]), patch(
                "pipeline.preflight.credential_report", return_value=[{"shop_id": "test-only", "ready": True}]), patch(
                "pipeline.preflight.build_upload_payload", return_value=payload), patch(
                "pipeline.preflight.payload_problems", return_value=[]):
            return preflight(self.directory, shop="test-only", urlopen=opener or self.anonymous_get)

    def test_manifest_binds_bytes_plan_review_and_repeat_has_no_put(self):
        self.storage.publish_product(self.directory)
        manifest = read_json(self.directory / "output/image-public-urls.json")
        self.assertEqual(manifest["image_manifest_version"], 2)
        self.assertEqual(len(manifest["image_plan_sha256"]), 64)
        self.assertEqual(len(manifest["images_review_sha256"]), 64)
        row = manifest["files"]["main-S1"]
        self.assertEqual(row["sha256"], hashlib.sha256((self.directory / row["output_path"]).read_bytes()).hexdigest())
        first_puts = sum(name == "put_object" for name, _ in self.client.calls)
        second = self.storage.publish_product(self.directory)
        self.assertEqual(second["uploaded"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(sum(name == "put_object" for name, _ in self.client.calls), first_puts)
        self.assertTrue(self.preflight()["ok"])

    def test_regenerated_reapproved_image_rejects_old_url_until_republished(self):
        first = self.storage.publish_product(self.directory)
        source = self.directory / self.slots[0]["output_path"]
        write_solid_png(source, 900, 1200, rgb=(100, 100, 100))
        approve(self.directory, "images")
        stale = self.preflight()
        self.assertFalse(stale["ok"])
        self.assertFalse(stale["image_publication_binding"]["ok"])
        self.assertIn("重新", " ".join(stale["problems"]))
        second = self.storage.publish_product(self.directory)
        self.assertNotEqual(first["urls"], second["urls"])
        self.assertTrue(self.preflight()["ok"])

    def test_plan_changed_reapproved_same_bytes_requires_new_manifest_without_put(self):
        self.storage.publish_product(self.directory)
        puts_before = sum(name == "put_object" for name, _ in self.client.calls)
        plan = read_json(self.directory / "output/image-plan.json")
        plan["main_images"][0]["prompt"] = "TEST ONLY revised prompt"
        write_json(self.directory / "output/image-plan.json", plan)
        report = read_json(self.directory / "output/image-generation-report.json")
        report["files"][0]["slot_fingerprint"] = slot_fingerprint(plan["main_images"][0])
        write_json(self.directory / "output/image-generation-report.json", report)
        approve(self.directory, "images")
        self.assertFalse(self.preflight()["ok"])
        self.storage.publish_product(self.directory)
        self.assertEqual(sum(name == "put_object" for name, _ in self.client.calls), puts_before)
        self.assertTrue(self.preflight()["ok"])

    def test_legacy_manifest_is_rejected_and_republish_upgrades_without_put(self):
        first = self.storage.publish_product(self.directory)
        write_json(self.directory / "output/image-public-urls.json", {"urls": first["urls"]})
        blocked = self.preflight()
        self.assertFalse(blocked["ok"])
        self.assertIn("缺少内容版本", " ".join(blocked["problems"]))
        second = self.storage.publish_product(self.directory)
        self.assertEqual(second["uploaded"], 0)
        self.assertTrue(self.preflight()["ok"])

    def test_unapproved_images_are_not_authorized_by_the_manifest(self):
        self.storage.publish_product(self.directory)
        write_json(self.directory / "input/guided-review.json", {"approved": {}})
        blocked = self.preflight()
        self.assertFalse(blocked["ok"])
        self.assertIn("尚未审核", " ".join(blocked["problems"]))

    def test_mapping_url_tamper_and_invalid_lengths_are_rejected(self):
        self.storage.publish_product(self.directory)
        manifest = read_json(self.directory / "output/image-public-urls.json")
        for changed in ("different.png", True, None):
            with self.subTest(changed=changed):
                current = read_json(self.directory / "output/image-public-urls.json")
                if isinstance(changed, str):
                    current["urls"]["main-S1"] = "https://test-only.invalid/" + changed
                else:
                    current["files"]["main-S1"]["size_bytes"] = changed
                self.assertFalse(image_publication_binding(self.directory, current)["ok"])
        self.assertTrue(image_publication_binding(self.directory, manifest)["ok"])

    def test_anonymous_200_with_other_bytes_is_rejected(self):
        self.storage.publish_product(self.directory)
        def wrong_body(request, timeout=None):
            response = io.BytesIO(b"<html>200 but not the reviewed image</html>")
            response.status = 200
            return response
        checked = self.preflight(opener=wrong_body)
        self.assertFalse(checked["ok"])
        self.assertFalse(checked["url_checks"][0]["content_matches"])

    def test_all_planned_urls_are_checked_not_just_first_twelve(self):
        self.make_images(count=13)
        self.storage.publish_product(self.directory)
        calls = []
        def last_wrong(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) == 13:
                response = io.BytesIO(b"wrong final image")
                response.status = 200
                return response
            return self.anonymous_get(request, timeout)
        checked = self.preflight(opener=last_wrong)
        self.assertEqual(len(calls), 13)
        self.assertFalse(checked["ok"])

    def test_mid_upload_edit_does_not_replace_previous_manifest(self):
        self.storage.publish_product(self.directory)
        target = self.directory / "output/image-public-urls.json"
        previous = target.read_bytes()
        source = self.directory / self.slots[0]["output_path"]
        write_solid_png(source, 900, 1200, rgb=(200, 200, 200))
        approve(self.directory, "images")
        real_put = self.client.put_object
        def edit_during_put(**kwargs):
            result = real_put(**kwargs)
            write_solid_png(source, 900, 1200, rgb=(10, 10, 10))
            return result
        with patch.object(self.client, "put_object", edit_during_put), self.assertRaisesRegex(CosError, "上传期间变化"):
            self.storage.publish_product(self.directory)
        self.assertEqual(target.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
