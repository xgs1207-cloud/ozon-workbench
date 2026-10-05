"""采集清单与"关键词随采集入库"测试（全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.collection_plan import (  # noqa: E402
    STATUS_COLLECTED,
    STATUS_PENDING,
    build_collection_plan,
    mark_keyword,
    render_collection_markdown,
    scan_products,
    write_collection_plan,
)
from collector.ingest import ingest_capture  # noqa: E402
from collector.sourcing import build_plan, ozon_search_url  # noqa: E402
from pipeline.doctor import product_keywords, render_report, run_doctor  # noqa: E402

CATEGORY_ID = "17028922"
TYPE_ID = "91875"


def sourcing_rows(keywords=None) -> list[dict]:
    rows = []
    for text in keywords or ["простынь на резинке 160х200", "наволочка 50х70"]:
        rows.append(
            {
                "keyword": text,
                "keyword_kind": "generic",
                "chinese_term": "床单",
                "chinese_term_source": "seerfar_category",
                "score": 0.9,
                "search_volume": 110906,
                "competitor_count": 206,
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "category_name_ru": "Простыни",
                "ozon_search_url": ozon_search_url(text),
                "alibaba_search_url": "https://s.1688.com/selloffer/offer_search.htm?keywords=%E5%BA%8A%E5%8D%95",
            }
        )
    return rows


def capture_payload(keywords=None, category=True) -> dict:
    payload = {
        "source_url": "https://detail.1688.com/offer/717171717.html",
        "title_zh": "纯棉床单",
        "skus": [{"sku_id": "S1", "purchase_price_cny": 18.0}],
    }
    if category:
        payload["category"] = {"category_id": CATEGORY_ID, "type_id": TYPE_ID}
    if keywords:
        payload["keywords"] = keywords
        payload["keyword_source"] = "collection_plan"
    return payload


class CollectionPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.products.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_pending_without_products(self):
        plan = build_collection_plan(sourcing_rows(), products_root=self.products)
        self.assertEqual(plan["total"], 2)
        self.assertEqual(plan["pending"], 2)
        self.assertEqual(plan["collected"], 0)
        self.assertTrue(all(item["status"] == STATUS_PENDING for item in plan["tasks"]))

    def test_collected_keyword_is_detected_from_product(self):
        ingest_capture(
            self.products,
            capture_payload(keywords=["простынь на резинке 160х200"]),
        )
        plan = build_collection_plan(sourcing_rows(), products_root=self.products)
        by_keyword = {item["keyword"]: item for item in plan["tasks"]}
        collected = by_keyword["простынь на резинке 160х200"]
        self.assertEqual(collected["status"], STATUS_COLLECTED)
        self.assertEqual(len(collected["products"]), 1)
        self.assertEqual(collected["products"][0]["product_id"], "P000001")
        self.assertEqual(by_keyword["наволочка 50х70"]["status"], STATUS_PENDING)
        self.assertEqual(plan["collected"], 1)
        self.assertEqual(plan["pending"], 1)

    def test_top_and_only_filters(self):
        plan = build_collection_plan(sourcing_rows(), products_root=self.products, top=1)
        self.assertEqual(plan["total"], 1)
        plan = build_collection_plan(
            sourcing_rows(), products_root=self.products, only=["наволочка 50х70"]
        )
        self.assertEqual(plan["total"], 1)
        self.assertEqual(plan["tasks"][0]["keyword"], "наволочка 50х70")

    def test_scan_products_reads_selected_keywords_too(self):
        summary = ingest_capture(self.products, capture_payload())
        product_dir = self.products / summary["product_id"]
        mark_keyword(product_dir, "наволочка 50х70")
        index = scan_products(self.products)
        self.assertIn("наволочка 50х70", index)
        self.assertEqual(index["наволочка 50х70"][0]["product_id"], product_dir.name)

    def test_mark_keyword_writes_both_files(self):
        summary = ingest_capture(self.products, capture_payload())
        product_dir = self.products / summary["product_id"]
        result = mark_keyword(product_dir, "простынь на резинке 160х200")
        self.assertTrue(result["ok"])
        source = json.loads((product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        selection = json.loads(
            (product_dir / "input" / "selected-keywords.json").read_text(encoding="utf-8")
        )
        self.assertEqual(source["keywords"][0]["keyword"], "простынь на резинке 160х200")
        self.assertEqual(selection["keywords"][0]["keyword"], "простынь на резинке 160х200")
        self.assertEqual(selection["source"], "manual_mark")
        # 重复标记不产生重复项
        mark_keyword(product_dir, "простынь на резинке 160х200")
        source = json.loads((product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(len(source["keywords"]), 1)

    def test_mark_rejects_short_keyword(self):
        summary = ingest_capture(self.products, capture_payload())
        with self.assertRaises(ValueError):
            mark_keyword(self.products / summary["product_id"], "x")

    def test_markdown_and_files(self):
        ingest_capture(self.products, capture_payload(keywords=["простынь на резинке 160х200"]))
        plan = build_collection_plan(sourcing_rows(), products_root=self.products)
        markdown = render_collection_markdown(plan)
        self.assertIn("采集清单", markdown)
        self.assertIn("✅ 已采集", markdown)
        self.assertIn("⬜ 待采集", markdown)
        self.assertIn("P000001", markdown)
        self.assertIn(CATEGORY_ID, markdown)
        written = write_collection_plan(self.root, plan)
        self.assertTrue(pathlib.Path(written["json"]).is_file())
        self.assertTrue(pathlib.Path(written["markdown"]).is_file())
        saved = json.loads(pathlib.Path(written["json"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["total"], 2)

    def test_cli_json_mode_from_library(self):
        from collector.collection_plan import main

        import io
        from os import environ
        from contextlib import redirect_stdout

        # 直接用 --plan 路径（避免依赖关键词库）
        plan_path = self.root / "sourcing-plan.json"
        plan_path.write_text(json.dumps({"rows": sourcing_rows()}, ensure_ascii=False), encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--plan",
                    str(plan_path),
                    "--products",
                    str(self.products),
                    "--out-dir",
                    str(self.root),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["pending"], 2)

    def test_cli_reports_missing_plan(self):
        from collector.collection_plan import main

        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--plan", str(self.root / "nope.json")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


class KeywordCarryTests(unittest.TestCase):
    """采集入库时带上关键词（来自选品清单）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.products = pathlib.Path(self.tmp.name) / "products"

    def tearDown(self):
        self.tmp.cleanup()

    def test_keywords_are_written_into_source_and_selection(self):
        summary = ingest_capture(
            self.products,
            capture_payload(keywords=["простынь на резинке 160х200", "наволочка 50х70"]),
        )
        product_dir = self.products / summary["product_id"]
        source = json.loads((product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(len(source["keywords"]), 2)
        self.assertEqual(source["keyword_source"], "collection_plan")
        selection = json.loads(
            (product_dir / "input" / "selected-keywords.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(selection["keywords"]), 2)
        self.assertEqual(selection["source"], "collection_plan")
        self.assertTrue(any("已带上 2 个关键词" in item for item in summary["warnings"]))
        self.assertEqual(product_keywords(product_dir), ["простынь на резинке 160х200", "наволочка 50х70"])

    def test_keyword_category_is_used_when_category_missing(self):
        payload = capture_payload(keywords=["простынь"], category=False)
        payload["keyword_category"] = {"category_id": CATEGORY_ID, "type_id": TYPE_ID}
        summary = ingest_capture(self.products, payload)
        source = json.loads(
            (self.products / summary["product_id"] / "input" / "source.json").read_text(encoding="utf-8")
        )
        self.assertEqual(source["selected_category"]["category_id"], CATEGORY_ID)
        self.assertEqual(source["keyword_category"]["type_id"], TYPE_ID)

    def test_string_keywords_are_accepted(self):
        summary = ingest_capture(self.products, capture_payload(keywords="простынь"))
        source = json.loads(
            (self.products / summary["product_id"] / "input" / "source.json").read_text(encoding="utf-8")
        )
        self.assertEqual(source["keywords"][0]["keyword"], "простынь")

    def test_capture_without_keywords_is_unchanged(self):
        summary = ingest_capture(self.products, capture_payload())
        source = json.loads(
            (self.products / summary["product_id"] / "input" / "source.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("keywords", source)
        self.assertFalse((self.products / summary["product_id"] / "input" / "selected-keywords.json").exists())


class DoctorKeywordRollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"

    def tearDown(self):
        self.tmp.cleanup()

    def test_doctor_lists_keyword_to_product_mapping(self):
        ingest_capture(self.products, capture_payload(keywords=["простынь на резинке 160х200"]))
        ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/818181818.html",
                "title_zh": "另一条床单",
                "category": {"category_id": CATEGORY_ID, "type_id": TYPE_ID},
                "skus": [{"sku_id": "S1", "purchase_price_cny": 20.0}],
                "keywords": ["простынь на резинке 160х200"],
            },
        )
        report = run_doctor(self.products, registry_path=self.root / "config" / "shops.json", env={})
        self.assertEqual(report["summary"]["keywords_with_products"], 1)
        rows = report["keywords"]["простынь на резинке 160х200"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["product_id"] for row in rows}, {"P000001", "P000002"})
        text = render_report(report)
        self.assertIn("关键词 → 商品", text)
        self.assertIn("простынь на резинке 160х200", text)


if __name__ == "__main__":
    unittest.main()
