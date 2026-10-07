"""HTTP offer-prefix/reservation and unified document use isolated local state."""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pipeline.listing_form import write_json
from tests.test_authorized_forms_api import AuthorizedFormsApiTests


class WorkbenchCardApiTests(unittest.TestCase):
    setUp = AuthorizedFormsApiTests.setUp
    tearDown = AuthorizedFormsApiTests.tearDown
    authorize = AuthorizedFormsApiTests.authorize
    product = AuthorizedFormsApiTests.product

    def setup_product(self):
        self.authorize()
        product_id, base = self.product()
        directory = self.root / "products" / product_id
        return product_id, base, directory

    def test_get_profile_and_document_do_not_allocate_fetch_or_create_registry(self):
        product_id, base, directory = self.setup_product()
        self.transports.clear()
        database = self.root / "runtime/listing-offer-ids.sqlite3"
        before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        response = self.client.get("/api/workbench/offer-prefix", params={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["profile"]["saved"])
        document = self.client.get(base + "/listing-document")
        self.assertEqual(document.status_code, 200, document.text)
        self.assertFalse(document.json()["document"]["offer_ids"]["complete"])
        self.assertEqual(document.json()["document"]["model_calls"], 0)
        self.assertFalse(database.exists())
        self.assertEqual(self.transports, [])
        after = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_employee_prefix_saved_once_and_reservation_reuses_it(self):
        product_id, base, directory = self.setup_product()
        saved = self.client.put("/api/workbench/offer-prefix", json={"profile_id": "qa-employee", "shop": "qa-store", "prefix": "xzj.jp"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["profile"]["saved"])
        first = self.client.post(base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertEqual(first.status_code, 200, first.text)
        offers = first.json()["offer_ids"]["offers"]
        self.assertEqual(len(set(offers.values())), 2)
        self.assertTrue(all(offer.startswith("xzj.jp.") for offer in offers.values()))
        repeated = self.client.post(base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "qa-store", "prefix": "another.prefix"})
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["offer_ids"]["offers"], offers)
        document = self.client.get(base + "/listing-document").json()["document"]
        self.assertEqual(document["offer_ids"]["offers"], offers)
        self.assertEqual(document["operational_fields"]["offer_ids"], offers)
        self.assertEqual({row["source_sku_id"]: row["offer_id"] for row in document["selected_skus"]}, offers)
        profile = self.client.get("/api/workbench/offer-prefix", params={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertEqual(profile.json()["profile"]["prefix"], "another.prefix")

    def test_new_selected_sku_allocates_only_its_number_and_old_offer_kept(self):
        product_id, base, directory = self.setup_product()
        selected = self.client.post(base + "/skus", json={"include": ["S1"]})
        self.assertEqual(selected.status_code, 200, selected.text)
        first = self.client.post(base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertEqual(first.status_code, 200, first.text)
        first_offers = first.json()["offer_ids"]["offers"]
        self.client.post(base + "/skus", json={"include": ["S1", "S2"]})
        before = self.client.get(base + "/listing-document").json()["document"]
        self.assertFalse(before["offer_ids"]["complete"])
        self.assertEqual(before["missing"]["offer_ids"], ["S2"])
        second = self.client.post(base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertEqual(second.status_code, 200, second.text)
        second_offers = second.json()["offer_ids"]["offers"]
        self.assertEqual(second_offers["S1"], first_offers["S1"])
        self.assertNotEqual(second_offers["S2"], first_offers["S1"])
        with closing(sqlite3.connect(self.root / "runtime/listing-offer-ids.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_bindings WHERE product_id=?", (product_id,)).fetchone()[0], 2)

    def test_foreign_store_invalid_prefix_and_post_submit_reservation_rejected(self):
        product_id, base, directory = self.setup_product()
        wrong = self.client.post(base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "foreign"})
        self.assertEqual(wrong.status_code, 422)
        invalid = self.client.put("/api/workbench/offer-prefix", json={"profile_id": "qa-employee", "shop": "qa-store", "prefix": "bad/prefix"})
        self.assertEqual(invalid.status_code, 422)
        write_json(directory / "runtime/listing-submit-attempt.json", {"state": "unknown_requires_readback"})
        blocked = self.client.post(base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertIn(blocked.status_code, (409, 422))
        self.assertFalse((self.root / "runtime/listing-offer-ids.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
