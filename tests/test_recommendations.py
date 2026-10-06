"""The recommendation UI must never mix vendors or invent missing history."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import api
from market_intelligence.recommend import RecommendConfig, recommend
from market_intelligence.sessions import attach_product, create_session, get_session, set_auto_publish
from market_intelligence.store import ingest_snapshot


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "market.sqlite3"
        self.old_db = api.MARKET_DB_PATH
        api.MARKET_DB_PATH = self.db

    def tearDown(self):
        api.MARKET_DB_PATH = self.old_db
        self.temp.cleanup()

    def insert(self, dataset, period, rows, *, source="seerfar"):
        ingest_snapshot(
            self.db, source=source, dataset=dataset,
            capture_method="file_import" if source == "seerfar" else "official_api",
            period=period, page_url="", captured_at=f"{period or '2026-10'}-06T00:00:00Z",
            records=rows,
        )

    def test_stable_low_competition_category_is_first_and_return_is_penalty(self):
        for month in ("2026-07", "2026-08", "2026-09"):
            self.insert("categories", month, [
                {"类目": "床单", "销售额": "10万", "销量": 1000, "竞品数": 100, "竞对数": 20, "退货取消率": "5%"},
                {"类目": "杯子", "销售额": "30万" if month == "2026-09" else "1万", "销量": 1000, "竞品数": 800, "竞对数": 800, "退货取消率": "10%"},
            ])
        result = recommend(self.db, dataset="categories")
        self.assertEqual(result["recommended_count"], 2)
        self.assertEqual(result["items"][0]["label"], "床单")
        self.assertIn("退货/取消率仅作扣分", " ".join(result["items"][0]["reasons"]))

    def test_one_month_is_not_recommended_and_ozon_demand_is_not_added(self):
        self.insert("keywords", "2026-09", [{"关键词": "простыня", "类目": "床单", "月搜热度": 1000, "竞对数": 10}])
        self.insert("keywords", "", [{"query": "простыня", "client_count": 999999}], source="ozon_seller_api")
        result = recommend(self.db, dataset="keywords", category_key="床单")
        row = result["items"][0]
        self.assertFalse(row["recommended"])
        self.assertEqual(row["metrics"]["demand"], 1000)
        self.assertIsNotNone(row["ozon_evidence"])

    def test_invalid_config_is_rejected(self):
        with self.assertRaises(ValueError):
            RecommendConfig.from_mapping({"weights": {"categories": {"demand": 2}}})

    def test_actual_report_columns_use_volume_for_category_competition(self):
        for month in ("2026-07", "2026-08", "2026-09"):
            self.insert("categories", month, [{"类目": "床单\nПростыня", "销售方式": "跨境卖家可售",
                                               "销售额": "100000₽", "销量": 1000, "竞对数": 20,
                                               "竞品数": 200, "退货取消率": "12.5%"}])
        row = recommend(self.db, dataset="categories")["items"][0]
        self.assertEqual(row["label"], "床单")
        self.assertEqual(row["metrics"]["competition_density"], 0.2)
        self.assertEqual(row["metrics"]["density_unit"], "竞品数/销量")
        self.assertTrue(row["recommended"])

    def test_bilingual_keyword_keeps_only_russian_query_for_listing(self):
        for month in ("2026-07", "2026-08", "2026-09"):
            self.insert("keywords", month, [{"关键词": "простыня\n(床单)", "类目": "床单\nПростыня",
                                              "销售方式": "跨境卖家可售", "月搜热度": 1000,
                                              "竞品数": 100, "竞对数": 10, "转化率": "10%"}])
        row = recommend(self.db, dataset="keywords", category_key="床单 простыня")["items"][0]
        self.assertEqual(row["key"], "простыня")
        self.assertEqual(row["label"], "простыня")
        self.assertTrue(row["recommended"])

    def test_api_exposes_config_and_read_only_recommendations(self):
        client = TestClient(api.app)
        response = client.get("/api/research/categories")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(client.put("/api/research/config", json={"min_history_months": 2}).status_code, 200)
        self.assertEqual(client.get("/api/research/config").json()["config"]["min_history_months"], 2)

    def test_one_research_session_can_bind_multiple_products_and_requires_manual_price(self):
        for month in ("2026-07", "2026-08", "2026-09"):
            self.insert("categories", month, [{"类目": "床单", "销售额": 100000, "销量": 1000,
                                               "竞品数": 100, "竞对数": 20}])
            self.insert("keywords", month, [
                {"关键词": "простыня", "类目": "床单", "月搜热度": 1000, "竞品数": 100, "竞对数": 10},
                {"关键词": "простыня хлопковая", "类目": "床单", "月搜热度": 500, "竞品数": 50, "竞对数": 5},
            ])
        session = create_session(self.db, category_key="床单", primary_keyword="простыня",
                                 secondary_keywords=["простыня хлопковая"])
        root = Path(self.temp.name) / "products"
        for index in (1, 2):
            product = root / f"P{index:06d}"
            (product / "input").mkdir(parents=True)
            (product / "status.json").write_text("{}", encoding="utf-8")
            (product / "input" / "source.json").write_text(
                '{"source_url":"https://detail.1688.com/offer/123456.html"}', encoding="utf-8"
            )
            attach_product(self.db, root, session_id=session["id"], product_id=product.name)
            self.assertTrue((product / "input" / "manual-pricing-required.json").is_file())
        self.assertEqual(len(get_session(self.db, session["id"])["products"]), 2)
        self.assertFalse(session["auto_publish_enabled"])
        with self.assertRaises(ValueError):
            set_auto_publish(self.db, session_id=session["id"], enabled=True)


if __name__ == "__main__":
    unittest.main()
