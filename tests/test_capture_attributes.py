"""1688 详情页属性（材质/包装数量/认证）打通测试：采集 → 入库 → facts 补全。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from collector.push_capture import build_capture_payload  # noqa: E402
from models.base import AnalysisRequest  # noqa: E402
from models.http_provider import enrich_facts_from_inputs  # noqa: E402


def capture_payload(**extra) -> dict:
    payload = {
        "source_url": "https://detail.1688.com/offer/939393939.html",
        "title_zh": "纯棉床单 200x200",
        "category": {"category_id": "17028731", "type_id": "92612"},
        "skus": [{"sku_id": "S1", "color_ru": "белый", "purchase_price_cny": 42.0}],
        "attributes_zh": {
            "材质": "100% 棉",
            "包装数量": "1",
            "认证": "OEKO-TEX、SGS",
            "克重": "120g",
            "品牌": "无",
            "适用季节": "四季",
        },
    }
    payload.update(extra)
    return payload


class IngestAttributesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, **extra) -> pathlib.Path:
        summary = ingest_capture(self.products, capture_payload(**extra))
        return self.products / summary["product_id"]

    def test_attributes_land_in_source_json(self):
        product = self.ingest()
        source = json.loads((product / "input" / "source.json").read_text(encoding="utf-8"))
        attributes = source.get("attributes_zh") or {}
        self.assertEqual(attributes.get("material"), "100% 棉")
        self.assertEqual(attributes.get("package_quantity"), 1)  # 字符串被规整成整数
        self.assertEqual(attributes.get("certifications"), ["OEKO-TEX", "SGS"])  # 顿号拆成数组
        self.assertEqual(attributes.get("brand"), "无")
        self.assertEqual(attributes.get("raw", {}).get("克重"), "120g")  # 原始属性表保留

    def test_explicit_fields_also_accepted(self):
        product = self.ingest(attributes_zh={}, material_zh="聚酯纤维", package_quantity=2)
        source = json.loads((product / "input" / "source.json").read_text(encoding="utf-8"))
        attributes = source.get("attributes_zh") or {}
        self.assertEqual(attributes.get("material"), "聚酯纤维")
        self.assertEqual(attributes.get("package_quantity"), 2)

    def test_product_without_attributes_still_works(self):
        product = self.ingest(attributes_zh={})
        source = json.loads((product / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertNotIn("attributes_zh", source)  # 没有属性就不写这个键


class PushCapturePassthroughTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.folder = self.root / "capture"
        for name in ("main-images", "sku-images", "detail-images"):
            directory = self.folder / name
            directory.mkdir(parents=True)
            (directory / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())

    def tearDown(self):
        self.tmp.cleanup()

    def test_attributes_are_sent_to_the_server(self):
        (self.folder / "product.json").write_text(
            json.dumps(
                {
                    "source_url": "https://detail.1688.com/offer/949494949.html",
                    "title_zh": "纯棉床单",
                    "skus": [{"sku_id": "S1", "purchase_price_cny": 42.0}],
                    "attributes_zh": {"材质": "棉", "包装数量": "1"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        payload = build_capture_payload(self.folder)
        payload.pop("_stats", None)
        self.assertEqual(payload["attributes_zh"], {"材质": "棉", "包装数量": "1"})


class FactsFromAttributesTests(unittest.TestCase):
    def test_facts_filled_from_capture_attributes(self):
        payload = {
            "facts": {
                "title_cn": "纯棉床单",
                "category_cn": None,
                "brand": None,
                "materials": [],
                "dimensions": "unknown",
                "weight": "unknown",
                "load_capacity": "unknown",
                "certifications": [],
                "functions": [],
                "package_quantity": "unknown",
                "accessories": [],
                "skus": [],
            }
        }
        request = AnalysisRequest(
            product_id="P000007",
            product_dir=None,
            source={
                "title_zh": "纯棉床单",
                "attributes_zh": {
                    "material": "100% 棉",
                    "package_quantity": 1,
                    "certifications": ["OEKO-TEX"],
                },
            },
            selected_keywords=[],
        )
        notes = enrich_facts_from_inputs(payload, request)
        facts = payload["facts"]
        self.assertEqual(facts["materials"], ["100% 棉"])
        self.assertEqual(facts["package_quantity"]["value"], 1)
        self.assertEqual(facts["certifications"], ["OEKO-TEX"])
        self.assertTrue(any("采集属性" in item for item in notes), notes)


if __name__ == "__main__":
    unittest.main()
