"""1688 抓取 JSON → 素材文件夹测试（用假 opener，全离线）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.fetch_images import (  # noqa: E402
    DEFAULT_REFERER,
    FetchError,
    detect_extension,
    fetch_file,
    fetch_folder,
    load_capture,
    main as fetch_main,
)
from collector.push_capture import build_capture_payload  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body"
GIF = b"GIF89a" + b"fake-gif-body"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"fake-webp-body"
NOT_IMAGE = b"<html>not an image</html>"


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """按 URL 返回内容；记录请求头（用于验证 Referer）。"""

    def __init__(self, mapping: dict[str, bytes], *, fail: set[str] | None = None):
        self.mapping = mapping
        self.fail = fail or set()
        self.requests: list[dict] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(
            {"url": url, "referer": request.get_header("Referer"), "ua": request.get_header("User-agent")}
        )
        if url in self.fail:
            import urllib.error

            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if url not in self.mapping:
            raise AssertionError(f"测试没准备这个 URL：{url}")
        return FakeResponse(self.mapping[url])


def capture_doc() -> dict:
    return {
        "source_url": "https://detail.1688.com/offer/808080808.html",
        "title_zh": "纯棉床单四件套",
        "skus": [
            {"sku_id": "白色 200x200", "purchase_price_cny": 42.0},
            {"sku_id": "灰色 200x200", "purchase_price_cny": 45.0},
        ],
        "keywords": ["простынь на резинке 160х200"],
        "images": {
            "main": ["https://cbu01.alicdn.com/img/main1.jpg", "https://cbu01.alicdn.com/img/main2.jpg"],
            "sku": ["https://cbu01.alicdn.com/img/sku1.jpg"],
            "detail": ["https://cbu01.alicdn.com/img/detail1.jpg", "https://cbu01.alicdn.com/img/detail2.jpg"],
        },
    }


def mapping_for(doc: dict) -> dict[str, bytes]:
    bodies = {
        "main1": PNG,
        "main2": JPEG,
        "sku1": GIF,
        "detail1": WEBP,
        "detail2": PNG + b"-detail2",
    }
    mapping: dict[str, bytes] = {}
    for urls in doc["images"].values():
        for url in urls:
            key = url.rsplit("/", 1)[-1].split(".")[0]
            mapping[url] = bodies[key]
    return mapping


class DetectionTests(unittest.TestCase):
    def test_magic_based_detection(self):
        self.assertEqual(detect_extension(PNG), ".png")
        self.assertEqual(detect_extension(JPEG), ".jpg")
        self.assertEqual(detect_extension(GIF), ".gif")
        self.assertEqual(detect_extension(WEBP), ".webp")
        self.assertIsNone(detect_extension(NOT_IMAGE))
        # RIFF 但不是 WEBP（例如 wav）不应被当成图片
        self.assertIsNone(detect_extension(b"RIFF\x00\x00\x00\x00WAVEfmt "))


class LoadCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload) -> pathlib.Path:
        path = self.root / "capture.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_accepts_mapping_form(self):
        doc = load_capture(self.write(capture_doc()))
        self.assertEqual(len(doc["images"]["main"]), 2)
        self.assertEqual(doc["title_zh"], "纯棉床单四件套")

    def test_accepts_flat_list_form(self):
        path = self.write({"source_url": "https://detail.1688.com/offer/1.html", "images": ["https://a/1.jpg"]})
        doc = load_capture(path)
        self.assertEqual(doc["images"], {"main": ["https://a/1.jpg"]})

    def test_dedupes_and_ignores_non_http(self):
        path = self.write(
            {
                "source_url": "https://detail.1688.com/offer/1.html",
                "images": {"main": ["https://a/1.jpg", "https://a/1.jpg", "javascript:void(0)", {"url": "https://a/2.jpg"}]},
            }
        )
        doc = load_capture(path)
        self.assertEqual(doc["images"]["main"], ["https://a/1.jpg", "https://a/2.jpg"])

    def test_errors_are_explicit(self):
        with self.assertRaises(FetchError):
            load_capture(self.root / "missing.json")
        with self.assertRaises(FetchError):
            load_capture(self.write({"images": {"main": []}}))
        with self.assertRaises(FetchError):
            load_capture(self.write({"source_url": "https://x", "images": {"main": []}}))
        with self.assertRaises(FetchError):
            load_capture(self.write([1, 2, 3]))


class FetchFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.doc = capture_doc()
        self.opener = FakeOpener(mapping_for(self.doc))

    def tearDown(self):
        self.tmp.cleanup()

    def test_downloads_into_role_folders(self):
        summary = fetch_folder(self.doc, self.root / "capture", opener=self.opener)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["downloaded"], 5)
        self.assertEqual(summary["planned"], {"main": 2, "sku": 1, "detail": 2})
        for relative, expected in (
            ("main-images/01.png", PNG),
            ("main-images/02.jpg", JPEG),
            ("sku-images/01.gif", GIF),
            ("detail-images/01.webp", WEBP),
        ):
            target = self.root / "capture" / relative
            self.assertTrue(target.is_file(), relative)
            self.assertEqual(target.read_bytes(), expected)

        descriptor = json.loads((self.root / "capture" / "product.json").read_text(encoding="utf-8"))
        self.assertEqual(descriptor["source_url"], self.doc["source_url"])
        self.assertEqual(len(descriptor["skus"]), 2)
        self.assertEqual(descriptor["keywords"], ["простынь на резинке 160х200"])

        # Referer 必须带上（1688 CDN 会校验），否则图片会被拒
        self.assertTrue(all(item["referer"] == DEFAULT_REFERER for item in self.opener.requests))
        self.assertTrue(all("Chrome" in (item["ua"] or "") for item in self.opener.requests))

    def test_downloaded_folder_feeds_push_capture(self):
        """端到端接上：下载出来的文件夹要能被 push_capture 打包。"""
        folder = self.root / "capture"
        fetch_folder(self.doc, folder, opener=self.opener)
        payload = build_capture_payload(folder)
        stats = payload.pop("_stats")
        self.assertEqual(stats["roles"], {"main": 2, "sku": 1, "detail": 2})
        self.assertEqual(payload["source_url"], self.doc["source_url"])
        self.assertEqual(payload["keywords"], ["простынь на резинке 160х200"])

    def test_duplicate_content_is_skipped(self):
        doc = {
            "source_url": "https://detail.1688.com/offer/2.html",
            "images": {"main": ["https://a/1.png", "https://a/2.png"]},
        }
        opener = FakeOpener({"https://a/1.png": PNG, "https://a/2.png": PNG})
        summary = fetch_folder(doc, self.root / "dup", opener=opener)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertIn("内容相同", summary["skipped"][0]["reason"])

    def test_http_error_and_non_image_are_skipped_not_faked(self):
        doc = {
            "source_url": "https://detail.1688.com/offer/3.html",
            "images": {"main": ["https://a/403.jpg", "https://a/html.jpg", "https://a/ok.png"]},
        }
        opener = FakeOpener(
            {"https://a/html.jpg": NOT_IMAGE, "https://a/ok.png": PNG}, fail={"https://a/403.jpg"}
        )
        summary = fetch_folder(doc, self.root / "mixed", opener=opener)
        self.assertEqual(summary["downloaded"], 1)
        reasons = {item["url"]: item["reason"] for item in summary["skipped"]}
        self.assertIn("HTTP 403", reasons["https://a/403.jpg"])
        self.assertIn("不是图片", reasons["https://a/html.jpg"])

    def test_oversize_image_is_skipped(self):
        doc = {"source_url": "https://detail.1688.com/offer/4.html", "images": {"main": ["https://a/big.png"]}}
        opener = FakeOpener({"https://a/big.png": PNG + b"x" * 2048})
        summary = fetch_folder(doc, self.root / "big", opener=opener, max_image_bytes=1024)
        self.assertEqual(summary["downloaded"], 0)
        self.assertIn("上限", summary["skipped"][0]["reason"])

    def test_dry_run_writes_nothing(self):
        summary = fetch_folder(self.doc, self.root / "dry", opener=self.opener, dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["downloaded"], 0)
        self.assertFalse((self.root / "dry" / "product.json").exists())
        self.assertEqual(self.opener.requests, [])
        self.assertIn("push_capture", summary["next"])

    def test_role_limit(self):
        doc = {
            "source_url": "https://detail.1688.com/offer/5.html",
            "images": {"main": [f"https://a/{index}.png" for index in range(40)]},
        }
        doc = load_capture_path(doc, self.root)
        self.assertEqual(len(doc["images"]["main"]), 30)

    def test_unknown_role_is_reported(self):
        doc = {"source_url": "https://detail.1688.com/offer/6.html", "images": {"main": ["https://a/1.png"], "weird": ["https://a/2.png"]}}
        opener = FakeOpener({"https://a/1.png": PNG})
        summary = fetch_folder(doc, self.root / "roles", opener=opener)
        self.assertTrue(any("未知图片角色" in item["reason"] for item in summary["skipped"]))


def load_capture_path(payload: dict, root: pathlib.Path) -> dict:
    path = root / "t.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_capture(path)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "capture.json"
        self.path.write_text(json.dumps(capture_doc(), ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_dry_run(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = fetch_main(["--json", str(self.path), "--out", str(self.root / "out"), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("计划下载", buffer.getvalue())
        self.assertIn('"main": 2', buffer.getvalue())

    def test_cli_reports_bad_json(self):
        import io
        from contextlib import redirect_stdout

        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = fetch_main(["--json", str(bad), "--out", str(self.root / "out")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])


class LocalHttpTests(unittest.TestCase):
    """真 HTTP 端到端：本机起一个临时图片服务器，用真实 urllib 下载（不碰外网）。"""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            bodies = {
                "/main1.png": PNG,
                "/detail1.jpg": JPEG,
                "/blocked.png": NOT_IMAGE,
            }
            seen_headers: list[dict] = []

            def do_GET(self):  # noqa: N802 - http.server 接口
                Handler.seen_headers.append({key.lower(): value for key, value in self.headers.items()})
                if self.path == "/403.png":
                    self.send_error(403, "Forbidden")
                    return
                body = Handler.bodies.get(self.path)
                if body is None:
                    self.send_error(404, "Not Found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 静音
                return

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.handler_cls = Handler
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_downloads_over_real_http_with_referer(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            doc = {
                "source_url": "https://detail.1688.com/offer/717171717.html",
                "title_zh": "真实 HTTP 测试",
                "images": {
                    "main": [f"{self.base}/main1.png", f"{self.base}/403.png", f"{self.base}/blocked.png"],
                    "detail": [f"{self.base}/detail1.jpg"],
                },
            }
            summary = fetch_folder(doc, pathlib.Path(tmp.name) / "capture", referer="https://detail.1688.com/")
            self.assertEqual(summary["downloaded"], 2)
            self.assertEqual((pathlib.Path(tmp.name) / "capture" / "main-images" / "01.png").read_bytes(), PNG)
            self.assertEqual((pathlib.Path(tmp.name) / "capture" / "detail-images" / "01.jpg").read_bytes(), JPEG)
            reasons = {item["url"]: item["reason"] for item in summary["skipped"]}
            self.assertIn("HTTP 403", reasons[f"{self.base}/403.png"])
            self.assertIn("不是图片", reasons[f"{self.base}/blocked.png"])
            # 请求头确实带了 Referer（1688 CDN 会校验）
            self.assertTrue(
                any(item.get("referer") == "https://detail.1688.com/" for item in self.handler_cls.seen_headers)
            )
        finally:
            tmp.cleanup()

    def test_backend_missing_url_reports_404(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            doc = {
                "source_url": "https://detail.1688.com/offer/818181818.html",
                "images": {"main": [f"{self.base}/nope.png"]},
            }
            summary = fetch_folder(doc, pathlib.Path(tmp.name) / "capture")
            self.assertEqual(summary["downloaded"], 0)
            self.assertIn("HTTP 404", summary["skipped"][0]["reason"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
