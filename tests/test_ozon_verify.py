"""提交后核对（pipeline.ozon_verify）的测试：用夹具响应覆盖真实遇到过的几类问题。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline.ozon_verify import verify_submitted  # noqa: E402


class FixtureTransport:
    name = "fixture"

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        return self.response


def build_product(root: pathlib.Path, *, offers=("P1-S1", "P1-S2")) -> pathlib.Path:
    product = root / "products" / "P1"
    (product / "output").mkdir(parents=True)
    (product / "output" / "store-publications.json").write_text(
        json.dumps(
            {
                "stores": {
                    "default": {
                        "sku_publications": [
                            {"sku_id": f"S{index}", "offer_id": offer, "status": "imported"}
                            for index, offer in enumerate(offers, start=1)
                        ]
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (product / "output" / "ozon-attributes-final.json").write_text(
        json.dumps(
            {
                "common_attributes": [
                    {"attribute_id": 85, "attribute_name": "品牌", "value": "Нет бренда"},
                    {"attribute_id": 9048, "attribute_name": "型号名称", "value": "PS-200x200"},
                ],
                "attributes_by_sku": {
                    "S1": [{"attribute_id": 10096, "attribute_name": "商品颜色", "value": "белый"}],
                    "S2": [{"attribute_id": 10096, "attribute_name": "商品颜色", "value": "серый"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return product


def ozon_item(offer: str, *, sku: int = 111, model_id: int = 999, count: int = 2, color: str = "белый",
              brand: str = "Нет бренда", model_name: str = "PS-200x200",
              primary: str = "https://ir.ozone.ru/s3/multimedia-1/main.jpg") -> dict:
    return {
        "offer_id": offer,
        "id": 111111,
        "sku": sku,
        "primary_image": primary,
        "model_info": {"model_id": model_id, "count": count},
        "attributes": [
            {"id": 85, "values": [{"dictionary_value_id": 126745801, "value": brand}]},
            {"id": 9048, "values": [{"value": model_name}]},
            {"id": 10096, "values": [{"value": color}]},
        ],
    }


class VerifySubmittedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.product = build_product(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_good_passes(self):
        transport = FixtureTransport(
            {"result": [ozon_item("P1-S1", color="белый"), ozon_item("P1-S2", color="серый")]}
        )
        report = verify_submitted(self.product, store_id="default", transport=transport)
        self.assertTrue(report["ok"], report["failed"])
        self.assertFalse(report["api_writes_performed"])
        self.assertEqual(transport.calls[0][0], "/v4/product/info/attributes")
        self.assertEqual(transport.calls[0][1]["filter"]["offer_id"], ["P1-S1", "P1-S2"])

    def test_missing_attribute_value_fails(self):
        item = ozon_item("P1-S1", color="")
        item["attributes"] = [a for a in item["attributes"] if a["id"] != 10096]
        transport = FixtureTransport({"result": [item, ozon_item("P1-S2", color="серый")]})
        report = verify_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertIn("attribute[P1-S1][10096]", report["failed"])

    def test_unmerged_variants_fail(self):
        transport = FixtureTransport(
            {
                "result": [
                    ozon_item("P1-S1", color="белый", model_id=1, count=1),
                    ozon_item("P1-S2", color="серый", model_id=2, count=1),
                ]
            }
        )
        report = verify_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertIn("same_model_id", report["failed"])

    def test_missing_sku_fails(self):
        transport = FixtureTransport(
            {"result": [ozon_item("P1-S1", sku=0, color="белый"), ozon_item("P1-S2", color="серый")]}
        )
        report = verify_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertIn("sku_assigned[P1-S1]", report["failed"])

    def test_image_not_rehosted_fails(self):
        transport = FixtureTransport(
            {
                "result": [
                    ozon_item("P1-S1", color="белый", primary="https://our-bucket.cos.example.com/x.png"),
                    ozon_item("P1-S2", color="серый"),
                ]
            }
        )
        report = verify_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertIn("image_rehosted[P1-S1]", report["failed"])

    def test_offer_not_found_fails(self):
        transport = FixtureTransport({"result": [ozon_item("P1-S2", color="серый")]})
        report = verify_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertIn("offers_readable", report["failed"])

    def test_missing_ledger_reports_clearly(self):
        empty = self.root / "products" / "EMPTY"
        empty.mkdir(parents=True)
        report = verify_submitted(empty, store_id="default", transport=FixtureTransport({"result": []}))
        self.assertFalse(report["ok"])
        self.assertIn("台账", report["error"])


if __name__ == "__main__":
    unittest.main()
