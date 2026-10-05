"""类目绑定测试：类目树摊平、名字检索、绑定文件、与 Seerfar 导入的联动（全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline.category import (  # noqa: E402
    bind_categories,
    binding_for,
    load_bindings,
    main as category_main,
    resolve_bindings,
    save_bindings,
    search_tree,
)
from pipeline.ozon_http import PATH_TREE, FixtureTransport, OzonClient  # noqa: E402

TREE = {
    "result": [
        {
            "description_category_id": 17028922,
            "category_name": "Дом и сад",
            "children": [
                {"type_id": 91873, "type_name": "Постельное белье", "children": []},
                {"type_id": 91875, "type_name": "Простыни", "children": []},
                {"type_id": 91877, "type_name": "Наволочки", "children": []},
                {"type_id": 91900, "type_name": "Подушки", "children": []},
            ],
        },
        {
            "description_category_id": 20000001,
            "category_name": "Товары для кухни",
            "children": [{"type_id": 95001, "type_name": "Термосы", "children": []}],
        },
    ]
}

NAME_MAP = {
    "Простыня": (17028922, 91875),
    "Простыни": (17028922, 91875),
    "床单": (17028922, 91875),
    "Наволочка": (17028922, 91877),
    "Термос": (20000001, 95001),
}


class TreeSearchTests(unittest.TestCase):
    def test_tree_is_flattened_with_paths(self):
        rows = search_tree(TREE, "Постельное")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type_id"], 91873)
        self.assertEqual(rows[0]["category_id"], 17028922)
        self.assertEqual(rows[0]["path"], ["Дом и сад", "Постельное белье"])

    def test_exact_match_ranks_first(self):
        rows = search_tree(TREE, "Простыни")
        self.assertEqual(rows[0]["type_name"], "Простыни")
        self.assertNotEqual(rows[0]["type_name"], "Постельное белье")

    def test_partial_match_returns_candidates(self):
        rows = search_tree(TREE, "Наволочк")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["type_name"], "Наволочки")

    def test_unknown_name_yields_nothing(self):
        self.assertEqual(search_tree(TREE, "床单"), [])
        self.assertEqual(search_tree(TREE, "несуществующее"), [])

    def test_resolve_bindings_reports_status(self):
        resolved = resolve_bindings(TREE, ["Простыни", "床单"])
        self.assertEqual(resolved["Простыни"]["status"], "matched")
        self.assertEqual(resolved["床单"]["status"], "not_found")


class BindingsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "config" / "category-bindings.json"
        self.fixture_dir = pathlib.Path(self.tmp.name) / "fixtures"
        self.fixture_dir.mkdir(parents=True)
        (self.fixture_dir / "ozon-category-tree.json").write_text(
            json.dumps(TREE, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_bind_writes_file_and_picks_first_candidate(self):
        summary = bind_categories(
            ["Простыни", "床单"],
            client=OzonClient(FixtureTransport({PATH_TREE: TREE})),
            bindings_path=self.path,
        )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["bound"], 1)
        self.assertEqual(summary["unmatched"], ["床单"])
        saved = load_bindings(self.path)
        entry = saved["bindings"]["Простыни"]
        self.assertEqual(entry["category_id"], 17028922)
        self.assertEqual(entry["type_id"], 91875)
        self.assertEqual(entry["source"], "ozon_seller_api")
        self.assertTrue(saved["warnings"])

    def test_binding_lookup_by_zh_or_ru_name(self):
        save_bindings(
            {
                "bindings": {
                    "Простыни": {"category_id": "17028922", "type_id": "91875", "source": "ozon_seller_api"},
                    "床单": {"category_id": "17028922", "type_id": "91875"},
                }
            },
            self.path,
        )
        bindings = load_bindings(self.path)
        self.assertIsNotNone(binding_for(bindings, "床单", "Простыня"))
        self.assertIsNone(binding_for(bindings, "Наволочка"))
        self.assertEqual(binding_for(bindings, "床单")["type_id"], "91875")

    def test_auto_pick_unique_skips_ambiguous(self):
        ambiguous = {
            "result": [
                {
                    "description_category_id": 1,
                    "category_name": "Дом",
                    "children": [
                        {"type_id": 11, "type_name": "Простыни", "children": []},
                        {"type_id": 12, "type_name": "Простыни", "children": []},
                    ],
                }
            ]
        }
        summary = bind_categories(
            ["Простыни"],
            client=OzonClient(FixtureTransport({PATH_TREE: ambiguous})),
            bindings_path=self.path,
            auto_pick_unique=True,
        )
        self.assertEqual(summary["bound"], 0)
        self.assertEqual(summary["unmatched"], ["Простыни"])

    def test_cli_with_fixture(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = category_main(
                [
                    "--name",
                    "Простыни",
                    "--fixture-dir",
                    str(self.fixture_dir),
                    "--bindings",
                    str(self.path),
                ]
            )
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        self.assertIn("category_id=17028922", printed)
        self.assertTrue(self.path.is_file())

    def test_cli_requires_names(self):
        with self.assertRaises(SystemExit):
            category_main(["--fixture-dir", str(self.fixture_dir)])


class SetProductCategoryTests(unittest.TestCase):
    """采集漏选类目后补选：写 category-selection.json + 回填 source.json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.product_dir = self.root / "P000001"
        (self.product_dir / "input").mkdir(parents=True)
        (self.product_dir / "input" / "source.json").write_text(
            json.dumps({"product_id": "P000001", "source_url": "https://detail.1688.com/offer/1.html", "selected_category": None}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_sets_selection_and_patches_source(self):
        from pipeline.category import set_product_category

        result = set_product_category(
            self.product_dir, category_id="17028922", type_id="91875", category_path_zh="家居/床上用品"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["source_patched"])
        selection = json.loads(
            (self.product_dir / "input" / "category-selection.json").read_text(encoding="utf-8")
        )
        self.assertEqual(selection["type_id"], "91875")
        source = json.loads((self.product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["selected_category"]["category_id"], "17028922")

    def test_requires_both_ids(self):
        from pipeline.category import set_product_category

        with self.assertRaises(ValueError):
            set_product_category(self.product_dir, category_id="1", type_id="")

    def test_cli_set_product(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = category_main(
                [
                    "--set-product",
                    str(self.product_dir),
                    "--category-id",
                    "17028922",
                    "--type-id",
                    "91875",
                ]
            )
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertTrue(json.loads(buffer.getvalue())["ok"])

    def test_cli_set_product_requires_ids(self):
        with self.assertRaises(SystemExit):
            category_main(["--set-product", str(self.product_dir)])

    def test_analysis_sees_later_category(self):
        """分析步骤必须能看到"后来补的类目"，否则商品永远卡在缺类目。"""
        from models.fake import FakeProvider
        from pipeline.handlers import run_single_step
        from pipeline.selection import set_selected_keywords

        set_selected_keywords(self.product_dir, ["простынь 200х200"])
        (self.product_dir / "input" / "source.json").write_text(
            json.dumps(
                {
                    "product_id": "P000001",
                    "collection_id": "COL-XXXXXXXXXXXX",
                    "source_url": "https://detail.1688.com/offer/1.html",
                    "title_zh": "床单",
                    "selected_category": None,
                    "skus": [{"sku_id": "S1", "purchase_price_cny": 20.0}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        from pipeline.category import set_product_category

        set_product_category(self.product_dir, category_id="17028922", type_id="91875")
        result = run_single_step(self.product_dir, "product_analysis", provider=FakeProvider())
        self.assertEqual(result["decision"], "continue")
        analysis = json.loads(
            (self.product_dir / "output" / "product-analysis.json").read_text(encoding="utf-8")
        )
        self.assertFalse(any("类目" in str(risk) for risk in analysis.get("risks") or []), analysis.get("risks"))


class SeerfarBindingTests(unittest.TestCase):
    """导入 Seerfar 表时套用类目绑定（真实 Ozon id 进关键词库）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.library = self.root / "keyword-library"
        bindings_path = self.root / "config" / "category-bindings.json"
        save_bindings(
            {
                "bindings": {
                    "床单": {"category_id": "17028922", "type_id": "91875", "source": "ozon_seller_api"},
                }
            },
            bindings_path,
        )
        self.bindings_path = bindings_path

        import openpyxl

        header = ["排名", "关键词", "类目", "月搜热度", "竞对数", "商品数", "竞品数"]
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet.append(header)
        sheet.append([1, "простынь на резинке\n(橡胶床单)", "床单\nПростыня", 110906, 206, 10827, 1800])
        sheet.append([2, "наволочка\n(枕套)", "枕套\nНаволочка", 5000, 900, 20000, 3000])
        self.xlsx = self.root / "s.xlsx"
        workbook.save(self.xlsx)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bound_records_use_real_ozon_ids(self):
        from collector.seerfar_xlsx import import_xlsx
        from keyword_library import store as keyword_store

        summary = import_xlsx(self.xlsx, self.library, bindings_path=self.bindings_path)
        self.assertEqual(summary["bound_records"], 1)
        self.assertIn("17028922:91875", summary["categories"])
        self.assertTrue(any("套用真实 Ozon 类目" in item for item in summary["warnings"]))

        records = {record["keyword"]: record for record in keyword_store.load_all(self.library)}
        self.assertEqual(records["простынь на резинке"]["category_id"], "17028922")
        # 没绑定的类目仍用合成主键（并告警）
        self.assertTrue(records["наволочка"]["category_id"].startswith("seerfar-"))

    def test_without_bindings_still_works(self):
        from collector.seerfar_xlsx import import_xlsx

        summary = import_xlsx(self.xlsx, self.library)
        self.assertEqual(summary["bound_records"], 0)
        self.assertTrue(any("合成主键" in item for item in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
