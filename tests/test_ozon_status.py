"""终态跟踪测试：import/info 解析、只读轮询、台账回写、与 upload_product 的联动、doctor 展示。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from pipeline import status as st  # noqa: E402
from pipeline import stores as store_registry  # noqa: E402
from pipeline.doctor import diagnose_product  # noqa: E402
from pipeline.ozon_http import OzonHttpError  # noqa: E402
from pipeline.ozon_status import (  # noqa: E402
    apply_confirmation,
    confirm_product,
    confirm_task,
    parse_import_info,
)
from pipeline.ozon_write import PATH_IMPORT, PATH_IMPORT_INFO, OzonWriteUploader  # noqa: E402
from pipeline.publications import load_publications, record_publication, save_publications  # noqa: E402
from pipeline.upload import UPLOAD_MODE_PRODUCTION, upload_product  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


class RecordingTransport:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, path, body):
        self.calls.append({"path": path, "body": json.loads(json.dumps(body))})
        result = self.responses.pop(0) if self.responses else {"result": {}}
        if isinstance(result, Exception):
            raise result
        return result


def info_response(*items) -> dict:
    return {"result": {"items": list(items)}}


def imported(offer_id: str, product_id: int | None = 555) -> dict:
    return {"offer_id": offer_id, "product_id": product_id, "status": "imported", "errors": []}


def pending(offer_id: str) -> dict:
    return {"offer_id": offer_id, "product_id": 0, "status": "pending", "errors": []}


def failed(offer_id: str, code: str = "VALUE_MUST_BE_DICTIONARY") -> dict:
    return {
        "offer_id": offer_id,
        "product_id": 0,
        "status": "failed",
        "errors": [{"code": code, "message": "должно быть значением из справочника", "attribute_id": 10097}],
    }


def sample_payload() -> dict:
    return {
        "product_id": "P000001",
        "shop_name": "default",
        "category": {"category_id": 1001, "type_id": 2001},
        "variants": [
            {"source_sku_id": "S1", "offer_id": "P000001-S1", "price": "1290.00", "currency_code": "RUB"},
            {"source_sku_id": "S2", "offer_id": "P000001-S2", "price": "1390.00", "currency_code": "RUB"},
        ],
        "images": [],
        "attributes": [],
        "sku_measurements": {},
    }


class ParseTests(unittest.TestCase):
    def test_terminal_when_all_items_done(self):
        parsed = parse_import_info(info_response(imported("P000001-S1"), failed("P000001-S2")), payload=sample_payload())
        self.assertTrue(parsed["terminal"])
        self.assertEqual(parsed["counts"], {"total": 2, "imported": 1, "failed": 1, "pending": 0})
        self.assertEqual(parsed["items"][0]["source_sku_id"], "S1")
        self.assertEqual(parsed["items"][0]["product_id"], 555)
        self.assertEqual(parsed["items"][1]["errors"][0]["code"], "VALUE_MUST_BE_DICTIONARY")

    def test_pending_is_not_terminal(self):
        parsed = parse_import_info(info_response(imported("P000001-S1"), pending("P000001-S2")), payload=sample_payload())
        self.assertFalse(parsed["terminal"])
        self.assertEqual(parsed["counts"]["pending"], 1)

    def test_empty_and_unknown_statuses(self):
        self.assertFalse(parse_import_info({"result": {"items": []}})["terminal"])
        parsed = parse_import_info(info_response({"offer_id": "P000001-S1", "status": "weird"}), payload=sample_payload())
        self.assertFalse(parsed["terminal"])
        self.assertEqual(parsed["unknown_statuses"], ["weird"])


class PollingTests(unittest.TestCase):
    def test_polls_until_terminal_and_sleeps_between(self):
        slept: list[float] = []
        transport = RecordingTransport(
            [
                info_response(pending("P000001-S1")),
                info_response(imported("P000001-S1")),
            ]
        )
        result = confirm_task(transport, "777", payload=sample_payload(), interval_seconds=2.0, sleep=slept.append)
        self.assertTrue(result["terminal"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(slept, [2.0])
        self.assertEqual({call["path"] for call in transport.calls}, {PATH_IMPORT_INFO})  # 只读
        self.assertEqual(transport.calls[0]["body"], {"task_id": 777})
        self.assertEqual(len(result["history"]), 2)

    def test_timeout_after_max_attempts(self):
        transport = RecordingTransport([info_response(pending("P000001-S1")) for _ in range(5)])
        result = confirm_task(
            transport, "777", payload=sample_payload(), max_attempts=3, interval_seconds=0, sleep=lambda _: None
        )
        self.assertFalse(result["terminal"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(transport.calls), 3)

    def test_query_failure_is_reported_not_raised(self):
        transport = RecordingTransport([OzonHttpError("boom", status=500)])
        result = confirm_task(transport, "777", payload=sample_payload(), sleep=lambda _: None)
        self.assertFalse(result["confirmed"])
        self.assertIn("查询 import 状态失败", result["error"])


class ApplyConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.product_dir = self.root / "P000001"
        (self.product_dir / "output" / "store-runs" / "default").mkdir(parents=True)
        (self.product_dir / "output" / "store-runs" / "default" / "payload.json").write_text(
            json.dumps(sample_payload(), ensure_ascii=False), encoding="utf-8"
        )
        (self.product_dir / "output" / "store-runs" / "default" / "ozon-result.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "local_product_id": "P000001",
                    "shop_name": "default",
                    "task_id": "777",
                    "status": "processing",
                    "moderation_status": "unknown",
                    "items": [],
                    "errors": [],
                    "error_code": None,
                    "error_message": None,
                    "failed_step": None,
                    "created_at": "2026-10-05T12:00:00+08:00",
                    "raw_response": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def confirm(self, response, *, task_id="777", terminal=True):
        transport = RecordingTransport([response])
        result = confirm_task(transport, task_id, payload=sample_payload(), sleep=lambda _: None)
        applied = apply_confirmation(self.product_dir, store_id="default", confirmation=result, task_id=task_id)
        return result, applied

    def test_all_imported_updates_ledger_and_receipt(self):
        _, applied = self.confirm(info_response(imported("P000001-S1", 111), imported("P000001-S2", 222)))
        self.assertEqual(applied["status"], "created")
        publications = load_publications(self.product_dir)
        entry = publications["stores"]["default"]
        self.assertEqual(entry["status"], "created")
        self.assertEqual(entry["import_counts"]["imported"], 2)
        self.assertEqual(validate_contract("store-publications", publications), [])
        rows = {row["sku_id"]: row for row in entry["sku_publications"]}
        self.assertEqual(rows["S1"]["status"], "imported")
        self.assertEqual(rows["S1"]["ozon_product_id"], "111")
        self.assertEqual(rows["S2"]["task_id"], "777")

        result = json.loads(
            (self.product_dir / "output" / "store-runs" / "default" / "ozon-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["status"], "created")
        self.assertEqual(validate_contract("ozon-result", result), [])
        info = json.loads(
            (self.product_dir / "output" / "store-runs" / "default" / "import-info.json").read_text(encoding="utf-8")
        )
        self.assertFalse(info["api_writes_performed"])
        self.assertEqual(info["counts"]["imported"], 2)

    def test_failure_marks_store_failed_with_reason(self):
        _, applied = self.confirm(info_response(failed("P000001-S1")))
        self.assertEqual(applied["status"], "failed")
        result = json.loads(
            (self.product_dir / "output" / "store-runs" / "default" / "ozon-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "OZON_IMPORT_FAILED")
        self.assertEqual(validate_contract("ozon-result", result), [])

    def test_partial_and_timeout_states(self):
        _, applied = self.confirm(info_response(imported("P000001-S1"), failed("P000001-S2")))
        self.assertEqual(applied["status"], "partially_created")

        transport = RecordingTransport([info_response(pending("P000001-S1"))])
        timed_out = confirm_task(transport, "777", payload=sample_payload(), max_attempts=1, sleep=lambda _: None)
        applied_timeout = apply_confirmation(
            self.product_dir, store_id="default", confirmation=timed_out, task_id="777"
        )
        self.assertEqual(applied_timeout["status"], "processing")


try:  # 复用"除生图外全部就绪"的完整夹具
    from test_upload import UploadFixture
except ImportError:  # pragma: no cover
    UploadFixture = None


@unittest.skipUnless(HAS_CONTRACTS and UploadFixture is not None, "需要合约与完整夹具")
class UploadIntegrationTests(UploadFixture):
    """提交 → 只读确认 → 台账落终态（全夹具，不发网络）。"""

    def setUp(self):
        super().setUp()
        from pipeline.batch import create_batch

        self.registry_path = self.root / "config" / "shops.json"
        registry = store_registry.example_registry()
        store_registry.set_enabled(registry, "default", True)
        store_registry.save_registry(registry, self.registry_path)
        create_batch(self.products, batches_root=self.batches, target_store_ids=["default"])

    def uploader(self, transport) -> OzonWriteUploader:
        return OzonWriteUploader(
            transport_factory=lambda credentials: transport,
            registry_path=self.registry_path,
            env={"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "secret"},
            sleep=lambda _: None,
        )

    def test_upload_confirm_flow_keeps_single_write(self):
        """提交（1 次写）+ 只读确认：写请求数不能因为确认而增加。"""
        transport = RecordingTransport(
            [
                {
                    "result": {
                        "task_id": 999,
                        "items": [
                            {"offer_id": "P000001-S1", "product_id": 0, "errors": []},
                            {"offer_id": "P000001-S2", "product_id": 0, "errors": []},
                        ],
                    }
                },
                info_response(imported("P000001-S1", 4242), imported("P000001-S2", 4243)),
            ]
        )
        summary = upload_product(
            self.product_dir, ["default"], self.uploader(transport), upload_mode=UPLOAD_MODE_PRODUCTION
        )
        self.assertEqual(summary["api_writes"], 1)
        confirmation = summary["stores"]["default"]["confirmation"]
        self.assertEqual(confirmation["status"], "created")
        self.assertTrue(confirmation["terminal"])
        self.assertEqual(confirmation["counts"]["imported"], 2)

        publications = load_publications(self.product_dir)
        entry = publications["stores"]["default"]
        self.assertEqual(entry["status"], "created")
        self.assertEqual(entry["import_counts"]["imported"], 2)
        self.assertEqual([call["path"] for call in transport.calls], [PATH_IMPORT, PATH_IMPORT_INFO])

        rows = {row["sku_id"]: row for row in entry["sku_publications"]}
        self.assertEqual(rows["S1"]["status"], "imported")
        self.assertEqual(rows["S1"]["ozon_product_id"], "4242")
        self.assertEqual(validate_contract("store-publications", publications), [])

    def test_confirmation_failure_does_not_lose_submission(self):
        transport = RecordingTransport(
            [
                {
                    "result": {
                        "task_id": 999,
                        "items": [
                            {"offer_id": "P000001-S1", "product_id": 0, "errors": []},
                            {"offer_id": "P000001-S2", "product_id": 0, "errors": []},
                        ],
                    }
                },
                OzonHttpError("service unavailable", status=503),
            ]
        )
        summary = upload_product(
            self.product_dir, ["default"], self.uploader(transport), upload_mode=UPLOAD_MODE_PRODUCTION
        )
        # 提交事实保留：task_id 已记账，确认失败只是告警
        self.assertEqual(summary["api_writes"], 1)
        entry = load_publications(self.product_dir)["stores"]["default"]
        self.assertTrue(any(row.get("task_id") == "999" for row in entry["sku_publications"]))
        self.assertIn("查询 import 状态失败", str(summary["stores"]["default"]["confirmation"]["error"]))


class ManualConfirmTests(unittest.TestCase):
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
                "source_url": "https://detail.1688.com/offer/121212124.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [{"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0}],
            },
        )
        self.product_dir = self.products / summary["product_id"]
        record_publication(
            self.product_dir, "default", sku_id="S1", offer_id=f"{self.product_dir.name}-S1", task_id="888", status="submitted"
        )
        self.addCleanup(self.tmp.cleanup)

    def test_manual_confirm_uses_stored_task_id(self):
        transport = RecordingTransport([info_response(imported("P000001-S1", 777))])
        summary = confirm_product(
            self.product_dir,
            "default",
            transport_factory=lambda credentials: transport,
            registry_path=self.registry_path,
            env={"OZON_DEFAULT_CLIENT_ID": "1", "OZON_DEFAULT_API_KEY": "2"},
            sleep=lambda _: None,
        )
        self.assertEqual(summary["stores"]["default"]["status"], "created")
        self.assertFalse(summary["api_writes_performed"])
        self.assertEqual(transport.calls[0]["body"], {"task_id": 888})

    def test_store_without_task_is_skipped(self):
        summary = confirm_product(self.product_dir, "other", transport_factory=lambda credentials: None)
        self.assertEqual(summary["stores"]["other"]["status"], "skipped")

    def test_no_stores_at_all_is_an_error(self):
        with self.assertRaises(ValueError):
            confirm_product(self.root / "nonexistent")

    def test_cli_with_fixture(self):
        from pipeline.ozon_status import main

        fixture = self.root / "info.json"
        fixture.write_text(
            json.dumps(info_response(imported("P000001-S1", 999)), ensure_ascii=False), encoding="utf-8"
        )
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--product-dir",
                    str(self.product_dir),
                    "--store",
                    "default",
                    "--fixture",
                    str(fixture),
                ]
            )
        self.assertEqual(code, 0)
        printed = json.loads(buffer.getvalue())
        self.assertTrue(printed["ok"])
        self.assertEqual(printed["stores"]["default"]["status"], "created")


@unittest.skipUnless(HAS_CONTRACTS, "contracts 尚未拉取")
class DoctorSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/121212125.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [{"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0}],
            },
        )
        self.product_dir = self.products / summary["product_id"]
        status = st.new_status(self.product_dir.name)
        status["target_store_ids"] = ["shop-a"]
        st.save_status(self.product_dir, status)
        self.addCleanup(self.tmp.cleanup)

    def test_doctor_reports_pending_and_failed_submissions(self):
        record_publication(self.product_dir, "shop-a", sku_id="S1", offer_id="X-1", task_id="t1", status="pending")
        payload = load_publications(self.product_dir)
        payload["stores"]["shop-a"]["import_counts"] = {"total": 1, "imported": 0, "failed": 0, "pending": 1}
        save_publications(self.product_dir, payload)

        report = diagnose_product(self.product_dir)
        submission = report["submission"]["shop-a"]
        self.assertEqual(submission["status"], "pending")
        self.assertEqual(submission["task_ids"], ["t1"])
        self.assertEqual(submission["pending"], 1)
        self.assertTrue(any("仍在 processing" in item for item in report["warnings"]))

    def test_doctor_reports_rejections(self):
        record_publication(self.product_dir, "shop-a", sku_id="S1", offer_id="X-1", task_id="t1", status="failed")
        payload = load_publications(self.product_dir)
        payload["stores"]["shop-a"]["import_counts"] = {"total": 1, "imported": 0, "failed": 1, "pending": 0}
        save_publications(self.product_dir, payload)

        report = diagnose_product(self.product_dir)
        self.assertEqual(report["submission"]["shop-a"]["failed"], 1)
        self.assertTrue(any("被 Ozon 拒绝" in item for item in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
