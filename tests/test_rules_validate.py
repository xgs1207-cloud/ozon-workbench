"""规则校验测试：对应 rules/*.md 里 [官方] 与 [项目] 的条目。"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rules.validate import (  # noqa: E402
    canonical_hashtag,
    normalize_capacity_text,
    normalize_russian_color_name,
    validate_color_name,
    validate_copy_bundle,
    validate_description_ru,
    validate_description_sections,
    validate_hashtags,
    validate_title_ru,
)


class HashtagTests(unittest.TestCase):
    def test_valid_tags_pass(self):
        tags = ["#термос", "#термосдлячая", "#посуда"]
        self.assertEqual(validate_hashtags(tags), [])

    def test_count_limit(self):
        problems = validate_hashtags([f"#тег{i}" for i in range(31)])
        self.assertTrue(any("超过上限 30" in item for item in problems))

    def test_latin_digits_and_uppercase_rejected(self):
        problems = validate_hashtags(["#termos", "#тег1", "#Термос"])
        joined = " ".join(problems)
        self.assertIn("西里尔字母", joined)
        self.assertIn("全小写", joined)

    def test_duplicates_are_case_insensitive(self):
        problems = validate_hashtags(["#термос", "#ТЕРМОС"])
        self.assertTrue(any("重复" in item for item in problems))

    def test_banned_and_weak_tags(self):
        problems = validate_hashtags(["#товар", "#хорошийвыбор", "#кухня"])
        joined = " ".join(problems)
        self.assertIn("通用凑数标签", joined)
        self.assertIn("弱标签", joined)

    def test_canonical_hashtag_multiwird(self):
        self.assertEqual(canonical_hashtag("Кружка для автомобиля"), "#кружкадляавтомобиля")

    def test_canonical_hashtag_rejects_latin_and_digits(self):
        self.assertIsNone(canonical_hashtag("black 1000 ml"))
        self.assertIsNone(canonical_hashtag("термос 500"))


class CapacityTests(unittest.TestCase):
    def test_liters_and_milliliters(self):
        self.assertEqual(normalize_capacity_text("12 л"), "12л")
        self.assertEqual(normalize_capacity_text("1.5 л"), "1500мл")
        self.assertEqual(normalize_capacity_text("500 ml"), "500мл")
        self.assertEqual(normalize_capacity_text("0,5 л"), "500мл")
        self.assertEqual(normalize_capacity_text("500мл"), "500мл")

    def test_unknown_returns_none(self):
        self.assertIsNone(normalize_capacity_text("примерно литр"))
        self.assertIsNone(normalize_capacity_text(""))


class ColorTests(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(normalize_russian_color_name("卡其"), "хаки")
        self.assertEqual(normalize_russian_color_name("透明"), "прозрачный")
        self.assertEqual(normalize_russian_color_name("black"), "черный")  # ё 归一为 е
        self.assertIsNone(normalize_russian_color_name("непонятно"))

    def test_color_field_rejects_capacity_and_latin(self):
        self.assertTrue(validate_color_name("601-800 мл"))
        self.assertTrue(validate_color_name("black 1000 ml"))
        self.assertEqual(validate_color_name("хаки"), [])
        self.assertEqual(validate_color_name("черный"), [])


class TitleAndDescriptionTests(unittest.TestCase):
    def test_title_rules(self):
        self.assertTrue(validate_title_ru("коротко"))
        self.assertTrue(any("中文" in item for item in validate_title_ru("Термос 不锈钢保温杯 500 мл")))
        self.assertTrue(any("重复" in item for item in validate_title_ru("термос, термос для чая 500 мл")))
        self.assertEqual(validate_title_ru("Термос из нержавеющей стали 500 мл для чая"), [])

    def test_description_rules(self):
        self.assertTrue(validate_description_ru("<p>HTML</p>" + "х" * 90))
        self.assertTrue(validate_description_ru("中文" * 50))
        self.assertEqual(validate_description_ru("Термос из нержавеющей стали. " * 5), [])
        self.assertTrue(validate_description_sections({"product_value": "коротко"}))
        self.assertEqual(
            validate_description_sections(
                {
                    "product_value": "Достаточно длинный текст про ценность.",
                    "usage_scenarios": "Достаточно длинный текст про сценарии.",
                    "core_advantages": "Достаточно длинный текст про плюсы.",
                    "usage_method": "Достаточно длинный текст про применение.",
                    "notices": "Достаточно длинный текст про уход.",
                }
            ),
            [],
        )

    def test_bundle(self):
        bundle = {
            "title_ru": "Термос из нержавеющей стали 500 мл для чая и кофе",
            "description_ru": "Термос из нержавеющей стали. " * 6,
            "description_sections": {
                "product_value": "Достаточно длинный текст про ценность.",
                "usage_scenarios": "Достаточно длинный текст про сценарии.",
                "core_advantages": "Достаточно длинный текст про плюсы.",
                "usage_method": "Достаточно длинный текст про применение.",
                "notices": "Достаточно длинный текст про уход.",
            },
            "hashtags": ["#термос", "#термосдлячая"],
        }
        self.assertEqual(validate_copy_bundle(bundle), [])


if __name__ == "__main__":
    unittest.main()
