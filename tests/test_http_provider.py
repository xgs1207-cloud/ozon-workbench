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
                    {"sku_id": "S1", "color_zh": "红色", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
                    {"sku_id": "S2", "color_zh": "蓝色", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
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
        broken = json.dumps({"schema_version": "1.0.0"})  # 缺大量必填
        transport = ScriptedTransport([broken, json.dumps(self.analysis(), ensure_ascii=False)])
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
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("上一次输出不合法", transport.calls[1]["user"])
        self.assertIn("product-analysis", transport.calls[1]["user"])
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(provider.calls[0]["ok"])
        self.assertTrue(provider.calls[1]["ok"])

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
        # 降级事实留在调用轨迹里（契约是 additionalProperties:false，不能塞额外字段）
        self.assertFalse(provider.calls[-1]["ok"])
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
