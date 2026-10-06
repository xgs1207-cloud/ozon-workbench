import json
import tempfile
import unittest
from pathlib import Path

from collector import search_phrases as sp

FIXTURE_CSV = (
    Path(__file__).resolve().parents[1]
    / "contracts" / "fixtures" / "search-phrases.csv"
)


def rows():
    return sp.parse_csv_text(FIXTURE_CSV.read_text(encoding="utf-8"))


class SearchPhrasesParseTests(unittest.TestCase):
    def test_parse_maps_headers_and_numbers(self):
        data = rows()
        self.assertEqual(len(data), 4)
        first = data[0]
        self.assertEqual(first["phrase"], "термос 500 мл")
        self.assertEqual(first["impressions"], 1000)
        self.assertEqual(first["clicks"], 40)
        self.assertEqual(first["spend"], 1200)
        self.assertEqual(first["orders"], 4)
        self.assertEqual(first["orders_revenue"], 6000)
        self.assertEqual(first["carts"], 12)

    def test_classify_winners_negatives_neutral(self):
        groups = sp.classify(rows())
        self.assertEqual(groups["winners"][0]["phrase"], "термос 500 мл")
        self.assertEqual(len(groups["winners"]), 2)
        self.assertEqual(groups["negatives"][0]["phrase"], "кружка термос")
        self.assertEqual(len(groups["negatives"]), 1)
        self.assertEqual(groups["neutral"][0]["phrase"], "термочашка маленькая")

    def test_keyword_records_shape(self):
        records = sp.to_keyword_records(rows(), category_id="9000", type_id="9500")
        self.assertEqual(records[0]["keyword"], "термос 500 мл")
        self.assertEqual(records[0]["source"], "performance_ad")
        self.assertEqual(records[0]["category_id"], "9000")
        self.assertEqual(records[0]["extra"]["orders"], 4)

    def test_feed_library_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = sp.feed_keyword_library(
                tmp, rows(), category_id="9000", type_id="9500",
                category_path_zh="家居/保温杯",
            )
            self.assertEqual(summary["created"], 4)
            files = list(Path(tmp).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            stored = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(stored[0]["category_path_zh"], "家居/保温杯")


class ScriptedPerfTransport:
    def __init__(self):
        self.calls = []
        self._state = ["NOT_STARTED", "IN_PROGRESS", "OK"]

    def token(self, client_id, client_secret):
        self.calls.append("token")
        return {"access_token": "BEARER123", "expires_in": 1800}

    def post_json(self, path, body, bearer):
        self.calls.append(("post", path, body, bearer))
        return {"UUID": "uuid-1"}

    def get_json(self, path, bearer):
        state = self._state.pop(0)
        if state == "OK":
            return {"state": "OK", "link": "/api/client/statistics/uuid-1/report"}
        return {"state": state}

    def get_text(self, path, bearer):
        self.calls.append(("get_text", path, bearer))
        return FIXTURE_CSV.read_text(encoding="utf-8")


class RunFlowTests(unittest.TestCase):
    def test_obtain_token_requires_credentials(self):
        with self.assertRaises(sp.SearchPhrasesError):
            sp.obtain_token(ScriptedPerfTransport(), client_id="", client_secret="")

    def test_create_report_validates_campaigns(self):
        t = ScriptedPerfTransport()
        with self.assertRaises(sp.SearchPhrasesError):
            sp.create_report(t, "b", campaigns=[], date_from="x", date_to="y")

    def test_wait_for_report_ok_returns_link(self):
        t = ScriptedPerfTransport()
        link = sp.wait_for_report(
            t, "b", "uuid-1", poll_interval=0, sleeper=lambda _s: None
        )
        self.assertEqual(link, "/api/client/statistics/uuid-1/report")

    def test_run_end_to_end_and_feed(self):
        t = ScriptedPerfTransport()
        with tempfile.TemporaryDirectory() as tmp:
            result = sp.run(
                t,
                campaigns=["12558"],
                days=14,
                category_id="9000",
                type_id="9500",
                library_root=tmp,
                credentials=sp.PerformanceCredentials("cid", "csecret"),
            )
        self.assertEqual(result["uuid"], "uuid-1")
        self.assertEqual(result["phrases"], 4)
        self.assertEqual(len(result["winners"]), 2)
        self.assertEqual(result["negatives"], ["кружка термос"])
        self.assertEqual(result["library"]["created"], 4)


if __name__ == "__main__":
    unittest.main()
