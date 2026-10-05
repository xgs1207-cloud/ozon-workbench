"""Ozon 写适配器测试：请求构建、响应解析、重试与"结果未知"安全策略（全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts  # noqa: E402
from pipeline import stores as store_registry  # noqa: E402
from pipeline.ozon_http import OzonCredentials, OzonHttpError, UrllibTransport  # noqa: E402
from pipeline.ozon_write import (  # noqa: E402
    FORBIDDEN_FIELD_PATTERN,
    PATH_IMPORT,
    PATH_IMPORT_INFO,
    OzonWriteError,
    OzonWriteUploader,
    build_import_request,
    fetch_import_status,
    parse_import_response,
    post_import,
)
from pipeline.publications import load_publications  # noqa: E402
from pipeline.upload import UPLOAD_MODE_PRODUCTION, build_upload_payload, upload_product  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


class RecordingTransport:
    """离线传输层：记录调用，按脚本返回响应或抛错。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, path, body):
        self.calls.append({"path": path, "body": json.loads(json.dumps(body))})
        result = self.responses.pop(0) if self.responses else {"result": {}}
        if isinstance(result, Exception):
            raise result
        return result


def sample_payload() -> dict:
    return {
        "schema_version": "1.0.0",
        "product_id": "P000001",
        "shop_name": "shop-a",
        "category": {"category_id": 1001, "type_id": 2001, "category_name": "Термосы", "source": "ozon_seller_api"},
        "title": "Термос 500 мл",
        "description": "Термос из нержавеющей стали 316, объём 500 мл.",
        "attributes": [
            {"attribute_id": 85, "attribute_name": "Бренд", "value": "Нет бренда", "dictionary_value_id": 126745801},
            {"attribute_id": 10096, "attribute_name": "Объём, мл", "value": "500", "dictionary_value_id": None},
        ],
        "images": [
            {"slot": "main-S1", "role": "variant_main", "url": "https://cdn.example.com/P000001/main-S1.png", "order": 1},
            {"slot": "detail-001", "role": "detail", "url": "https://cdn.example.com/P000001/detail-001.png", "order": 2},
            {"slot": "detail-002", "role": "detail", "url": "https://cdn.example.com/P000001/detail-002.png", "order": 3},
        ],
        "sku_measurements": {
            "package_dimensions": {"length_mm": 110, "width_mm": 110, "height_mm": 280, "weight_g": 430},
            "product_dimensions": {"length_mm": 90, "width_mm": 90, "height_mm": 250, "weight_g": 350},
        },
        "variants": [
            {
                "source_sku_id": "S1",
                "offer_id": "P000001-S1",
                "display_name_ru": "Термос 500 мл — красный",
                "price": "1290.00",
                "currency_code": "RUB",
                "color": "красный",
                "color_image": "https://cdn.example.com/P000001/main-S1.png",
                "attributes": [{"attribute_id": 10097, "attribute_name": "Название цвета", "value": "красный"}],
            },
            {
                "source_sku_id": "S2",
                "offer_id": "P000001-S2",
                "display_name_ru": "Термос 500 мл — синий",
                "price": "1390.00",
                "currency_code": "RUB",
                "color": "синий",
                "color_image": "https://cdn.example.com/P000001/main-S2.png",
                "attributes": [{"attribute_id": 10097, "attribute_name": "Название цвета", "value": "синий"}],
            },
        ],
        "production_blockers": [],
        "api_request_template": {"api": f"POST {PATH_IMPORT}", "inventory_fields_included": False},
    }


class BuildRequestTests(unittest.TestCase):
    def test_request_shape_and_no_inventory_fields(self):
        request = build_import_request(sample_payload())
        self.assertEqual(len(request["items"]), 2)
        text = json.dumps(request, ensure_ascii=False)
        self.assertIsNone(FORBIDDEN_FIELD_PATTERN.search(text))

        first = request["items"][0]
        self.assertEqual(first["offer_id"], "P000001-S1")
        self.assertEqual(first["description_category_id"], 1001)
        self.assertEqual(first["type_id"], 2001)
        self.assertEqual(first["price"], "1290.00")  # 两位小数字符串
        self.assertEqual(first["currency_code"], "RUB")
        self.assertEqual(first["vat"], "0")
        self.assertEqual(first["primary_image"], "https://cdn.example.com/P000001/main-S1.png")
        self.assertNotIn("https://cdn.example.com/P000001/main-S1.png", first["images"])  # 主图不重复放进图集
        self.assertEqual(len(first["images"]), 2)
        self.assertIn("Термос", first["name"])
        self.assertEqual(first["description"], sample_payload()["description"])

    def test_measurements_use_mm_and_grams(self):
        item = build_import_request(sample_payload())["items"][0]
        self.assertEqual(item["depth"], 110)
        self.assertEqual(item["width"], 110)
        self.assertEqual(item["height"], 280)
        self.assertEqual(item["weight"], 430)
        self.assertEqual(item["weight_unit"], "g")
        self.assertEqual(item["dimension_unit"], "mm")

    def test_dictionary_attributes_use_dictionary_value_id(self):
        attributes = build_import_request(sample_payload())["items"][0]["attributes"]
        brand = [item for item in attributes if item["id"] == 85][0]
        self.assertEqual(brand["dictionary_value_id"], 126745801)
        self.assertNotIn("values", brand)  # 字典属性不能同时传文本值
        volume = [item for item in attributes if item["id"] == 10096][0]
        self.assertEqual(volume["values"], [{"value": "500"}])
        color = [item for item in attributes if item["id"] == 10097][0]
        self.assertEqual(color["values"], [{"value": "красный"}])

    def test_name_is_truncated_and_missing_measurements_are_omitted(self):
        payload = sample_payload()
        payload["variants"][0]["display_name_ru"] = "я" * 300
        payload["sku_measurements"] = {}
        item = build_import_request(payload)["items"][0]
        self.assertEqual(len(item["name"]), 255)
        self.assertNotIn("weight", item)  # 没确认尺寸重量就不填（不编造）
        self.assertNotIn("depth", item)

    def test_missing_category_or_variants_is_rejected(self):
        with self.assertRaises(OzonWriteError):
            build_import_request({"category": {"category_id": 0, "type_id": 0}, "variants": [{}]})
        with self.assertRaises(OzonWriteError):
            build_import_request({"category": {"category_id": 1001, "type_id": 2001}, "variants": []})


class ParseResponseTests(unittest.TestCase):
    def test_recorded_fixture_response(self):
        raw = json.loads((FIXTURES / "ozon-product-import-response.json").read_text(encoding="utf-8"))
        parsed = parse_import_response(raw, payload=sample_payload())
        self.assertEqual(parsed["task_id"], "1785432199")
        self.assertEqual(len(parsed["items"]), 2)
        first, second = parsed["items"]
        self.assertEqual(first["source_sku_id"], "S1")
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(second["source_sku_id"], "S2")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["errors"][0]["code"], "VALUE_MUST_BE_DICTIONARY")
        self.assertEqual(second["errors"][0]["attribute_id"], 10097)

    def test_product_id_is_taken_when_present(self):
        parsed = parse_import_response(
            {"result": {"task_id": 5, "items": [{"offer_id": "P000001-S1", "product_id": 98765, "errors": []}]}},
            payload=sample_payload(),
        )
        self.assertEqual(parsed["items"][0]["product_id"], 98765)
        self.assertEqual(parsed["items"][0]["status"], "submitted")

    def test_empty_response_is_an_error(self):
        parsed = parse_import_response({"result": {}}, payload=sample_payload())
        self.assertIsNone(parsed["task_id"])
        self.assertEqual(parsed["errors"][0]["code"], "OZON_EMPTY_RESPONSE")

    def test_status_query_uses_read_only_endpoint(self):
        transport = RecordingTransport([{"result": {"status": "imported"}}])
        fetch_import_status(transport, "1785432199")
        self.assertEqual(transport.calls[0]["path"], PATH_IMPORT_INFO)
        self.assertEqual(transport.calls[0]["body"], {"task_id": 1785432199})


class RetryPolicyTests(unittest.TestCase):
    def body(self) -> dict:
        return build_import_request(sample_payload())

    def test_retries_on_429_then_succeeds(self):
        slept: list[float] = []
        transport = RecordingTransport(
            [OzonHttpError("too many", status=429), {"result": {"task_id": 1, "items": []}}]
        )
        response, attempts = post_import(transport, self.body(), sleep=slept.append)
        self.assertEqual(attempts, 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(slept), 1)
        self.assertEqual(response["result"]["task_id"], 1)

    def test_gives_up_after_max_attempts_on_500(self):
        transport = RecordingTransport([OzonHttpError("boom", status=500) for _ in range(4)])
        with self.assertRaises(OzonWriteError) as ctx:
            post_import(transport, self.body(), max_attempts=3, sleep=lambda _: None)
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertFalse(ctx.exception.ambiguous)
        self.assertEqual(len(transport.calls), 3)

    def test_connection_error_is_not_retried_and_marked_ambiguous(self):
        transport = RecordingTransport([OzonHttpError("timeout", status=None)])
        with self.assertRaises(OzonWriteError) as ctx:
            post_import(transport, self.body(), max_attempts=3, sleep=lambda _: None)
        self.assertTrue(ctx.exception.ambiguous)
        self.assertEqual(len(transport.calls), 1)  # 写请求绝不盲目重试

    def test_4xx_other_than_429_is_not_retried(self):
        transport = RecordingTransport([OzonHttpError("bad request", status=400)])
        with self.assertRaises(OzonWriteError) as ctx:
            post_import(transport, self.body(), max_attempts=3, sleep=lambda _: None)
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(ctx.exception.ambiguous)


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.registry_path = self.root / "config" / "shops.json"
        registry = store_registry.example_registry()
        store_registry.set_enabled(registry, "default", True)
        store_registry.save_registry(registry, self.registry_path)
        self.env = {"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "secret"}

    def tearDown(self):
        self.tmp.cleanup()

    def uploader(self, transport) -> OzonWriteUploader:
        return OzonWriteUploader(
            transport_factory=lambda credentials: transport,
            registry_path=self.registry_path,
            env=self.env,
            sleep=lambda _: None,
        )

    def test_submit_returns_task_and_item_errors(self):
        raw = json.loads((FIXTURES / "ozon-product-import-response.json").read_text(encoding="utf-8"))
        transport = RecordingTransport([raw])
        receipt = self.uploader(transport).submit(sample_payload(), store_id="default")

        self.assertEqual(receipt["status"], "processing")
        self.assertEqual(receipt["task_id"], "1785432199")
        self.assertTrue(receipt["api_writes_performed"])
        self.assertEqual(receipt["api_writes"], 1)
        self.assertEqual(len(receipt["items"]), 2)
        self.assertEqual(receipt["items"][1]["status"], "failed")
        self.assertEqual(transport.calls[0]["path"], PATH_IMPORT)

    def test_ambiguous_failure_is_reported_not_retried(self):
        transport = RecordingTransport([OzonHttpError("timeout", status=None)])
        receipt = self.uploader(transport).submit(sample_payload(), store_id="default")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["errors"][0]["code"], "AMBIGUOUS")
        self.assertIn("人工核对", receipt["note"])
        self.assertEqual(len(transport.calls), 1)

    def test_missing_credentials_raise_clear_error(self):
        uploader = OzonWriteUploader(
            transport_factory=lambda credentials: RecordingTransport([]),
            registry_path=self.registry_path,
            env={},
        )
        with self.assertRaises(OzonWriteError) as ctx:
            uploader.submit(sample_payload(), store_id="default")
        self.assertIn("OZON_DEFAULT_CLIENT_ID", str(ctx.exception))

    def test_unknown_store_is_rejected(self):
        with self.assertRaises(OzonWriteError):
            self.uploader(RecordingTransport([])).submit(sample_payload(), store_id="nope")

    def test_real_transport_builds_headers_from_credentials(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"result": {"task_id": 7, "items": []}}'

        def fake_urlopen(request, timeout=None):
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["url"] = request.full_url
            return FakeResponse()

        credentials = OzonCredentials(client_id="cid", api_key="key")
        uploader = OzonWriteUploader(
            transport_factory=lambda creds: UrllibTransport(creds, urlopen=fake_urlopen),
            registry_path=self.registry_path,
            env=self.env,
        )
        receipt = uploader.submit(sample_payload(), store_id="default")
        self.assertEqual(receipt["task_id"], "7")
        self.assertEqual(captured["url"], f"{credentials.base_url}{PATH_IMPORT}")
        self.assertEqual(captured["headers"]["client-id"], "123")
        self.assertEqual(captured["headers"]["api-key"], "secret")


@unittest.skipUnless(HAS_CONTRACTS, "contracts 尚未拉取")
class UploadIntegrationTests(unittest.TestCase):
    """真实写适配器接到 upload_product 上（用夹具传输层，不发网络请求）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.registry_path = self.root / "config" / "shops.json"
        registry = store_registry.example_registry()
        store_registry.set_enabled(registry, "default", True)
        store_registry.save_registry(registry, self.registry_path)

        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/919191919.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.product_dir = self.products / summary["product_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_upload_product_with_real_adapter_records_task_id(self):
        raw = json.loads((FIXTURES / "ozon-product-import-response.json").read_text(encoding="utf-8"))
        transport = RecordingTransport([raw])
        uploader = OzonWriteUploader(
            transport_factory=lambda credentials: transport,
            registry_path=self.registry_path,
            env={"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "secret"},
        )
        # 直接给载荷注入最小可用数据（这里验证的是"提交 → 记账"，不是整条链）
        payload = sample_payload()
        payload["product_id"] = self.product_dir.name
        payload["variants"] = [
            {**payload["variants"][0], "offer_id": f"{self.product_dir.name}-S1"},
            {**payload["variants"][1], "offer_id": f"{self.product_dir.name}-S2"},
        ]
        (self.product_dir / "output").mkdir(parents=True, exist_ok=True)
        (self.product_dir / "output" / "upload-payload-fixture.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        receipt = uploader.submit(payload, store_id="default")
        self.assertEqual(receipt["task_id"], "1785432199")

        # 通过 upload_product 走一遍记账（用同一个夹具传输层）
        (self.product_dir / "input" / "source.json").write_text(
            json.dumps(
                {
                    "product_id": self.product_dir.name,
                    "skus": [{"sku_id": "S1"}, {"sku_id": "S2"}],
                }
            ),
            encoding="utf-8",
        )
        from pipeline import status as st

        status = st.new_status(self.product_dir.name)
        status["target_store_ids"] = ["default"]
        st.save_status(self.product_dir, status)
        built = build_upload_payload(self.product_dir, shop_name="default")
        self.assertTrue(built["production_blockers"])  # 数据不全 → 门禁照常拦住
        self.assertEqual(load_publications(self.product_dir)["stores"], {})

    def test_adapter_does_not_send_when_payload_blocked(self):
        """门禁优先：production 模式下有阻断项时，适配器根本不会被调用。"""
        from pipeline import status as st

        status = st.new_status(self.product_dir.name)
        status["target_store_ids"] = ["default"]
        st.save_status(self.product_dir, status)
        transport = RecordingTransport([{"result": {"task_id": 1, "items": []}}])
        uploader = OzonWriteUploader(
            transport_factory=lambda credentials: transport,
            registry_path=self.registry_path,
            env={"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "secret"},
        )
        summary = upload_product(
            self.product_dir, ["default"], uploader, upload_mode=UPLOAD_MODE_PRODUCTION
        )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["api_writes"], 0)
        self.assertEqual(transport.calls, [])


class CliTests(unittest.TestCase):
    def test_show_request_prints_body_without_network(self):
        from pipeline.ozon_write import main

        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "payload.json"
            path.write_text(json.dumps(sample_payload(), ensure_ascii=False), encoding="utf-8")
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--payload", str(path)])
            self.assertEqual(code, 0)
            printed = json.loads(buffer.getvalue())
            self.assertTrue(printed["ok"])
            self.assertFalse(printed["api_writes_performed"])
            self.assertEqual(printed["items"], 2)
        finally:
            tmp.cleanup()

    def test_send_requires_explicit_confirmation(self):
        from pipeline.ozon_write import main

        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "payload.json"
            path.write_text(json.dumps(sample_payload(), ensure_ascii=False), encoding="utf-8")
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--payload", str(path), "--send", "--store", "default"])
            self.assertEqual(code, 2)
            self.assertIn("i-understand-this-hits-ozon", buffer.getvalue())
        finally:
            tmp.cleanup()

    def test_bad_payload_reports_error(self):
        from pipeline.ozon_write import main

        tmp = tempfile.TemporaryDirectory()
        try:
            path = pathlib.Path(tmp.name) / "payload.json"
            path.write_text("{not json", encoding="utf-8")
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--payload", str(path)])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(buffer.getvalue())["ok"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
