from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from market_intelligence import store
from market_intelligence.product_opportunities import listing_age, list_opportunities, number, product_url


class ProductOpportunityTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db = Path(self.temp.name) / "market.sqlite3"
        self.today = date(2026, 10, 7)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, dataset, records, captured="2026-10-07T10:00:00Z", period_kind=None):
        return store.ingest_snapshot(self.db, source="seerfar", dataset=dataset,
            capture_method="browser_extension", period="2026-10",
            period_kind=period_kind or ("rolling_30d" if dataset == "products" else "calendar_month"),
            captured_at=captured, page_url="https://www.seerfar.cn/admin/product-search", records=records)

    def seed(self):
        self.ingest("categories", [{"类目": f"类目{i}", "竞品数": 10 * (i + 1), "销量": 100} for i in range(5)])
        self.ingest("products", [{"SKU": str(100000 + i), "商品信息 / SKU": f"商品{i}\n{100000+i}\n|\n品牌",
            "类目": "类目0", "上架时间": "2026-09-01\n36 天", "销量": 100-i*10,
            "评论数": i*10+1, "售价": "1 200₽", "退货取消率": "5%"} for i in range(5)])

    def result(self, **kwargs):
        return list_opportunities(self.db, today=self.today, **kwargs)

    def test_strict_age_boundary_and_elapsed_snapshot_age(self):
        self.assertEqual(listing_age({"上架天数": 89}, "2026-10-06T00:00:00Z", self.today)[0], 90)
        self.assertEqual(listing_age({"上架时间": "2026年9月1日"}, "", self.today)[0], 36)
        self.assertIsNone(listing_age({"上架时间": "2026-10-08"}, "", self.today)[0])
        self.assertIsNone(listing_age({"上架天数": 20}, "invalid", self.today)[0])
        self.ingest("products", [{"SKU": "123456", "上架天数": 90, "销量": 1}])
        self.assertEqual(self.result(status="all")["items"][0]["status"], "excluded")

    def test_sales_does_not_absorb_growth_and_safe_card_links(self):
        self.assertEqual(number({"销量": "1,200\n+10%"}, "销量"), 1200)
        self.assertEqual(number({"价格": "1 200₽"}, "价格"), 1200)
        self.assertIsNone(number({"销量": "—"}, "销量"))
        self.assertEqual(product_url({"商品链接": ["https://ozon.ru.evil.test/product/123456"]}, "123456"),
                         "https://www.ozon.ru/product/123456/")
        self.assertIsNone(product_url({"商品链接": ["javascript:alert(1)"]}, "unknown"))

    def test_recommendation_requires_competition_and_review_evidence(self):
        self.seed()
        result = self.result()
        first = next(x for x in result["items"] if x["sku"] == "100000")
        self.assertEqual(first["age_days"], 36)
        self.assertEqual(first["title"], "商品0")
        self.assertEqual(first["price"], 1200)
        self.assertEqual(first["competition_density"], .1)
        self.assertGreater(first["score"], 60)
        self.assertIn("评价壁垒，不是竞争度", " ".join(first["reasons"]))
        self.assertTrue(first["product_url"].endswith("/100000/"))

    def test_missing_competition_never_becomes_zero(self):
        self.ingest("products", [{"SKU": "123456", "类目": "缺数据", "上架天数": 10,
                                  "销量": 20, "评论数": 0}])
        item = self.result(status="all")["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["competition_density"])
        self.assertEqual(self.result()["total"], 0)

    def test_latest_sku_snapshot_replaces_old_sales_not_sums(self):
        self.seed()
        self.ingest("products", [{"SKU": "100000", "类目": "类目0", "上架时间": "2026-09-01",
                                  "销量": 0, "评论数": 2}], captured="2026-10-07T12:00:00Z")
        result = self.result(status="all")
        self.assertEqual(result["all_count"], 5)
        item = next(x for x in result["items"] if x["sku"] == "100000")
        self.assertEqual(item["sales"], 0)
        self.assertEqual(item["status"], "excluded")

    def test_stale_sales_and_mixed_period_category_peers_are_pending(self):
        self.seed()
        self.ingest("categories", [{"类目": "类目0", "竞品数": 1, "销量": 100}], period_kind="rolling_30d")
        self.assertEqual(self.result(status="all")["counts"]["pending"], 5)
        self.ingest("products", [{"SKU": "123456", "类目": "类目0", "上架时间": "2026-09-01",
                                  "销量": 10, "评论数": 1}], captured="2026-09-10T00:00:00Z")
        item = next(x for x in self.result(status="all")["items"] if x["sku"] == "123456")
        self.assertIn("超过 14 天", " ".join(item["warnings"]))

    def test_filtering_pagination_does_not_change_comparison_pool(self):
        self.seed()
        all_rows = self.result(status="all")
        filtered = self.result(status="all", q="100000", limit=1)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["score"], next(x["score"] for x in all_rows["items"] if x["sku"] == "100000"))
        self.assertEqual(self.result(status="all", limit=2, offset=2)["items"], all_rows["items"][2:4])

    def test_read_only_api_has_no_ingestion_token_and_validates_filters(self):
        self.seed()
        with patch.object(api, "MARKET_DB_PATH", self.db), TestClient(api.app) as client:
            response = client.get("/api/research/product-opportunities?status=all")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["all_count"], 5)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(client.get("/api/research/product-opportunities?status=invalid").status_code, 422)
