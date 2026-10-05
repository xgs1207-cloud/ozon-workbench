"""关键词库存储层单元测试。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from keyword_library import store  # noqa: E402
from keyword_library.scoring import ScoreConfig  # noqa: E402


def rows():
    return [
        {"keyword": "Термос 500 мл", "category_id": "100", "type_id": "200",
         "category_path_zh": "家居/厨房", "search_volume": 500, "competitor_count": 10},
        {"keyword": "термос 500 мл", "category_id": "100", "type_id": "200",
         "search_volume": 500, "competitor_count": 10},
        {"keyword": "Термос 1 л", "category_id": "100", "type_id": "200",
         "search_volume": 100, "competitor_count": 50},
        {"keyword": "Коврик", "category_id": "300", "type_id": "400",
         "search_volume": 300, "competitor_count": 20},
    ]


class UpsertTests(unittest.TestCase):
    def test_duplicate_keyword_merges_into_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = store.upsert(tmp, rows())
            self.assertEqual(summary["created"], 3)  # 大小写不同的同名词合并
            self.assertEqual(summary["updated"], 1)
            records = store.load_category(tmp, "100", "200")
            self.assertEqual(len(records), 2)
            keys = {item["normalized_keyword"] for item in records}
            self.assertIn("термос 500 мл", keys)

    def test_merge_keeps_identity_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, rows())
            record = store.query(tmp, category_id="100", type_id="200")[0]
            store.set_status(tmp, [record["key"]], store.STATUS_IN_LIBRARY, reason="人工确认")
            first_seen = record["first_seen_at"]

            store.upsert(
                tmp,
                [{"keyword": record["keyword"], "category_id": "100", "type_id": "200",
                  "search_volume": 999, "competitor_count": 5}],
            )
            again = store.query(tmp, category_id="100", type_id="200", status=store.STATUS_IN_LIBRARY)
            self.assertEqual(len(again), 1)
            self.assertEqual(again[0]["first_seen_at"], first_seen)
            self.assertEqual(again[0]["search_volume"], 999)
            self.assertTrue(any(item.get("event") == "metrics_updated" for item in again[0]["history"]))

    def test_blank_keyword_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = store.upsert(tmp, [{"keyword": "   ", "category_id": "1", "type_id": "2"}])
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["skipped"], 1)


class ScoringIntegrationTests(unittest.TestCase):
    def test_upsert_promotes_qualified_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, rows())
            qualified = store.query(tmp, status=store.STATUS_QUALIFIED)
            self.assertTrue(qualified)
            self.assertTrue(all(item["score"] is not None for item in qualified))

    def test_rescore_can_demote_when_thresholds_tighten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, rows())
            before = len(store.query(tmp, status=store.STATUS_QUALIFIED))
            self.assertGreater(before, 0)
            store.rescore(tmp, ScoreConfig(min_heat_percentile=0.99, max_competition_percentile=0.01))
            after = store.query(tmp, status=store.STATUS_QUALIFIED)
            self.assertLessEqual(len(after), before)

    def test_missing_metric_never_gets_a_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, [{"keyword": "нет данных", "category_id": "9", "type_id": "9"}])
            record = store.query(tmp, category_id="9", type_id="9")[0]
            self.assertIsNone(record["score"])
            self.assertEqual(record["status"], store.STATUS_CANDIDATE)


class QueryTests(unittest.TestCase):
    def test_filters_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, rows())
            top = store.query(tmp, order="score", limit=2)
            self.assertEqual(len(top), 2)
            self.assertGreaterEqual(top[0]["score"], top[1]["score"])
            self.assertTrue(store.query(tmp, text="термос"))
            self.assertFalse(store.query(tmp, text="несуществующее"))

    def test_set_status_records_product_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, rows())
            key = store.query(tmp, category_id="300", type_id="400")[0]["key"]
            store.set_status(tmp, [key], store.STATUS_USED, product_id="P000123")
            record = store.query(tmp, category_id="300", type_id="400")[0]
            self.assertEqual(record["status"], store.STATUS_USED)
            self.assertIn("P000123", record["selected_for"])


class IndexTests(unittest.TestCase):
    def test_index_summarises_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.upsert(tmp, rows())
            payload = store.index(tmp)
            self.assertEqual(payload["category_count"], 2)
            self.assertEqual(payload["total"], 3)
            self.assertTrue((pathlib.Path(tmp) / "index.json").is_file())

    def test_invalid_status_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                store.set_status(tmp, ["x"], "не_статус")


if __name__ == "__main__":
    unittest.main()
