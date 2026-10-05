"""提交前预检的测试：店铺/凭据/载荷/图片可达性，且**绝不做写操作**。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline.preflight import check_image_urls, preflight  # noqa: E402
from pipeline.stores import ensure_registry, save_registry  # noqa: E402

IMAGE_URL = "https://example-bucket.cos.ap-hongkong.myqcloud.com/ozon-images/P1/main-S1.png"


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"png-bytes") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def build_product(root: pathlib.Path, *, with_urls: bool = True, blockers: bool = False) -> pathlib.Path:
    product = root / "products" / "P000900"
    (product / "input").mkdir(parents=True)
    (product / "output").mkdir(parents=True)
    (product / "input" / "source.json").write_text(
        json.dumps(
            {
                "product_id": "P000900",
                "source_url": "https://detail.1688.com/offer/1.html",
                "title_zh": "纯棉床单",
                "skus": [{"sku_id": "S1", "color_ru": "белый", "purchase_price_cny": 42.0}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if with_urls:
        (product / "output" / "image-public-urls.json").write_text(
            json.dumps({"urls": {"main-S1": IMAGE_URL}}, ensure_ascii=False), encoding="utf-8"
        )
    (product / "output" / "ozon-category.json").write_text(
        json.dumps(
            {
                "product_id": "P000900",
                "category_id": 17028731,
                "type_id": 92612,
                "category_name": "床单",
                "source": "ozon_seller_api",
                "match_status": "api_confirmed",
                "metadata_source": "ozon_seller_api",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (product / "output" / "image-plan.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "product_id": "P000900",
                "main_images": [{"slot": "main-S1", "source_sku_id": "S1", "prompt": "p", "russian_text": []}],
                "detail_images": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (product / "output" / "copy-ru.json").write_text(
        json.dumps({"title_ru": "Простыня", "description_ru": "х" * 200, "copy_bundle": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return product


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        registry = ensure_registry(None)
        registry["shops"] = [
            {
                "id": "default",
                "name": "default",
                "enabled": True,
                "client_id_env": "OZON_DEFAULT_CLIENT_ID",
                "api_key_env": "OZON_DEFAULT_API_KEY",
                "default_currency_code": "CNY",
                "default_vat": "0",
            }
        ]
        self.registry = self.root / "shops.json"
        save_registry(registry, self.registry)
        self.env = {"OZON_DEFAULT_CLIENT_ID": "1", "OZON_DEFAULT_API_KEY": "k"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_shop_and_missing_credentials_are_reported(self):
        registry = ensure_registry(self.registry)
        registry["shops"][0]["enabled"] = False
        save_registry(registry, self.registry)
        report = preflight(
            build_product(self.root), shop="default", registry_path=self.registry, env={}, verify_urls=False
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("未启用" in item for item in report["problems"]), report["problems"])
        self.assertTrue(any("缺凭据" in item for item in report["problems"]), report["problems"])
        self.assertFalse(report["api_writes_performed"])

    def test_unknown_shop_is_reported(self):
        report = preflight(
            build_product(self.root), shop="nope", registry_path=self.registry, env=self.env, verify_urls=False
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("店铺不存在" in item for item in report["problems"]))

    def test_missing_image_urls_are_reported(self):
        report = preflight(
            build_product(self.root, with_urls=False),
            shop="default",
            registry_path=self.registry,
            env=self.env,
            verify_urls=False,
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("图片还没发布" in item for item in report["problems"]), report["problems"])

    def test_unreachable_image_blocks_submission(self):
        def opener(request, timeout=None):
            raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(request.full_url, 403, "Forbidden", {}, None)

        report = preflight(
            build_product(self.root),
            shop="default",
            registry_path=self.registry,
            env=self.env,
            verify_urls=True,
            urlopen=opener,
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("图片取不到" in item for item in report["problems"]), report["problems"])
        self.assertEqual(report["url_checks"][0]["status"], 403)

    def test_url_check_uses_anonymous_get(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(request)
            return FakeResponse(200, b"x" * 42)

        checks = check_image_urls([IMAGE_URL], urlopen=opener)
        self.assertEqual(checks[0]["status"], 200)
        self.assertEqual(checks[0]["bytes"], 42)
        self.assertTrue(checks[0]["ok"])
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertIsNone(calls[0].get_header("Authorization"))

    def test_preflight_never_calls_a_write_endpoint(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("不许联网写")):
            report = preflight(
                build_product(self.root, with_urls=False),
                shop="default",
                registry_path=self.registry,
                env=self.env,
                verify_urls=False,
            )
        self.assertIn("只读预检", report["note"])


if __name__ == "__main__":
    unittest.main()
