"""Market research DB: source separation, idempotency and protected HTTP sink."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from market_intelligence import store
from market_intelligence import recommend as market_recommend


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

    def test_rolling_window_is_labeled_and_deduped_separately(self):
        common = dict(source="seerfar", dataset="keywords", capture_method="browser_extension",
                      period="2026-10", page_url="https://seerfar.cn/admin/market",
                      captured_at="2026-10-07T00:00:00Z",
                      records=[{"关键词": "термос", "类目": "保温杯", "月搜热度": "1000"}])
        calendar = store.ingest_snapshot(self.db, **common)
        rolling = store.ingest_snapshot(self.db, period_kind="rolling_30d", **common)
        repeated = store.ingest_snapshot(self.db, period_kind="rolling_30d", **common)
        self.assertEqual((calendar["inserted"], rolling["inserted"]), (1, 1))
        self.assertTrue(repeated["duplicate_batch"])
        items = store.list_observations(self.db, dataset="keywords")
        self.assertEqual({item["period_kind"] for item in items}, {"calendar_month", "rolling_30d"})

    def test_source_records_keep_original_fields_order_values_and_pagination(self):
        common = dict(source="seerfar", dataset="keywords", capture_method="browser_extension",
                      period="2026-10", period_kind="rolling_30d",
                      page_url="https://seerfar.cn/admin/market", captured_at="2026-10-07T00:00:00Z")
        first = {"排名": "1", "关键词": "термос\n保温杯", "类目": "保温杯",
                 "关键词相关产品图片": ["https://example.com/1.jpg"], "月搜热度": "1,000"}
        second = {"排名": "2", "关键词": "кружка", "类目": "杯子", "月搜热度": "980"}
        store.ingest_snapshot(self.db, records=[first, second], **common)
        all_rows = store.list_source_records(self.db, dataset="keywords", limit=1)
        self.assertEqual(all_rows["total"], 2)
        self.assertEqual((all_rows["limit"], all_rows["offset"]), (1, 0))
        self.assertEqual(all_rows["items"][0]["raw"], second)
        self.assertEqual(all_rows["items"][0]["capture_method"], "browser_extension")
        self.assertEqual(all_rows["items"][0]["page_url"], common["page_url"])
        filtered = store.list_source_records(self.db, dataset="keywords", q="термос", period="2026-10",
                                             category_key="保温杯", limit=1, offset=0)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["raw"], first)
        self.assertEqual(filtered["columns"], list(first))
        self.assertEqual(store.list_source_records(self.db, dataset="keywords", limit=1, offset=2)["items"], [])
        self.assertEqual(store.list_source_records(self.db, dataset="keywords", period="2026-09")["total"], 0)
        with self.assertRaises(ValueError):
            store.list_source_records(self.db, dataset="keywords", source="ozon_seller_api")

    def test_source_records_restore_known_header_order_for_legacy_json(self):
        raw = {"关键词": "термос", "排名": "1", "月搜热度": "1,000", "类目": "保温杯"}
        store.ingest_snapshot(self.db, source="seerfar", dataset="keywords", capture_method="file_import",
                              period="2026-09", page_url="", captured_at="2026-10-06T00:00:00Z",
                              records=[raw])
        # Simulate JSON written by older releases with sort_keys=True.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE observations SET raw_json=?", (store._json(raw),))
        page = store.list_source_records(self.db, dataset="keywords")
        self.assertEqual(page["columns"], ["排名", "关键词", "类目", "月搜热度"])

    def test_order_preservation_does_not_change_existing_dedup_hash(self):
        common = dict(source="seerfar", dataset="keywords", capture_method="file_import",
                      period="2026-09", page_url="", captured_at="2026-10-06T00:00:00Z")
        first = store.ingest_snapshot(self.db, records=[{"关键词": "термос", "月搜热度": "1000"}], **common)
        again = store.ingest_snapshot(self.db, records=[{"月搜热度": "1000", "关键词": "термос"}], **common)
        self.assertEqual(first["inserted"], 1)
        self.assertTrue(again["duplicate_batch"])

    def test_invalid_period_kind_and_rolling_without_bucket_rejected(self):
        common = dict(source="seerfar", dataset="keywords", capture_method="browser_extension",
                      page_url="https://seerfar.cn/admin/market", captured_at="2026-10-07T00:00:00Z",
                      records=[{"关键词": "термос", "类目": "保温杯"}])
        with self.assertRaisesRegex(ValueError, "统计周期口径"):
            store.ingest_snapshot(self.db, period="2026-10", period_kind="week", **common)
        with self.assertRaisesRegex(ValueError, "采集月份"):
            store.ingest_snapshot(self.db, period="", period_kind="rolling_30d", **common)

    def test_legacy_database_gets_calendar_kind_columns(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE ingest_batches (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, dataset TEXT NOT NULL,
                    capture_method TEXT NOT NULL, period TEXT NOT NULL DEFAULT '',
                    page_url TEXT NOT NULL DEFAULT '', captured_at TEXT NOT NULL,
                    imported_at TEXT NOT NULL, payload_hash TEXT NOT NULL UNIQUE,
                    record_count INTEGER NOT NULL
                );
                CREATE TABLE observations (
                    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES ingest_batches(id),
                    source TEXT NOT NULL, dataset TEXT NOT NULL, entity_key TEXT NOT NULL,
                    category_key TEXT NOT NULL DEFAULT '', period TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL, raw_json TEXT NOT NULL, row_hash TEXT NOT NULL,
                    UNIQUE(source, dataset, entity_key, category_key, period, row_hash)
                );
                INSERT INTO ingest_batches
                    (source,dataset,capture_method,period,page_url,captured_at,imported_at,payload_hash,record_count)
                    VALUES ('seerfar','keywords','file_import','2026-09','','2026-10-06','','legacy',1);
                INSERT INTO observations
                    (batch_id,source,dataset,entity_key,category_key,period,captured_at,raw_json,row_hash)
                    VALUES (1,'seerfar','keywords','термос','保温杯','2026-09','2026-10-06',
                            '{"关键词":"термос"}','legacy-row');
            """)
        items = store.list_observations(self.db, dataset="keywords")
        self.assertEqual(items[0]["period_kind"], "calendar_month")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT period_kind FROM ingest_batches").fetchone()[0], "calendar_month")

    def test_recommender_never_combines_calendar_and_rolling_months(self):
        common = dict(source="seerfar", dataset="keywords", capture_method="file_import",
                      page_url="", records=[{"关键词": "термос", "类目": "保温杯", "月搜热度": "1000",
                                              "竞品数": "100", "竞对数": "20"}])
        store.ingest_snapshot(self.db, period="2026-08", captured_at="2026-09-01T00:00:00Z", **common)
        store.ingest_snapshot(self.db, period="2026-09", captured_at="2026-10-01T00:00:00Z", **common)
        store.ingest_snapshot(self.db, period="2026-10", period_kind="rolling_30d",
                              captured_at="2026-10-07T00:00:00Z", **common)
        result = market_recommend.recommend(self.db, dataset="keywords")
        self.assertEqual(len(result["items"]), 1)
        metrics = result["items"][0]["metrics"]
        self.assertEqual(metrics["period_kind"], "rolling_30d")
        self.assertEqual(metrics["history_months"], 1)
        self.assertEqual(metrics["periods"], ["2026-10"])


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

    def test_api_accepts_rolling_kind_and_rejects_invalid_kind(self):
        body = {"source": "seerfar", "dataset": "categories", "capture_method": "browser_extension",
                "period": "2026-10", "period_kind": "rolling_30d",
                "page_url": "https://seerfar.cn/admin/market", "captured_at": "2026-10-07T00:00:00Z",
                "records": [{"类目": "保温杯", "销售额": "100000", "竞对数": "500"}]}
        headers = {"X-Market-Ingest-Token": "test-only-secret"}
        response = self.client.post("/api/collector/market-snapshots", json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        listed = self.client.get("/api/market-data/observations", params={"dataset": "categories"}, headers=headers)
        self.assertEqual(listed.json()["items"][0]["period_kind"], "rolling_30d")
        body["period_kind"] = "unknown"
        rejected = self.client.post("/api/collector/market-snapshots", json=body, headers=headers)
        self.assertEqual(rejected.status_code, 422)

    def test_workbench_source_records_are_read_only_token_free_and_seerfar_only(self):
        body = {"source": "seerfar", "dataset": "categories", "capture_method": "browser_extension",
                "period": "2026-10", "page_url": "https://seerfar.cn/admin/market",
                "captured_at": "2026-10-07T00:00:00Z",
                "records": [{"排名": "1", "类目": "保温杯", "销售额": "100,000₽"}]}
        headers = {"X-Market-Ingest-Token": "test-only-secret"}
        self.assertEqual(self.client.post("/api/collector/market-snapshots", json=body, headers=headers).status_code, 200)
        listed = self.client.get("/api/research/source-records", params={"dataset": "categories", "q": "保温杯"})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.headers["cache-control"], "private, no-store")
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(listed.json()["items"][0]["raw"]["销售额"], "100,000₽")
        self.assertEqual(listed.json()["columns"], ["排名", "类目", "销售额"])
        self.assertEqual(self.client.get("/api/research/source-records", params={"source": "ozon_seller_api"}).status_code, 422)
        self.assertEqual(self.client.get("/api/market-data/observations", params={"dataset": "categories"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
