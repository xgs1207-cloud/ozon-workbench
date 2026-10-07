"""选品/采集闭环的 HTTP 接口测试：清单生成、读回、标记已采集、关键词→商品汇总。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    import api as api_module
    from fastapi.testclient import TestClient

    HAS_DEPS = True
except ImportError:  # pragma: no cover
    HAS_DEPS = False

from keyword_library import store as keyword_store  # noqa: E402

CATEGORY_ID = "17028922"
TYPE_ID = "91875"


def seed_library(root: pathlib.Path) -> None:
    """4 条词，热度与竞争**同序**排列，因此恰好 2 条达标（便于断言筛选行为）。"""
    keyword_store.upsert(
        root,
        [
            {
                "keyword": "простынь на резинке 160х200",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 110906,
                "competitor_count": 120,
                "extra": {"category_name_zh": "床单", "category_name_ru": "Простыня"},
            },
            {
                "keyword": "наволочка 50х70",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 95000,
                "competitor_count": 206,
                "extra": {"category_name_zh": "枕套", "category_name_ru": "Наволочка"},
            },
            {
                "keyword": "простынь евро 200х200",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 100000,
                "competitor_count": 150,
                "extra": {"category_name_zh": "床单", "category_name_ru": "Простыня"},
            },
            {
                "keyword": "слабый запрос",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 10,
                "competitor_count": 99999,
                "extra": {"category_name_zh": "床单"},
            },
        ],
        source="seerfar",
    )


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class SourcingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        api_module.PRODUCTS_ROOT = self.root / "products"
        api_module.LIBRARY_ROOT = self.root / "keyword-library"
        (self.root / "products").mkdir(parents=True, exist_ok=True)
        seed_library(api_module.LIBRARY_ROOT)
        self.client = TestClient(api_module.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sourcing_plan_endpoint_builds_and_writes(self):
        response = self.client.post(
            "/api/workbench/sourcing-plan",
            json={"category_id": CATEGORY_ID, "type_id": TYPE_ID, "top": 5},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        plan = body["plan"]
        self.assertEqual(plan["keywords"], 2)  # 只取达标词
        row = plan["rows"][0]
        self.assertTrue(row["ozon_search_url"].startswith("https://www.ozon.ru/search/"))
        self.assertTrue(row["alibaba_search_url"].startswith("https://s.1688.com/"))
        self.assertEqual(row["chinese_term"], "床单")
        self.assertTrue(pathlib.Path(body["written"]["json"]).is_file())

        again = self.client.get("/api/workbench/sourcing-plan")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["plan"]["keywords"], 2)

    def test_sourcing_plan_404_before_generation(self):
        response = self.client.get("/api/workbench/sourcing-plan")
        self.assertEqual(response.status_code, 404)
        self.assertIn("还没有选品清单", response.json()["detail"])

    def test_translate_without_model_reports_422(self):
        response = self.client.post(
            "/api/workbench/sourcing-plan",
            json={"translate": True, "provider": "ark"},
        )
        # 没有 ARK_API_KEY/ARK_TEXT_MODEL 时应明确报配置缺项，而不是 500
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("模型层不可用", response.json()["detail"])

    def test_collection_plan_from_sourcing_plan_then_mark(self):
        self.client.post(
            "/api/workbench/sourcing-plan",
            json={"category_id": CATEGORY_ID, "type_id": TYPE_ID, "top": 5},
        )
        response = self.client.post("/api/workbench/collection-plan", json={})
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()["plan"]
        self.assertEqual(plan["total"], 2)
        self.assertEqual(plan["pending"], 2)
        keyword = plan["tasks"][0]["keyword"]

        # 采集一个商品（走采集入库接口），带上关键词 → 采集清单应变成"已采集"
        import io
        import shutil

        folder = self.root / "capture"
        (folder / "main-images").mkdir(parents=True)
        (folder / "detail-images").mkdir(parents=True)
        (folder / "sku-images").mkdir(parents=True)
        (folder / "product.json").write_text(
            json.dumps(
                {
                    "source_url": "https://detail.1688.com/offer/616161616.html",
                    "title_zh": "纯棉床单",
                    "category": {"category_id": CATEGORY_ID, "type_id": TYPE_ID},
                    "skus": [{"sku_id": "S1", "purchase_price_cny": 18.0, "image_path": "input/sku-images/001-01.png"}],
                    "keywords": [keyword],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for role in ("main-images", "detail-images", "sku-images"):
            (folder / role / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")

        ingest = self.client.post(
            "/api/collector/products/import-folder",
            json={"folder": str(folder)},
        )
        self.assertEqual(ingest.status_code, 200, ingest.text)
        product_id = ingest.json()["product_id"]

        refreshed = self.client.post("/api/workbench/collection-plan", json={}).json()["plan"]
        collected = [item for item in refreshed["tasks"] if item["status"] == "collected"]
        self.assertEqual(len(collected), 1, refreshed["tasks"])
        self.assertEqual(collected[0]["keyword"], keyword)
        self.assertEqual(collected[0]["products"][0]["product_id"], product_id)

        # mark 接口：给另一个商品补记关键词
        marked = self.client.post(
            "/api/workbench/collection-plan/mark",
            json={"product_id": product_id, "keyword": "наволочка 50х70"},
        )
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(marked.json()["keyword"], "наволочка 50х70")

        rollup = self.client.get("/api/workbench/keyword-products").json()
        self.assertEqual(rollup["count"], 2)
        self.assertIn(product_id, [row["product_id"] for row in rollup["keywords"][keyword]])

    def test_mark_requires_existing_product(self):
        response = self.client.post(
            "/api/workbench/collection-plan/mark",
            json={"product_id": "P999999", "keyword": "простынь"},
        )
        self.assertEqual(response.status_code, 404)

    def test_mark_rejects_short_keyword(self):
        response = self.client.post(
            "/api/workbench/collection-plan/mark",
            json={"product_id": "P000001", "keyword": "x"},
        )
        self.assertEqual(response.status_code, 422)

    def test_collection_plan_from_library_without_sourcing_plan(self):
        response = self.client.post(
            "/api/workbench/collection-plan",
            json={"from_library": True, "category_id": CATEGORY_ID, "type_id": TYPE_ID, "top": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["plan"]["total"], 1)

    def test_collection_plan_404_without_source(self):
        response = self.client.post("/api/workbench/collection-plan", json={})
        self.assertEqual(response.status_code, 404)
        self.assertIn("还没有选品清单", response.json()["detail"])

    def test_collection_plan_read_before_generation(self):
        response = self.client.get("/api/workbench/collection-plan")
        self.assertEqual(response.status_code, 404)

    def test_launch_endpoint_defaults_to_dry_run(self):
        """界面上点一下：默认 fake + 占位生图 + 干跑，绝不真提交。"""
        import io
        import json as _json
        import shutil

        # 先采集一个商品（带关键词与类目）
        folder = self.root / "capture-launch"
        for role in ("main-images", "sku-images", "detail-images"):
            (folder / role).mkdir(parents=True, exist_ok=True)
            (folder / role / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n" + role.encode())
        (folder / "product.json").write_text(
            _json.dumps(
                {
                    "source_url": "https://detail.1688.com/offer/515151515.html",
                    "title_zh": "纯棉床单",
                    "category": {"category_id": CATEGORY_ID, "type_id": TYPE_ID},
                    "skus": [{"sku_id": "S1", "purchase_price_cny": 18.0, "image_path": "input/sku-images/001-01.png"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        ingest = self.client.post("/api/collector/products/import-folder", json={"folder": str(folder)})
        self.assertEqual(ingest.status_code, 200, ingest.text)
        product_id = ingest.json()["product_id"]

        response = self.client.post(
            f"/api/workbench/products/{product_id}/launch",
            json={"image_generator": "placeholder", "oss": "none", "step_budget": 6, "ozon_fixture": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()["report"]
        self.assertFalse(report["ok"])  # 没发布图片 / 没跑完，如实报有阻断
        self.assertEqual(report["product_id"], product_id)

    def test_launch_endpoint_with_ozon_fixture_reaches_images(self):
        """带 ozon_fixture 时，category_match 能跑过（离线演练整条链）。"""
        import json as _json

        folder = self.root / "capture-launch-fixture"
        for role in ("main-images", "sku-images", "detail-images"):
            (folder / role).mkdir(parents=True, exist_ok=True)
            (folder / role / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n" + role.encode())
        (folder / "product.json").write_text(
            _json.dumps(
                {
                    "source_url": "https://detail.1688.com/offer/525252525.html",
                    "title_zh": "纯棉床单",
                    "category": {"category_id": "1001", "type_id": "2001"},
                    "skus": [{"sku_id": "S1", "purchase_price_cny": 18.0, "image_path": "input/sku-images/001-01.png"}],
                    "keywords": ["простынь 200х200"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        ingest = self.client.post("/api/collector/products/import-folder", json={"folder": str(folder)})
        product_id = ingest.json()["product_id"]

        response = self.client.post(
            f"/api/workbench/products/{product_id}/launch",
            json={
                "image_generator": "placeholder",
                "oss": "none",
                "step_budget": 14,
                "ozon_fixture": True,
                "stores": ["shop-a"],  # 没目标店铺会停在 authorize（也是门禁之一）
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        phases = response.json()["report"]["phases"]
        content = next(item for item in phases if item["phase"] == "content_and_images")
        self.assertIn("category_match", content["completed_steps"])
        self.assertIn("image_generation", content["completed_steps"])

    def test_launch_endpoint_refuses_real_upload(self):
        response = self.client.post(
            "/api/workbench/products/P000001/launch", json={"uploader": "ozon-api"}
        )
        # 商品不存在 → 404；商品存在时 → 409 且说明只能用 CLI
        self.assertIn(response.status_code, (404, 409))

    def test_launch_endpoint_local_oss_requires_paths(self):
        response = self.client.post(
            "/api/workbench/products/P000001/launch", json={"oss": "local"}
        )
        self.assertIn(response.status_code, (404, 422))


if __name__ == "__main__":
    unittest.main()
