"""工作台 HTTP 接口测试：选词与文案生成（需要 fastapi + httpx）。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))

if HAS_DEPS:
    import api as api_module
    from fastapi.testclient import TestClient

from contracts import available_contracts, validate_contract  # noqa: E402
from keyword_library import store as keyword_store  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
SOURCE_URL = "https://detail.1688.com/offer/246813579.html"


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class WorkbenchCopyApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        api_module.PRODUCTS_ROOT = self.root / "products"
        api_module.LIBRARY_ROOT = self.root / "keyword-library"
        self.client = TestClient(api_module.app)
        response = self.client.post(
            "/api/collector/products",
            json={
                "source_url": SOURCE_URL,
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.product_id = response.json()["product_id"]
        self.product_dir = self.root / "products" / self.product_id
        (self.product_dir / "output").mkdir(parents=True, exist_ok=True)
        (self.product_dir / "output" / "attribute-fill-input.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_keywords_manual_then_get(self):
        response = self.client.put(
            f"/api/workbench/products/{self.product_id}/keywords",
            json={"keywords": ["термос 500 мл", "термос для чая"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["selection"]["keywords"]), 2)

        fetched = self.client.get(f"/api/workbench/products/{self.product_id}/keywords").json()
        self.assertEqual([item["keyword"] for item in fetched["selection"]["keywords"]], ["термос 500 мл", "термос для чая"])

    def test_put_keywords_empty_is_422(self):
        response = self.client.put(
            f"/api/workbench/products/{self.product_id}/keywords", json={"keywords": []}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_put_keywords_from_library(self):
        keyword_store.upsert(
            api_module.LIBRARY_ROOT,
            [
                {"keyword": "термос 500 мл", "category_id": "1001", "type_id": "2001", "search_volume": 12400, "competitor_count": 830},
                {"keyword": "термос для чая", "category_id": "1001", "type_id": "2001", "search_volume": 3100, "competitor_count": 120},
            ],
        )
        # 真实流程：人工确认入库的词优先被选走
        top = keyword_store.query(api_module.LIBRARY_ROOT, category_id="1001", type_id="2001", order="score", limit=1)[0]
        keyword_store.set_status(api_module.LIBRARY_ROOT, [top["key"]], keyword_store.STATUS_IN_LIBRARY)

        response = self.client.put(
            f"/api/workbench/products/{self.product_id}/keywords",
            json={"from_library": True, "limit": 5},
        )
        self.assertEqual(response.status_code, 200, response.text)
        keywords = [item["keyword"] for item in response.json()["selection"]["keywords"]]
        self.assertEqual(keywords, [top["keyword"]])

    def test_put_keywords_from_library_without_candidates_is_422(self):
        """库里既没有 in_library 也没有 qualified 时，必须报错而不是随便选词。"""
        keyword_store.upsert(
            api_module.LIBRARY_ROOT,
            [
                {"keyword": "термос для чая", "category_id": "1001", "type_id": "2001", "search_volume": 3100, "competitor_count": 120},
            ],
        )
        response = self.client.put(
            f"/api/workbench/products/{self.product_id}/keywords",
            json={"from_library": True, "limit": 5},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("没有可用词", response.json()["detail"])

    def test_put_keywords_unknown_product_404(self):
        response = self.client.put(
            "/api/workbench/products/P999999/keywords", json={"keywords": ["термос"]}
        )
        self.assertEqual(response.status_code, 404)

    @unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
    def test_copy_generation_end_to_end(self):
        self.client.put(
            f"/api/workbench/products/{self.product_id}/keywords",
            json={"keywords": ["термос 500 мл", "термос для чая", "термос подарочный"]},
        )
        response = self.client.post(
            f"/api/workbench/products/{self.product_id}/copy",
            json={"provider": "fake", "steps": ["product_analysis", "russian_copy"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["provider"], "fake")
        self.assertEqual([item["step"] for item in body["results"]], ["product_analysis", "russian_copy"])

        for contract_name, filename in (
            ("title-ru", "output/title-ru.json"),
            ("description-ru", "output/description-ru.json"),
            ("keywords-ru", "output/keywords-ru.json"),
        ):
            document = json.loads((self.product_dir / filename).read_text(encoding="utf-8"))
            self.assertEqual(validate_contract(contract_name, document), [], contract_name)

    def test_copy_without_keywords_returns_409(self):
        response = self.client.post(
            f"/api/workbench/products/{self.product_id}/copy",
            json={"provider": "fake", "steps": ["product_analysis", "russian_copy"]},
        )
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["step"], "russian_copy")
        self.assertIn("选词", detail["reason"])

    def test_copy_unknown_step_or_provider_is_422(self):
        unknown_step = self.client.post(
            f"/api/workbench/products/{self.product_id}/copy", json={"steps": ["not_a_step"]}
        )
        self.assertEqual(unknown_step.status_code, 422)

        unknown_provider = self.client.post(
            f"/api/workbench/products/{self.product_id}/copy",
            json={"provider": "codex-cli", "steps": ["product_analysis"]},
        )
        self.assertEqual(unknown_provider.status_code, 422, unknown_provider.text)


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class WorkbenchPipelineApiTests(unittest.TestCase):
    """产物清单 / 发布台账 / 预检 / 运行 四个新接口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        api_module.PRODUCTS_ROOT = self.root / "products"
        api_module.LIBRARY_ROOT = self.root / "keyword-library"
        self.client = TestClient(api_module.app)
        response = self.client.post(
            "/api/collector/products",
            json={
                "source_url": "https://detail.1688.com/offer/171717171.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.product_id = response.json()["product_id"]
        self.product_dir = self.root / "products" / self.product_id

    def tearDown(self):
        self.tmp.cleanup()

    def test_artifacts_endpoint_lists_missing_then_present(self):
        first = self.client.get(f"/api/workbench/products/{self.product_id}/artifacts").json()
        by_path = {item["path"]: item for item in first["files"]}
        self.assertTrue(by_path["input/source.json"]["exists"])
        self.assertFalse(by_path["output/copy-ru.json"]["exists"])

        self.client.put(
            f"/api/workbench/products/{self.product_id}/keywords",
            json={"keywords": ["термос 500 мл"]},
        )
        second = self.client.get(f"/api/workbench/products/{self.product_id}/artifacts").json()
        by_path = {item["path"]: item for item in second["files"]}
        self.assertTrue(by_path["input/selected-keywords.json"]["exists"])
        self.assertEqual(second["summary"]["product_id"], self.product_id)

    def test_publications_endpoint_returns_plan(self):
        payload = self.client.get(f"/api/workbench/products/{self.product_id}/publications").json()
        self.assertEqual(payload["product_id"], self.product_id)
        self.assertIn("publications", payload)
        self.assertEqual(payload["plan"], [])  # 还没选店铺

    def test_doctor_endpoint(self):
        payload = self.client.get("/api/workbench/doctor", params={"product_id": self.product_id}).json()
        self.assertEqual(payload["summary"]["products"], 1)
        self.assertFalse(payload["products"][0]["ready_to_submit"])
        self.assertIn("没有选择目标店铺", payload["products"][0]["blockers"])

    def test_run_endpoint_requires_authorization(self):
        response = self.client.post(
            f"/api/workbench/products/{self.product_id}/run", json={"provider": "fake", "step_budget": 2}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("未授权", response.json()["detail"])

    def test_run_endpoint_runs_after_batch_authorization(self):
        from pipeline.batch import create_batch

        create_batch(api_module.PRODUCTS_ROOT, batches_root=self.root / "batches", target_store_ids=["shop-a"])
        response = self.client.post(
            f"/api/workbench/products/{self.product_id}/run",
            json={"provider": "fake", "step_budget": 3},
        )
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()["report"]
        self.assertEqual(report["dry_run"], True)
        self.assertEqual(report["api_write_count"], 0)
        self.assertIn("validate_source", report["completed_steps"])


if __name__ == "__main__":
    unittest.main()
