import tempfile
import unittest
from pathlib import Path
from pipeline.listing_form import write_json
from pipeline.reference_images import selected_reference_images


class SelectedReferenceImageTests(unittest.TestCase):
    def test_selected_thumbnails_first_and_ids_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = ["input/main-images/a.jpg", "input/sku-images/red.jpg",
                     "input/sku-images/orange.jpg", "input/sku-images/unknown.jpg", "input/detail-images/a.jpg"]
            for path in paths:
                local = root / path
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(b"fixture")
            write_json(root / "input/source.json", {"skus": [
                {"sku_id": "red24", "image_path": paths[1]},
                {"sku_id": "red28", "image_path": paths[1]},
                {"sku_id": "orange24", "image_path": paths[2]}]})
            write_json(root / "input/selected-skus.json", {"selected": ["red24"]})
            rows = selected_reference_images(root, annotate=True)
            self.assertEqual([row["path"] for row in rows], [paths[1], paths[0], paths[4]])
            red_id = rows[0]["id"]
            self.assertEqual(rows[0]["selected_sku_ids"], ["red24"])
            write_json(root / "input/selected-skus.json", {"selected": ["red28", "orange24"]})
            updated = selected_reference_images(root)
            self.assertEqual(next(row["id"] for row in updated if row["path"] == paths[1]), red_id)
            self.assertEqual(len(updated), 4)

    def test_no_confirmed_selection_hides_variant_photos(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ["input/sku-images/S1.jpg", "input/main-images/a.jpg"]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            write_json(root / "input/source.json", {"skus": [{"sku_id": "S1", "image_path": "input/sku-images/S1.jpg"}]})
            self.assertEqual([row["role"] for row in selected_reference_images(root)], ["main"])


if __name__ == "__main__":
    unittest.main()
