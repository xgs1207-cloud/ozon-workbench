"""Number reservation is durable, idempotent, and unique across workers."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.listing_form import read_json, write_json
from pipeline.listing_offer_ids import (read_profile, save_profile, read_offer_ids,
                                       reserve_offer_ids, offer_for_variant)


INSTANT = datetime(2026, 10, 7, 16, 5, tzinfo=timezone.utc)


def process_reserve(arguments):
    product, database, profile = arguments
    return reserve_offer_ids(Path(product), "shop-a", profile, db_path=Path(database), now=INSTANT)["offers"]


class ListingOfferIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "runtime/offers.sqlite3"
        self.product = self.make_product("P000001")

    def tearDown(self):
        self.tmp.cleanup()

    def make_product(self, name, *, skus=None):
        directory = self.root / "products" / name
        write_json(directory / "input/source.json", {"product_id": name, "title_zh": "商品", "skus": skus or [{"sku_id": "S1"}, {"sku_id": "S2"}]})
        write_json(directory / "input/selected-skus.json", {"selected": ["S1", "S2"]})
        write_json(directory / "input/category-selection.json", {"shop_id": "shop-a"})
        write_json(directory / "input/guided-workflow.json", {})
        return directory

    def reserve(self, product=None, **kwargs):
        return reserve_offer_ids(product or self.product, "shop-a", "employee-a", db_path=self.database, now=kwargs.pop("now", INSTANT), **kwargs)

    def test_get_no_database_file_creation_or_number_allocation(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(read_profile("employee-a", "shop-a", db_path=self.database)["prefix"], "xzj.jp")
        self.assertFalse(read_offer_ids(self.product, "shop-a", db_path=self.database)["complete"])
        self.assertIsNone(offer_for_variant(self.product, "shop-a", {"sku_id": "S1"}, 1, db_path=self.database))
        self.assertFalse(self.database.exists())
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_get_during_initialization_does_not_create_missing_sql_schema(self):
        self.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE offer_profiles (profile_id TEXT)")
            connection.commit()
        self.assertFalse(read_profile("employee-a", "shop-a", db_path=self.database)["saved"])
        self.assertFalse(read_offer_ids(self.product, "shop-a", db_path=self.database)["complete"])
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"offer_profiles"})

    def test_beijing_date_unpadded_month_day_and_each_sku_unique(self):
        result = self.reserve()
        self.assertEqual(result["offers"], {"S1": "xzj.jp.10.8.1", "S2": "xzj.jp.10.8.2"})
        self.assertEqual(result["beijing_date"], "2026-10-08")
        self.assertEqual(read_json(self.product / "input/listing-offer-ids.json")["shops"]["shop-a"]["offers"], result["offers"])

    def test_refresh_retry_and_prefix_change_keep_original_bound_ids(self):
        first = self.reserve()
        save_profile("employee-a", "shop-a", "other.staff", db_path=self.database)
        next_day = datetime(2026, 10, 8, 16, 5, tzinfo=timezone.utc)
        second = self.reserve(now=next_day)
        self.assertEqual(first["offers"], second["offers"])
        self.assertEqual(second["beijing_date"], "2026-10-08")
        self.assertEqual(second["requested_date"], "2026-10-09")
        self.assertEqual(second["items"][0]["prefix"], "xzj.jp")
        other = self.reserve(self.make_product("P000002"), now=next_day)
        self.assertEqual(other["offers"]["S1"], "other.staff.10.9.1")

    def test_profiles_persist_per_employee_and_shop_without_spending_numbers(self):
        save_profile("e1", "shop-a", "one.staff", db_path=self.database)
        save_profile("e2", "shop-a", "two.staff", db_path=self.database)
        save_profile("e1", "shop-b", "third.staff", db_path=self.database)
        self.assertEqual(read_profile("e1", "shop-a", db_path=self.database)["prefix"], "one.staff")
        self.assertEqual(read_profile("e1", "shop-b", db_path=self.database)["prefix"], "third.staff")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_bindings").fetchone()[0], 0)

    def test_day_resets_and_previous_year_rendered_collisions_are_skipped(self):
        self.reserve()
        next_day = self.reserve(self.make_product("P000002"), now=datetime(2026, 10, 8, 16, 5, tzinfo=timezone.utc))
        self.assertEqual(next_day["offers"]["S1"], "xzj.jp.10.9.1")
        next_year = self.reserve(self.make_product("P000003"), now=datetime(2027, 10, 7, 16, 5, tzinfo=timezone.utc))
        self.assertEqual(next_year["offers"], {"S1": "xzj.jp.10.8.3", "S2": "xzj.jp.10.8.4"})

    def test_selected_sku_addition_allocates_only_new_binding(self):
        write_json(self.product / "input/selected-skus.json", {"selected": ["S1"]})
        first = self.reserve()
        write_json(self.product / "input/selected-skus.json", {"selected": ["S2", "S1"]})
        second = self.reserve()
        self.assertEqual(second["offers"]["S1"], first["offers"]["S1"])
        self.assertEqual(second["offers"]["S2"], "xzj.jp.10.8.2")

    def test_legacy_exact_offer_ids_preserved_not_sanitized_or_replaced(self):
        source = read_json(self.product / "input/source.json")
        source["skus"][0]["offer_id"] = "老货号.已发布"
        write_json(self.product / "input/source.json", source)
        result = self.reserve()
        self.assertEqual(result["offers"]["S1"], "老货号.已发布")
        self.assertEqual(result["items"][0]["source"], "legacy")
        self.assertEqual(result["offers"]["S2"], "xzj.jp.10.8.1")

    def test_published_ledger_offer_wins_and_cannot_be_renumbered(self):
        write_json(self.product / "output/store-publications.json", {"stores": {"shop-a": {"sku_publications": [{"sku_id": "S1", "offer_id": "published.unchanged", "task_id": "9"}]}}})
        write_json(self.product / "status.json", {"api_write_count": 1})
        self.assertEqual(read_offer_ids(self.product, "shop-a", db_path=self.database)["offers"]["S1"], "published.unchanged")
        with self.assertRaisesRegex(ValueError, "写入"):
            self.reserve()
        self.assertFalse(self.database.exists())

    def test_legacy_offer_collision_rolls_back_whole_reservation(self):
        original = self.make_product("P000010", skus=[{"sku_id": "S1", "offer_id": "same.offer"}])
        self.reserve(original)
        conflicting = self.make_product("P000011", skus=[{"sku_id": "S1", "offer_id": "same.offer"}, {"sku_id": "S2"}])
        with self.assertRaisesRegex(ValueError, "冲突|占用"):
            self.reserve(conflicting)
        self.assertFalse(read_offer_ids(conflicting, "shop-a", db_path=self.database)["items"][1].get("offer_id"))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_bindings WHERE product_id='P000011'").fetchone()[0], 0)

    def test_old_local_published_rendered_offer_is_skipped_before_first_allocation(self):
        old = self.make_product("P000099", skus=[{"sku_id": "S1"}])
        write_json(old / "output/store-publications.json", {"stores": {"shop-a": {"sku_publications": [{"sku_id": "S1", "offer_id": "xzj.jp.10.8.1", "task_id": "1"}]}}})
        result = self.reserve()
        self.assertEqual(result["offers"]["S1"], "xzj.jp.10.8.2")
        self.assertEqual(read_offer_ids(old, "shop-a", db_path=self.database)["offers"]["S1"], "xzj.jp.10.8.1")

    def test_crash_after_sql_commit_reuses_same_ids_and_repairs_mirror(self):
        with patch("pipeline.listing_offer_ids.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.reserve()
        self.assertFalse((self.product / "input/listing-offer-ids.json").exists())
        result = self.reserve()
        self.assertEqual(result["offers"]["S1"], "xzj.jp.10.8.1")
        other = self.reserve(self.make_product("P000002"))
        self.assertEqual(other["offers"]["S1"], "xzj.jp.10.8.3")

    def test_threads_do_not_duplicate_daily_sequence(self):
        products = [self.make_product(f"P{index:06d}") for index in range(10, 20)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda directory: self.reserve(directory), products))
        offers = [offer for result in results for offer in result["offers"].values()]
        self.assertEqual(len(set(offers)), 20)

    def test_processes_do_not_duplicate_daily_sequence(self):
        products = [self.make_product(f"P{index:06d}") for index in range(30, 34)]
        save_profile("seed", "shop-a", "xzj.jp", db_path=self.database)
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as workers:
            results = list(workers.map(process_reserve, [(str(path), str(self.database), "employee-a") for path in products]))
        offers = [offer for result in results for offer in result.values()]
        self.assertEqual(len(set(offers)), 8)

    def test_retries_same_product_different_employees_only_two_total_numbers(self):
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(lambda n: reserve_offer_ids(self.product, "shop-a", f"employee-{n}", db_path=self.database, now=INSTANT), range(6)))
        self.assertTrue(all(result["offers"] == results[0]["offers"] for result in results))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_bindings").fetchone()[0], 2)

    def test_invalid_prefix_timezone_or_foreign_shop_never_allocates(self):
        for prefix in ("", "prefix/unsafe", "坏前缀", "x" * 29):
            with self.assertRaises(ValueError):
                self.reserve(prefix=prefix)
        with self.assertRaisesRegex(ValueError, "时区"):
            self.reserve(now=datetime(2026, 10, 8))
        with self.assertRaisesRegex(ValueError, "类目"):
            reserve_offer_ids(self.product, "shop-b", "e", db_path=self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_bindings").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
