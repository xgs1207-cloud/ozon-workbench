"""Vision service safety and Chinese advice, entirely offline with real tiny images."""
from __future__ import annotations

from copy import deepcopy
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from collector.ingest import ingest_capture
from models.base import ModelError
from models.http_provider import OpenAICompatibleTransport, build_ark_vision_transport
from pipeline.image_insights import analyze_image_insights, read_image_insights
from pipeline.listing_form import read_json, write_json
from pipeline.sku_selection import set_selection


def response() -> dict:
    return {
        "visible_observations": [{"id": "obs1", "label_zh": "粉色外观", "description_zh": "参考图中的主体呈粉色。",
                                  "image_ids": ["image-001"]}],
        "seller_claims": [],
        "selling_points": [{"title_zh": "粉色外观一眼可辨", "why_it_matters_zh": "买家更容易看清所选颜色。",
                            "image_howto_zh": "拍摄商品正面，用浅灰背景和柔和光线突出粉色外观。",
                            "prompt_zh": "参考图商品居中，浅灰背景，柔光实拍，清楚展示粉色外观。",
                            "image_ids": ["image-001"], "observation_ids": ["obs1"], "fact_ids": []}],
        "unknowns": ["图片不能确认真实材质和认证。"],
    }


class VisionTransport:
    name = "offline-vision"
    model = "vision-fixture"

    def __init__(self, payload=None, action=None):
        self.payload = payload if payload is not None else response()
        self.action = action
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.action:
            self.action()
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)


class ImageInsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        paths = []
        for index, color in enumerate(("pink", "blue", "white")):
            path = root / f"source-{index}.png"
            Image.new("RGB", (32, 48), color).save(path)
            paths.append(path)
        capture = ingest_capture(root / "products", {
            "source_url": "https://detail.1688.com/offer/123456.html",
            "title_zh": "多颜色玩具，广告认证声明不可自动当事实",
            "collection_mode": "all_skus",
            "skus": [{"sku_id": "S1", "sku_name": "粉色幽灵", "purchase_price_cny": 3},
                     {"sku_id": "S2", "sku_name": "蓝色幽灵", "purchase_price_cny": 4}],
            "images": {"sku": [{"sku_id": "S1", "path": str(paths[0])},
                               {"sku_id": "S2", "path": str(paths[1])}],
                       "detail": [{"path": str(paths[2])}]},
        })
        self.directory = root / "products" / capture["product_id"]
        set_selection(self.directory, include=["S1"])
        source = read_json(self.directory / "input/source.json")
        # Local file imports preserve filenames; legacy ingestion does not
        # attach sku_id from images dictionaries. Record exact captured paths,
        # as the browser collector does, instead of using gallery order.
        for index, sku in enumerate(source["skus"]):
            sku["image_path"] = next(path for path in source["stored_images"] if f"source-{index}.png" in path)
        write_json(self.directory / "input/source.json", source)
        self.selected_image = source["skus"][0]["image_path"]
        self.other_image = source["skus"][1]["image_path"]
        self.shared_image = next(path for path in source["stored_images"] if "/detail-images/" in path)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self, transport=None, **kwargs):
        return analyze_image_insights(self.directory, image_paths=[self.selected_image],
                                      provider=transport or VisionTransport(), **kwargs)

    def test_real_bytes_as_data_url_and_exact_sku_provenance(self):
        transport = VisionTransport()
        before = (self.directory / "input/source.json").read_bytes()
        result = self.analyze(transport)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(len(transport.calls), 1)
        payload = result["payload"]
        self.assertEqual(payload["selected_sku_ids"], ["S1"])
        self.assertEqual(payload["visible_observations"][0]["source_sku_ids"], ["S1"])
        self.assertFalse(payload["visible_observations"][0]["confirmed"])
        self.assertTrue(payload["advisory_only"])
        self.assertFalse(payload["automatic_fact_updates"])
        content = transport.calls[0]["user"]
        url = next(row["image_url"]["url"] for row in content if row["type"] == "image_url")
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), (self.directory / self.selected_image).read_bytes())
        self.assertNotIn("蓝色幽灵", content[0]["text"])
        self.assertEqual(before, (self.directory / "input/source.json").read_bytes())
        self.assertFalse((self.directory / "output/product-analysis.json").exists())
        self.assertFalse((self.directory / "output/copy-ru.json").exists())
        self.assertNotIn("data:image", (self.directory / "output/image-insights.json").read_text(encoding="utf-8"))

    def test_get_never_calls_model_and_cache_hit_without_configuration(self):
        self.assertEqual(read_image_insights(self.directory)["status"], "none")
        self.analyze()
        with patch("pipeline.image_insights.build_ark_vision_transport", side_effect=AssertionError("No network")):
            cached = analyze_image_insights(self.directory, image_paths=[self.selected_image])
            status = read_image_insights(self.directory)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["model_calls"], 0)
        self.assertEqual(status["status"], "ready")

    def test_selection_changes_make_result_stale(self):
        self.analyze()
        set_selection(self.directory, include=["S2"])
        self.assertEqual(read_image_insights(self.directory)["status"], "stale")
        self.assertIsNone(read_image_insights(self.directory)["payload"])

    def test_unselected_reference_rejected_before_rpc(self):
        transport = VisionTransport()
        with self.assertRaisesRegex(ValueError, "未选规格"):
            analyze_image_insights(self.directory, image_paths=[self.other_image], provider=transport)
        self.assertEqual(transport.calls, [])

    def test_default_references_do_not_contain_unselected_sku(self):
        result = analyze_image_insights(self.directory, provider=VisionTransport())
        paths = [row["path"] for row in result["payload"]["image_evidence"]]
        self.assertIn(self.selected_image, paths)
        self.assertNotIn(self.other_image, paths)

    def test_shared_reference_remains_unbound(self):
        result = analyze_image_insights(self.directory, image_paths=[self.shared_image], provider=VisionTransport())
        obs = result["payload"]["visible_observations"][0]
        self.assertEqual(obs["scope"], "shared_unverified")
        self.assertEqual(obs["source_sku_ids"], [])

    def test_printed_material_numeric_and_certification_are_unverified(self):
        payload = response()
        payload["visible_observations"].extend([
            {"id": "obs2", "label_zh": "重量144克", "description_zh": "图片标注重量144克", "image_ids": ["image-001"]},
            {"id": "obs3", "label_zh": "硅胶材质", "description_zh": "看起来像硅胶", "image_ids": ["image-001"]},
            {"id": "obs4", "label_zh": "认证", "description_zh": "图片有EAC认证标记", "image_ids": ["image-001"]},
        ])
        payload["confirmed_facts"] = [{"id": "fake-cert", "verified": True}]
        payload["selling_points"].append({**deepcopy(payload["selling_points"][0]), "title_zh": "无毒安全材质"})
        result = self.analyze(VisionTransport(payload))["payload"]
        self.assertEqual(len(result["visible_observations"]), 1)
        self.assertEqual(len(result["unverified_seller_claims"]), 3)
        self.assertTrue(all(row["verification"] == "unverified" for row in result["unverified_seller_claims"]))
        self.assertEqual(result["confirmed_facts"], [])
        self.assertEqual(len(result["selling_points"]), 1)

    def test_unprovided_image_or_fabricated_fact_rejected(self):
        for field, value in (("image_ids", ["foreign-image"]), ("fact_ids", ["fake-fact"])):
            payload = response()
            payload["selling_points"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.analyze(VisionTransport(payload))
        self.assertFalse((self.directory / "output/image-insights.json").exists())

    def test_english_prompt_rejected(self):
        payload = response()
        payload["selling_points"][0]["prompt_zh"] = "A seller photo on a grey background"
        with self.assertRaisesRegex(ValueError, "中文"):
            self.analyze(VisionTransport(payload))

    def test_file_integrity_size_path_bounds_are_checked_before_rpc(self):
        transport = VisionTransport()
        for relative in ("../secret.png", "output/generated-images/main.png", "https://evil.invalid/secret.png"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                analyze_image_insights(self.directory, image_paths=[relative], provider=transport)
        path = self.directory / self.selected_image
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "封存"):
            self.analyze(transport)
        self.assertEqual(transport.calls, [])

    def test_oversize_rejected_before_rpc(self):
        path = self.directory / self.selected_image
        path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        transport = VisionTransport()
        with self.assertRaisesRegex(ValueError, "5 MB"):
            self.analyze(transport)
        self.assertEqual(transport.calls, [])

    def test_selection_changes_during_rpc_do_not_save_old_result(self):
        transport = VisionTransport(action=lambda: set_selection(self.directory, include=["S2"]))
        with self.assertRaises(ValueError):
            self.analyze(transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse((self.directory / "output/image-insights.json").exists())

    def test_image_bytes_changed_during_rpc_do_not_replace_previous_result(self):
        self.analyze()
        previous = (self.directory / "output/image-insights.json").read_bytes()
        path = self.directory / self.selected_image
        transport = VisionTransport(action=lambda: path.write_bytes(path.read_bytes() + b"changed"))
        with self.assertRaisesRegex(ValueError, "封存"):
            self.analyze(transport, force=True)
        self.assertEqual((self.directory / "output/image-insights.json").read_bytes(), previous)
        self.assertEqual(read_image_insights(self.directory)["status"], "stale")

    def test_image_count_and_malformed_lists_do_not_produce_artifact(self):
        transport = VisionTransport()
        with self.assertRaises(ValueError):
            analyze_image_insights(self.directory, image_paths=[], provider=transport)
        self.assertEqual(transport.calls, [])
        payload = response()
        payload["visible_observations"] = {"invented": "shape"}
        with self.assertRaisesRegex(ValueError, "数组"):
            self.analyze(VisionTransport(payload))
        payload = response()
        payload["visible_observations"][0]["image_ids"] = ["../secret.png"]
        with self.assertRaisesRegex(ValueError, "未提供"):
            self.analyze(VisionTransport(payload))
        self.assertFalse((self.directory / "output/image-insights.json").exists())

    def test_supplier_claims_never_become_confirmed_facts(self):
        payload = response()
        payload["seller_claims"] = [{"claim_zh": "供应商图写着144克和认证标记", "image_ids": ["image-001"], "verified": True}]
        payload["confirmed_facts"] = [{"id": "supplier-ad", "value": "144克", "verified": True}]
        result = self.analyze(VisionTransport(payload))["payload"]
        self.assertEqual(result["confirmed_facts"], [])
        self.assertEqual(result["unverified_seller_claims"][0]["verification"], "unverified")
        self.assertFalse(result["automatic_fact_updates"])

    def test_provider_error_and_invalid_json_are_not_retried(self):
        transport = VisionTransport(ModelError("Bearer sk-secret data:image/png;base64,private"))
        with self.assertRaises(ModelError) as caught:
            self.analyze(transport)
        self.assertNotIn("sk-secret", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)
        transport = VisionTransport("not json")
        with self.assertRaises(ValueError):
            self.analyze(transport)
        self.assertEqual(len(transport.calls), 1)


class ArkVisionTransportTests(unittest.TestCase):
    def test_vision_model_priority_and_default_cap(self):
        transport = build_ark_vision_transport({"ARK_API_KEY": "fixture", "ARK_TEXT_MODEL": "text",
                                              "ARK_VISION_MODEL": "vision", "MODEL_MAX_TOKENS": "99999"})
        self.assertEqual(transport.model, "vision")
        self.assertEqual(transport.max_tokens, 6000)
        self.assertEqual(transport.thinking, "disabled")
        self.assertEqual(transport.endpoint, "https://ark.cn-beijing.volces.com/api/v3/chat/completions")

    def test_multimodal_request_uses_official_content_blocks(self):
        transport = OpenAICompatibleTransport(base_url="https://ark.example.invalid/api/v3", api_key="fixture", model="vision")
        content = [{"type": "text", "text": "只观察所选规格"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8=", "detail": "high"}}]
        request = transport.build_request(system="json only", user=content, temperature=.2)
        body = json.loads(request.data)
        self.assertEqual(body["messages"][1]["content"], content)
        self.assertEqual(body["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
