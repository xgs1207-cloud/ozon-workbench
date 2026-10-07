"""腾讯云 COS 对象存储适配器测试（用假客户端，全离线）。

真实的签名/上传路径要等你给 COS 凭据后跑 `python -m pipeline.oss_cos --check` 才算验证过；
这里锁的是：键布局、URL 生成、增量跳过、映射文件、错误提示、自检流程。
"""

from __future__ import annotations

import json
import hashlib
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.local_image import LocalPlaceholderGenerator  # noqa: E402
from pipeline.context import StepContext  # noqa: E402
from pipeline.handlers import run_single_step  # noqa: E402
from pipeline.image_generation import handle_image_generation  # noqa: E402
from pipeline.oss_cos import (  # noqa: E402
    CHECK_BODY,
    CHECK_KEY,
    CosError,
    CosObjectStorage,
    config_from_env,
    content_type_for,
    planned_slots,
)
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.upload import build_upload_payload, resolve_image_urls  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
ENV = {
    "COS_SECRET_ID": "AKIDexample",
    "COS_SECRET_KEY": "secret",
    "COS_BUCKET": "ozon-images-1250000000",
    "COS_REGION": "ap-hongkong",
    "COS_KEY_PREFIX": "ozon-images",
}


class FakeCosClient:
    """记录调用的假 COS 客户端；可脚本化让某些调用抛错。"""

    def __init__(self, *, existing: dict[str, int] | None = None, fail_put: int = 0) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.sizes = dict(existing or {})
        self.calls: list[tuple[str, dict]] = []
        self.fail_put = fail_put

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        key = kwargs["Key"]
        size = self.sizes.get(key)
        if size is None:
            raise RuntimeError("NoSuchKey: 404")
        head = {"Content-Length": str(size), **self.metadata.get(key, {})}
        if key in self.objects:
            head["ETag"] = f'"{hashlib.md5(self.objects[key], usedforsecurity=False).hexdigest()}"'
        return head

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        if self.fail_put > 0:
            self.fail_put -= 1
            raise RuntimeError("500 InternalError")
        self.objects[kwargs["Key"]] = kwargs["Body"]
        self.sizes[kwargs["Key"]] = len(kwargs["Body"])
        self.metadata[kwargs["Key"]] = dict(kwargs.get("Metadata") or {})
        return {"ETag": f'"{hashlib.md5(kwargs["Body"], usedforsecurity=False).hexdigest()}"'}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self.objects.pop(kwargs["Key"], None)
        self.sizes.pop(kwargs["Key"], None)
        self.metadata.pop(kwargs["Key"], None)
        return {}

    def get_object(self, **kwargs):  # pragma: no cover - 仅接口完整性
        self.calls.append(("get_object", kwargs))
        return {"Body": self.objects.get(kwargs["Key"], b"")}


class ConfigTests(unittest.TestCase):
    def test_missing_env_lists_every_key(self):
        with self.assertRaises(CosError) as ctx:
            config_from_env({})
        message = str(ctx.exception)
        for key in ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET", "COS_REGION"):
            self.assertIn(key, message)

    def test_defaults_and_overrides(self):
        config = config_from_env(ENV)
        self.assertEqual(config["key_prefix"], "ozon-images")
        self.assertIsNone(config["public_base_url"])
        self.assertEqual(config["scheme"], "https")

        config = config_from_env({**ENV, "COS_KEY_PREFIX": "/a/b/", "COS_PUBLIC_BASE_URL": "https://img.example.com/"})
        self.assertEqual(config["key_prefix"], "a/b")
        self.assertEqual(config["public_base_url"], "https://img.example.com")

    def test_content_types(self):
        self.assertEqual(content_type_for("a.png"), "image/png")
        self.assertEqual(content_type_for("a.JPG"), "image/jpeg")
        self.assertEqual(content_type_for("a.bin"), "application/octet-stream")


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/313131313.html",
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
        run_single_step(self.product_dir, "product_analysis", provider=provider)
        run_single_step(self.product_dir, "russian_copy", provider=provider)
        run_single_step(self.product_dir, "image_plan", provider=provider)
        self.client = FakeCosClient()
        self.storage = CosObjectStorage(
            self.client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"], key_prefix="ozon-images"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def generate(self) -> None:
        handle_image_generation(
            StepContext(
                product_dir=self.product_dir,
                step="image_generation",
                image_generator=LocalPlaceholderGenerator(),
            )
        )


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class UploadTests(StorageFixture):
    def test_key_and_url_layout(self):
        uuid = self.product_dir.name
        self.assertEqual(self.storage.key_for(uuid, "main-S1"), f"ozon-images/{uuid}/main-S1.png")
        self.assertEqual(
            self.storage.url_for(uuid, "main-S1"),
            f"https://ozon-images-1250000000.cos.ap-hongkong.myqcloud.com/ozon-images/{uuid}/main-S1.png",
        )

    def test_custom_public_base_url(self):
        storage = CosObjectStorage(
            self.client,
            bucket=ENV["COS_BUCKET"],
            region=ENV["COS_REGION"],
            key_prefix="ozon-images",
            public_base_url="https://cdn.du4s.com",
        )
        url = storage.url_for("P000001", "detail-001")
        self.assertTrue(url.startswith("https://cdn.du4s.com/ozon-images/P000001/detail-001.png"))

    def test_uploads_every_slot_and_writes_urls(self):
        self.generate()
        summary = self.storage.publish_product(self.product_dir)
        self.assertEqual(summary["slots"], 10)
        self.assertEqual(summary["uploaded"], 10)
        self.assertEqual(summary["missing"], [])
        self.assertTrue(summary["https_ok"])
        self.assertEqual(len(self.client.objects), 10)

        urls = resolve_image_urls(self.product_dir)
        self.assertEqual(len(urls), 10)
        self.assertTrue(all(value.startswith("https://") for value in urls.values()))
        saved = json.loads((self.product_dir / "output" / "image-public-urls.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["storage"], "tencent-cos")
        self.assertEqual(saved["bucket"], ENV["COS_BUCKET"])

        # content-type 与缓存头要带上（Ozon 抓取体验）
        put_calls = [kwargs for name, kwargs in self.client.calls if name == "put_object"]
        self.assertEqual(put_calls[0]["ContentType"], "image/png")
        self.assertIn("max-age", put_calls[0]["CacheControl"])
        self.assertTrue(put_calls[0]["EnableMD5"])
        self.assertEqual(
            put_calls[0]["Metadata"]["x-cos-meta-sha256"],
            hashlib.sha256(put_calls[0]["Body"]).hexdigest(),
        )

    def test_second_run_skips_unchanged(self):
        self.generate()
        self.storage.publish_product(self.product_dir)
        second = self.storage.publish_product(self.product_dir)
        self.assertEqual(second["uploaded"], 0)
        self.assertEqual(second["unchanged"], 10)

    def test_changed_file_is_reuploaded(self):
        self.generate()
        self.storage.publish_product(self.product_dir)
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        from pipeline.image_probe import write_solid_png

        write_solid_png(self.product_dir / plan["main_images"][0]["output_path"], 900, 1200)
        summary = self.storage.publish_product(self.product_dir)
        self.assertEqual(summary["uploaded"], 1)
        self.assertEqual(summary["unchanged"], 9)

    def test_same_size_changed_bytes_get_new_key_and_public_url(self):
        self.generate()
        first = self.storage.publish_product(self.product_dir, slots=["main-S1"])
        old = first["results"][0]
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        source = self.product_dir / plan["main_images"][0]["output_path"]
        original = source.read_bytes()
        changed = original[:-1] + bytes([original[-1] ^ 1])
        self.assertEqual(len(original), len(changed))
        source.write_bytes(changed)

        second = self.storage.publish_product(self.product_dir, slots=["main-S1"])
        new = second["results"][0]
        self.assertEqual(second["uploaded"], 1)
        self.assertEqual(second["unchanged"], 0)
        self.assertNotEqual(old["key"], new["key"])
        self.assertNotEqual(old["url"], new["url"])
        self.assertEqual(self.client.objects[old["key"]], original)
        self.assertEqual(self.client.objects[new["key"]], changed)
        self.assertIn(hashlib.sha256(changed).hexdigest(), new["key"])
        self.assertEqual(resolve_image_urls(self.product_dir)["main-S1"], new["url"])

    def test_same_size_corrupt_remote_bytes_are_not_reused(self):
        self.generate()
        first = self.storage.publish_product(self.product_dir, slots=["main-S1"])
        key = first["results"][0]["key"]
        original = self.client.objects[key]
        self.client.objects[key] = original[:-1] + bytes([original[-1] ^ 1])
        second = self.storage.publish_product(self.product_dir, slots=["main-S1"])
        self.assertEqual(second["uploaded"], 1)
        self.assertEqual(second["unchanged"], 0)
        self.assertEqual(self.client.objects[key], original)

    def test_unverifiable_head_never_reuses_an_object(self):
        from unittest import mock

        self.generate()
        first = self.storage.publish_product(self.product_dir, slots=["main-S1"])
        row = first["results"][0]
        body = self.client.objects[row["key"]]
        good = {
            "Content-Length": str(len(body)),
            "ETag": f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"',
            "x-cos-meta-sha256": row["sha256"],
        }
        bad_headers = [
            {"Content-Length": good["Content-Length"]},
            {**good, "ETag": good["ETag"][:-1] + '-2"'},
            {**good, "ETag": ""},
            {**good, "x-cos-meta-sha256": "0" * 64},
            {**good, "Content-Length": "invalid"},
        ]
        for head in bad_headers:
            with self.subTest(head=head), mock.patch.object(self.client, "head_object", return_value=head):
                result = self.storage.publish_product(self.product_dir, slots=["main-S1"])
            self.assertEqual(result["uploaded"], 1)
            self.assertEqual(result["unchanged"], 0)

    def test_versioned_key_preserves_actual_jpeg_format(self):
        source = self.product_dir / "output" / "generated-images" / "photo.JPG"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\xff\xd8\xffjpeg")
        row = {"slot": "detail-001", "output_path": str(source.relative_to(self.product_dir))}
        storage = CosObjectStorage(
            self.client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"],
            key_prefix="custom path", public_base_url="https://cdn.example.com/media",
        )
        result = storage.publish_slot(self.product_dir, row)
        self.assertTrue(result["key"].endswith(".jpg"))
        self.assertIn("detail-001-" + hashlib.sha256(source.read_bytes()).hexdigest(), result["key"])
        self.assertTrue(result["url"].startswith("https://cdn.example.com/media/custom%20path/"))
        put = next(kwargs for name, kwargs in self.client.calls if name == "put_object")
        self.assertEqual(put["ContentType"], "image/jpeg")

    def test_external_source_path_rejected_before_any_cos_request(self):
        source = self.root / "not-product.png"
        source.write_bytes(IMAGE_BYTES)
        with self.assertRaisesRegex(CosError, "当前商品目录"):
            self.storage.publish_slot(self.product_dir, {"slot": "main-S1", "output_path": str(source)})
        self.assertEqual(self.client.calls, [])

    def test_invalid_public_base_url_rejected(self):
        for base in ("http://cdn.example.com", "https://user:secret@cdn.example.com", "https://cdn.example.com?secret=1"):
            with self.subTest(base=base), self.assertRaises(CosError):
                CosObjectStorage(self.client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"], public_base_url=base)

    def test_url_mapping_replace_failure_preserves_previous_map(self):
        from unittest import mock

        self.generate()
        self.storage.publish_product(self.product_dir)
        path = self.product_dir / "output" / "image-public-urls.json"
        previous = path.read_bytes()
        with mock.patch("pipeline.oss_cos.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.storage.publish_product(self.product_dir)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(path.parent.glob(".image-public-urls.json.*.tmp")), [])

    def test_dry_run_uploads_nothing(self):
        self.generate()
        storage = CosObjectStorage(
            self.client,
            bucket=ENV["COS_BUCKET"],
            region=ENV["COS_REGION"],
            key_prefix="ozon-images",
            dry_run=True,
        )
        summary = storage.publish_product(self.product_dir)
        self.assertEqual(summary["uploaded"], 0)
        self.assertTrue(all(item["status"] == "would_upload" for item in summary["results"]))
        self.assertEqual(self.client.objects, {})
        self.assertIsNone(summary["urls_file"])

    def test_missing_local_file_is_reported(self):
        self.generate()
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        (self.product_dir / plan["main_images"][0]["output_path"]).unlink()
        summary = self.storage.publish_product(self.product_dir)
        self.assertEqual(len(summary["missing"]), 1)

    def test_retry_then_success(self):
        self.generate()
        client = FakeCosClient(fail_put=1)
        storage = CosObjectStorage(
            client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"], sleep=lambda _: None
        )
        summary = storage.publish_product(self.product_dir, slots=["main-S1"])
        self.assertEqual(summary["uploaded"], 1)

    def test_put_failure_raises_cos_error(self):
        self.generate()
        client = FakeCosClient(fail_put=99)
        storage = CosObjectStorage(
            client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"], sleep=lambda _: None, max_attempts=2
        )
        with self.assertRaises(CosError):
            storage.publish_product(self.product_dir, slots=["main-S1"])

    def test_upload_payload_uses_cos_urls(self):
        self.generate()
        self.storage.publish_product(self.product_dir)
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(payload["image_upload_gate"]["passed"])
        self.assertEqual(payload["image_upload_gate"]["urls_source"], "output/image-public-urls.json")
        self.assertTrue(all(image["url"].startswith("https://") for image in payload["images"]))

    def test_missing_plan_raises(self):
        with self.assertRaises(CosError):
            self.storage.publish_product(self.root / "nope")


class ProbeTests(unittest.TestCase):
    """匿名可读性探测：不需要密钥就能判断"Ozon 能不能抓到"。"""

    def test_public_image_passes(self):
        from pipeline.oss_cos import probe_public_url

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=None):
                return b"\x89PNG\r\n\x1a\n" + b"rest"

        report = probe_public_url(
            "https://ozon-images-1486640018.cos.ap-hongkong.myqcloud.com/ozon-images/a.png",
            urlopen=lambda request, timeout=None: Response(),
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["anonymous_get"], "HTTP 200")
        self.assertTrue(report["looks_like_image"])

    def test_private_bucket_reports_actionable_hint(self):
        import urllib.error

        from pipeline.oss_cos import probe_public_url

        def denying(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        report = probe_public_url("https://bucket.cos.region.myqcloud.com/a.png", urlopen=denying)
        self.assertFalse(report["ok"])
        self.assertEqual(report["anonymous_get"], "HTTP 403")
        self.assertIn("阻止公共访问", report["hint"])
        self.assertIn("公有读", report["hint"])

    def test_missing_key_hint(self):
        import urllib.error

        from pipeline.oss_cos import probe_public_url

        def missing(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        report = probe_public_url("https://bucket.cos.region.myqcloud.com/a.png", urlopen=missing)
        self.assertFalse(report["ok"])
        self.assertIn("不存在", report["hint"])

    def test_network_error_is_reported(self):
        from pipeline.oss_cos import probe_public_url

        def broken(request, timeout=None):
            raise OSError("connection refused")

        report = probe_public_url("https://nope.example.com/a.png", urlopen=broken)
        self.assertFalse(report["ok"])
        self.assertIn("连不上", report["hint"])

    def test_cli_probe_builds_url_from_key(self):
        import io
        import os
        from contextlib import redirect_stdout
        from unittest import mock

        from pipeline.oss_cos import main as cos_main

        saved = {key: os.environ.get(key) for key in ("COS_BUCKET", "COS_REGION")}
        os.environ["COS_BUCKET"] = "ozon-images-1486640018"
        os.environ["COS_REGION"] = "ap-hongkong"
        calls: list[str] = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=None):
                return b"\xff\xd8\xff\xe0jpeg"

        def opener(request, timeout=None):
            calls.append(request.full_url)
            return Response()

        try:
            with mock.patch("urllib.request.urlopen", opener):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = cos_main(["--probe", "--key", "ozon-images/P000002/main-S1.png", "--json"])
            self.assertEqual(code, 0, buffer.getvalue())
            self.assertEqual(
                calls[0],
                "https://ozon-images-1486640018.cos.ap-hongkong.myqcloud.com/ozon-images/P000002/main-S1.png",
            )
            self.assertTrue(json.loads(buffer.getvalue())["ok"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_cli_probe_without_target_errors(self):
        from pipeline.oss_cos import main as cos_main

        with self.assertRaises(SystemExit):
            cos_main(["--probe"])


class CheckTests(unittest.TestCase):
    def test_check_runs_put_get_delete_and_detects_public_read(self):
        client = FakeCosClient()
        storage = CosObjectStorage(client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"], key_prefix="ozon-images")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return CHECK_BODY

        report = storage.check(urlopen=lambda url, timeout=None: Response())
        self.assertTrue(report["ok"])
        self.assertEqual(report["anonymous_get"], "ok")
        self.assertEqual(report["delete"], "ok")
        names = [name for name, _ in client.calls]
        self.assertEqual(names, ["put_object", "delete_object"])
        self.assertIn(f"ozon-images/{CHECK_KEY}", report["key"])

    def test_check_flags_private_bucket(self):
        import urllib.error

        client = FakeCosClient()
        storage = CosObjectStorage(client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"])

        def denying(url, timeout=None):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

        report = storage.check(urlopen=denying)
        self.assertFalse(report["ok"])
        self.assertEqual(report["anonymous_get"], "http_403")
        self.assertIn("公有读", report["hint"])

    def test_check_reports_content_mismatch(self):
        client = FakeCosClient()
        storage = CosObjectStorage(client, bucket=ENV["COS_BUCKET"], region=ENV["COS_REGION"])

        class Wrong:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"something else"

        report = storage.check(urlopen=lambda url, timeout=None: Wrong())
        self.assertFalse(report["ok"])
        self.assertEqual(report["anonymous_get"], "content_mismatch")


class CliTests(unittest.TestCase):
    def test_cli_without_env_reports_missing_keys(self):
        from pipeline.oss_cos import main

        import io
        import os
        from contextlib import redirect_stdout

        saved = {key: os.environ.pop(key, None) for key in ENV}
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--check"])
            self.assertEqual(code, 1)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("COS_SECRET_ID", payload["error"])
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_cli_check_uses_env_and_env_override(self):
        from pipeline.oss_cos import main

        import io
        import os
        from contextlib import redirect_stdout
        from unittest import mock

        saved = {key: os.environ.get(key) for key in ENV}
        os.environ.update(ENV)
        try:
            fake = FakeCosClient()

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return CHECK_BODY

            with mock.patch("pipeline.oss_cos.build_client", return_value=fake), mock.patch(
                "urllib.request.urlopen", lambda url, timeout=None: Response()
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = main(["--check", "--json"])
            self.assertEqual(code, 0, buffer.getvalue())
            self.assertTrue(json.loads(buffer.getvalue())["ok"])
            self.assertTrue(any(name == "put_object" for name, _ in fake.calls))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_planned_slots_requires_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(planned_slots(directory), [])


if __name__ == "__main__":
    unittest.main()
