"""HTTP 模型适配器测试：JSON 提取、请求构建、契约校验、修复重试、降级策略（全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from models import CopyRequest, ModelError, PositionRequest, load_provider  # noqa: E402
from models.base import AnalysisRequest, DesignRequest, ImagePlanRequest, ImageRequest  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.http_provider import (  # noqa: E402
    HttpModelProvider,
    OpenAICompatibleTransport,
    ProviderConfig,
    build_provider_from_env,
    extract_json,
)
from models.image_plan import build_image_plan  # noqa: E402
from models.local_image import LocalPlaceholderGenerator  # noqa: E402
from pipeline.attributes import build_attribute_fill_input, compile_attributes  # noqa: E402
from pipeline.context import StepContext  # noqa: E402
from pipeline.image_generation import handle_image_generation  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
ENV = {"MODEL_BASE_URL": "https://api.example.com/v1", "MODEL_API_KEY": "sk-secret", "MODEL_NAME": "some-model"}


class ScriptedTransport:
    """脚本化传输层：按顺序返回文本或抛错，并记录每次调用。"""

    name = "scripted"

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, temperature=None) -> str:
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if not self.responses:
            raise AssertionError("脚本化传输层没有更多响应了")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prose_around_object(self):
        text = '好的，这是结果：\n{"a": {"b": [1, 2]}, "c": "}"}\n希望有帮助'
        self.assertEqual(extract_json(text), {"a": {"b": [1, 2]}, "c": "}"})

    def test_braces_inside_strings_do_not_confuse(self):
        self.assertEqual(extract_json('prefix {"x": "a{b}c"} suffix'), {"x": "a{b}c"})

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_json("没有 JSON"))
        self.assertIsNone(extract_json("[1, 2, 3]"))
        self.assertIsNone(extract_json(""))
        self.assertIsNone(extract_json(None))  # type: ignore[arg-type]


class TransportTests(unittest.TestCase):
    def test_builds_openai_compatible_request(self):
        captured: dict = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        transport = OpenAICompatibleTransport(
            base_url="https://api.example.com/v1/", api_key="sk-secret", model="m1", timeout=42, urlopen=fake_urlopen
        )
        content = transport.complete(system="sys", user="hi", temperature=0.2)
        self.assertEqual(content, '{"ok": true}')
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["headers"]["authorization"], "Bearer sk-secret")
        self.assertEqual(captured["timeout"], 42)
        self.assertEqual(captured["body"]["model"], "m1")
        self.assertEqual(captured["body"]["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(captured["body"]["temperature"], 0.2)
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})

    def test_http_error_and_bad_json_are_wrapped(self):
        import urllib.error

        def raising(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        transport = OpenAICompatibleTransport(
            base_url="https://api.example.com", api_key="k", model="m", urlopen=raising
        )
        with self.assertRaises(ModelError) as ctx:
            transport.complete(system="s", user="u")
        self.assertIn("HTTP 401", str(ctx.exception))

        class PlainResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"not json"

        transport = OpenAICompatibleTransport(
            base_url="https://api.example.com", api_key="k", model="m", urlopen=lambda request, timeout=None: PlainResponse()
        )
        with self.assertRaises(ModelError):
            transport.complete(system="s", user="u")

    def test_bad_base_url_is_rejected(self):
        with self.assertRaises(ModelError):
            OpenAICompatibleTransport(base_url="api.example.com", api_key="k", model="m")


class ConfigTests(unittest.TestCase):
    def test_missing_env_reports_every_key(self):
        with self.assertRaises(ModelError) as ctx:
            ProviderConfig.from_env({})
        message = str(ctx.exception)
        for key in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"):
            self.assertIn(key, message)

    def test_optional_values_and_flags(self):
        config = ProviderConfig.from_env(
            {**ENV, "MODEL_TIMEOUT": "15", "MODEL_TEMPERATURE": "0.1", "MODEL_MAX_ATTEMPTS": "3",
             "MODEL_FALLBACK_TO_DETERMINISTIC": "true"}
        )
        self.assertEqual(config.timeout, 15)
        self.assertEqual(config.temperature, 0.1)
        self.assertEqual(config.max_attempts, 3)
        self.assertTrue(config.fallback_to_deterministic)

    def test_broken_numbers_fall_back_to_defaults(self):
        config = ProviderConfig.from_env({**ENV, "MODEL_TIMEOUT": "abc"})
        self.assertEqual(config.timeout, 90)

    def test_load_provider_registers_http(self):
        import os

        previous = {key: os.environ.get(key) for key in ENV}
        try:
            os.environ.update(ENV)
            provider = load_provider("deepseek")
            self.assertIsInstance(provider, HttpModelProvider)
            self.assertEqual(provider.transport.model, "some-model")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_build_provider_from_env(self):
        provider = build_provider_from_env(ENV)
        self.assertEqual(provider.transport.base_url, "https://api.example.com/v1")
        self.assertEqual(provider.transport.api_key, "sk-secret")

    def test_ark_config_shares_one_key_with_image_generation(self):
        """一个 ARK_API_KEY 走完文本 + 生图（用户选定方案）。"""
        config = ProviderConfig.ark_from_env({"ARK_API_KEY": "ark-key", "ARK_TEXT_MODEL": "ep-2026xxx"})
        self.assertEqual(config.base_url, "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(config.api_key, "ark-key")
        self.assertEqual(config.model, "ep-2026xxx")

        # MODEL_* 显式覆盖优先
        overridden = ProviderConfig.ark_from_env(
            {
                "ARK_API_KEY": "ark-key",
                "ARK_BASE_URL": "https://ark.example.com",
                "MODEL_BASE_URL": "https://override.example.com/v1",
                "MODEL_NAME": "m2",
                "MODEL_API_KEY": "mw",
            }
        )
        self.assertEqual(overridden.base_url, "https://override.example.com/v1")
        self.assertEqual(overridden.api_key, "mw")
        self.assertEqual(overridden.model, "m2")

    def test_ark_config_reports_missing_pieces(self):
        with self.assertRaises(ModelError) as ctx:
            ProviderConfig.ark_from_env({})
        message = str(ctx.exception)
        self.assertIn("ARK_API_KEY", message)
        self.assertIn("ARK_TEXT_MODEL", message)

        with self.assertRaises(ModelError) as ctx2:
            ProviderConfig.ark_from_env({"ARK_API_KEY": "k"})
        self.assertIn("ARK_TEXT_MODEL", str(ctx2.exception))

    def test_load_provider_ark_alias(self):
        import os

        previous = {key: os.environ.get(key) for key in ("ARK_API_KEY", "ARK_TEXT_MODEL")}
        try:
            os.environ["ARK_API_KEY"] = "ark-key"
            os.environ["ARK_TEXT_MODEL"] = "ep-2026xxx"
            provider = load_provider("ark")
            self.assertIsInstance(provider, HttpModelProvider)
            self.assertEqual(provider.transport.base_url, "https://ark.cn-beijing.volces.com/api/v3")
            self.assertEqual(provider.transport.model, "ep-2026xxx")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class ProviderBehaviourTests(unittest.TestCase):
    """用确定性 fake 的产物当"模型的正确输出"，验证校验/重试/降级路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/737373737.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_zh": "红色", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5, "image_path": "input/sku-images/01.png"},
                    {"sku_id": "S2", "color_zh": "蓝色", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0, "image_path": "input/sku-images/02.png"},
                ],
            },
        )
        self.product_dir = self.products / self.summary["product_id"]
        for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
            directory = self.product_dir / "input" / relative
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        self.source = json.loads((self.product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        self.source["selected_category"] = {"category_id": "1001", "type_id": "2001"}
        self.fake = FakeProvider()

    def tearDown(self):
        self.tmp.cleanup()

    def analysis(self) -> dict:
        return self.fake.analyze_product(
            AnalysisRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[{"keyword": "термос 500 мл"}, {"keyword": "термос для чая"}],
            )
        )

    def copy(self) -> dict:
        return self.fake.write_copy_ru(
            CopyRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[{"keyword": "термос 500 мл"}, {"keyword": "термос для чая"}],
                analysis=self.analysis(),
            )
        )

    def test_analyze_accepts_valid_json_and_records_call(self):
        provider = HttpModelProvider(ScriptedTransport([json.dumps(self.analysis(), ensure_ascii=False)]))
        payload = provider.analyze_product(
            AnalysisRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[{"keyword": "термос 500 мл"}],
            )
        )
        self.assertEqual(validate_contract("product-analysis", payload), [])
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(provider.calls[0]["ok"])
        self.assertNotIn("sk-secret", json.dumps(provider.calls, ensure_ascii=False))

    def test_repair_retry_includes_problems(self):
        """新设计下：facts 由代码填，模型只写叙述；叙述里的推荐枚举写错仍会触发带错误清单的重试。"""
        broken = json.dumps({"recommendation": {"decision": "maybe", "reason": "拿不准"}})  # 枚举非法
        good = json.dumps({"recommendation": {"decision": "continue", "reason": "证据齐全"}})
        transport = ScriptedTransport([broken, good])
        provider = HttpModelProvider(transport, max_attempts=2)
        payload = provider.analyze_product(
            AnalysisRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[{"keyword": "термос 500 мл"}],
            )
        )
        self.assertEqual(validate_contract("product-analysis", payload), [])
        self.assertEqual(payload["recommendation"]["decision"], "continue")
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("上一次输出不合法", transport.calls[1]["user"])
        self.assertIn("recommendation.decision", transport.calls[1]["user"])
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(provider.calls[0]["ok"])
        self.assertTrue(provider.calls[1]["ok"])

    def test_narrative_prompt_covers_contract_required_fields(self):
        """防漂移：叙述字段的形状必须与 product-analysis 契约一致。

        踩过的坑：提示词里自己编了 inferences={area,statement,basis}，而契约要
        {field,value,confidence,basis} → 真模型连试 3 次都过不了校验。
        """
        import json as _json
        import pathlib as _pathlib

        from models.http_provider import build_narrative_prompt

        request = AnalysisRequest(
            product_id=self.product_dir.name,
            product_dir=self.product_dir,
            source=self.source,
            selected_keywords=[{"keyword": "термос 500 мл"}],
        )
        prompt = build_narrative_prompt(request)

        schema = _json.loads(
            (_pathlib.Path(__file__).resolve().parents[1] / "contracts" / "original"
             / "product-analysis.schema.json").read_text(encoding="utf-8")
        )
        props = schema.get("properties") or {}
        defs = schema.get("$defs") or {}
        for key in ("selling_points", "inferences", "unknowns", "risks"):
            items = (props.get(key) or {}).get("items") or {}
            ref = items.get("$ref")
            target = defs.get(ref.split("/")[-1]) if ref else items
            for field in (target or {}).get("required") or []:
                self.assertIn(field, prompt, f"{key} 的必填字段 {field} 没写进提示词")
        for decision in ("continue", "needs_human_input", "reject", "unknown"):
            self.assertIn(decision, prompt, f"decision 枚举 {decision} 没写进提示词")
        # 系统字段不该让模型输出
        self.assertIn("不要输出", prompt)

    def test_truncated_reply_is_detected_and_retry_asks_for_shorter_output(self):
        """真机踩坑：copy 回复 11000+ 字符撞 max_tokens 被截断 → 重试必须要求精简，否则白重试。"""
        from models.http_provider import looks_truncated

        self.assertTrue(looks_truncated('{"title_ru": {"title_ru": "Простыня'))
        self.assertTrue(looks_truncated('```json\n{"a": 1}\n```extra'))
        self.assertFalse(looks_truncated('{"a": {"b": 1}}'))
        self.assertFalse(looks_truncated('```json\n{"a": {"b": "}"}}\n```'))

        transport = ScriptedTransport(['{"title_ru": {"title_ru": "обрезано', "{}"])
        provider = HttpModelProvider(transport, max_attempts=2)
        with self.assertRaises(Exception):
            provider._call_json(task="russian_copy", user="u", validate=lambda data: ["还是不行"])
        self.assertIn("输出被截断", transport.calls[1]["user"])
        self.assertIn("精简内容", transport.calls[1]["user"])

    def test_copy_prompt_covers_bundle_rules(self):
        """防漂移：copy_bundle 的形状来自我们自己的规则，必须出现在提示词里。

        真机踩坑：提示词没写形状 → 真模型给的标题/描述为空、五个 section 全缺、
        标签写成 #простыня200х200（规则只允许西里尔字母）。
        """
        from rules.validate import _DESCRIPTION_SECTIONS, copy_bundle_hint

        hint = copy_bundle_hint()
        for name in _DESCRIPTION_SECTIONS:
            self.assertIn(name, hint, f"section {name} 没写进提示词")
        self.assertIn("西里尔字母", hint)
        self.assertIn("description_sections", hint)
        self.assertIn("不能含数字", hint)

    def test_schema_hint_covers_array_item_required_fields(self):
        """真机踩过：数组元素内部的必填字段没告诉模型 → 模型漏 claim_type，连续 3 次过不了校验。"""
        from models.http_provider import _schema_hint

        hint = _schema_hint("product-positioning")
        self.assertIn("buyer_selling_points", hint)
        for field in ("text", "claim_type", "source_refs"):
            self.assertIn(field, hint, f"{field} 没写进 schema 提示")
        self.assertIn("fact", hint)
        self.assertIn("supported_inference", hint)
        analysis_hint = _schema_hint("product-analysis")
        self.assertIn("inferences", analysis_hint)
        self.assertIn("confidence", analysis_hint)

    def test_thinking_toggle_and_max_tokens_are_sent(self):
        """省钱快捷：默认关掉方舟思考链，并给单次回复设上限（实测 13.1s/527token → 4.5s/83token）。"""
        import json as _json

        from models.http_provider import OpenAICompatibleTransport, ProviderConfig

        transport = OpenAICompatibleTransport(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key="k",
            model="ep-test",
            thinking="disabled",
            max_tokens=1234,
        )
        request = transport.build_request(system="s", user="u", temperature=0.3)
        body = _json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 1234)

    def test_config_defaults_to_disabled_thinking(self):
        from models.http_provider import ProviderConfig

        config = ProviderConfig.ark_from_env(
            {"ARK_API_KEY": "ark-x", "ARK_TEXT_MODEL": "ep-test", "MODEL_TIMEOUT": "300"}
        )
        self.assertEqual(config.thinking, "disabled")
        self.assertEqual(config.max_tokens, 4000)
        off = ProviderConfig.ark_from_env(
            {"ARK_API_KEY": "ark-x", "ARK_TEXT_MODEL": "ep-test", "ARK_THINKING": "enabled"}
        )
        self.assertEqual(off.thinking, "enabled")

    def test_facts_enrichment_uses_confirmed_inputs(self):
        """已知事实（类目名/人工确认的尺寸重量/无品牌规则）由代码补进 facts，不靠模型照抄。"""
        import json as _json
        import tempfile

        from models.http_provider import enrich_facts_from_inputs

        with tempfile.TemporaryDirectory() as tmp:
            product_dir = pathlib.Path(tmp)
            (product_dir / "input").mkdir()
            (product_dir / "input" / "category-selection.json").write_text(
                _json.dumps({"category_id": "17028731", "type_id": "92612", "category_path_zh": "住宅和花园/床上用品/床单"}),
                encoding="utf-8",
            )
            (product_dir / "input" / "workbench-sku-overrides.json").write_text(
                _json.dumps({"product": {"product_length_mm": 200, "product_width_mm": 200,
                                         "product_height_mm": 40, "product_weight_g": 900}}),
                encoding="utf-8",
            )
            payload = {"facts": {"title_cn": "纯棉床单", "category_cn": None, "brand": None,
                                 "dimensions": "unknown", "weight": "unknown"}}
            request = AnalysisRequest(
                product_id="P000006", product_dir=product_dir, source={"title_zh": "纯棉床单"}, selected_keywords=[]
            )
            notes = enrich_facts_from_inputs(payload, request)
            self.assertEqual(payload["facts"]["category_cn"], "住宅和花园/床上用品/床单")
            self.assertEqual(payload["facts"]["dimensions"]["length_mm"], 200)
            self.assertEqual(payload["facts"]["weight"]["value_g"], 900)
            self.assertEqual(payload["facts"]["brand"], "Нет бренда")
            self.assertTrue(any("人工确认" in item for item in notes), notes)

    def test_facts_enrichment_keeps_existing_values(self):
        from models.http_provider import enrich_facts_from_inputs

        payload = {"facts": {"category_cn": "已有类目", "brand": "RealBrand"}}
        request = AnalysisRequest(product_id="P1", product_dir=None, source={}, selected_keywords=[])
        enrich_facts_from_inputs(payload, request)
        self.assertEqual(payload["facts"]["category_cn"], "已有类目")
        self.assertEqual(payload["facts"]["brand"], "RealBrand")

    def test_facts_always_come_from_code_even_if_model_sends_its_own(self):
        """模型就算硬塞一份 facts，也不能覆盖代码从 source 派生的事实。"""
        model_reply = json.dumps(
            {
                "facts": {"title_cn": "模型瞎写的标题", "brand": "某品牌"},
                "recommendation": {"decision": "continue", "reason": "ok"},
            }
        )
        provider = HttpModelProvider(ScriptedTransport([model_reply]), max_attempts=1)
        payload = provider.analyze_product(
            AnalysisRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[],
            )
        )
        self.assertEqual(payload["facts"]["title_cn"], self.source["title_zh"])
        self.assertNotEqual(payload["facts"].get("brand"), "某品牌")

    def test_exhausted_retries_fail_loudly(self):
        transport = ScriptedTransport(["不是 JSON", "仍然不是 JSON"])
        provider = HttpModelProvider(transport, max_attempts=2)
        with self.assertRaises(ModelError) as ctx:
            provider.analyze_product(
                AnalysisRequest(
                    product_id=self.product_dir.name,
                    product_dir=self.product_dir,
                    source=self.source,
                    selected_keywords=[],
                )
            )
        self.assertIn("连续 2 次未通过校验", str(ctx.exception))

    def test_fallback_only_when_enabled(self):
        request = AnalysisRequest(
            product_id=self.product_dir.name,
            product_dir=self.product_dir,
            source=self.source,
            selected_keywords=[{"keyword": "термос 500 мл"}],
        )
        with self.assertRaises(ModelError):
            HttpModelProvider(ScriptedTransport(["nope"]), max_attempts=1).analyze_product(request)

        provider = HttpModelProvider(ScriptedTransport(["nope"]), max_attempts=1, fallback_to_deterministic=True)
        payload = provider.analyze_product(request)
        self.assertEqual(validate_contract("product-analysis", payload), [])
        # 降级说明只留在调用轨迹里（契约是 additionalProperties:false，不能塞额外字段）
        self.assertIn("退化为确定性基座", str(provider.calls[-1].get("note") or ""))
        self.assertEqual(provider.calls[-1]["task"], "product_analysis")

    def test_write_copy_validates_contracts_and_rules(self):
        provider = HttpModelProvider(ScriptedTransport([json.dumps(self.copy(), ensure_ascii=False)]))
        payload = provider.write_copy_ru(
            CopyRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[{"keyword": "термос 500 мл"}, {"keyword": "термос для чая"}],
                analysis=self.analysis(),
            )
        )
        for contract, key in (("title-ru", "title_ru"), ("description-ru", "description_ru"), ("keywords-ru", "keywords_ru")):
            self.assertEqual(validate_contract(contract, payload[key]), [], contract)
        self.assertIn("http", payload["copy_bundle"]["generated_by"])

    def test_write_copy_repairs_bad_hashtags(self):
        bad = self.copy()
        bad["copy_bundle"]["hashtags"] = ["#termos"]
        good = self.copy()
        transport = ScriptedTransport([json.dumps(bad, ensure_ascii=False), json.dumps(good, ensure_ascii=False)])
        provider = HttpModelProvider(transport, max_attempts=2)
        payload = provider.write_copy_ru(
            CopyRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                selected_keywords=[{"keyword": "термос 500 мл"}],
                analysis=self.analysis(),
            )
        )
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(payload["copy_bundle"]["hashtags"])

    def test_position_contract_enforced(self):
        positioning = self.fake.position_product(
            PositionRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                analysis=self.analysis(),
                copy_bundle=self.copy()["copy_bundle"],
            )
        )
        provider = HttpModelProvider(ScriptedTransport([json.dumps(positioning, ensure_ascii=False)]))
        payload = provider.position_product(
            PositionRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                analysis=self.analysis(),
                copy_bundle=self.copy()["copy_bundle"],
            )
        )
        self.assertEqual(validate_contract("product-positioning", payload), [])

    REFS = ["input/source.json", "output/product-analysis.json", "output/copy-ru.json", "input/selected-keywords.json"]

    def test_design_uses_model_copy_and_assembler_structure(self):
        copy_bundle = self.copy()["copy_bundle"]
        fill_input = build_attribute_fill_input(
            source=self.source, copy_bundle=copy_bundle, analysis=self.analysis()
        )
        attributes = compile_attributes(
            product_id=self.product_dir.name,
            category_snapshot={"attributes": []},
            fill_input=fill_input,
        )
        image_plan = build_image_plan(
            product_dir=self.product_dir,
            source=self.source,
            copy_bundle=copy_bundle,
            analysis=self.analysis(),
            source_refs=self.REFS,
        )
        design = self.fake.design_listing(
            DesignRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                source_refs=self.REFS,
                analysis=self.analysis(),
                copy_bundle=copy_bundle,
                image_plan=image_plan,
                attributes_final=attributes,
            )
        )
        self.assertEqual(validate_contract("ozon-ecommerce-design", design), [])

    def test_plan_images_returns_rule_based_plan(self):
        copy_bundle = self.copy()["copy_bundle"]
        provider = HttpModelProvider(ScriptedTransport([]))
        plan = provider.plan_images(
            ImagePlanRequest(
                product_id=self.product_dir.name,
                product_dir=self.product_dir,
                source=self.source,
                source_refs=self.REFS,
                copy_bundle=copy_bundle,
                analysis=self.analysis(),
            )
        )
        self.assertEqual(validate_contract("image-plan", plan), [])
        self.assertTrue(any("规则装配器" in item for item in provider.last_notes))
        self.assertEqual(provider.calls, [])

    def test_generate_image_is_not_this_providers_job(self):
        provider = HttpModelProvider(ScriptedTransport([]))
        with self.assertRaises(ModelError):
            provider.generate_image(
                ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source=self.source)
            )

    def test_provider_integrates_with_pipeline_step(self):
        from pipeline.handlers import run_single_step

        provider = HttpModelProvider(ScriptedTransport([json.dumps(self.analysis(), ensure_ascii=False)]))
        result = run_single_step(self.product_dir, "product_analysis", provider=provider)
        self.assertEqual(result["decision"], "continue")
        self.assertTrue((self.product_dir / "output" / "product-analysis.json").is_file())


if __name__ == "__main__":
    unittest.main()
