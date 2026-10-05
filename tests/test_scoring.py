"""打分逻辑的单元测试（只用标准库，可脱离 FastAPI 运行）。

运行： python -m unittest discover -s tests -p 'test*.py'
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from keyword_library.scoring import ScoreConfig, score_records  # noqa: E402


def record(keyword: str, heat, competitors, category_id="100", type_id="200"):
    return {
        "key": f"{category_id}:{type_id}:{keyword}",
        "keyword": keyword,
        "category_id": category_id,
        "type_id": type_id,
        "search_volume": heat,
        "competitor_count": competitors,
    }


class PercentileTests(unittest.TestCase):
    def test_percentiles_are_rank_based_within_category(self):
        rows = [
            record("a", 100, 10),
            record("b", 200, 10),
            record("c", 300, 10),
            record("d", 400, 10),
            record("e", 500, 10),
        ]
        results = score_records(rows)
        heats = [results[row["key"]].heat_percentile for row in rows]
        self.assertEqual(heats, [0.1, 0.3, 0.5, 0.7, 0.9])

    def test_low_competition_is_a_low_percentile(self):
        rows = [
            record("a", 500, 10),
            record("b", 500, 50),
        ]
        results = score_records(rows)
        self.assertLess(
            results[rows[0]["key"]].competition_percentile,
            results[rows[1]["key"]].competition_percentile,
        )
        self.assertGreater(results[rows[0]["key"]].score, results[rows[1]["key"]].score)

    def test_categories_are_scored_independently(self):
        rows = [
            record("hot", 10_000, 100, category_id="1"),
            record("cold", 10, 1, category_id="1"),
            record("mid", 50, 5, category_id="2"),
            record("mid2", 60, 6, category_id="2"),
        ]
        results = score_records(rows)
        # 类目 1 里 10 的搜索量虽然绝对值很小，但在本类目内是最低分位
        self.assertLess(results[rows[1]["key"]].heat_percentile, 0.5)
        self.assertGreater(results[rows[0]["key"]].heat_percentile, 0.5)


class QualificationTests(unittest.TestCase):
    def test_qualified_needs_high_heat_and_low_competition(self):
        rows = [
            record("winner", 500, 10),
            record("loser", 100, 50),
            record("middle", 300, 30),
            record("hotcrowded", 400, 40),
            record("coldempty", 200, 20),
        ]
        results = score_records(rows)
        self.assertTrue(results[rows[0]["key"]].qualified)
        self.assertFalse(results[rows[1]["key"]].qualified)
        self.assertIn("竞争分位", " ".join(results[rows[1]["key"]].reasons))

    def test_missing_metric_is_not_guessed(self):
        rows = [
            record("noh", None, 10),
            record("noc", 500, None),
            record("ok", 500, 10),
        ]
        results = score_records(rows)
        self.assertIsNone(results[rows[0]["key"]].score)
        self.assertFalse(results[rows[0]["key"]].qualified)
        self.assertIn("搜索量", " ".join(results[rows[0]["key"]].reasons))
        self.assertIsNone(results[rows[1]["key"]].score)
        self.assertIn("竞品数", " ".join(results[rows[1]["key"]].reasons))

    def test_string_and_comma_metrics_are_parsed(self):
        rows = [
            record("a", "1,200", "3"),
            record("b", "900", "12"),
        ]
        results = score_records(rows)
        self.assertIsNotNone(results[rows[0]["key"]].score)

    def test_lambda_shifts_ranking(self):
        rows = [
            record("hotcrowded", 500, 50),
            record("warmclean", 300, 10),
        ]
        gentle = score_records(rows, ScoreConfig(lam=0.1, min_heat_percentile=0.0, max_competition_percentile=1.0))
        harsh = score_records(rows, ScoreConfig(lam=3.0, min_heat_percentile=0.0, max_competition_percentile=1.0))
        self.assertGreater(gentle[rows[0]["key"]].score, gentle[rows[1]["key"]].score)
        self.assertGreater(harsh[rows[1]["key"]].score, harsh[rows[0]["key"]].score)

    def test_absolute_bounds(self):
        rows = [record("a", 100, 10), record("b", 500, 10)]
        config = ScoreConfig(
            lam=0.0,
            min_heat_percentile=0.0,
            max_competition_percentile=1.0,
            min_search_volume=200,
        )
        results = score_records(rows, config)
        self.assertFalse(results[rows[0]["key"]].qualified)
        self.assertTrue(results[rows[1]["key"]].qualified)

    def test_small_group_is_flagged_but_still_scored(self):
        rows = [record("a", 500, 10), record("b", 100, 50)]
        results = score_records(rows)
        self.assertIsNotNone(results[rows[0]["key"]].score)
        self.assertIn("参考性弱", " ".join(results[rows[0]["key"]].reasons))


if __name__ == "__main__":
    unittest.main()
