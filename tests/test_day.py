"""一天流程编排测试：选词 → 选品 → 采集清单 → 跑商品 → 预检（全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from contracts import available_contracts  # noqa: E402
from pipeline.day import main as day_main, render_report, run_day  # noqa: E402

HAS_OPENPYXL = True
try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover
    HAS_OPENPYXL = False

HAS_CONTRACTS = len(available_contracts()) > 0

HEADER = [
    "排名", "关键词", "类目", "关键词相关产品", "销售方式", "平均价格", "销量", "销售额",
    "月搜热度", "月搜增长", "商品数", "竞对数", "竞品数", "加购人数", "加购率", "转化率",
    "评论数", "评分", "市场空间", "商品可见度", "转化集中度", "重量", "体积", "退货取消率",
]


def write_seerfar_xlsx(path: pathlib.Path) -> pathlib.Path:
    """造一张与真实导出表同结构的表（24 列，列名/单位/百分号/中文注释都照抄）。"""
    book = Workbook()
    sheet = book.active
    sheet.append(HEADER)
    rows = [
        # 排名 关键词 类目 相关产品 销售方式 均价 销量 销售额 月搜热度 月搜增长 商品数 竞对数 竞品数 加购 加购率 转化率 评论 评分 市场空间 可见度 集中度 重量 体积 退货率
        (1, "термос 500 мл", "Термосы (保温杯)", "термос", "FBS", "1200₽", 800, "960000₽", 12000, "5.0%", 300, 8, 8, 500, "6.0%", "3.0%", 120, 4.8, "0.80", "0.60", "0.20", "450 g", "1.2 L", "3.2%"),
        (2, "термос для чая", "Термосы (保温杯)", "термос", "FBS", "1350₽", 600, "810000₽", 9000, "4.0%", 280, 12, 12, 420, "5.5%", "2.8%", 90, 4.7, "0.75", "0.58", "0.22", "480 g", "1.4 L", "4.0%"),
        (3, "кружка керамическая", "Кружки (陶瓷杯)", "кружка", "FBS", "600₽", 300, "180000₽", 15000, "2.0%", 900, 200, 200, 200, "3.0%", "1.5%", 400, 4.9, "0.40", "0.30", "0.50", "300 g", "0.6 L", "2.0%"),
        (4, "стакан бумажный", "Стаканы (纸杯)", "стакан", "FBS", "300₽", 120, "36000₽", 4000, "1.5%", 120, 5, 5, 60, "2.0%", "1.0%", 30, 4.5, "0.30", "0.20", "0.40", "50 g", "0.3 L", "1.5%"),
    ]
    for row in rows:
        sheet.append(list(row))
    book.save(path)
    return path


class DayFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.library = self.root / "keyword-library"
        self.products = self.root / "products"
        self.products.mkdir(parents=True, exist_ok=True)


@unittest.skipUnless(HAS_OPENPYXL, "需要 openpyxl")
class ImportAndPlanTests(DayFixture):
    def test_import_then_sourcing_and_collection_plan(self):
        xlsx = write_seerfar_xlsx(self.root / "seerfar.xlsx")
        report = run_day(
            xlsx=xlsx,
            library_root=self.library,
            products_root=self.products,
            base=self.root,
            top=5,
            collection_top=5,
        )
        by_name = {item["step"]: item for item in report["steps"]}
        self.assertEqual(by_name["import_keywords"]["status"], "ok")
        self.assertEqual(by_name["import_keywords"]["rows_parsed"], 4)
        self.assertEqual(by_name["import_keywords"]["keywords"], 4)
        self.assertEqual(by_name["sourcing_plan"]["status"], "ok")
        self.assertGreater(by_name["sourcing_plan"]["candidates"], 0)
        # 采集清单产出文件
        self.assertTrue((self.root / "output" / "collection-plan.md").is_file())
        self.assertTrue((self.root / "output" / "sourcing-plan.md").is_file())
        # 没有商品、没有店铺 → 跑商品被跳过，并给出待办
        self.assertEqual(by_name["launch"]["status"], "skipped")
        self.assertTrue(any("采集" in item for item in report["todos"]))
        self.assertIn("预检", render_report(report))

    def test_without_xlsx_uses_existing_library(self):
        xlsx = write_seerfar_xlsx(self.root / "seerfar.xlsx")
        run_day(xlsx=xlsx, library_root=self.library, products_root=self.products, base=self.root, top=3)
        again = run_day(library_root=self.library, products_root=self.products, base=self.root, top=3)
        by_name = {item["step"]: item for item in again["steps"]}
        self.assertEqual(by_name["import_keywords"]["status"], "skipped")
        self.assertEqual(by_name["sourcing_plan"]["status"], "ok")

    def test_empty_library_reports_clearly(self):
        report = run_day(library_root=self.root / "empty-lib", products_root=self.products, base=self.root)
        by_name = {item["step"]: item for item in report["steps"]}
        self.assertIn(by_name["sourcing_plan"]["status"], {"ok", "empty"})
        self.assertFalse(report["ok"])

    def test_bad_xlsx_reports_error_not_crash(self):
        bad = self.root / "bad.xlsx"
        bad.write_text("not an xlsx", encoding="utf-8")
        report = run_day(xlsx=bad, library_root=self.library, products_root=self.products, base=self.root)
        by_name = {item["step"]: item for item in report["steps"]}
        self.assertEqual(by_name["import_keywords"]["status"], "failed")
        self.assertIn("error", by_name["import_keywords"])
        self.assertFalse(report["ok"])


@unittest.skipUnless(HAS_OPENPYXL and HAS_CONTRACTS, "需要 openpyxl 与契约")
class LaunchStepTests(DayFixture):
    def _capture_product(self, keyword: str) -> str:
        from collector.ingest import ingest_capture

        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/555555555.html",
                "title_zh": "316 保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [{"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0}],
                "keywords": [keyword],
            },
        )
        directory = self.products / summary["product_id"]
        for relative in ("main-images", "sku-images", "detail-images"):
            target = directory / "input" / relative
            target.mkdir(parents=True, exist_ok=True)
            for index in range(1, 4):
                (target / f"{index:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + relative.encode() + bytes([index]))
        (directory / "input" / "workbench-sku-overrides.json").write_text(
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
                }
            ),
            encoding="utf-8",
        )
        return summary["product_id"]

    def test_day_runs_launch_for_collected_products(self):
        from models.fake import FakeProvider
        from models.local_image import LocalPlaceholderGenerator
        from pipeline.ozon_http import FixtureTransport, OzonClient
        from pipeline.oss_local import LocalObjectStorage
        from pipeline.upload import DryRunUploader

        xlsx = write_seerfar_xlsx(self.root / "seerfar.xlsx")
        library = self.root / "keyword-library"
        from collector.seerfar_xlsx import import_xlsx

        import_xlsx(xlsx, library)

        # 用库里真实存在的达标词挂到商品上，保证采集清单能识别"已采集"
        from keyword_library import store

        records = [row for row in store.load_all(library) if row.get("status") == "qualified"]
        self.assertTrue(records, "没有达标词")
        keyword = records[0]["keyword"]
        product_id = self._capture_product(keyword)

        fixtures = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
        report = run_day(
            library_root=library,
            products_root=self.products,
            base=self.root,
            stores=["shop-a"],
            top=5,
            collection_top=5,
            provider=FakeProvider(),
            image_generator=LocalPlaceholderGenerator(),
            uploader=DryRunUploader(),
            publisher=LocalObjectStorage(self.root / "www", "https://img.example.com"),
            ozon_client=OzonClient(FixtureTransport(directory=fixtures)),
            execute_upload=False,
        )
        by_name = {item["step"]: item for item in report["steps"]}
        self.assertEqual(by_name["launch"]["status"], "ok", report)
        self.assertEqual(by_name["launch"]["summary"]["products"], 1)
        self.assertEqual(by_name["launch"]["summary"]["ok"], 1)
        self.assertTrue(by_name["launch"]["dry_run"])
        # 干跑不得产生 task_id
        result = self.products / product_id / "output" / "store-runs" / "shop-a" / "ozon-result.json"
        if result.is_file():
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertIn(payload.get("task_id"), (None, "unknown"))

    def test_day_reports_attention_without_images(self):
        from models.fake import FakeProvider
        from models.local_image import LocalPlaceholderGenerator
        from pipeline.ozon_http import FixtureTransport, OzonClient
        from pipeline.upload import DryRunUploader

        xlsx = write_seerfar_xlsx(self.root / "seerfar.xlsx")
        library = self.root / "keyword-library"
        from collector.seerfar_xlsx import import_xlsx

        import_xlsx(xlsx, library)
        from keyword_library import store

        keyword = [row for row in store.load_all(library) if row.get("status") == "qualified"][0]["keyword"]
        self._capture_product(keyword)

        fixtures = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
        report = run_day(
            library_root=library,
            products_root=self.products,
            base=self.root,
            stores=["shop-a"],
            provider=FakeProvider(),
            image_generator=LocalPlaceholderGenerator(),
            uploader=DryRunUploader(),
            publisher=None,  # 没发布图片 → 上传门禁会拦
            ozon_client=OzonClient(FixtureTransport(directory=fixtures)),
        )
        by_name = {item["step"]: item for item in report["steps"]}
        # 干跑不会因为缺图而"失败"（这是刻意的：干跑只产生回执与阻断项），但预检必须如实指出它还不可提交
        self.assertEqual(by_name["doctor"]["ready_to_submit"], 0)
        self.assertTrue(
            any("P0000" in item for item in report["todos"]),
            f"待办里应当点名该商品：{report['todos']}",
        )
        self.assertFalse(report["ok"])


class CliTests(DayFixture):
    def test_cli_json_and_refusal_of_real_uploader(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = day_main(
                [
                    "--library",
                    str(self.root / "lib"),
                    "--products",
                    str(self.products),
                    "--base",
                    str(self.root),
                    "--json",
                ]
            )
        self.assertIn(code, (0, 1))
        payload = json.loads(buffer.getvalue())
        self.assertIn("steps", payload)

        with self.assertRaises(SystemExit):
            day_main(["--uploader", "ozon-api", "--store", "shop-a"])

    def test_cli_local_oss_requires_paths(self):
        with self.assertRaises(SystemExit):
            day_main(["--oss", "local"])


if __name__ == "__main__":
    unittest.main()
