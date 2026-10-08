"""Unified shop editor uses actual encrypted vaults and isolated fake HTTP."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import shop_authorization as authorization, stores
from pipeline.ozon_http import OzonHttpError
from pipeline.performance_access import PerformanceAccess
from pipeline.shop_management import ShopManagement, ShopManagementError
from tests.test_shop_authorization import FakeTransport as SellerFixture, SECRET_CLIENT, SECRET_KEY
from tests.test_performance_access import FakeTransport as AdvertisingFixture, CLIENT, SECRET


class ShopManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = self.root / "config" / "shops.json"
        self.vault = self.root / "shop-vault"
        self.seller = SellerFixture()
        self.advertising = AdvertisingFixture()
        self.invalidated = []
        self.manager = ShopManagement(self.root, registry_path=self.registry, vault_root=self.vault,
                                      transport_factory=lambda _: self.seller,
                                      performance_factory=lambda root: PerformanceAccess(root, transport=self.advertising),
                                      invalidate_shop_cache=self.invalidated.append)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, shop_id="shop-a", **kwargs):
        fields = {"mode": "create", "shop_id": shop_id, "display_name": "店铺甲",
                  "seller_client_id": SECRET_CLIENT, "seller_api_key": SECRET_KEY}
        fields.update(kwargs)
        return self.manager.save(**fields)

    def update(self, **kwargs):
        fields = {"mode": "update", "shop_id": "shop-a", "display_name": "新的店名"}
        fields.update(kwargs)
        return self.manager.save(**fields)

    def credentials(self, shop="shop-a"):
        registry = authorization._read_registry(self.registry, self.vault)
        return stores.credential_values(authorization._shop(registry, shop))

    def test_create_and_list_separate_encrypted_authorizations(self):
        result = self.create(advertising_client_id=CLIENT, advertising_client_secret=SECRET)
        self.assertFalse(result["partial"])
        self.assertTrue(result["seller_saved"])
        self.assertTrue(result["advertising_saved"])
        self.assertFalse(result["api_writes_performed"])
        self.assertEqual(self.invalidated, ["shop-a"])
        self.assertEqual([path for path, body in self.seller.calls], ["/v1/roles", "/v1/description-category/tree"])
        self.assertEqual([(method, path) for method, path, body in self.advertising.calls],
                         [("POST", "/api/client/token"), ("GET", "/api/client/campaign")])
        output = json.dumps([result, self.manager.list_shops()])
        for secret in (SECRET_CLIENT, SECRET_KEY, CLIENT, SECRET):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, self.registry.read_text(encoding="utf-8"))
            for encrypted in self.vault.glob("*.fernet"):
                self.assertNotIn(secret.encode(), encrypted.read_bytes())
        row = next(row for row in self.manager.list_shops() if row["id"] == "shop-a")
        self.assertTrue(row["advertising"]["configured"])
        self.assertFalse(row["advertising"]["ad_writes_enabled"])
        self.assertEqual(self.credentials(), (SECRET_CLIENT, SECRET_KEY))

    def test_empty_credentials_edit_only_metadata_without_network(self):
        self.create(advertising_client_id=CLIENT, advertising_client_secret=SECRET)
        previous_files = set(self.vault.glob("*.fernet"))
        self.seller.calls.clear()
        self.advertising.calls.clear()
        result = self.update(default_currency_code="RUB", seller_client_id=" ", seller_api_key="",
                             advertising_client_id="", advertising_client_secret=" ")
        self.assertFalse(result["seller_saved"])
        self.assertFalse(result["advertising_saved"])
        self.assertTrue(result["metadata_saved"])
        self.assertEqual(result["shop"]["display_name"], "新的店名")
        self.assertEqual(result["shop"]["default_currency_code"], "RUB")
        self.assertEqual(previous_files, set(self.vault.glob("*.fernet")))
        self.assertEqual(self.seller.calls, [])
        self.assertEqual(self.advertising.calls, [])
        self.assertEqual(self.credentials(), (SECRET_CLIENT, SECRET_KEY))
        self.assertTrue(result["shop"]["advertising"]["configured"])

    def test_partial_pairs_rejected_before_any_persistence(self):
        self.create()
        before = self.registry.read_bytes()
        for kwargs in ({"seller_client_id": SECRET_CLIENT}, {"seller_api_key": SECRET_KEY},
                       {"advertising_client_id": CLIENT}, {"advertising_client_secret": SECRET}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ShopManagementError):
                    self.update(**kwargs)
                self.assertEqual(self.registry.read_bytes(), before)

    def test_create_duplicate_and_update_missing_never_overwrite(self):
        self.create()
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(ShopManagementError, "已存在"):
            self.create(display_name="错误覆盖")
        with self.assertRaisesRegex(ShopManagementError, "不存在"):
            self.update(shop_id="unknown")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_new_shop_requires_seller_both_fields(self):
        with self.assertRaisesRegex(ShopManagementError, "新增店铺必须"):
            self.create(seller_client_id="", seller_api_key="")
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.vault.exists())

    def test_credentials_validation_failure_keeps_previous_metadata_and_key(self):
        self.create()
        before = self.registry.read_bytes()
        self.seller.error = OzonHttpError(SECRET_KEY + " upstream reflection", status=403)
        with self.assertRaises(authorization.ShopAuthorizationError) as caught:
            self.update(seller_client_id=SECRET_CLIENT, seller_api_key="replacement-seller-key")
        self.assertNotIn(SECRET_KEY, str(caught.exception))
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.credentials(), (SECRET_CLIENT, SECRET_KEY))

    def test_changed_client_requires_new_shop_and_does_not_read_network(self):
        self.create()
        before = self.registry.read_bytes()
        self.seller.calls.clear()
        with self.assertRaisesRegex(ShopManagementError, "请新增店铺"):
            self.update(seller_client_id="992113322", seller_api_key="replacement-seller-key")
        self.assertEqual(self.seller.calls, [])
        self.assertEqual(self.registry.read_bytes(), before)

    def test_same_client_rotation_preserves_identity_and_ad_binding(self):
        self.create(advertising_client_id=CLIENT, advertising_client_secret=SECRET)
        result = self.update(seller_client_id=SECRET_CLIENT, seller_api_key="replacement-seller-key")
        self.assertEqual(result["shop"]["id"], "shop-a")
        self.assertEqual(self.credentials(), (SECRET_CLIENT, "replacement-seller-key"))
        self.assertTrue(result["shop"]["advertising"]["configured"])

    def test_failed_ad_update_reports_partial_keeps_old_ad_and_saved_seller(self):
        self.create(advertising_client_id=CLIENT, advertising_client_secret=SECRET)
        previous_ad = self.manager.performance._loaded("shop-a")
        self.advertising.failure = RuntimeError(SECRET + " must-not-echo")
        result = self.update(seller_client_id=SECRET_CLIENT, seller_api_key="new-valid-seller-key",
                             advertising_client_id=CLIENT, advertising_client_secret="replacement-ad-secret")
        self.assertTrue(result["partial"])
        self.assertTrue(result["seller_saved"])
        self.assertFalse(result["advertising_saved"])
        self.assertIn("广告授权未保存", result["warning"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.credentials(), (SECRET_CLIENT, "new-valid-seller-key"))
        current_ad = self.manager.performance._loaded("shop-a")
        self.assertEqual(current_ad["generation"], previous_ad["generation"])
        self.assertEqual(current_ad["client_secret"], SECRET)

    def test_failed_new_ad_returns_created_shop_not_fictitious_rollback(self):
        self.advertising.failure = RuntimeError(SECRET)
        result = self.create(advertising_client_id=CLIENT, advertising_client_secret=SECRET)
        self.assertTrue(result["partial"])
        self.assertTrue(result["metadata_saved"])
        self.assertFalse(result["shop"]["advertising"]["configured"])
        self.assertEqual(self.credentials(), (SECRET_CLIENT, SECRET_KEY))

    def test_saved_metadata_but_failed_default_returns_partial(self):
        self.create()
        authorization.set_shop_enabled("shop-a", False, registry_path=self.registry, vault_root=self.vault)
        result = self.update(make_default=True)
        self.assertTrue(result["partial"])
        self.assertFalse(result["default_saved"])
        self.assertFalse(result["shop"]["enabled"])
        self.assertEqual(result["shop"]["display_name"], "新的店名")
        self.assertIn("未能设置默认", result["warning"])

    def test_select_second_shop_as_default(self):
        self.create()
        result = self.create("shop-b", display_name="店铺乙", make_default=True)
        self.assertTrue(result["default_saved"])
        self.assertTrue(result["shop"]["is_default"])
        self.assertFalse(next(row for row in self.manager.list_shops() if row["id"] == "shop-a")["is_default"])

    def test_names_cannot_include_current_new_or_other_shop_secrets(self):
        self.create(advertising_client_id=CLIENT, advertising_client_secret=SECRET)
        before = self.registry.read_bytes()
        for secret in (SECRET_CLIENT, SECRET_KEY, CLIENT, SECRET, "new-secret-key"):
            with self.subTest(secret=secret):
                with self.assertRaises(ShopManagementError):
                    self.update(display_name="名称" + secret, seller_client_id=SECRET_CLIENT,
                                seller_api_key="new-secret-key")
                self.assertEqual(self.registry.read_bytes(), before)
        with self.assertRaises(ShopManagementError):
            self.create("shop-b", display_name="复制" + SECRET)

    def test_invalid_metadata_and_mode_rejected(self):
        self.create()
        for kwargs in ({"display_name": " "}, {"display_name": "坏\n名称"},
                       {"default_currency_code": "FAKE"}, {"mode": "anything"}):
            with self.assertRaises(ValueError):
                self.update(**kwargs)

    def test_create_race_checked_again_after_network_validation(self):
        def racing_factory(_):
            self.create()
            return self.seller
        with self.assertRaisesRegex(authorization.ShopAuthorizationError, "已存在"):
            authorization.authorize_shop("shop-a", "并发店铺", SECRET_CLIENT, "another-valid-key",
                                         registry_path=self.registry, vault_root=self.vault,
                                         transport_factory=racing_factory, expected_mode="create")
        self.assertEqual(self.credentials(), (SECRET_CLIENT, SECRET_KEY))

    def test_rotation_checks_client_again_after_network_validation(self):
        self.create()
        def racing_factory(_):
            authorization.authorize_shop("shop-a", "已切换账户", "9948811", "other-account-key",
                                         registry_path=self.registry, vault_root=self.vault,
                                         transport_factory=lambda _: self.seller)
            return self.seller
        with self.assertRaisesRegex(authorization.ShopAuthorizationError, "不一致"):
            authorization.authorize_shop("shop-a", "并发替换", SECRET_CLIENT, "another-valid-key",
                                         registry_path=self.registry, vault_root=self.vault,
                                         transport_factory=racing_factory, expected_mode="update",
                                         require_same_client=True)
        self.assertEqual(self.credentials(), ("9948811", "other-account-key"))

    def test_cache_failure_is_safe_partial_success(self):
        def broken(_):
            raise RuntimeError(SECRET_KEY)
        self.manager.invalidate_shop_cache = broken
        result = self.create()
        self.assertTrue(result["partial"])
        self.assertNotIn(SECRET_KEY, result["warning"])
        self.assertEqual(self.credentials(), (SECRET_CLIENT, SECRET_KEY))

    def test_direct_metadata_updater_keeps_cipher_and_default(self):
        self.create()
        registry = authorization._read_registry(self.registry, self.vault)
        old_ref = authorization._shop(registry, "shop-a")["credential_ref"]
        row = authorization.update_shop_metadata("shop-a", "仅改名称", default_currency_code="USD",
                                                 registry_path=self.registry, vault_root=self.vault)
        self.assertTrue(row["is_default"])
        new_registry = authorization._read_registry(self.registry, self.vault)
        self.assertEqual(authorization._shop(new_registry, "shop-a")["credential_ref"], old_ref)

    def test_missing_previous_legacy_client_cannot_replace_account(self):
        registry = stores.example_registry()
        registry["shops"][0]["enabled"] = True
        registry["shops"][0]["checked_at"] = "2026-10-08T00:00:00Z"
        stores.save_registry(registry, self.registry)
        before = self.registry.read_bytes()
        with patch.dict("os.environ", {"OZON_DEFAULT_CLIENT_ID": "", "OZON_DEFAULT_API_KEY": ""}):
            with self.assertRaisesRegex(ShopManagementError, "无法确认原店铺"):
                self.manager.save(mode="update", shop_id="default", display_name="原店铺",
                                  seller_client_id=SECRET_CLIENT, seller_api_key=SECRET_KEY)
        self.assertEqual(before, self.registry.read_bytes())
        self.assertEqual(self.seller.calls, [])

    def test_unconfigured_example_shop_can_be_authorized_as_edit(self):
        with patch.dict("os.environ", {"OZON_DEFAULT_CLIENT_ID": "", "OZON_DEFAULT_API_KEY": ""}):
            result = self.manager.save(mode="update", shop_id="default", display_name="第一次授权",
                                       seller_client_id=SECRET_CLIENT, seller_api_key=SECRET_KEY)
        self.assertTrue(result["seller_saved"])
        self.assertTrue(result["shop"]["credentials_ready"])


if __name__ == "__main__":
    unittest.main()
