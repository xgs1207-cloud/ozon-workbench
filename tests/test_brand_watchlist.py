"""品牌观察名单测试：俄文品牌名也要被识别成疑似品牌词（真机踩到 шуйские ситцы / озон хоум）。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.sourcing import (  # noqa: E402
    DEFAULT_BRAND_WATCHLIST,
    brand_in_text,
    classify_keyword,
    load_brand_watchlist,
)


class BrandWatchlistTests(unittest.TestCase):
    def test_cyrillic_brand_is_flagged(self):
        """真机实测：`шуйские ситцы` 与 `озон хоум` 都是品牌，但没有任何拉丁字母。"""
        for keyword in ("шуйские ситцы", "Шуйские Ситцы", "озон хоум постельное белье"):
            kind, note = classify_keyword(keyword)
            self.assertEqual(kind, "brand_or_latin", keyword)
            self.assertIn("品牌观察名单", note or "", keyword)

    def test_generic_keyword_is_not_flagged(self):
        kind, note = classify_keyword("простыня 200х200")
        self.assertEqual(kind, "generic")
        self.assertIsNone(note)

    def test_word_boundary_avoids_false_positives(self):
        """`тва` 不该命中 `тварь`（按词边界匹配）。"""
        self.assertIsNone(brand_in_text("тварь", DEFAULT_BRAND_WATCHLIST))
        self.assertEqual(brand_in_text("тва постельное", DEFAULT_BRAND_WATCHLIST), "тва")

    def test_custom_watchlist_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "brand-watchlist.txt").write_text("# 注释\n我的牌子\n", encoding="utf-8")
            watchlist = load_brand_watchlist(root)
            self.assertEqual(watchlist, ("我的牌子",))
            # 自定义名单生效：这个俄文词之前不会被拉丁规则抓到
            kind, note = classify_keyword("我的牌子 товар", watchlist)
            self.assertEqual(kind, "brand_or_latin")
            self.assertIn("品牌观察名单", note or "")

    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = load_brand_watchlist(pathlib.Path(tmp))
        self.assertEqual(watchlist, DEFAULT_BRAND_WATCHLIST)

    def test_explicit_watchlist_overrides_default(self):
        kind, note = classify_keyword("abc brand товар", watchlist=("некийбренд",))
        # 没命中自定义名单 → 退回拉丁规则
        self.assertEqual(kind, "brand_or_latin")
        self.assertIn("拉丁字母", note or "")


if __name__ == "__main__":
    unittest.main()
