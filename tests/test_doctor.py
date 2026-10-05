"""上线前预检（doctor）测试：环境检查、产物缺失、完整商品可提交、报告渲染。"""

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
from pipeline.doctor import check_environment, diagnose_product, render_report, run_doctor  # noqa: E402

try:  # 复用上传链路的完整夹具（tests 目录在 sys.path 上）
    from test_upload import UploadFixture
except ImportError:  # pragma: no cover
    UploadFixture = None

HAS_CONTRACTS = len(available_contracts()) > 0


def capture_payload() -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/313131313.html",
        "title_zh": "316 不锈钢保温杯",
        "category": {"category_id": "1001", "type_id": "2001"},
        "skus": [{"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0}],
    }


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.registry_path = self.root / "config" / "shops.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_registry_is_a_blocker(self):
        environment = check_environment(registry_path=self.registry_path, env={})
        self.assertEqual(environment["shops_enabled"], [])
        self.assertTrue(any("没有任何启用的店铺" in item for item in environment["blockers"]))
        self.assertGreater(environment["contracts_available"], 0)

    def test_enabled_shop_with_credentials_is_ready(self):
        registry = store_registry.example_registry()
        store_registry.set_enabled(registry, "default", True)
        store_registry.save_registry(registry, self.registry_path)
        environment = check_environment(
            registry_path=self.registry_path,
            env={"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "secret"},
        )
        self.assertEqual(environment["shops_enabled"], ["default"])
        self.assertEqual(environment["shops_with_credentials"], ["default"])
        self.assertEqual(environment["blockers"], [])

    def test_enabled_shop_without_credentials_blocks(self):
        registry = store_registry.example_registry()
        store_registry.set_enabled(registry, "default", True)
        store_registry.save_registry(registry, self.registry_path)
        environment = check_environment(registry_path=self.registry_path, env={})
        self.assertTrue(any("缺凭据" in item for item in environment["blockers"]))


class EmptyProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.products = pathlib.Path(self.tmp.name) / "products"
        self.products.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_products_suggests_capture(self):
        report = run_doctor(self.products, registry_path=self.products.parent / "config" / "shops.json", env={})
        self.assertEqual(report["summary"]["products"], 0)
        self.assertTrue(any("import_folder" in item or "采集" in item for item in report["next_steps"]))

    def test_partial_product_lists_missing_artifacts(self):
        summary = ingest_capture(self.products, capture_payload())
        report = run_doctor(
            self.products,
            product_ids=[summary["product_id"]],
            registry_path=self.products.parent / "config" / "shops.json",
            env={},
        )
        product = report["products"][0]
        self.assertFalse(product["ready_to_submit"])
        self.assertIn("没有选择目标店铺", product["blockers"])
        missing = {item["name"] for item in product["checks"] if item["status"] == "missing"}
        self.assertIn("俄文标题/简介", missing)
        self.assertIn("最终属性", missing)
        self.assertIn("图片公网地址", missing)
        self.assertIn("发布台账", missing)

    def test_diagnose_product_is_pure(self):
        summary = ingest_capture(self.products, capture_payload())
        directory = self.products / summary["product_id"]
        before = sorted(path.name for path in directory.rglob("*"))
        diagnose_product(directory)
        after = sorted(path.name for path in directory.rglob("*"))
        self.assertEqual(before, after)  # 预检不写任何文件

    def test_render_report_contains_table_and_next_steps(self):
        summary = ingest_capture(self.products, capture_payload())
        report = run_doctor(
            self.products,
            product_ids=[summary["product_id"]],
            registry_path=self.products.parent / "config" / "shops.json",
            env={},
        )
        text = render_report(report)
        self.assertIn("上线前预检", text)
        self.assertIn("| 商品 | 状态 | 可提交 |", text)
        self.assertIn(summary["product_id"], text)
        self.assertIn("本预检不做任何 Ozon 调用", text)


@unittest.skipUnless(HAS_CONTRACTS and UploadFixture is not None, "需要合约与完整夹具")
class ReadyProductDoctorTests(UploadFixture):
    def test_full_product_passes_payload_blockers(self):
        from pipeline.batch import create_batch

        create_batch(self.products, batches_root=self.batches, target_store_ids=["shop-a"])
        report = run_doctor(
            self.products,
            product_ids=[self.product_dir.name],
            registry_path=self.batches.parent / "config" / "shops.json",
            env={},
        )
        product = report["products"][0]
        self.assertEqual(product["blockers"], [], product["blockers"])
        self.assertTrue(product["ready_to_submit"])
        self.assertEqual(product["image_qc_decision"], "revise")  # 语义维度未评分
        self.assertEqual(product["missing_attributes"], 0)
        self.assertEqual(product["target_stores"], ["shop-a"])
        self.assertTrue(any(item["status"] == "ok" for item in product["checks"]))

    def test_doctor_marks_blocker_when_image_urls_removed(self):
        from pipeline.batch import create_batch

        create_batch(self.products, batches_root=self.batches, target_store_ids=["shop-a"])
        (self.product_dir / "output" / "image-public-urls.json").unlink()
        product = diagnose_product(self.product_dir)
        self.assertTrue(any("https" in item for item in product["blockers"]))
        self.assertFalse(product["ready_to_submit"])


if __name__ == "__main__":
    unittest.main()
