"""远程采集入库测试：base64 图片 → 服务器解包 → 入库，以及本地推送工具（全离线）。"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.push_capture import PushError, build_capture_payload, main as push_main, push_capture  # noqa: E402

try:
    import api as api_module
    from fastapi.testclient import TestClient

    HAS_DEPS = True
except ImportError:  # pragma: no cover
    HAS_DEPS = False

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


def make_folder(root: pathlib.Path, *, images: int = 3, skus: bool = True) -> pathlib.Path:
    folder = root / "capture-123"
    for role, name in (("main", "main-images"), ("sku", "sku-images"), ("detail", "detail-images")):
        directory = folder / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "01.png").write_bytes(PNG + role.encode())
    descriptor = {
        "source_url": "https://detail.1688.com/offer/717171717.html",
        "title_zh": "纯棉床单",
        "category": {"category_id": "17028922", "type_id": "91875"},
        "keywords": ["простынь на резинке 160х200"],
    }
    if skus:
        descriptor["skus"] = [{"sku_id": "S1", "purchase_price_cny": 18.5}]
    (folder / "product.json").write_text(json.dumps(descriptor, ensure_ascii=False), encoding="utf-8")
    return folder


class PayloadBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.folder = make_folder(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_base64_payload(self):
        payload = build_capture_payload(self.folder)
        stats = payload.pop("_stats")
        self.assertEqual(payload["source_url"], "https://detail.1688.com/offer/717171717.html")
        self.assertEqual(payload["skus"][0]["sku_id"], "S1")
        self.assertEqual(payload["keywords"], ["простынь на резинке 160х200"])
        self.assertEqual(stats["roles"], {"main": 1, "sku": 1, "detail": 1})
        self.assertEqual(stats["images"], 3)
        decoded = base64.b64decode(payload["images"]["main"][0]["data_base64"])
        self.assertTrue(decoded.startswith(b"\x89PNG"))

    def test_cli_overrides_and_missing_bits(self):
        payload = build_capture_payload(
            self.folder,
            source_url="https://detail.1688.com/offer/818181818.html",
            category={"category_id": "1", "type_id": "2"},
            skus=[{"sku_id": "S9", "purchase_price_cny": 9.9}],
        )
        payload.pop("_stats")
        self.assertEqual(payload["source_url"], "https://detail.1688.com/offer/818181818.html")
        self.assertEqual(payload["skus"][0]["sku_id"], "S9")
        self.assertEqual(payload["category"], {"category_id": "1", "type_id": "2"})

        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(PushError):
            build_capture_payload(empty)

        # 有 product.json 但没写 skus → 明确报 SKU 缺失
        folder2 = self.root / "capture-456"
        (folder2 / "main-images").mkdir(parents=True)
        (folder2 / "main-images" / "a.png").write_bytes(PNG)
        (folder2 / "product.json").write_text(
            json.dumps({"source_url": "https://detail.1688.com/offer/616161616.html"}), encoding="utf-8"
        )
        with self.assertRaises(PushError) as ctx:
            build_capture_payload(folder2)
        self.assertIn("SKU", str(ctx.exception))

        # 连 source_url 都没有时，先报 source_url
        folder3 = self.root / "capture-789"
        (folder3 / "main-images").mkdir(parents=True)
        (folder3 / "main-images" / "a.png").write_bytes(PNG)
        with self.assertRaises(PushError) as ctx3:
            build_capture_payload(folder3)
        self.assertIn("source_url", str(ctx3.exception))

    def test_oversize_single_image_is_rejected(self):
        big = self.folder / "main-images" / "big.png"
        big.write_bytes(PNG + b"x" * 2048)
        with self.assertRaises(PushError) as ctx:
            build_capture_payload(self.folder, max_image_bytes=1024)
        self.assertIn("太大", str(ctx.exception))

    def test_dry_run_cli(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = push_main(["--folder", str(self.folder), "--dry-run"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["stats"]["images"], 3)


class PushErrorTests(unittest.TestCase):
    def test_connection_error_mentions_tunnel(self):
        import urllib.error

        def failing(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        tmp = tempfile.TemporaryDirectory()
        try:
            folder = make_folder(pathlib.Path(tmp.name))
            with self.assertRaises(PushError) as ctx:
                push_capture(folder, api="http://127.0.0.1:9", opener=failing)
            self.assertIn("隧道", str(ctx.exception))
        finally:
            tmp.cleanup()

    def test_http_error_is_reported(self):
        import urllib.error

        class Body:
            """假的错误响应体：Python 3.14 回收 HTTPError 时会调 close()，所以必须实现。"""

            def read(self):
                return b'{"detail":"duplicate"}'

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def failing(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, Body())

        tmp = tempfile.TemporaryDirectory()
        try:
            folder = make_folder(pathlib.Path(tmp.name))
            with self.assertRaises(PushError) as ctx:
                push_capture(folder, opener=failing)
            self.assertIn("HTTP 409", str(ctx.exception))
        finally:
            tmp.cleanup()


@unittest.skipUnless(HAS_DEPS, "需要 fastapi 与 httpx")
class CaptureEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        api_module.PRODUCTS_ROOT = self.root / "products"
        api_module.LIBRARY_ROOT = self.root / "keyword-library"
        self.client = TestClient(api_module.app)
        self.folder = make_folder(self.root)
        self.payload = build_capture_payload(self.folder)
        self.payload.pop("_stats")

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_upload_end_to_end(self):
        response = self.client.post("/api/collector/products/capture", json=self.payload)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        product_id = body["product_id"]
        self.assertEqual(body["via"], "capture-upload")

        product_dir = api_module.PRODUCTS_ROOT / product_id
        self.assertTrue((product_dir / "input" / "source.json").is_file())
        source = json.loads((product_dir / "input" / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["title_zh"], "纯棉床单")
        self.assertEqual(source["keywords"][0]["keyword"], "простынь на резинке 160х200")
        self.assertEqual(source["selected_category"]["category_id"], "17028922")
        # 图片落盘并计数
        self.assertTrue(any((product_dir / "input" / "main-images").iterdir()))
        self.assertEqual(source["images"]["main"], 1)
        # 关键词直接写好，可跳过手工选词
        selection = json.loads((product_dir / "input" / "selected-keywords.json").read_text(encoding="utf-8"))
        self.assertEqual(selection["keywords"][0]["keyword"], "простынь на резинке 160х200")

    def test_duplicate_returns_409(self):
        first = self.client.post("/api/collector/products/capture", json=self.payload)
        self.assertEqual(first.status_code, 200, first.text)
        again = self.client.post("/api/collector/products/capture", json=self.payload)
        self.assertEqual(again.status_code, 409, again.text)
        detail = again.json()["detail"]
        self.assertEqual(detail["duplicate_of"], first.json()["product_id"])
        self.assertIn("create_new_version", detail["options"])

    def test_new_version_flag_allows_second(self):
        self.client.post("/api/collector/products/capture", json=self.payload)
        payload = {**self.payload, "allow_new_version": True}
        response = self.client.post("/api/collector/products/capture", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], 2)

    def test_validation_errors(self):
        bad_role = {**self.payload, "images": {"weird": [{"name": "a.png", "data_base64": "AAAA"}]}}
        self.assertEqual(self.client.post("/api/collector/products/capture", json=bad_role).status_code, 422)

        bad_b64 = {**self.payload, "images": {"main": [{"name": "a.png", "data_base64": "not base64!!"}]}}
        self.assertEqual(self.client.post("/api/collector/products/capture", json=bad_b64).status_code, 422)

        no_images = {**self.payload, "images": {}}
        self.assertEqual(self.client.post("/api/collector/products/capture", json=no_images).status_code, 422)

        empty_image = {**self.payload, "images": {"main": [{"name": "a.png", "data_base64": ""}]}}
        self.assertEqual(self.client.post("/api/collector/products/capture", json=empty_image).status_code, 422)

    def test_size_limit(self):
        original = api_module.MAX_CAPTURE_BYTES
        api_module.MAX_CAPTURE_BYTES = 10
        try:
            response = self.client.post("/api/collector/products/capture", json=self.payload)
            self.assertEqual(response.status_code, 413, response.text)
            self.assertIn("上限", response.json()["detail"])
        finally:
            api_module.MAX_CAPTURE_BYTES = original

    def test_too_many_skus_rejected_by_schema(self):
        payload = {
            **self.payload,
            "skus": [{"sku_id": f"S{i}", "purchase_price_cny": 1.0} for i in range(11)],
        }
        self.assertEqual(self.client.post("/api/collector/products/capture", json=payload).status_code, 422)

    def test_filename_is_sanitized(self):
        payload = {
            **self.payload,
            "images": {"main": [{"name": "../../evil name.png", "data_base64": base64.b64encode(PNG).decode()}]},
        }
        response = self.client.post("/api/collector/products/capture", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        product_dir = api_module.PRODUCTS_ROOT / response.json()["product_id"]
        stored = list((product_dir / "input" / "main-images").iterdir())
        self.assertEqual(len(stored), 1)
        self.assertNotIn("..", stored[0].name)
        self.assertNotIn(" ", stored[0].name)


if __name__ == "__main__":
    unittest.main()
