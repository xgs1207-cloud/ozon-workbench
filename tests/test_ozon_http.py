"""Ozon 只读适配器测试：注入式传输层、规范化、凭据解析、category_match handler（全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from pipeline import stores as store_registry  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.category import handle_category_match  # noqa: E402
from pipeline.context import PipelineGateError, StepContext  # noqa: E402
from pipeline.ozon_http import (  # noqa: E402
    PATH_ATTRIBUTES,
    PATH_ATTRIBUTE_VALUES,
    PATH_TREE,
    FixtureTransport,
    OzonClient,
    OzonCredentials,
    OzonHttpError,
    UrllibTransport,
    attach_dictionary_values,
    build_category_snapshot,
    check_connectivity,
    find_category_in_tree,
)
from pipeline.runner import run_product  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def add_images(product_dir: pathlib.Path, *, main: int = 2, sku: int = 2, detail: int = 3) -> None:
    for relative, count in (("main-images", main), ("sku-images", sku), ("detail-images", detail)):
        directory = product_dir / "input" / relative
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + f"{relative}{index}".encode())


def fixture_client() -> OzonClient:
    return OzonClient(FixtureTransport(directory=FIXTURES))


class TransportTests(unittest.TestCase):
    def test_fixture_transport_records_calls(self):
        transport = FixtureTransport(
            {PATH_TREE: {"result": []}, PATH_ATTRIBUTES: {"result": []}}
        )
        client = OzonClient(transport)
        client.fetch_category_tree()
        client.fetch_category_attributes(category_id=1001, type_id=2001)
        self.assertEqual([call["path"] for call in transport.calls], [PATH_TREE, PATH_ATTRIBUTES])
        self.assertEqual(transport.calls[1]["body"]["description_category_id"], 1001)

    def test_urllib_transport_builds_headers_and_body(self):
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"result": []}'

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        credentials = OzonCredentials(client_id="123", api_key="secret")
        transport = UrllibTransport(credentials, timeout=17, urlopen=fake_urlopen)
        result = transport.post(PATH_ATTRIBUTES, {"description_category_id": 1001, "type_id": 2001})

        self.assertEqual(result, {"result": []})
        self.assertEqual(captured["url"], "https://api-seller.ozon.ru" + PATH_ATTRIBUTES)
        self.assertEqual(captured["headers"]["client-id"], "123")
        self.assertEqual(captured["headers"]["api-key"], "secret")
        self.assertEqual(captured["timeout"], 17)
        self.assertEqual(captured["body"]["type_id"], 2001)

    def test_http_error_is_wrapped(self):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        transport = UrllibTransport(OzonCredentials(client_id="x", api_key="y"), urlopen=fake_urlopen)
        with self.assertRaises(OzonHttpError) as ctx:
            transport.post(PATH_TREE, {})
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_credentials_come_from_env_and_report_missing(self):
        shop = store_registry.example_registry()["shops"][0]
        with self.assertRaises(OzonHttpError) as ctx:
            OzonCredentials.from_shop(shop, env={})
        self.assertIn("OZON_DEFAULT_CLIENT_ID", str(ctx.exception))

        credentials = OzonCredentials.from_shop(
            shop, env={"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "abc"}
        )
        self.assertEqual(credentials.client_id, "123")
        self.assertEqual(credentials.api_key, "abc")


class NormalizationTests(unittest.TestCase):
    def test_find_category_in_tree_inherits_parent_category_id(self):
        tree = json.loads((FIXTURES / "ozon-category-tree.json").read_text(encoding="utf-8"))
        found = find_category_in_tree(tree, category_id=1001, type_id=2001)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["name"], "Термосы")
        self.assertEqual(found["path"], ["Дом и сад", "Термосы"])
        self.assertIsNone(find_category_in_tree(tree, category_id=9999, type_id=2001))

    @unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
    def test_snapshot_matches_contract_and_normalizes_dictionary_id(self):
        client = fixture_client()
        response = client.fetch_category_attributes(category_id=1001, type_id=2001)
        snapshot = build_category_snapshot(
            product_id="P000001",
            category_id=1001,
            type_id=2001,
            category_name="Термосы",
            attributes_response=response,
            fetched_at="2026-10-05T12:00:00+08:00",
        )
        self.assertEqual(validate_contract("ozon-category-attributes", snapshot), [])
        brand = [item for item in snapshot["attributes"] if item["attribute_id"] == 85][0]
        self.assertEqual(brand["dictionary_id"], 28732)
        self.assertTrue(brand["required"])
        color = [item for item in snapshot["attributes"] if item["attribute_id"] == 10097][0]
        self.assertIsNone(color["dictionary_id"])  # Ozon 的 0 → null

    @unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
    def test_dictionary_values_are_attached(self):
        client = fixture_client()
        snapshot = build_category_snapshot(
            product_id="P000001",
            category_id=1001,
            type_id=2001,
            category_name="Термосы",
            attributes_response=client.fetch_category_attributes(category_id=1001, type_id=2001),
            fetched_at="2026-10-05T12:00:00+08:00",
        )
        enriched = attach_dictionary_values(snapshot, client=client)
        self.assertEqual(validate_contract("ozon-category-attributes", enriched), [])
        brand = [item for item in enriched["attributes"] if item["attribute_id"] == 85][0]
        self.assertTrue(brand["allowed_values"])
        self.assertIn("Нет бренда", [item["value"] for item in brand["allowed_values"]])

    def test_check_connectivity_summary(self):
        summary = check_connectivity(fixture_client(), category_id=1001, type_id=2001)
        self.assertTrue(summary["category_found_in_tree"])
        self.assertEqual(summary["attributes_total"], 4)
        self.assertEqual(summary["attributes_required"], 2)
        self.assertEqual(summary["dictionary_attributes"], 1)
        self.assertFalse(summary["api_writes_performed"])

    def test_only_read_paths_are_implemented(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / "pipeline" / "ozon_http.py").read_text(encoding="utf-8")
        for forbidden in ("/v1/product/import", "/v3/product/import", "/v1/product/update", "stocks"):
            self.assertNotIn(forbidden, source)


class CategoryMatchHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/454545454.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.product_dir = self.products / self.summary["product_id"]

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
    def test_handler_writes_confirmed_category_and_snapshot(self):
        result = handle_category_match(
            StepContext(
                product_dir=self.product_dir,
                step="category_match",
                ozon_client=fixture_client(),
            )
        )
        self.assertEqual(result["match_status"], "api_confirmed")
        self.assertEqual(result["attributes"], 4)

        category = json.loads((self.product_dir / "output" / "ozon-category.json").read_text(encoding="utf-8"))
        self.assertEqual(category["metadata_source"], "ozon_seller_api")
        self.assertEqual(category["match_status"], "api_confirmed")
        self.assertEqual(category["category_name"], "Термосы")

        snapshot = json.loads(
            (self.product_dir / "output" / "ozon-category-attributes.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_contract("ozon-category-attributes", snapshot), [])

    def test_handler_requires_client(self):
        with self.assertRaises(PipelineGateError) as ctx:
            handle_category_match(StepContext(product_dir=self.product_dir, step="category_match"))
        self.assertIn("ozon_client", str(ctx.exception))

    def test_handler_requires_selected_category(self):
        selection = self.product_dir / "input" / "category-selection.json"
        selection.write_text("{}", encoding="utf-8")
        source_path = self.product_dir / "input" / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["selected_category"] = None
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(PipelineGateError) as ctx:
            handle_category_match(
                StepContext(product_dir=self.product_dir, step="category_match", ozon_client=fixture_client())
            )
        self.assertIn("没有选择 Ozon 类目", str(ctx.exception))

    def test_empty_tree_is_a_hard_failure(self):
        client = OzonClient(FixtureTransport({PATH_TREE: {"result": []}, PATH_ATTRIBUTES: {"result": []}}))
        with self.assertRaises(PipelineGateError) as ctx:
            handle_category_match(
                StepContext(product_dir=self.product_dir, step="category_match", ozon_client=client)
            )
        self.assertIn("类目树返回为空", str(ctx.exception))

    def test_category_not_in_tree_needs_review_not_fake_confirmed(self):
        transport = FixtureTransport(
            {
                PATH_TREE: {"result": [{"description_category_id": 1001, "category_name": "Дом", "children": []}]},
                PATH_ATTRIBUTES: json.loads((FIXTURES / "ozon-category-attributes.json").read_text(encoding="utf-8")),
            }
        )
        result = handle_category_match(
            StepContext(product_dir=self.product_dir, step="category_match", ozon_client=OzonClient(transport))
        )
        self.assertEqual(result["match_status"], "api_match_needs_review")
        self.assertTrue(any("不在返回的类目树里" in item for item in result["warnings"]))
        category = json.loads((self.product_dir / "output" / "ozon-category.json").read_text(encoding="utf-8"))
        self.assertEqual(category["match_status"], "api_match_needs_review")


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class CategoryMatchPipelineTests(CategoryMatchHandlerTests):
    def test_pipeline_uses_real_category_match_when_client_present(self):
        from models.fake import FakeProvider
        from pipeline.selection import set_selected_keywords

        add_images(self.product_dir)
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        create_batch(self.products, batches_root=self.root / "batches", target_store_ids=["shop-a"])
        report = run_product(
            self.product_dir,
            provider=FakeProvider(),
            ozon_client=fixture_client(),
            step_budget=25,
        )
        self.assertIn("category_match", report["completed_steps"])
        self.assertIn("measurements", report["completed_steps"])
        self.assertIn("ecommerce_design", report["completed_steps"])
        # 生图未接后端 → 如实停在 image_generation
        self.assertEqual(report["stop_reason"], "handler_not_implemented")
        self.assertEqual(report["stopped_at"], "image_generation")
        self.assertEqual(report["api_write_count"], 0)


if __name__ == "__main__":
    unittest.main()
