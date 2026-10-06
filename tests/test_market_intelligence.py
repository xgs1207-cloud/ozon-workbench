"""Market research DB: source separation, idempotency and protected HTTP sink."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from market_intelligence import store


class MarketStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "market.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_distinct_sources_and_repeat_capture(self):
        common = dict(dataset="keywords", period="2026-09", page_url="https://seerfar.cn/report",
                      captured_at="2026-10-06T00:00:00Z")
        seerfar = store.ingest_snapshot(self.db, source="seerfar", capture_method="browser_extension",
                                        records=[{"关键词": "термос", "类目": "保温杯", "月搜热度": "1000"}], **common)
        again = store.ingest_snapshot(self.db, source="seerfar", capture_method="browser_extension",
                                      records=[{"关键词": "термос", "类目": "保温杯", "月搜热度": "1000"}], **common)
        ozon = store.ingest_snapshot(self.db, source="ozon_seller_api", capture_method="official_api",
                                     records=[{"query": "термос", "client_count": 700, "sellers_count": 20}],
                                     dataset="keywords", period="", page_url="https://api-seller.ozon.ru/v1/search-queries/top",
                                     captured_at="2026-10-06T00:00:00Z")
        self.assertEqual(seerfar["inserted"], 1)
        self.assertTrue(again["duplicate_batch"])
        self.assertEqual(ozon["inserted"], 1)
        self.assertEqual(len(store.list_observations(self.db, dataset="keywords")), 2)
        self.assertEqual(store.list_observations(self.db, dataset="keywords", source="seerfar")[0]["category_key"], "保温杯")

    def test_invalid_source_and_missing_month_rejected(self):
        base = dict(dataset="keywords", page_url="https://seerfar.cn/", captured_at="",
                    records=[{"关键词": "термос"}])
        with self.assertRaises(ValueError):
            store.ingest_snapshot(self.db, source="ozon_seller_api", capture_method="browser_extension", period="2026-09", **base)
        with self.assertRaises(ValueError):
            store.ingest_snapshot(self.db, source="seerfar", capture_method="browser_extension", period="", **base)

    def test_ozon_category_id_is_preserved(self):
        store.ingest_snapshot(self.db, source="ozon_seller_api", dataset="categories", capture_method="official_api",
                              period="", page_url="https://api-seller.ozon.ru/v1/analytics/category/comparison",
                              captured_at="", records=[{"id": "123", "label": "Термосы", "metric_gmv": 1000}])
        item = store.list_observations(self.db, dataset="categories")[0]
        self.assertEqual(item["entity_key"], "123")
        self.assertEqual(item["raw"]["metric_gmv"], 1000)

    def test_periodless_ozon_snapshots_keep_distinct_capture_days(self):
        common = dict(source="ozon_seller_api", dataset="keywords", capture_method="official_api",
                      period="", page_url="https://api-seller.ozon.ru/v1/search-queries/top",
                      records=[{"query": "термос", "client_count": 700}])
        store.ingest_snapshot(self.db, captured_at="2026-10-06T00:00:00Z", **common)
        store.ingest_snapshot(self.db, captured_at="2026-11-06T00:00:00Z", **common)
        self.assertEqual(len(store.list_observations(self.db, dataset="keywords")), 2)


class MarketApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "market.sqlite3"
        self.old_db = api.MARKET_DB_PATH
        api.MARKET_DB_PATH = self.db
        self.client = TestClient(api.app)
        self.token_env = patch.dict(os.environ, {"WORKBENCH_MARKET_INGEST_TOKEN": "test-only-secret"})
        self.token_env.start()

    def tearDown(self):
        self.token_env.stop()
        api.MARKET_DB_PATH = self.old_db
        self.temp.cleanup()

    def test_auth_import_and_query(self):
        body = {"source": "seerfar", "dataset": "categories", "capture_method": "browser_extension",
                "period": "2026-09", "page_url": "https://seerfar.cn/report", "captured_at": "2026-10-06T00:00:00Z",
                "records": [{"类目": "保温杯", "销售额": "100000", "竞对数": "500"}]}
        self.assertEqual(self.client.post("/api/collector/market-snapshots", json=body).status_code, 401)
        headers = {"X-Market-Ingest-Token": "test-only-secret"}
        response = self.client.post("/api/collector/market-snapshots", json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["inserted"], 1)
        listed = self.client.get("/api/market-data/observations", params={"dataset": "categories"}, headers=headers)
        self.assertEqual(listed.json()["items"][0]["raw"]["竞对数"], "500")


if __name__ == "__main__":
    unittest.main()
