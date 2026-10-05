"""契约驱动的机械归一化测试 + 真模型常见错误的修复路径。

真实踩到（火山方舟真模型）：返回的 JSON 里
- ``facts`` 多了一个 ``title_ru``（契约 additionalProperties=false）
- ``facts.dimensions`` 给成 list（契约要 object）
- ``facts.certifications`` 给成 null（契约要 array）
→ 校验直接判不合规，连重试也救不回来。
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from contracts import load_contract, validate_contract  # noqa: E402
from contracts.normalize import normalize_payload  # noqa: E402
from models.http_provider import HttpModelProvider  # noqa: E402

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title_cn"],
            "properties": {
                "title_cn": {"type": "string"},
                "certifications": {"type": "array", "items": {"type": "string"}},
                "dimensions": {
                    "oneOf": [
                        {"type": "object", "additionalProperties": False, "properties": {"length_mm": {"type": "number"}}},
                        {"type": "null"},
                    ]
                },
                "notes": {"type": "string"},
            },
        },
        "risks": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
}


class NormalizeUnitTests(unittest.TestCase):
    def test_unknown_keys_are_dropped(self):
        payload = {"facts": {"title_cn": "床单", "title_ru": "Простыня", "notes": "x"}, "extra": 1}
        normalized, notes = normalize_payload(payload, SCHEMA)
        self.assertEqual(set(normalized), {"facts"})
        self.assertEqual(set(normalized["facts"]), {"title_cn", "notes"})
        self.assertTrue(any("title_ru" in item and "丢弃" in item for item in notes), notes)
        self.assertTrue(any(item.startswith("$.extra") for item in notes), notes)

    def test_null_array_becomes_empty_list(self):
        payload = {"facts": {"title_cn": "床单", "certifications": None}}
        normalized, notes = normalize_payload(payload, SCHEMA)
        self.assertEqual(normalized["facts"]["certifications"], [])
        self.assertTrue(any("certifications" in item and "[]" in item for item in notes), notes)

    def test_array_items_are_normalised_recursively(self):
        payload = {"risks": [{"area": "x", "why": None}]}
        normalized, _ = normalize_payload(payload, SCHEMA)
        self.assertEqual(normalized["risks"][0]["why"], None)  # additionalProperties: true → 原样保留

    def test_one_of_branch_with_null_is_accepted_as_is(self):
        payload = {"facts": {"title_cn": "床单", "dimensions": None}}
        normalized, notes = normalize_payload(payload, SCHEMA)
        # dimensions 的 oneOf 里有 null 分支 → 不该被强行改成 {}
        self.assertIsNone(normalized["facts"]["dimensions"])
        self.assertEqual([item for item in notes if "dimensions" in item], [])

    def test_object_field_null_becomes_empty_object(self):
        schema = {
            "type": "object",
            "properties": {"facts": {"type": "object", "properties": {"title_cn": {"type": "string"}}}},
        }
        normalized, notes = normalize_payload({"facts": None}, schema)
        self.assertEqual(normalized["facts"], {})
        self.assertTrue(any("facts" in item and "{}" in item for item in notes), notes)

    def test_string_where_array_expected_is_wrapped(self):
        """真模型会把数组写成字符串（materials/ functions）→ 包成单元素数组是无损修正。"""
        schema = {
            "type": "object",
            "properties": {"materials": {"type": "array", "items": {"type": "string"}}},
        }
        normalized, notes = normalize_payload({"materials": "хлопок, полиэстер"}, schema)
        self.assertEqual(normalized["materials"], ["хлопок, полиэстер"])
        self.assertTrue(any("字符串 → [字符串]" in item for item in notes), notes)

    def test_object_where_string_expected_takes_inner_string(self):
        """真模型会把 source_refs 写成 [{'path': 'input/source.json'}] → 取内部字符串。"""
        schema = {
            "type": "object",
            "properties": {"source_refs": {"type": "array", "items": {"type": "string"}}},
        }
        normalized, notes = normalize_payload({"source_refs": [{"path": "input/source.json"}]}, schema)
        self.assertEqual(normalized["source_refs"], ["input/source.json"])
        self.assertTrue(any("对象 → 其中的字符串" in item for item in notes), notes)

    def test_object_with_several_strings_is_left_alone(self):
        schema = {"type": "object", "properties": {"ref": {"type": "string"}}}
        normalized, notes = normalize_payload({"ref": {"a": "x", "b": "y"}}, schema)
        self.assertIsInstance(normalized["ref"], dict)
        self.assertEqual(notes, [])

    def test_real_product_analysis_contract_is_normalisable(self):
        """用真契约（product-analysis）验证：真模型那种错误形状能被修好。"""
        schema = load_contract("product-analysis")
        payload = {
            "schema_version": "1.0.0",
            "product_id": "P000005",
            "source_refs": ["input/source.json"],
            "facts": {
                "title_cn": "纯棉床单",
                "category_cn": "床上用品",
                "brand": None,
                "materials": [],
                "dimensions": [{"name": "length_mm", "value": 200}],   # 错：给了 list
                "weight": None,
                "load_capacity": None,
                "certifications": None,                                # 错：给了 null
                "functions": [],
                "package_quantity": None,
                "accessories": [],
                "skus": [],
                "title_ru": "Простыня",                                # 错：多字段
            },
            "selling_points": [],
            "inferences": [],
            "unknowns": [],
            "risks": [],
            "recommendation": {"decision": "continue", "reason": "ok"},
            "processing": {
                "model_mode": "connected_codex",
                "model_name": "ep-test",
                "generated_at": "2026-10-05T00:00:00+08:00",
                "prompt_version": "1.0.0",
            },
        }
        normalized, notes = normalize_payload(payload, schema)
        self.assertNotIn("title_ru", normalized["facts"])
        self.assertEqual(normalized["facts"]["certifications"], [])
        self.assertTrue(notes)
        # dimensions 仍是 list（oneOf 里没有 list 分支）→ 这类"语义错误"要留给重试，不能被静默改掉
        self.assertIsInstance(normalized["facts"]["dimensions"], list)


class ProviderRetryTests(unittest.TestCase):
    class ScriptedTransport:
        name = "scripted"

        def __init__(self, replies):
            self.replies = list(replies)
            self.prompts: list[str] = []

        def complete(self, *, system: str, user: str, temperature=None) -> str:
            self.prompts.append(user)
            return self.replies.pop(0) if self.replies else "{}"

    def test_normalisation_avoids_a_wasted_retry(self):
        """机械错误能被归一化修掉 → 第一次就该通过，不该浪费一次重试。

        做法：先拿一份**合法**的 product-analysis（假模型产出，契约校验通过），
        再往里注入真模型那种机械错误（多字段 / null / 类型不符）。
        """
        from models.base import AnalysisRequest
        from models.fake import FakeProvider

        fake = FakeProvider()
        valid = fake.analyze_product(
            AnalysisRequest(
                product_id="P000005",
                product_dir=pathlib.Path("."),
                source={
                    "product_id": "P000005",
                    "source_url": "https://detail.1688.com/offer/1.html",
                    "title_zh": "纯棉床单",
                    "skus": [{"sku_id": "S1", "color_ru": "белый", "purchase_price_cny": 42.0}],
                },
                selected_keywords=[{"keyword": "простыня 200х200"}],
            )
        )
        payload = json.loads(json.dumps(valid, ensure_ascii=False))
        payload["facts"]["title_ru"] = "Простыня"          # 真模型爱多塞字段
        payload["facts"]["certifications"] = None           # 数组字段给了 null
        payload["unexpected_top_level"] = {"x": 1}          # 顶层多字段

        transport = self.ScriptedTransport([json.dumps(payload, ensure_ascii=False)])
        provider = HttpModelProvider(transport, max_attempts=3)
        result, warnings = provider._call_json(
            task="product_analysis",
            user="请分析",
            validate=lambda data: validate_contract("product-analysis", data),
            contract="product-analysis",
        )
        self.assertEqual(len(transport.prompts), 1, f"归一化后不该再重试：{provider.calls}")
        self.assertNotIn("title_ru", result["facts"])
        self.assertNotIn("unexpected_top_level", result)
        self.assertTrue(any("丢弃" in item for item in warnings), warnings)
        self.assertEqual(provider.calls[0]["ok"], True)

    def test_semantic_error_still_retries_with_problem_list(self):
        transport = self.ScriptedTransport(["{}", "{}", "{}"])
        provider = HttpModelProvider(transport, max_attempts=3)
        with self.assertRaises(Exception) as ctx:
            provider._call_json(
                task="product_analysis",
                user="请分析",
                validate=lambda data: validate_contract("product-analysis", data),
                contract="product-analysis",
            )
        self.assertIn("连续 3 次", str(ctx.exception))
        self.assertEqual(len(transport.prompts), 3)
        self.assertIn("上一次输出不合法", transport.prompts[1])  # 第二次带上了问题清单
        self.assertIn("上一次输出不合法", transport.prompts[2])


if __name__ == "__main__":
    unittest.main()
