import json
import tempfile
import unittest
from pathlib import Path

from collector import product_queries as pq

FIXTURE_DIR = Path(__file__).resolve().parents[0]


class ScriptedTransport:
    """模拟分页：每页 page_size 条，返回夹具里的 items 切片。"""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def post(self, path, body):
        self.calls.append({"path": path, "body": dict(body)})
        all_items = self.raw["items"]
        size = int(body["page_size"])
        page = int(body["page"])
        start = (page - 1) * size
        chunk = all_items[start:start + size]
        page_count = max(1, (len(all_items) + size - 1) // size)
        return {
            "analytics_period": self.raw.get("analytics_period", {}),
            "total": len(all_items),
            "page_count": page_count,
            "items": chunk,
        }


def load_raw():
    return json.loads(
        (FIXTURE_DIR.parents[0] / "contracts" / "fixtures" / "product-queries.json")
        .read_text(encoding="utf-8")
    )


class ProductQueriesTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_raw()
        self.transport = ScriptedTransport(self.raw)

    def test_fetch_all_normalizes_every_item(self):
        result = pq.fetch_all(
            self.transport,
            skus=["5956082914", "5956083009"],
            date_from="2026-09-03T00:00:00Z",
            date_to="2026-10-03T23:59:59Z",
        )
        self.assertEqual(result["total"], 2)
        first = result["items"][0]
        self.assertEqual(first["sku"], 5956082914)
        self.assertEqual(first["unique_search_users"], 320)
        self.assertEqual(first["position"], 12.4)
        self.assertEqual(first["view_conversion"], 9.2)
        self.assertEqual(first["gmv"], 18450)
        # 请求体带了正确路径与 SKU
        self.assertEqual(self.transport.calls[0]["path"], pq.PATH_PRODUCT_QUERIES)
        self.assertIn("5956082914", self.transport.calls[0]["body"]["skus"])

    def test_pagination_walks_pages(self):
        result = pq.fetch_all(
            self.transport,
            skus=["5956082914", "5956083009"],
            date_from="x",
            page_size=1,
        )
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual([c["body"]["page"] for c in self.transport.calls], [1, 2])

    def test_premium_fields_become_none_when_missing(self):
        raw = {"items": [{
            "sku": 1, "offer_id": "a", "name": "n", "category": "c",
            "unique_search_users": 10, "gmv": 0
        }]}
        transport = ScriptedTransport(raw)
        result = pq.fetch_all(transport, skus=["1"], date_from="x")
        item = result["items"][0]
        self.assertIsNone(item["position"])
        self.assertIsNone(item["unique_view_users"])
        self.assertIsNone(item["view_conversion"])

    def test_empty_skus_raise(self):
        with self.assertRaises(pq.ProductQueriesError):
            pq.fetch_all(self.transport, skus=[], date_from="x")

    def test_collect_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "P000006"
            (product / "output").mkdir(parents=True)
            report = pq.collect(
                product, self.transport, skus=["5956082914", "5956083009"]
            )
            written = json.loads(
                (product / "output" / "product-queries.json").read_text(encoding="utf-8")
            )
            self.assertEqual(written["product_id"], "P000006")
            self.assertEqual(len(written["items"]), 2)
            self.assertEqual(report["total"], 2)
            self.assertIsNotNone(pq.read_report(product))

    def test_collect_without_skus_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pq.ProductQueriesError):
                pq.collect(tmp, self.transport)

    def test_enumerate_ozon_skus_from_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "P1"
            (product / "output").mkdir(parents=True)
            (product / "output" / "publications.json").write_text(
                json.dumps({"results": [{"sku": 5956082914}, {"sku": 5956083009}]}),
                encoding="utf-8",
            )
            self.assertEqual(
                pq.enumerate_ozon_skus(product),
                ["5956082914", "5956083009"],
            )


if __name__ == "__main__":
    unittest.main()
