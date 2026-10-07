"""端到端回归：从采集入库一路跑到 Ozon 上传回执 —— **一个桩都不剩**。

全部走真实代码：validate_source → variant_rules → offer_exists_check → upload_feasibility →
product_analysis(fake 模型层) → category_match(真实只读适配器 + 夹具传输层) →
measurements(真实定价引擎 + 人工确认尺寸) → product_positioning / ecommerce_design(真实设计装配器) →
russian_copy(从设计纯投影) → field_completion → image_plan → image_generation(本地占位图) →
image_qc(真质检) → publish_urls(图链映射) → ozon_upload(模拟上传器)。

断言整条链的最终状态：15 步全绿、台账有 task_id、幂等、预检可提交、载荷无库存字段、
干跑零写请求 / production 精确写 N 次。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.local_image import LocalPlaceholderGenerator  # noqa: E402
from pipeline import status as st  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.doctor import run_doctor  # noqa: E402
from pipeline.publications import load_publications  # noqa: E402
from pipeline.publish_urls import write_url_map  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.steps import PIPELINE_STEPS  # noqa: E402
from pipeline.upload import DryRunUploader, SimulatedUploader  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
STORE_IDS = ["shop-a", "shop-b"]
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


def capture_payload() -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/808080808.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
        "skus": [
            {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5, "image_path": "input/sku-images/01.png"},
            {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0, "image_path": "input/sku-images/02.png"},
        ],
    }


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class EndToEndChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        self.provider = FakeProvider()

        summary = ingest_capture(self.products, capture_payload())
        self.product_dir = self.products / summary["product_id"]
        for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
            directory = self.product_dir / "input" / relative
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))

        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        self.write_confirmed_measurements()
        create_batch(self.products, batches_root=self.batches, target_store_ids=list(STORE_IDS))

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 工具

    def write_confirmed_measurements(self) -> None:
        """真实流程里这一步是人在 SKU 资料表里填的 —— 它是**输入**，不是桩。"""
        (self.product_dir / "input" / "workbench-sku-overrides.json").write_text(
            json.dumps(
                {
                    "product": {
                        "product_length_mm": 90,
                        "product_width_mm": 90,
                        "product_height_mm": 250,
                        "product_weight_g": 350,
                        "package_length_mm": 110,
                        "package_width_mm": 110,
                        "package_height_mm": 280,
                        "package_weight_g": 430,
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def run_full(self, uploader, *, dry_run: bool, until: str | None = None, step_budget: int = 30):
        from pipeline.ozon_http import FixtureTransport, OzonClient

        return run_product(
            self.product_dir,
            until=until,
            provider=self.provider,
            image_generator=LocalPlaceholderGenerator(),
            uploader=uploader,
            ozon_client=OzonClient(FixtureTransport(directory=FIXTURES)),
            dry_run=dry_run,
            app_mode="production" if not dry_run else "development",
            step_budget=step_budget,
        )

    def prepare_through_images(self, uploader) -> dict:
        """跑到图片质检为止（不含上传），然后写出图位 → 公网 URL 映射。"""
        report = self.run_full(uploader, dry_run=True, until="ozon_upload")
        write_url_map(self.product_dir, "https://cdn.example.com")
        return report

    def read(self, relative: str):
        return json.loads((self.product_dir / relative).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 用例

    def test_full_chain_to_upload_receipt(self):
        first = self.prepare_through_images(DryRunUploader())
        # 到上传前的每一步都真实完成（没有 handler_not_implemented / 桩）
        self.assertEqual(first["stop_reason"], "until_reached")
        self.assertEqual(list(first["completed_steps"]), ["collect_source", *PIPELINE_STEPS[:-1]])
        self.assertTrue((self.product_dir / "output" / "ozon-ecommerce-design.json").is_file())
        design = self.read("output/ozon-ecommerce-design.json")
        self.assertEqual(len(design["detail_images"]), 8)
        self.assertEqual(len(design["main_images"]), 2)
        self.assertEqual(design["decision_trace"]["compliance_status"], "PASS")

        report = self.run_full(SimulatedUploader(), dry_run=False)

        self.assertEqual(report["stop_reason"], "all_available_steps_done")
        self.assertEqual(list(report["completed_steps"]), ["collect_source", *PIPELINE_STEPS])
        self.assertEqual(report["api_write_count"], 2)  # 两家店铺各一次写请求

        saved = st.load_status(self.product_dir)
        self.assertEqual(saved["status"], "UPLOADING")
        self.assertEqual(saved["pending_steps"], [])
        self.assertEqual(saved["progress"], 100)

        publications = load_publications(self.product_dir)
        for store in STORE_IDS:
            entry = publications["stores"][store]
            self.assertEqual(entry["selected"], True)
            self.assertTrue(all(item["task_id"] for item in entry["sku_publications"]), entry)
            receipt = self.read(f"output/store-runs/{store}/ozon-result.json")
            self.assertEqual(receipt["status"], "submitted")
            self.assertNotEqual(receipt["task_id"], "unknown")
            payload = self.read(f"output/store-runs/{store}/payload.json")
            self.assertEqual(payload["production_blockers"], [])
            self.assertFalse(payload["api_request_template"]["inventory_fields_included"])
            self.assertNotIn("stock", json.dumps(payload, ensure_ascii=False).lower().replace("stock_mode", ""))

        summary = self.read("output/upload-summary.json")
        self.assertEqual(summary["submitted"], 2)
        self.assertEqual(summary["api_writes"], 2)

        # 幂等：再跑一次不会重复创建
        again = self.run_full(SimulatedUploader(), dry_run=False)
        self.assertEqual(again["api_write_count"], 2)  # 没有新增
        self.assertEqual(again["stop_reason"], "all_available_steps_done")
        summary_again = self.read("output/upload-summary.json")
        self.assertEqual(summary_again["submitted"], 0)
        self.assertEqual(summary_again["skipped"], 2)

    def test_dry_run_never_writes_and_yields_no_task(self):
        self.prepare_through_images(DryRunUploader())
        report = self.run_full(DryRunUploader(), dry_run=True)

        # 干跑模式下 runner 直接停在闸门前：ozon_upload 根本不执行（设计如此）
        self.assertEqual(report["stop_reason"], "upload_gate_refused_dry_run")
        self.assertEqual(report["stopped_at"], "ozon_upload")
        self.assertNotIn("ozon_upload", report["completed_steps"])
        self.assertEqual(report["api_write_count"], 0)
        self.assertEqual(load_publications(self.product_dir)["stores"], {})
        self.assertEqual(st.load_status(self.product_dir)["api_write_count"], 0)

    def test_direct_dry_run_upload_writes_receipts_without_task(self):
        """干跑上传只能通过 upload_product() 直接调用（runner 的 ozon_upload 语义是"真提交"）。"""
        from pipeline.upload import build_upload_payload, upload_product

        self.prepare_through_images(DryRunUploader())
        summary = upload_product(self.product_dir, STORE_IDS, DryRunUploader())

        self.assertEqual(summary["api_writes"], 0)
        self.assertEqual(summary["submitted"], 0)
        publications = load_publications(self.product_dir)
        for store in STORE_IDS:
            self.assertFalse(any(item["task_id"] for item in publications["stores"][store]["sku_publications"]))
        self.assertEqual(build_upload_payload(self.product_dir, shop_name=STORE_IDS[0])["production_blockers"], [])

    def test_doctor_reports_ready_after_chain(self):
        self.prepare_through_images(DryRunUploader())
        report = run_doctor(
            self.products,
            product_ids=[self.product_dir.name],
            registry_path=self.root / "config" / "shops.json",
            env={},
        )
        product = report["products"][0]
        self.assertEqual(product["blockers"], [], product["blockers"])
        self.assertTrue(product["ready_to_submit"])
        self.assertEqual(product["target_stores"], STORE_IDS)

    def test_missing_public_urls_blocks_the_chain(self):
        """故意跳过对象存储那一步：必须在 ozon_upload 前被拦住。"""
        self.run_full(DryRunUploader(), dry_run=True, until="ozon_upload")
        report = self.run_full(SimulatedUploader(), dry_run=False)
        self.assertEqual(report["stop_reason"], "gate_failed")
        self.assertEqual(report["stopped_at"], "ozon_upload")
        self.assertEqual(report["api_write_count"], 0)
        self.assertEqual(st.load_status(self.product_dir)["status"], "NEEDS_ATTENTION")

        details = report["executed"][-1]["details"]["stores"]["shop-a"]
        self.assertEqual(details["status"], "failed")
        self.assertTrue(any("https" in item for item in details["blockers"]), details)


if __name__ == "__main__":
    unittest.main()
