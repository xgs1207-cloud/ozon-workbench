"""One selected-reference whitelist across UI, vision, old plans and originals."""
from copy import deepcopy
import hashlib
import unittest

from models.image_plan import _list_reference_images
from pipeline import image_jobs
from pipeline.captured_images import capture_proof
from pipeline.image_insights import analyze_image_insights
from pipeline.listing_form import read_json, write_json
from pipeline.reference_images import selected_reference_images
from tests import test_image_insights as vision_fixtures
from tests.test_image_jobs import OfflineGenerator


class ReferenceWhitelistTests(unittest.TestCase):
    def setUp(self):
        self.fixture = vision_fixtures.ImageInsightsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.directory = self.fixture.directory
        self.unknown = self.fixture.other_image
        source = read_json(self.directory / "input/source.json")
        source["skus"][1].pop("image_path", None)
        source["skus"][1].pop("image_refs", None)
        source["image_sources"] = [row for row in source.get("image_sources") or [] if row.get("path") != self.unknown]
        write_json(self.directory / "input/source.json", source)
        # Seal this intentionally unassociated fixture; failures must be from
        # scope checks, not an unrelated source tamper check.
        manifest = read_json(self.directory / "input/source-manifest.json")
        for row in manifest.get("files") or []:
            if row["path"] == "input/source.json":
                data = (self.directory / row["path"]).read_bytes()
                row.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        write_json(self.directory / "input/source-manifest.json", manifest)

    def test_unknown_variant_photo_hidden_and_not_in_automatic_vision_rpc(self):
        self.assertNotIn(self.unknown, [row["path"] for row in selected_reference_images(self.directory)])
        transport = vision_fixtures.VisionTransport()
        result = analyze_image_insights(self.directory, provider=transport)
        paths = [row["path"] for row in result["payload"]["image_evidence"]]
        self.assertNotIn(self.unknown, paths)
        self.assertEqual(paths[0], self.fixture.selected_image)
        self.assertIn(self.fixture.shared_image, paths)

    def test_direct_vision_selection_of_unknown_variant_rejected_before_rpc(self):
        transport = vision_fixtures.VisionTransport()
        with self.assertRaisesRegex(ValueError, "规格关联"):
            analyze_image_insights(self.directory, image_paths=[self.unknown], provider=transport)
        self.assertEqual(transport.calls, [])

    def test_old_plan_unknown_variant_cannot_queue_paid_generation(self):
        selected = selected_reference_images(self.directory)[0]
        created = image_jobs.add_image_slot(self.directory, prompt="突出真实商品的外观", reference_ids=[selected["id"]])
        plan = deepcopy(created["image_plan"])
        unknown = next(row for row in _list_reference_images(self.directory) if row["path"] == self.unknown)
        spec = next(row for row in image_jobs._slots(plan) if row["slot"] == created["slot"])
        spec.update(reference_product_images=[self.unknown], reference_image_ids=[unknown["id"]])
        plan["reference_images"].append(unknown)
        write_json(self.directory / image_jobs.PLAN_FILE, plan)
        generator = OfflineGenerator()
        with self.assertRaisesRegex(ValueError, "规格关联"):
            image_jobs.enqueue_image(self.directory, created["slot"], generator=generator, dispatch=False)
        self.assertEqual(generator.calls, [])
        self.assertFalse((self.directory / image_jobs.JOBS_FILE).is_file())

    def test_direct_original_adoption_of_unknown_variant_rejected(self):
        with self.assertRaisesRegex(ValueError, "规格关联"):
            capture_proof(self.directory, self.unknown, source_sku_id="S1")
        proof = capture_proof(self.directory, self.fixture.selected_image, source_sku_id="S1")
        self.assertEqual(proof["assigned_sku_id"], "S1")

    def test_duplicate_thumbnail_for_selected_and_unselected_same_color_remains_usable(self):
        source = read_json(self.directory / "input/source.json")
        source["skus"][1]["image_path"] = self.fixture.selected_image
        write_json(self.directory / "input/source.json", source)
        transport = vision_fixtures.VisionTransport()
        result = analyze_image_insights(self.directory, image_paths=[self.fixture.selected_image], provider=transport)
        self.assertEqual(result["payload"]["selected_sku_ids"], ["S1"])
        self.assertEqual(result["payload"]["image_evidence"][0]["source_sku_ids"], ["S1", "S2"])


if __name__ == "__main__":
    unittest.main()
