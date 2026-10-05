"""关键词库 HTTP 接口测试（需要 fastapi + httpx，缺失则整体跳过）。"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))

if HAS_DEPS:
    import api as api_module
    from fastapi.testclient import TestClient

SAMPLE = [
    {"keyword": "термос 500 мл", "search_volume": 12400, "competitor_count": 830},
    {"keyword": "термос для чая", "search_volume": 3100, "competitor_count": 120},
    {"keyword": "термос стальной", "search_volume": 8800, "competitor_count": 1900},
    {"keyword": "термос без данных"},
]


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class KeywordApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api_module.LIBRARY_ROOT = pathlib.Path(self.tmp.name)
        self.client = TestClient(api_module.app)

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, keywords=None):
        response = self.client.post(
            "/api/keywords/ingest",
            json={
                "source": "seerfar",
                "category": {
                    "category_id": "1001",
                    "type_id": "2001",
                    "category_path_zh": "家居/厨房",
                },
                "keywords": keywords if keywords is not None else SAMPLE,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_health(self):
        payload = self.client.get("/health").json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["score_config"]["lam"], 0.6)

    def test_ingest_then_query(self):
        summary = self.ingest()
        self.assertEqual(summary["created"], 4)
        self.assertEqual(summary["promoted"], 1)

        listing = self.client.get("/api/keywords", params={"order": "score", "limit": 10}).json()
        self.assertEqual(listing["count"], 4)
        top = listing["items"][0]
        self.assertEqual(top["keyword"], "термос 500 мл")
        # 分位数随同组样本数变化，所以断言公式而不是写死数值：
        # score = heat_percentile - lam * competition_percentile
        self.assertAlmostEqual(
            top["score"], round(top["heat_percentile"] - 0.6 * top["competition_percentile"], 4),
            places=4,
        )
        self.assertGreater(top["score"], listing["items"][1]["score"])
        # 缺指标的一条永远排最后且没有分数
        self.assertIsNone(listing["items"][-1]["score"])
        self.assertEqual(listing["items"][-1]["status"], "candidate")

    def test_text_filter(self):
        self.ingest()
        listing = self.client.get("/api/keywords", params={"q": "стальной"}).json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["items"][0]["keyword"], "термос стальной")

    def test_only_qualified(self):
        self.ingest()
        listing = self.client.get("/api/keywords", params={"only_qualified": "true"}).json()
        self.assertEqual([item["keyword"] for item in listing["items"]], ["термос 500 мл"])

    def test_status_change_and_export_prefers_library(self):
        self.ingest()
        top = self.client.get("/api/keywords", params={"order": "score", "limit": 1}).json()["items"][0]
        response = self.client.post(
            "/api/keywords/status",
            json={"keys": [top["key"]], "status": "in_library", "reason": "人工确认"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changed"], 1)

        export = self.client.get(
            "/api/keywords/export", params={"category_id": "1001", "type_id": "2001", "limit": 2}
        ).json()
        self.assertEqual(export["keywords"][0]["keyword"], "термос 500 мл")
        self.assertEqual(export["keywords"][0]["status"], "in_library")

    def test_invalid_status_returns_422(self):
        response = self.client.post(
            "/api/keywords/status", json={"keys": ["x"], "status": "не_статус"}
        )
        self.assertEqual(response.status_code, 422)

    def test_ingest_without_category_returns_422(self):
        response = self.client.post(
            "/api/keywords/ingest",
            json={"keywords": [{"keyword": "без категории", "search_volume": 10, "competitor_count": 1}]},
        )
        self.assertEqual(response.status_code, 422)

    def test_rescore_tightens_thresholds(self):
        self.ingest()
        before = self.client.get("/api/keywords", params={"only_qualified": "true"}).json()["count"]
        response = self.client.post(
            "/api/keywords/score", json={"min_heat_percentile": 0.99, "max_competition_percentile": 0.01}
        )
        self.assertEqual(response.status_code, 200, response.text)
        after = self.client.get("/api/keywords", params={"only_qualified": "true"}).json()["count"]
        self.assertLessEqual(after, before)

    def test_categories_summary(self):
        self.ingest()
        payload = self.client.get("/api/keywords/categories").json()
        self.assertEqual(payload["category_count"], 1)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["categories"]["1001:2001"]["qualified"], 1)


if __name__ == "__main__":
    unittest.main()
