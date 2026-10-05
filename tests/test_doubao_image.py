"""豆包（火山方舟）生图适配器测试：请求构建、参考图编码、响应处理、归一化、计划驱动（全离线）。"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from models import ModelError, load_image_generator  # noqa: E402
from models.base import ImageRequest  # noqa: E402
from models.doubao_image import (  # noqa: E402
    DEFAULT_MODEL,
    DoubaoImageGenerator,
    ArkImageTransport,
    normalize_to_qc_png,
)
from models.fake import FakeProvider  # noqa: E402
from models.image_plan import build_image_plan  # noqa: E402
from pipeline.image_probe import probe_image, write_solid_png  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"

try:
    import PIL  # noqa: F401

    HAS_PILLOW = True
except ImportError:  # pragma: no cover
    HAS_PILLOW = False


def png_bytes(width: int = 1024, height: int = 1024) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        path = write_solid_png(pathlib.Path(directory) / "x.png", width, height)
        return path.read_bytes()


class RecordingTransport:
    """假的 Ark 传输层：记录请求、返回脚本化响应。"""

    def __init__(self, responses, *, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, *, prompt, images=(), size=None, max_attempts=2):
        self.calls.append({"prompt": prompt, "images": list(images), "size": size})
        result = self.responses.pop(0) if self.responses else [png_bytes()]
        if isinstance(result, Exception):
            raise result
        return result


class ArkTransportTests(unittest.TestCase):
    def test_request_shape(self):
        captured: dict = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"data": [{"b64_json": base64.b64encode(b"PNGDATA").decode()}], "model": "m"}).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        transport = ArkImageTransport(api_key="ark-key", model="ep-123", base_url="https://ark.example.com/api/v3", urlopen=fake_urlopen)
        images = transport.generate(prompt="фото товара", images=["data:image/png;base64,AAA"], size="1152x1536")

        self.assertEqual(images, [b"PNGDATA"])
        self.assertEqual(captured["url"], "https://ark.example.com/api/v3/images/generations")
        self.assertEqual(captured["headers"]["authorization"], "Bearer ark-key")
        self.assertEqual(captured["body"]["model"], "ep-123")
        self.assertEqual(captured["body"]["prompt"], "фото товара")
        self.assertEqual(captured["body"]["size"], "1152x1536")
        self.assertEqual(captured["body"]["watermark"], False)
        self.assertEqual(captured["body"]["response_format"], "b64_json")
        self.assertEqual(captured["body"]["image"], "data:image/png;base64,AAA")

    def test_url_response_is_downloaded(self):
        calls: list[str] = []

        class FakeResponse:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.payload

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if request.method == "GET":
                return FakeResponse(b"IMAGEBYTES")
            return FakeResponse(json.dumps({"data": [{"url": "https://cdn.example.com/a.png"}]}).encode())

        transport = ArkImageTransport(api_key="k", urlopen=fake_urlopen)
        images = transport.generate(prompt="p")
        self.assertEqual(images, [b"IMAGEBYTES"])
        self.assertEqual(calls[1], "https://cdn.example.com/a.png")

    def test_error_envelope_and_http_error_are_raised(self):
        class ErrorResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"error": {"code": "InvalidParameter", "message": "size not supported"}}).encode()

        transport = ArkImageTransport(api_key="k", urlopen=lambda request, timeout=None: ErrorResponse())
        with self.assertRaises(ModelError) as ctx:
            transport.generate(prompt="p")
        self.assertIn("InvalidParameter", str(ctx.exception))
        self.assertIn("size not supported", str(ctx.exception))

    def test_missing_api_key_is_rejected(self):
        with self.assertRaises(ModelError):
            ArkImageTransport(api_key="")
        with self.assertRaises(ModelError):
            DoubaoImageGenerator.from_env({})
        with self.assertRaises(ModelError) as ctx:
            load_image_generator("doubao", env={})
        self.assertIn("ARK_API_KEY", str(ctx.exception))

    def test_bad_base_url_rejected(self):
        with self.assertRaises(ModelError):
            ArkImageTransport(api_key="k", base_url="ark.example.com")

    def test_transport_retries_on_429(self):
        class FakeResponse:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.payload

        import urllib.error

        attempts: list[int] = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
            return FakeResponse(json.dumps({"data": [{"b64_json": base64.b64encode(b"OK").decode()}]}).encode())

        transport = ArkImageTransport(api_key="k", urlopen=fake_urlopen)
        self.assertEqual(transport.generate(prompt="p", max_attempts=2), [b"OK"])
        self.assertEqual(len(attempts), 2)

    def test_transport_does_not_retry_other_4xx(self):
        import urllib.error

        attempts: list[int] = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, None)

        transport = ArkImageTransport(api_key="k", urlopen=fake_urlopen)
        with self.assertRaises(ModelError):
            transport.generate(prompt="p", max_attempts=3)
        self.assertEqual(len(attempts), 1)


@unittest.skipUnless(HAS_PILLOW, "归一化需要 Pillow")
class NormalizeTests(unittest.TestCase):
    def test_square_image_is_cropped_to_3x4_and_scaled(self):
        normalized = normalize_to_qc_png(png_bytes(1024, 1024))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "n.png"
            path.write_bytes(normalized)
            probe = probe_image(path)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["format"], "png")
        self.assertEqual((probe["width"], probe["height"]), (900, 1200))

    def test_landscape_image_is_cropped(self):
        normalized = normalize_to_qc_png(png_bytes(1200, 800))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "n.png"
            path.write_bytes(normalized)
            probe = probe_image(path)
        self.assertEqual((probe["width"], probe["height"]), (900, 1200))

    def test_small_image_is_upscaled(self):
        normalized = normalize_to_qc_png(png_bytes(300, 400))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "n.png"
            path.write_bytes(normalized)
            probe = probe_image(path)
        self.assertEqual((probe["width"], probe["height"]), (900, 1200))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/929292929.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.5},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.product_dir = self.products / summary["product_id"]
        for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
            directory = self.product_dir / "input" / relative
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        provider = FakeProvider()
        from pipeline.handlers import run_single_step

        run_single_step(self.product_dir, "product_analysis", provider=provider)
        run_single_step(self.product_dir, "russian_copy", provider=provider)
        run_single_step(self.product_dir, "image_plan", provider=provider)
        self.plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_generate_writes_every_slot_to_planned_path(self):
        expected_slots = [item["slot"] for item in self.plan["main_images"] + self.plan["detail_images"]]
        transport = RecordingTransport([[png_bytes(1024, 1024)] for _ in expected_slots])
        generator = DoubaoImageGenerator(transport, normalize=HAS_PILLOW)
        result = generator.generate(
            ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={})
        )

        self.assertEqual(result["generator"], "doubao")
        self.assertTrue(result["final_images"])
        self.assertEqual([item["slot"] for item in result["generated"]], expected_slots)
        self.assertEqual(len(transport.calls), len(expected_slots))
        for item in result["generated"]:
            probe = probe_image(self.product_dir / item["path"])
            self.assertTrue(probe["ok"], item)
            if HAS_PILLOW:
                self.assertEqual((probe["width"], probe["height"]), (900, 1200))

    def test_reference_images_are_attached_as_data_urls(self):
        transport = RecordingTransport([[png_bytes()] for _ in range(20)])
        generator = DoubaoImageGenerator(transport, normalize=False)
        generator.generate(ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={}))
        first = transport.calls[0]
        self.assertTrue(first["images"])
        self.assertTrue(first["images"][0].startswith("data:image/png;base64,"))
        self.assertLessEqual(len(first["images"]), 3)

    def test_no_reference_flag(self):
        transport = RecordingTransport([[png_bytes()] for _ in range(20)])
        generator = DoubaoImageGenerator(transport, normalize=False, use_reference=False)
        generator.generate(ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={}))
        self.assertEqual(transport.calls[0]["images"], [])

    def test_slot_filter_and_limit(self):
        transport = RecordingTransport([[png_bytes()] for _ in range(5)])
        generator = DoubaoImageGenerator(transport, normalize=False, slot_filter=["main-S1"])
        result = generator.generate(
            ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={})
        )
        self.assertEqual([item["slot"] for item in result["generated"]], ["main-S1"])
        self.assertEqual(len(transport.calls), 1)

    def test_partial_failures_are_reported(self):
        transport = RecordingTransport([ModelError("第一张失败"), [png_bytes()]])
        generator = DoubaoImageGenerator(transport, normalize=False, slot_filter=["main-S1", "main-S2"])
        result = generator.generate(
            ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={})
        )
        self.assertEqual(len(result["generated"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("第一张失败", result["skipped"][0]["reason"])

    def test_all_failures_raise(self):
        transport = RecordingTransport([ModelError("全挂") for _ in range(5)])
        generator = DoubaoImageGenerator(transport, normalize=False, slot_filter=["main-S1"])
        with self.assertRaises(ModelError) as ctx:
            generator.generate(ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={}))
        self.assertIn("全部失败", str(ctx.exception))

    def test_missing_plan_raises(self):
        (self.product_dir / "output" / "image-plan.json").unlink()
        generator = DoubaoImageGenerator(RecordingTransport([]), normalize=False)
        with self.assertRaises(ModelError) as ctx:
            generator.generate(ImageRequest(product_id=self.product_dir.name, product_dir=self.product_dir, source={}))
        self.assertIn("image-plan.json", str(ctx.exception))

    def test_from_env_builds_generator(self):
        generator = DoubaoImageGenerator.from_env(
            {"ARK_API_KEY": "ark-key", "ARK_IMAGE_MODEL": "ep-999", "ARK_IMAGE_SIZE": "2K"}
        )
        self.assertEqual(generator.transport.model, "ep-999")
        self.assertEqual(generator.size, "2K")
        self.assertTrue(generator.produces_final_images)

    def test_load_image_generator_names(self):
        self.assertEqual(load_image_generator("placeholder").name, "local-placeholder")
        self.assertEqual(load_image_generator("doubao", env={"ARK_API_KEY": "k"}).name, "doubao")
        self.assertIsNone(load_image_generator("none"))
        with self.assertRaises(ModelError):
            load_image_generator("midjourney")

    def test_cli_show_request_does_not_call_api(self):
        from models.doubao_image import main

        import io
        import os
        from contextlib import redirect_stdout

        os.environ["ARK_API_KEY"] = "ark-key"
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--product-dir", str(self.product_dir), "--show-request", "--limit", "2"])
            self.assertEqual(code, 0)
            printed = json.loads(buffer.getvalue())
            self.assertTrue(printed["ok"])
            self.assertFalse(printed["api_writes_performed"])
            self.assertEqual(len(printed["slots"]), 2)
            self.assertTrue(printed["slots"][0]["prompt_chars"] > 0)
        finally:
            os.environ.pop("ARK_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
