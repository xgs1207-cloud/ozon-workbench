"""Full 1688 catalog collection stays inert until workbench SKU selection."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector.ingest import CaptureValidationError, ingest_capture, normalize_payload
from pipeline.batch import create_batch
from pipeline.sku_selection import active_skus, clear_selection, selection_state, set_selection


def catalog(count=56):
    return {"source_url": "https://detail.1688.com/offer/1072823232979.html",
            "title_cn": "圣诞树捏捏乐", "collection_mode": "all_skus",
            "skus": [{"sku_id": str(10000 + i), "sku_name": f"颜色{i}", "purchase_price": 4.5}
                     for i in range(count)]}


class FullCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_56_preserved_no_implicit_listing_and_selection_is_non_destructive(self):
        saved = ingest_capture(self.root, catalog())
        directory = self.root / saved["product_id"]
        source_file = directory / "input/source.json"
        original = source_file.read_bytes()
        source = json.loads(original)
        self.assertEqual(len(source["skus"]), 56)
        self.assertIsNone(source["selected_category"])
        self.assertTrue(saved["sku_selection_required"])
        self.assertEqual(active_skus(directory, source["skus"]), [])
        self.assertEqual(selection_state(directory)["selected"], [])
        self.assertFalse((directory / "input/selected-skus.json").exists())
        with self.assertRaises(ValueError):
            create_batch(self.root, product_ids=[saved["product_id"]], batches_root=self.root / "batches")
        set_selection(directory, include=["10000", "10055"])
        self.assertEqual(len(active_skus(directory, source["skus"])), 2)
        self.assertEqual(source_file.read_bytes(), original)
        batch = create_batch(self.root, product_ids=[saved["product_id"]], batches_root=self.root / "batches")
        self.assertEqual(batch["sku_count"], 2)
        clear_selection(directory)
        self.assertEqual(active_skus(directory, source["skus"]), [])

    def test_incomplete_catalog_rows_preserved_but_cannot_be_selected(self):
        payload = catalog(3)
        payload["skus"][1]["purchase_price"] = None
        payload["skus"][2]["sku_id"] = "10000"
        saved = ingest_capture(self.root, payload)
        directory = self.root / saved["product_id"]
        source = json.loads((directory / "input/source.json").read_text(encoding="utf-8"))
        self.assertEqual(len(source["skus"]), 3)
        self.assertIsNone(source["skus"][1]["purchase_price_cny"])
        self.assertEqual(source["skus"][2]["source_sku_id"], "10000")
        self.assertEqual(source["skus"][2]["sku_id"], "CAPTURE-ROW-3")
        with self.assertRaises(ValueError):
            set_selection(directory, include=["10001"])
        set_selection(directory, include=["10000"])

    def test_legacy_limit_and_catalog_safety_cap_are_not_silent_truncation(self):
        old = catalog(11)
        old.pop("collection_mode")
        with self.assertRaises(CaptureValidationError):
            normalize_payload(old)
        with self.assertRaisesRegex(CaptureValidationError, "未截断"):
            normalize_payload(catalog(2001))
        self.assertEqual(len(normalize_payload(catalog(2000))["skus"]), 2000)

    def test_image_roles_and_local_sku_mapping_survive_duplicates_and_download_once(self):
        payload = catalog(2)
        url = "https://cbu01.alicdn.com/img/ibank/test.jpg"
        payload.update(main_images=[{"url": url}], detail_images=[{"url": url}])
        for sku in payload["skus"]:
            sku["image_url"] = url
        with patch("collector.ingest._download_remote_image", return_value=(b"\xff\xd8\xffsame", ".jpg")) as download:
            saved = ingest_capture(self.root, payload)
        download.assert_called_once_with(url)
        self.assertEqual([saved["counts"][f"{role}_images"] for role in ("main", "sku", "detail")], [1, 1, 1])
        directory = self.root / saved["product_id"]
        source = json.loads((directory / "input/source.json").read_text(encoding="utf-8"))
        paths = [row["image_path"] for row in source["skus"]]
        self.assertEqual(paths[0], paths[1])
        self.assertTrue((directory / paths[0]).is_file())
        self.assertTrue(paths[0].endswith(".jpg"))

    def test_http_collection_and_backend_selection_with_metadata(self):
        import api as api_module
        from fastapi.testclient import TestClient
        with patch.object(api_module, "PRODUCTS_ROOT", self.root):
            client = TestClient(api_module.app)
            result = client.post("/api/collector/products", json=catalog())
            self.assertEqual(result.status_code, 200, result.text)
            pid = result.json()["product_id"]
            state = client.get(f"/api/workbench/products/{pid}/skus").json()
            self.assertTrue(state["pending_selection"])
            self.assertEqual(len(state["skus"]), 56)
            self.assertEqual(state["skus"][55]["sku_name"], "颜色55")
            self.assertFalse(any(row["listed"] for row in state["skus"]))
            before = (self.root / pid / "input/source.json").read_bytes()
            rejected = client.post(f"/api/workbench/products/{pid}/skus", json={"all": True})
            self.assertEqual(rejected.status_code, 422)
            selected = client.post(f"/api/workbench/products/{pid}/skus", json={"include": ["10000", "10055"]})
            self.assertEqual(selected.status_code, 200, selected.text)
            self.assertEqual(selected.json()["state"]["active_count"], 2)
            self.assertEqual((self.root / pid / "input/source.json").read_bytes(), before)
            version = client.post("/api/collector/products?allow_new_version=true", json=catalog())
            self.assertEqual(version.status_code, 200, version.text)
            self.assertEqual(version.json()["duplicate_of"], pid)

    def test_empty_selection_file_is_fail_closed(self):
        saved = ingest_capture(self.root, catalog(2))
        directory = self.root / saved["product_id"]
        (directory / "input/selected-skus.json").write_text('{"selected":[]}', encoding="utf-8")
        self.assertEqual(active_skus(directory, catalog(2)["skus"]), [])


if __name__ == "__main__":
    unittest.main()
