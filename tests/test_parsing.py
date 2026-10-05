"""数字解析规则的单元测试（歧义写法是采集数据里最容易出错的地方）。"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from parsing import is_positive_int, parse_number  # noqa: E402


class ParseNumberTests(unittest.TestCase):
    def test_plain_numbers(self):
        self.assertEqual(parse_number(12), 12.0)
        self.assertEqual(parse_number(12.5), 12.5)
        self.assertEqual(parse_number("12.5"), 12.5)

    def test_thousands_separators(self):
        self.assertEqual(parse_number("1,234"), 1234.0)
        self.assertEqual(parse_number("1,234.5"), 1234.5)
        self.assertEqual(parse_number("1 234 567"), 1234567.0)
        self.assertEqual(parse_number("1\u00a0234"), 1234.0)

    def test_decimal_comma(self):
        self.assertEqual(parse_number("19,0"), 19.0)
        self.assertEqual(parse_number("1 234,5"), 1234.5)
        self.assertEqual(parse_number("1.234,56"), 1234.56)
        self.assertEqual(parse_number("0,99"), 0.99)

    def test_units_and_currency_are_stripped(self):
        self.assertEqual(parse_number("12,400 ₽"), 12400.0)
        self.assertEqual(parse_number("¥18.50"), 18.5)
        self.assertEqual(parse_number("500 мл"), 500.0)

    def test_garbage_returns_none(self):
        for value in ("", None, "—", "-", "мл", True, False):
            with self.subTest(value=value):
                self.assertIsNone(parse_number(value))


class PositiveIntTests(unittest.TestCase):
    def test_positive_ints(self):
        self.assertTrue(is_positive_int("123456"))
        self.assertTrue(is_positive_int(789))
        self.assertTrue(is_positive_int("1,234"))

    def test_rejects_non_positive_or_fractional(self):
        for value in (0, "-5", "12.5", "abc", None):
            with self.subTest(value=value):
                self.assertFalse(is_positive_int(value))


if __name__ == "__main__":
    unittest.main()
