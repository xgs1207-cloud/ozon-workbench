"""CSV 入库路径的单元测试（列名识别与数字解析最容易出错，先锁住）。"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

WORKBENCH = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKBENCH))

MODULE_PATH = WORKBENCH / "collector" / "import_csv.py"
_spec = importlib.util.spec_from_file_location("ozon_workbench_import_csv", MODULE_PATH)
import_csv = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(import_csv)


class ColumnDetectionTests(unittest.TestCase):
    def test_english_headers(self):
        mapping = import_csv.detect_columns(["Keyword", "Search Volume", "Competition", "CPC"])
        self.assertEqual(mapping["keyword"], 0)
        self.assertEqual(mapping["search_volume"], 1)
        self.assertEqual(mapping["competitor_count"], 2)
        self.assertEqual(mapping["cpc"], 3)

    def test_russian_headers(self):
        mapping = import_csv.detect_columns(["Запрос", "Показы", "Конкуренты"])
        self.assertEqual(mapping["keyword"], 0)
        self.assertEqual(mapping["search_volume"], 1)
        self.assertEqual(mapping["competitor_count"], 2)

    def test_chinese_headers(self):
        mapping = import_csv.detect_columns(["关键词", "搜索量", "竞品数", "类目"])
        self.assertEqual(mapping["keyword"], 0)
        self.assertEqual(mapping["search_volume"], 1)
        self.assertEqual(mapping["competitor_count"], 2)
        self.assertEqual(mapping["category_path_zh"], 3)

    def test_unknown_headers_are_skipped_not_guessed(self):
        mapping = import_csv.detect_columns(["Rank", "Something Else"])
        self.assertNotIn("keyword", mapping)
        self.assertNotIn("search_volume", mapping)


class NumberParsingTests(unittest.TestCase):
    def test_thousands_separator_and_spaces(self):
        self.assertEqual(import_csv.to_number("12,400"), 12400.0)
        self.assertEqual(import_csv.to_number("12 400"), 12400.0)
        self.assertEqual(import_csv.to_number("1\u00a0234"), 1234.0)

    def test_blank_and_garbage_become_none(self):
        self.assertIsNone(import_csv.to_number(""))
        self.assertIsNone(import_csv.to_number(None))
        self.assertIsNone(import_csv.to_number("—"))


if __name__ == "__main__":
    unittest.main()
