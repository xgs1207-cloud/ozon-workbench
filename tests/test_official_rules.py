"""Ozon 官方规则校验测试：联系方式/外链/价格促销/大写/绝对化/emoji/长度上限 与严重度分级。"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rules.validate import (  # noqa: E402
    DESCRIPTION_NEAR_LIMIT,
    OFFICIAL_MAX_DESCRIPTION,
    OFFICIAL_MAX_TITLE,
    RECOMMENDED_TITLE,
    main as validate_main,
    official_copy_checks,
    validate_copy_bundle,
)

GOOD_TITLE = "Термос из нержавеющей стали 500 мл с двойными стенками"
GOOD_DESCRIPTION = (
    "Термос сохраняет температуру напитка до 12 часов благодаря двойным стенкам из нержавеющей стали. "
    "Подходит для чая, кофе и воды в дороге, на работе и на прогулке."
)


def bundle(**overrides) -> dict:
    payload = {
        "title_ru": GOOD_TITLE,
        "description_ru": GOOD_DESCRIPTION,
        "description_sections": {
            "product_value": "Держит тепло до 12 часов",
            "usage_scenarios": "Дорога, работа, прогулки",
            "core_advantages": "Двойные стенки, сталь 316",
            "usage_method": "Промыть перед первым использованием",
            "notices": "Не мыть в посудомоечной машине",
        },
        "hashtags": ["#термос", "#посуда"],
    }
    payload.update(overrides)
    return payload


class BlockingRuleTests(unittest.TestCase):
    """必拒审的东西必须被拦下。"""

    def test_clean_copy_has_no_blockers(self):
        report = official_copy_checks(bundle())
        self.assertEqual(report["blocking"], [], report)

    def test_phone_number_blocks(self):
        report = official_copy_checks(bundle(title_ru=f"{GOOD_TITLE} +7 999 123-45-67"))
        self.assertTrue(any("联系方式" in item for item in report["blocking"]), report)

    def test_email_and_url_block(self):
        for text in ("zakaz@example.com", "https://shop.example.com", "www.example.com", "t.me/myshop"):
            with self.subTest(text=text):
                report = official_copy_checks(bundle(description_ru=f"{GOOD_DESCRIPTION} {text}"))
                self.assertTrue(any("联系方式" in item for item in report["blocking"]), report)

    def test_social_and_messenger_words_block(self):
        report = official_copy_checks(bundle(description_ru=f"{GOOD_DESCRIPTION} пишите в WhatsApp"))
        self.assertTrue(any("联系方式" in item for item in report["blocking"]), report)

    def test_price_and_promo_block(self):
        for text in ("цена 1500 руб", "скидка 20%", "акция действует", "распродажа", "промокод"):
            with self.subTest(text=text):
                report = official_copy_checks(bundle(description_ru=f"{GOOD_DESCRIPTION} {text}"))
                self.assertTrue(any("价格/促销" in item for item in report["blocking"]), report)

    def test_ruble_symbol_blocks(self):
        report = official_copy_checks(bundle(title_ru=f"{GOOD_TITLE} 1200₽"))
        self.assertTrue(any("价格/促销" in item for item in report["blocking"]), report)

    def test_title_over_official_limit_blocks(self):
        report = official_copy_checks(bundle(title_ru="Термос " * 50))
        self.assertTrue(any(str(OFFICIAL_MAX_TITLE) in item for item in report["blocking"]), report)

    def test_description_over_official_limit_blocks(self):
        report = official_copy_checks(bundle(description_ru="Очень удобный термос. " * 400))
        self.assertTrue(any(str(OFFICIAL_MAX_DESCRIPTION) in item for item in report["blocking"]), report)


class AdvisoryRuleTests(unittest.TestCase):
    """不违反 Ozon 但值得提醒的，不能误判成阻断。"""

    def test_long_but_legal_title_is_advisory_only(self):
        title = GOOD_TITLE + " для горячих и холодных напитков в дороге и на работе, отличный подарок"
        self.assertGreater(len(title), RECOMMENDED_TITLE)
        self.assertLessEqual(len(title), OFFICIAL_MAX_TITLE)
        report = official_copy_checks(bundle(title_ru=title))
        self.assertEqual(report["blocking"], [], report)
        self.assertTrue(any("推荐" in item for item in report["advisory"]), report)

    def test_caps_words_are_advisory(self):
        report = official_copy_checks(bundle(title_ru=f"{GOOD_TITLE} ТЕРМОС"))
        self.assertEqual(report["blocking"], [], report)
        self.assertTrue(any("全大写" in item for item in report["advisory"]), report)

    def test_superlative_is_advisory(self):
        report = official_copy_checks(bundle(title_ru=f"{GOOD_TITLE} лучший выбор"))
        self.assertEqual(report["blocking"], [], report)
        self.assertTrue(any("绝对化" in item for item in report["advisory"]), report)

    def test_emoji_and_punctuation_are_advisory(self):
        report = official_copy_checks(bundle(title_ru=f"{GOOD_TITLE} 🔥!!!", description_ru=GOOD_DESCRIPTION + " Супер!!!"))
        self.assertEqual(report["blocking"], [], report)
        joined = " ".join(report["advisory"])
        self.assertIn("emoji", joined)
        self.assertIn("连续标点", joined)

    def test_description_near_limit_is_advisory(self):
        text = "Термос " * ((DESCRIPTION_NEAR_LIMIT // 7) + 10)
        report = official_copy_checks(bundle(description_ru=text))
        self.assertEqual(report["blocking"], [], report)
        self.assertTrue(any("接近上限" in item for item in report["advisory"]), report)


class BundleIntegrationTests(unittest.TestCase):
    def test_validate_copy_bundle_includes_official_blockers(self):
        problems = validate_copy_bundle(bundle(description_ru=f"{GOOD_DESCRIPTION} пишите на zakaz@example.com"))
        self.assertTrue(any("联系方式" in item for item in problems), problems)

    def test_validate_copy_bundle_does_not_block_on_advisory(self):
        title = GOOD_TITLE + " для горячих и холодных напитков в дороге и на работе, отличный подарок"
        problems = validate_copy_bundle(bundle(title_ru=title))
        self.assertEqual([item for item in problems if "推荐" in item], [], problems)

    def test_good_bundle_passes_existing_rules(self):
        self.assertEqual(validate_copy_bundle(bundle()), [])


class CliTests(unittest.TestCase):
    def test_cli_reports_blocking_and_advisory(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate_main(
                [
                    "--title",
                    f"{GOOD_TITLE} ТЕРМОС +7 999 123-45-67",
                    "--description",
                    f"{GOOD_DESCRIPTION} скидка 20%",
                    "--json",
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertFalse(payload["ok"])
        self.assertTrue(any("联系方式" in item for item in payload["blocking"]))
        self.assertTrue(any("价格/促销" in item for item in payload["blocking"]))
        self.assertTrue(any("全大写" in item for item in payload["advisory"]))

    def test_cli_clean_text_exits_zero(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate_main(["--title", GOOD_TITLE, "--description", GOOD_DESCRIPTION, "--json"])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertTrue(json.loads(buffer.getvalue())["ok"])


class CopyStepWiringTests(unittest.TestCase):
    """文案步骤要把官方"建议项"写进 warnings（不阻断），把阻断项转成 gate 失败。"""

    def setUp(self):
        import tempfile

        from collector.ingest import ingest_capture
        from pipeline.selection import set_selected_keywords

        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        summary = ingest_capture(
            root / "products",
            {
                "source_url": "https://detail.1688.com/offer/424242424.html",
                "title_zh": "316 保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [{"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0}],
            },
        )
        self.product_dir = root / "products" / summary["product_id"]
        set_selected_keywords(
            self.product_dir,
            ["термос 500 мл"],
            category={"category_id": "1001", "type_id": "2001"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _copy_bundle_with(self, **overrides) -> dict:
        return bundle(**overrides)

    def test_advisory_written_as_warning(self):
        from models.fake import FakeProvider
        from pipeline.handlers import run_single_step

        class AdvisoryProvider(FakeProvider):
            def write_copy_ru(self, request):  # type: ignore[override]
                result = super().write_copy_ru(request)
                result["copy_bundle"]["title_ru"] = result["copy_bundle"]["title_ru"] + " ТЕРМОС"
                return result

        run_single_step(self.product_dir, "product_analysis", provider=FakeProvider())
        result = run_single_step(self.product_dir, "russian_copy", provider=AdvisoryProvider())
        self.assertTrue(any("全大写" in item for item in result["warnings"]), result["warnings"])

    def test_blocking_copy_fails_gate(self):
        from models.fake import FakeProvider
        from pipeline.context import PipelineGateError
        from pipeline.handlers import run_single_step

        class ContactProvider(FakeProvider):
            def write_copy_ru(self, request):  # type: ignore[override]
                result = super().write_copy_ru(request)
                result["copy_bundle"]["description_ru"] = (
                    result["copy_bundle"]["description_ru"] + " Пишите на zakaz@example.com"
                )
                return result

        run_single_step(self.product_dir, "product_analysis", provider=FakeProvider())
        with self.assertRaises(PipelineGateError) as ctx:
            run_single_step(self.product_dir, "russian_copy", provider=ContactProvider())
        problems = " ".join(str(item) for item in ctx.exception.details.get("problems") or [])
        self.assertIn("联系方式", problems, ctx.exception.details)


if __name__ == "__main__":
    unittest.main()
