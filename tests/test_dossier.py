"""商品档案测试：汇总正确 + 缺数据如实说"缺"（全离线）。"""

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
from pipeline.batch import create_batch  # noqa: E402
from pipeline.dossier import collect_dossier, main as dossier_main, render_dossier, write_dossier  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.sku_selection import set_selection  # noqa: E402
from pipeline.upload import SimulatedUploader  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


class DossierFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.batches = self.root / "batches"
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/707070707.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5, "image_path": "input/sku-images/01.png"},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0, "image_path": "input/sku-images/02.png"},
                    {"sku_id": "S3", "color_ru": "зелёный", "capacity": "500 мл", "purchase_price_cny": 19.5, "image_path": "input/sku-images/03.png"},
                ],
            },
        )
        self.product_dir = self.products / summary["product_id"]
        for relative, count in (("main-images", 3), ("sku-images", 3), ("detail-images", 3)):
            target = self.product_dir / "input" / relative
            target.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (target / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(
            self.product_dir,
            ["термос 500 мл", "термос для чая"],
            category={"category_id": "1001", "type_id": "2001"},
        )
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
                    },
                    "sku_overrides": {
                        sku_id: {
                            "product_length_mm": 90,
                            "product_width_mm": 90,
                            "product_height_mm": 250,
                            "product_weight_g": 350,
                            "package_length_mm": 110,
                            "package_width_mm": 110,
                            "package_height_mm": 280,
                            "package_weight_g": 430,
                        }
                        for sku_id in ("S1", "S2", "S3")
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_chain(self, *, until: str | None = "ozon_upload"):
        from pipeline.ozon_http import FixtureTransport, OzonClient

        fixtures = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
        create_batch(
            self.products,
            batches_root=self.batches,
            product_ids=[self.product_dir.name],
            target_store_ids=["shop-a"],
        )
        return run_product(
            self.product_dir,
            until=until,
            dry_run=True,
            provider=FakeProvider(),
            image_generator=LocalPlaceholderGenerator(),
            uploader=SimulatedUploader(),
            ozon_client=OzonClient(FixtureTransport(directory=fixtures)),
        )


class CollectTests(DossierFixture):
    def test_collect_before_running_chain_reports_missing_honestly(self):
        dossier = collect_dossier(self.product_dir)
        self.assertEqual(dossier["product_id"], self.product_dir.name)
        self.assertEqual(len(dossier["skus"]), 3)
        self.assertEqual(dossier["copy"]["title_ru"], None)
        text = render_dossier(dossier)
        self.assertIn("还没有公网图片地址", text)
        self.assertIn("还没有店铺回执", text)
        # 关键词在、但没有分数 → 要明确提示，而不是留空让人以为已打分
        self.assertEqual(len(dossier["keywords"]), 2)
        self.assertIn("没有分数", text)
        # 还没跑 upload_feasibility → 不能说"没有阻断项"
        self.assertIn("无法判断", text)
        self.assertNotIn("✅ 没有阻断项", text)

    def test_missing_status_file_is_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            collect_dossier(self.root / "nope")


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class AfterChainTests(DossierFixture):
    def test_dossier_after_chain_has_everything(self):
        report = self.run_chain()
        self.assertEqual(report["stop_reason"], "until_reached", report)
        dossier = collect_dossier(self.product_dir)

        self.assertEqual(len(dossier["keywords"]), 2)
        self.assertEqual(dossier["skus"][0]["selling_price_rub"] is not None, True)
        self.assertTrue(dossier["copy"]["title_ru"])
        self.assertGreater(dossier["copy"]["title_length"], 10)
        self.assertEqual(dossier["copy"]["rule_blocking"], [])
        self.assertEqual(len(dossier["images"]["main"]), 3)
        self.assertEqual(dossier["images"]["published"], 0)  # 还没发布
        self.assertIn(dossier["attributes"]["missing"], (None, 0, *range(0, 100)))

        text = render_dossier(dossier)
        for section in ("一、选词与类目", "二、SKU 与上架范围", "三、产品信息总结", "四、俄文文案", "五、图片规划", "六、Ozon 类目属性", "七、上传载荷", "八、下一步"):
            self.assertIn(section, text)
        self.assertIn("main-S1", text)
        self.assertIn("上架 **3** / 采集 3 个 SKU", text)

    def test_sku_selection_reflected_in_dossier(self):
        set_selection(self.product_dir, include=["S1", "S3"], reason="只上两个颜色")
        self.run_chain()
        dossier = collect_dossier(self.product_dir)
        listed = {item["sku_id"]: item["listed"] for item in dossier["skus"]}
        self.assertEqual(listed, {"S1": True, "S2": False, "S3": True})
        text = render_dossier(dossier)
        self.assertIn("上架 **2** / 采集 3 个 SKU", text)
        self.assertIn("只上两个颜色", text)
        self.assertEqual(len(dossier["images"]["main"]), 2)  # 未上架的不生成主图

    def test_dossier_surfaces_rule_advisory(self):
        (self.product_dir / "output" / "copy-ru.json").write_text(
            json.dumps(
                {
                    "title_ru": "Термос 500 мл ТЕРМОС",
                    "description_ru": "Очень удобный термос. " * 6 + "Цена 1500 руб",
                    "hashtags": ["#термос"],
                    "description_sections": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        dossier = collect_dossier(self.product_dir)
        self.assertTrue(any("价格/促销" in item for item in dossier["copy"]["rule_blocking"]))
        self.assertTrue(any("全大写" in item for item in dossier["copy"]["rule_advisory"]))
        text = render_dossier(dossier)
        self.assertIn("⛔", text)
        self.assertIn("⚠️", text)

    def test_capture_quality_shown_in_dossier(self):
        (self.product_dir / "output" / "source-quality.json").write_text(
            json.dumps(
                {
                    "blocking": [],
                    "warnings": ["一张图都没有（input/main-images 等为空）：图片规划/质检做不了，补图后再跑"],
                    "stats": {"skus_active": 3, "skus_total": 3, "images": {"main": 0}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        dossier = collect_dossier(self.product_dir)
        self.assertTrue(dossier["capture_quality"]["checked"])
        text = render_dossier(dossier)
        self.assertIn("采集体检", text)
        self.assertIn("一张图都没有", text)

    def test_write_and_cli(self):
        self.run_chain()
        result = write_dossier(self.product_dir)
        self.assertTrue(result["ok"])
        path = self.product_dir / "output" / "dossier.md"
        self.assertTrue(path.is_file())
        self.assertIn("商品档案", path.read_text(encoding="utf-8"))

        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = dossier_main(["--product-dir", str(self.product_dir), "--json"])
        self.assertEqual(code, 0, buffer.getvalue())
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["product_id"], self.product_dir.name)
        self.assertIn("skus", payload)

    def test_cli_on_missing_product(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = dossier_main(["--product-dir", str(self.root / "nope")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
