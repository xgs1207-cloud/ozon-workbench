"""店铺注册表命令行的测试：开关 enabled、凭据体检、**绝不打印密钥**。"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline.stores import (  # noqa: E402
    ensure_registry,
    load_registry,
    main,
    save_registry,
    set_enabled,
    shop_summary,
)

SECRET_CLIENT = "1370000000"
SECRET_KEY = "super-secret-api-key-value"


def temp_registry(directory: pathlib.Path) -> pathlib.Path:
    registry = ensure_registry(None)
    registry["shops"] = [
        {
            "id": "default",
            "name": "default",
            "display_name": "我的店",
            "enabled": False,
            "client_id_env": "OZON_DEFAULT_CLIENT_ID",
            "api_key_env": "OZON_DEFAULT_API_KEY",
            "default_currency_code": "CNY",
            "default_vat": "0",
        },
        {
            "id": "shop-b",
            "name": "shop-b",
            "display_name": "第二家店",
            "enabled": False,
            "client_id_env": "OZON_SHOP_B_CLIENT_ID",
            "api_key_env": "OZON_SHOP_B_API_KEY",
            "default_currency_code": "CNY",
            "default_vat": "0",
        },
    ]
    path = directory / "shops.json"
    save_registry(registry, path)
    return path


class StoresCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path = temp_registry(self.root)
        self.env = {
            "OZON_DEFAULT_CLIENT_ID": SECRET_CLIENT,
            "OZON_DEFAULT_API_KEY": SECRET_KEY,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--registry", str(self.path), *args])
        return code, buffer.getvalue()

    def test_list_reports_missing_credentials_without_secrets(self):
        code, text = self.run_cli("--list")
        self.assertEqual(code, 1)
        self.assertIn("default", text)
        self.assertIn("OZON_DEFAULT_CLIENT_ID", text)
        self.assertIn("没有任何启用的店铺", text)
        self.assertNotIn(SECRET_KEY, text)
        self.assertNotIn(SECRET_CLIENT, text)

    def test_enable_persists_and_reports_ready_with_env(self):
        code, text = self.run_cli("--enable", "default")
        registry = load_registry(self.path)
        enabled = {item["id"]: item["enabled"] for item in registry["shops"]}
        self.assertTrue(enabled["default"])
        self.assertFalse(enabled["shop-b"])
        # 环境变量里没有凭据时仍应报"缺凭据"
        self.assertEqual(code, 1)
        self.assertIn("缺", text)

        rows = shop_summary(registry, self.env)
        default_row = next(row for row in rows if row["id"] == "default")
        self.assertTrue(default_row["credentials_ready"])
        self.assertEqual(default_row["missing_env"], [])
        self.assertNotIn(SECRET_KEY, json.dumps(rows, ensure_ascii=False))

    def test_second_shop_needs_its_own_credentials(self):
        registry = set_enabled(load_registry(self.path), "shop-b", True)
        save_registry(registry, self.path)
        rows = shop_summary(registry, self.env)  # 只配了 default 的凭据
        shop_b = next(row for row in rows if row["id"] == "shop-b")
        self.assertFalse(shop_b["credentials_ready"])
        self.assertEqual(shop_b["missing_env"], ["OZON_SHOP_B_CLIENT_ID", "OZON_SHOP_B_API_KEY"])

    def test_unknown_shop_id_is_rejected(self):
        with self.assertRaises(ValueError):
            set_enabled(load_registry(self.path), "nope", True)

    def test_disable_leaves_no_enabled_store(self):
        registry = load_registry(self.path)
        save_registry(set_enabled(registry, "default", True), self.path)
        code, _ = self.run_cli("--disable", "default")
        self.assertEqual(code, 1)
        self.assertFalse(load_registry(self.path)["shops"][0]["enabled"])

    def test_json_output_is_machine_readable(self):
        code, text = self.run_cli("--list", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["shops"]), 2)
        self.assertTrue(payload["problems"])


if __name__ == "__main__":
    unittest.main()
