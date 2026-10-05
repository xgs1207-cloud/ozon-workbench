"""采集入库测试：校验、查重、落盘、采集绑定，以及与流水线的联动。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import (  # noqa: E402
    CaptureValidationError,
    DuplicateCaptureError,
    allocate_product_id,
    import_folder,
    ingest_capture,
)
from pipeline import status as st  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.runner import run_product  # noqa: E402

SOURCE_URL = "https://detail.1688.com/offer/123456789.html"


def sample_payload(**overrides):
    base = {
        "source_url": SOURCE_URL,
        "title_zh": "测试保温杯",
        "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
        "skus": [
            {"sku_id": "S1", "offer_id": "SKU-RED-500", "purchase_price_cny": 18.5, "color_zh": "红色"},
            {"sku_id": "S2", "offer_id": "SKU-BLUE-500", "purchase_price_cny": "19,0", "color_zh": "蓝色"},
        ],
        "extra": {"attributes": [{"name": "材质", "value": "304 不锈钢"}]},
    }
    base.update(overrides)
    return base


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_writes_expected_files(self):
        summary = ingest_capture(self.products, sample_payload())
        self.assertEqual(summary["product_id"], "P000001")
        self.assertEqual(summary["status"], "COLLECTED")
        self.assertEqual(summary["counts"]["skus"], 2)
        self.assertEqual(summary["counts"]["attributes"], 1)
        self.assertEqual(summary["duplicate_of"], None)

        directory = self.products / "P000001"
        for relative in (
            "input/source.json",
            "input/raw-snapshot.json",
            "input/source-manifest.json",
            "input/category-selection.json",
            "status.json",
        ):
            self.assertTrue((directory / relative).is_file(), relative)

        source = json.loads((directory / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["offer_id"], "123456789")
        self.assertEqual(source["skus"][1]["purchase_price_cny"], 19.0)
        self.assertEqual(source["skus"][0]["position"], 1)
        self.assertEqual(source["version"], 1)

        saved = st.load_status(directory)
        self.assertEqual(saved["status"], "COLLECTED")
        self.assertEqual(saved["completed_steps"], ["collect_source"])
        self.assertEqual(saved["collection_id"], summary["collection_id"])

    def test_collection_binding_is_usable_by_pipeline(self):
        summary = ingest_capture(self.products, sample_payload())
        directory = self.products / summary["product_id"]
        binding = st.source_snapshot_binding(directory)
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding["collection_id"], summary["collection_id"])
        manifest = directory / "input" / "source-manifest.json"
        expected = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertEqual(binding["source_manifest_sha256"], expected)

    def test_validation_failures(self):
        cases = {
            "非 1688 链接": sample_payload(source_url="https://www.ozon.ru/product/1"),
            "没有 SKU": sample_payload(skus=[]),
            "SKU 超过 10 个": sample_payload(
                skus=[{"sku_id": f"S{i}", "purchase_price_cny": 1} for i in range(11)]
            ),
            "缺 sku_id": sample_payload(skus=[{"purchase_price_cny": 5}]),
            "缺采购价": sample_payload(skus=[{"sku_id": "S1"}]),
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(CaptureValidationError):
                    ingest_capture(self.products, payload)

    def test_duplicate_then_new_version(self):
        first = ingest_capture(self.products, sample_payload())
        with self.assertRaises(DuplicateCaptureError) as ctx:
            ingest_capture(self.products, sample_payload())
        self.assertEqual(ctx.exception.existing_product_id, first["product_id"])
        self.assertIn("create_new_version", ctx.exception.to_dict()["options"])

        second = ingest_capture(self.products, sample_payload(), allow_new_version=True)
        self.assertEqual(second["product_id"], "P000002")
        self.assertEqual(second["duplicate_of"], "P000001")
        self.assertEqual(second["version"], 2)
        self.assertEqual(len(list(self.products.glob("P*"))), 2)

    def test_allocate_skips_used_numbers(self):
        (self.products / "P000005" / "input").mkdir(parents=True)
        self.assertEqual(allocate_product_id(self.products), "P000006")

    def test_images_copied_and_deduped(self):
        image_a = self.root / "a.png"
        image_b = self.root / "b.png"
        image_a.write_bytes(b"same-bytes")
        image_b.write_bytes(b"same-bytes")
        payload = sample_payload(
            images={
                "main": [{"path": str(image_a), "name": "a.png"}, {"path": str(image_b), "name": "b.png"}],
                "detail": [{"path": str(image_a), "name": "a.png"}],
            }
        )
        summary = ingest_capture(self.products, payload)
        self.assertEqual(summary["counts"]["main_images"], 1)  # 内容相同 → 去重
        self.assertEqual(summary["counts"]["detail_images"], 0)
        self.assertTrue(any("重复图片" in item for item in summary["warnings"]))
        stored = list((self.products / summary["product_id"] / "input" / "main-images").iterdir())
        self.assertEqual(len(stored), 1)

    def test_missing_image_only_warns(self):
        summary = ingest_capture(
            self.products, sample_payload(images={"main": [{"path": str(self.root / "nope.png")}]})
        )
        self.assertEqual(summary["counts"]["main_images"], 0)
        self.assertTrue(any("不存在" in item for item in summary["warnings"]))

    def test_import_folder_with_descriptor(self):
        folder = self.root / "capture"
        (folder / "main-images").mkdir(parents=True)
        (folder / "detail-images").mkdir(parents=True)
        (folder / "main-images" / "m1.png").write_bytes(b"m1")
        (folder / "detail-images" / "d1.png").write_bytes(b"d1")
        (folder / "detail-images" / "d2.png").write_bytes(b"d2")
        (folder / "product.json").write_text(
            json.dumps(
                {
                    "source_url": SOURCE_URL,
                    "title_zh": "文件夹导入测试",
                    "category": {"category_id": "1001", "type_id": "2001"},
                    "skus": [{"sku_id": "S1", "purchase_price_cny": 12}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        summary = import_folder(self.products, folder)
        self.assertEqual(summary["counts"]["skus"], 1)
        self.assertEqual(summary["counts"]["main_images"], 1)
        self.assertEqual(summary["counts"]["detail_images"], 2)


class PipelineIntegrationTests(unittest.TestCase):
    """采集 → 批次 → 干跑：证明真实数据能一路走到阶段 A 的门禁。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingested_product_runs_into_phase_a(self):
        summary = ingest_capture(self.products, sample_payload())
        product_id = summary["product_id"]

        batch = create_batch(
            self.products, batches_root=self.batches, target_store_ids=["shop-a"]
        )
        self.assertEqual(batch["product_count"], 1)
        saved = st.load_status(self.products / product_id)
        self.assertTrue(saved["task_authorized"])
        self.assertIsNotNone(saved["sku_run_snapshot"]["dependency_hash"])
        self.assertEqual(saved["source_snapshot_binding"]["collection_id"], summary["collection_id"])

        report = run_product(self.products / product_id, until="product_analysis")
        self.assertEqual(report["stop_reason"], "until_reached")
        self.assertEqual(report["completed_steps"], ["collect_source", "validate_source"])
        self.assertEqual(report["api_write_count"], 0)
        # 采集入库写了 raw-snapshot 与 category-selection，所以不应有"缺少可选输入"的告警；
        # 但采集体检会提醒"这个商品还没有图片"（这是提醒不是阻断，图片步骤自己会再拦）
        warnings = report["executed"][0]["warnings"]
        self.assertFalse(any("缺少可选输入" in item for item in warnings), warnings)
        self.assertTrue(any("图" in item for item in warnings), warnings)

        # 再往下跑：因为步骤还没实现，必须停下而不是假装完成
        again = run_product(self.products / product_id)
        self.assertEqual(again["stop_reason"], "handler_not_implemented")
        self.assertEqual(again["stopped_at"], "product_analysis")


if __name__ == "__main__":
    unittest.main()
