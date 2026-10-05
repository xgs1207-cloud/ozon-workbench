"""选品清单测试：候选筛选、链接编码、中文找货词（类目兜底 / 模型翻译）、产物写出。"""

from __future__ import annotations

import csv
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.sourcing import (  # noqa: E402
    alibaba_search_url,
    build_plan,
    build_sourcing_plan,
    ozon_search_url,
    pick_keywords,
    render_markdown,
    translate_terms,
    write_plan,
)
from keyword_library import store as keyword_store  # noqa: E402
from models import ModelError  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.http_provider import HttpModelProvider  # noqa: E402

CATEGORY_ID = "1001"
TYPE_ID = "2001"


def seed_library(root: pathlib.Path) -> None:
    keyword_store.upsert(
        root,
        [
            {
                "keyword": "простынь на резинке 160х200",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 110906,
                "competitor_count": 206,
                "extra": {"category_name_zh": "床单", "category_name_ru": "Простыня"},
            },
            {
                "keyword": "простынь",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 88185,
                "competitor_count": 249,
                "extra": {"category_name_zh": "床单", "category_name_ru": "Простыня"},
            },
            {
                "keyword": "простынь евро 200х200",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 95000,
                "competitor_count": 120,
                "extra": {"category_name_zh": "床单", "category_name_ru": "Простыня"},
            },
            {
                "keyword": "наволочка 50х70",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "search_volume": 500,
                "competitor_count": 9000,
                "extra": {"category_name_zh": "枕套", "category_name_ru": "Наволочка"},
            },
            {
                "keyword": "простынь без данных",
                "category_id": CATEGORY_ID,
                "type_id": TYPE_ID,
                "extra": {"category_name_zh": "床单"},
            },
        ],
        source="seerfar",
    )


class UrlTests(unittest.TestCase):
    def test_urls_are_percent_encoded(self):
        url = ozon_search_url("простынь на резинке 160х200")
        self.assertTrue(url.startswith("https://www.ozon.ru/search/?text="))
        self.assertNotIn(" ", url)
        self.assertIn("%D0%BF", url)  # 西里尔字母被编码

    def test_alibaba_url_uses_chinese_term(self):
        url = alibaba_search_url("床单")
        self.assertTrue(url.startswith("https://s.1688.com/selloffer/offer_search.htm?keywords="))
        self.assertIn("%E5%BA%8A%E5%8D%95", url)

    def test_keyword_classification(self):
        from collector.sourcing import classify_keyword

        self.assertEqual(classify_keyword("простынь на резинке 160х200")[0], "generic")
        kind, note = classify_keyword("yerrna постельное белье")
        self.assertEqual(kind, "brand_or_latin")
        self.assertIn("品牌", note)
        self.assertEqual(classify_keyword("AmazonBasics®")[0], "brand_or_latin")


class PickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = pathlib.Path(self.tmp.name) / "keyword-library"
        seed_library(self.library)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_qualified_by_default_and_sorted(self):
        rows = pick_keywords(self.library, top_n=10)
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "qualified" for row in rows), [row["status"] for row in rows])
        scores = [row["score"] for row in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # 没有指标的那条既不打分也不达标，不该出现
        self.assertNotIn("простынь без данных", [row["keyword"] for row in rows])

    def test_top_n_and_min_score(self):
        self.assertEqual(len(pick_keywords(self.library, top_n=1)), 1)
        rows = pick_keywords(self.library, min_score=0.9)
        self.assertTrue(all(row["score"] >= 0.9 for row in rows))

    def test_statuses_filter_can_include_candidates(self):
        rows = pick_keywords(self.library, statuses=("candidate",))
        self.assertTrue(all(row["status"] == "candidate" for row in rows))
        self.assertIn("наволочка 50х70", [row["keyword"] for row in rows])

    def test_empty_statuses_means_no_filter(self):
        rows = pick_keywords(self.library, statuses=(), top_n=99)
        self.assertIn("простынь без данных", [row["keyword"] for row in rows])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = pathlib.Path(self.tmp.name) / "keyword-library"
        seed_library(self.library)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chinese_term_falls_back_to_seerfar_category(self):
        plan = build_sourcing_plan(self.library, top_n=2)
        self.assertEqual(plan["keywords"], 2)
        self.assertTrue(all(row["chinese_term"] == "床单" for row in plan["rows"]))
        self.assertTrue(all(row["chinese_term_source"] == "seerfar_category" for row in plan["rows"]))
        self.assertTrue(all(row["alibaba_search_url"] for row in plan["rows"]))

    def test_model_translation_wins_when_available(self):
        class Translator(FakeProvider):
            name = "translator"

            def translate_terms(self, keywords, context=None):
                return {str(key): "床单四件套" for key in keywords}

        plan = build_sourcing_plan(self.library, top_n=1, provider=Translator(), translate=True)
        row = plan["rows"][0]
        self.assertEqual(row["chinese_term"], "床单四件套")
        self.assertEqual(row["chinese_term_source"], "model")

    def test_missing_chinese_term_is_reported_not_invented(self):
        keyword_store.upsert(
            self.library,
            [
                {
                    "keyword": "халатик",
                    "category_id": CATEGORY_ID,
                    "type_id": TYPE_ID,
                    "search_volume": 99999,
                    "competitor_count": 1,
                }
            ],
            source="manual",
        )
        plan = build_sourcing_plan(self.library, top_n=99)
        rows = {row["keyword"]: row for row in plan["rows"]}
        missing = [row for row in plan["rows"] if row["chinese_term_source"] == "missing"]
        self.assertTrue(missing, rows)
        self.assertIsNone(missing[0]["alibaba_search_url"])

    def test_plan_ignores_unknown_translations(self):
        class NoisyTranslator(FakeProvider):
            def translate_terms(self, keywords, context=None):
                return {"неизвестное слово": "随便", str(keywords[0]): "床单"}

        terms, warnings = translate_terms(["простынь"], provider=NoisyTranslator())
        # 只保留被请求的键（未请求的键不进入结果）
        self.assertEqual(set(terms), {"простынь"})
        self.assertEqual(warnings, [])

    def test_provider_without_translation_is_reported(self):
        # fake 实现了接口但**不翻译**（返回空），所以要靠"没有翻译结果"的告警提示兜底
        terms, warnings = translate_terms(["простынь"], provider=FakeProvider())
        self.assertEqual(terms, {})
        self.assertTrue(any("没有翻译结果" in item for item in warnings))

        class NoHook:
            name = "no-hook"

        terms, warnings = translate_terms(["простынь"], provider=NoHook())
        self.assertEqual(terms, {})
        self.assertTrue(any("不支持翻译" in item for item in warnings))

        class Broken(FakeProvider):
            name = "broken"

            def translate_terms(self, keywords, context=None):
                raise ModelError("模型挂了")

        terms, warnings = translate_terms(["простынь"], provider=Broken())
        self.assertEqual(terms, {})
        self.assertTrue(any("翻译失败" in item for item in warnings))

    def test_markdown_flags_brand_like_keywords(self):
        keyword_store.upsert(
            self.library,
            [
                {
                    "keyword": "yerrna постельное белье",
                    "category_id": CATEGORY_ID,
                    "type_id": TYPE_ID,
                    "search_volume": 120000,
                    "competitor_count": 1,
                    "extra": {"category_name_zh": "床单"},
                }
            ],
            source="seerfar",
        )
        plan = build_sourcing_plan(self.library, top_n=99)
        self.assertGreaterEqual(plan["brand_like"], 1)
        markdown = render_markdown(plan)
        self.assertIn("疑似品牌词", markdown)
        self.assertIn("不要照抄品牌", markdown)

    def test_markdown_contains_links_and_usage(self):
        plan = build_sourcing_plan(self.library, top_n=2)
        markdown = render_markdown(plan)
        self.assertIn("选品清单", markdown)
        self.assertIn("ozon.ru/search", markdown)
        self.assertIn("s.1688.com", markdown)
        self.assertIn("怎么用", markdown)
        self.assertIn("простынь", markdown)

    def test_write_plan_outputs_json_markdown_and_csv(self):
        plan = build_sourcing_plan(self.library, top_n=2)
        self.assertEqual(plan["keywords"], 2)  # 默认只取达标词
        target = pathlib.Path(self.tmp.name) / "out"
        csv_path = pathlib.Path(self.tmp.name) / "plan.csv"
        written = write_plan(target, plan, limit=1, csv_path=csv_path)
        self.assertTrue(pathlib.Path(written["json"]).is_file())
        self.assertTrue(pathlib.Path(written["markdown"]).is_file())
        saved = json.loads(pathlib.Path(written["json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(saved["rows"]), 2)
        markdown = pathlib.Path(written["markdown"]).read_text(encoding="utf-8")
        self.assertEqual(markdown.count("| 1 |"), 1)
        self.assertNotIn("| 2 |", markdown)  # limit=1 只列一行
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)  # CSV 仍是全部
        self.assertIn("ozon_search_url", rows[0])

    def test_cli_json_mode(self):
        from collector.sourcing import main

        import io
        from contextlib import redirect_stdout

        out_dir = pathlib.Path(self.tmp.name) / "cli-out"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "--library",
                    str(self.library),
                    "--top",
                    "2",
                    "--out-dir",
                    str(out_dir),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["keywords"], 2)
        self.assertTrue(pathlib.Path(payload["written"]["markdown"]).is_file())


class HttpTranslationTests(unittest.TestCase):
    class Scripted:
        name = "scripted"

        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def complete(self, *, system, user, temperature=None):
            self.calls.append({"system": system, "user": user})
            return self.responses.pop(0)

    def test_http_provider_translates_and_validates_keys(self):
        transport = self.Scripted([json.dumps({"terms": {"простынь": "床单", "термос": "保温杯"}}, ensure_ascii=False)])
        provider = HttpModelProvider(transport)
        terms = provider.translate_terms(["простынь", "термос"], context={"category": "床单"})
        self.assertEqual(terms, {"простынь": "床单", "термос": "保温杯"})
        self.assertIn("простынь", transport.calls[0]["user"])

    def test_http_provider_repairs_unknown_keys(self):
        good = json.dumps({"terms": {"простынь": "床单"}}, ensure_ascii=False)
        bad = json.dumps({"terms": {"простынь": "床单", "лишний": "多余"}}, ensure_ascii=False)
        transport = self.Scripted([bad, good])
        provider = HttpModelProvider(transport, max_attempts=2)
        terms = provider.translate_terms(["простынь"])
        self.assertEqual(terms, {"простынь": "床单"})
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("上一次输出不合法", transport.calls[1]["user"])

    def test_empty_input_short_circuits(self):
        transport = self.Scripted([])
        provider = HttpModelProvider(transport)
        self.assertEqual(provider.translate_terms([]), {})
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
