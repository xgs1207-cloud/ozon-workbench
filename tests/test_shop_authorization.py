"""店铺授权：只读验证、加密持久化、原子更新和密钥不泄露。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from pipeline import shop_authorization as authorization
from pipeline import stores
from pipeline.ozon_http import OzonHttpError

SECRET_CLIENT = "893761024"
SECRET_KEY = "authorization-secret-api-key-12345"
TREE = {"result": [{"description_category_id": 100, "children": [{"type_id": 200, "children": []}]}]}
ROLES = {
    "expires_at": "2099-01-01T00:00:00Z",
    "roles": [{"name": "Admin", "methods": [
        "/v1/description-category/tree", "/v1/description-category/attribute",
        "/v1/description-category/attribute/values", "/v1/description-category/attribute/values/search",
    ]}],
}


class FakeTransport:
    def __init__(self, *, roles=None, tree=None, error=None):
        self.calls = []
        self.roles = ROLES if roles is None else roles
        self.tree = TREE if tree is None else tree
        self.error = error

    def post(self, path, body):
        self.calls.append((path, dict(body)))
        if self.error:
            raise self.error
        if path == authorization.PATH_ROLES:
            return self.roles
        if path == authorization.PATH_TREE:
            return self.tree
        raise AssertionError("Unauthorized endpoint")


class ShopAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.registry_path = self.root / "config" / "shops.json"
        self.vault_root = self.root / "runtime" / "shop-vault"
        self.environment = patch.dict(os.environ, {
            "WORKBENCH_SHOP_REGISTRY_PATH": str(self.registry_path),
            "WORKBENCH_SHOP_VAULT_ROOT": str(self.vault_root),
        })
        self.environment.start()
        self.transport = FakeTransport()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def authorize(self, shop_id="store-a", **kwargs):
        return authorization.authorize_shop(
            shop_id, kwargs.pop("display_name", "测试店铺"),
            kwargs.pop("client_id", SECRET_CLIENT), kwargs.pop("api_key", SECRET_KEY),
            transport_factory=kwargs.pop("transport_factory", lambda credentials: self.transport),
            **kwargs,
        )

    def test_success_calls_only_roles_and_tree_and_selects_default(self):
        result = self.authorize()
        self.assertEqual(self.transport.calls, [
            ("/v1/roles", {}), ("/v1/description-category/tree", {"language": "ZH_HANS"}),
        ])
        self.assertEqual(result["connection_status"], "connected")
        self.assertTrue(result["credentials_ready"])
        self.assertTrue(result["enabled"])
        self.assertTrue(result["is_default"])
        self.assertFalse(result["api_writes_performed"])
        self.assertEqual(result["category_count"], 1)
        self.assertTrue(result["capabilities"]["read_attributes"])
        self.assertFalse(result["capabilities"]["import_products"])

    def test_credentials_are_encrypted_and_registry_and_responses_are_safe(self):
        result = self.authorize()
        metadata = self.registry_path.read_text(encoding="utf-8")
        responses = json.dumps([result, authorization.list_authorized_shops()], ensure_ascii=False)
        for secret in (SECRET_CLIENT, SECRET_KEY):
            self.assertNotIn(secret, metadata)
            self.assertNotIn(secret, responses)
            for item in self.vault_root.glob("*.fernet"):
                self.assertNotIn(secret.encode(), item.read_bytes())
        self.assertNotIn("_vault_root", metadata)
        self.assertNotIn("credential_ref", result)

    def test_fresh_registry_load_and_cli_read_persisted_vault_without_process_env(self):
        self.authorize()
        registry = stores.load_registry()
        shop = authorization.select_read_shop(registry)
        self.assertEqual(stores.credential_values(shop, env={}), (SECRET_CLIENT, SECRET_KEY))
        self.assertTrue(stores.resolve_credentials(shop, env={})["ready"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = stores.main(["--registry", str(self.registry_path), "--check", "--json"])
        self.assertEqual(result, 0)
        self.assertNotIn(SECRET_CLIENT, buffer.getvalue())
        self.assertNotIn(SECRET_KEY, buffer.getvalue())

    def test_explicit_registry_path_locates_default_adjacent_vault(self):
        with patch.dict(os.environ, {"WORKBENCH_SHOP_VAULT_ROOT": ""}):
            path = self.root / "separate" / "shops.json"
            self.authorize(registry_path=path)
            shop = authorization.select_read_shop(stores.load_registry(path))
            self.assertEqual(stores.credential_values(shop), (SECRET_CLIENT, SECRET_KEY))
            self.assertTrue((path.parent / "shop-vault" / "master.key").is_file())

    @unittest.skipIf(os.name == "nt", "POSIX file mode is not a Windows ACL")
    def test_vault_and_files_have_private_posix_permissions(self):
        self.authorize()
        self.assertEqual(self.vault_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.registry_path.stat().st_mode & 0o777, 0o600)
        for item in self.vault_root.iterdir():
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)

    def test_authorization_failure_preserves_previous_working_credentials(self):
        self.authorize()
        previous = self.registry_path.read_bytes()
        error = OzonHttpError(f"bad {SECRET_KEY} {SECRET_CLIENT}", status=403)
        with self.assertRaises(authorization.ShopAuthorizationError) as caught:
            self.authorize(api_key="replacement-secret-api-key", transport_factory=lambda credentials: FakeTransport(error=error))
        self.assertNotIn(SECRET_KEY, str(caught.exception))
        self.assertNotIn(SECRET_CLIENT, str(caught.exception))
        self.assertIn("403", str(caught.exception))
        self.assertEqual(previous, self.registry_path.read_bytes())
        self.assertEqual(stores.credential_values(authorization.select_read_shop(stores.load_registry())), (SECRET_CLIENT, SECRET_KEY))

    def test_new_authorization_failure_creates_no_config_or_vault(self):
        with self.assertRaises(authorization.ShopAuthorizationError):
            self.authorize(transport_factory=lambda credentials: FakeTransport(error=OzonHttpError(SECRET_KEY, status=401)))
        self.assertFalse(self.registry_path.exists())
        self.assertFalse(self.vault_root.exists())

    def test_transport_constructor_error_does_not_echo_credentials(self):
        def factory(credentials):
            raise RuntimeError(repr(credentials))
        with self.assertRaises(authorization.ShopAuthorizationError) as caught:
            self.authorize(transport_factory=factory)
        self.assertNotIn(SECRET_KEY, str(caught.exception))
        self.assertNotIn(SECRET_CLIENT, str(caught.exception))

    def test_exact_import_method_is_required_not_admin_role(self):
        role = {"roles": [{"name": "Admin", "methods": ["/v1/product", "/v3/product/import/info"]}]}
        self.assertFalse(self.authorize(transport_factory=lambda credentials: FakeTransport(roles=role))["capabilities"]["import_products"])
        role["roles"][0]["methods"].append("/v3/product/import")
        self.assertTrue(self.authorize(transport_factory=lambda credentials: FakeTransport(roles=role))["capabilities"]["import_products"])

    def test_role_metadata_echo_is_redacted_before_persistence(self):
        roles = {"roles": [{"name": f"role {SECRET_KEY} {SECRET_CLIENT}", "methods": []}]}
        result = self.authorize(transport_factory=lambda credentials: FakeTransport(roles=roles))
        self.assertNotIn(SECRET_KEY, json.dumps(result))
        self.assertNotIn(SECRET_CLIENT, self.registry_path.read_text(encoding="utf-8"))

    def test_expired_key_is_rejected(self):
        roles = {**ROLES, "expires_at": "2000-01-01T00:00:00Z"}
        with self.assertRaisesRegex(authorization.ShopAuthorizationError, "已过期"):
            self.authorize(transport_factory=lambda credentials: FakeTransport(roles=roles))
        self.assertFalse(self.registry_path.exists())

    def test_invalid_expiry_and_response_shape_are_rejected(self):
        for roles, tree in (({**ROLES, "expires_at": "not-a-date"}, TREE), ({}, TREE), (ROLES, {"result": "bad"})):
            with self.subTest(roles=roles, tree=tree):
                with self.assertRaises(authorization.ShopAuthorizationError):
                    self.authorize(transport_factory=lambda credentials: FakeTransport(roles=roles, tree=tree))

    def test_only_enabled_leaf_types_count_as_real_categories(self):
        tree = {"result": [
            {"disabled": True, "children": [{"type_id": 1}]},
            {"type_id": 2, "children": [{"type_id": 3, "disabled": True}, {"type_id": 4}]},
        ]}
        self.assertEqual(self.authorize(transport_factory=lambda credentials: FakeTransport(tree=tree))["category_count"], 1)
        with self.assertRaises(authorization.ShopAuthorizationError):
            self.authorize(transport_factory=lambda credentials: FakeTransport(tree={"result": []}))

    def test_second_shop_does_not_replace_default_unless_requested(self):
        self.authorize("store-a")
        self.assertFalse(self.authorize("store-b")["is_default"])
        self.assertTrue(self.authorize("store-b", make_default=True)["is_default"])
        selected = authorization.select_read_shop(stores.load_registry())
        self.assertEqual(selected["id"], "store-b")

    def test_disable_switches_default_and_reenable_revalidates_only_reads(self):
        self.authorize("store-a")
        self.authorize("store-b")
        disabled = authorization.set_shop_enabled("store-a", False)
        self.assertFalse(disabled["enabled"])
        self.assertEqual(stores.load_registry()["default_read_shop"], "store-b")
        transport = FakeTransport()
        enabled = authorization.set_shop_enabled("store-a", True, transport_factory=lambda credentials: transport)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(authorization.set_default_shop("store-a")["is_default"])

    def test_failed_recheck_disables_shop_but_keeps_ciphertext(self):
        self.authorize()
        old_files = set(self.vault_root.iterdir())
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.test_shop_connection("store-a", transport_factory=lambda credentials: FakeTransport(error=OzonHttpError(SECRET_KEY, status=403)))
        shop = next(item for item in stores.list_shops(stores.load_registry()) if item["id"] == "store-a")
        self.assertFalse(shop["enabled"])
        self.assertEqual(shop["connection_status"], "error")
        self.assertEqual(old_files, set(self.vault_root.iterdir()))
        self.assertEqual(stores.credential_values(shop), (SECRET_CLIENT, SECRET_KEY))

    def test_successful_recheck_does_not_enable_disabled_shop(self):
        self.authorize()
        authorization.set_shop_enabled("store-a", False)
        result = authorization.test_shop_connection("store-a", transport_factory=lambda credentials: FakeTransport())
        self.assertFalse(result["enabled"])
        self.assertEqual(result["connection_status"], "connected")

    def test_transient_recheck_failure_does_not_disable_previously_enabled_shop(self):
        self.authorize()
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.test_shop_connection("store-a", transport_factory=lambda credentials: FakeTransport(error=OzonHttpError("temporary", status=503)))
        registry = stores.load_registry()
        shop = next(item for item in stores.list_shops(registry) if item["id"] == "store-a")
        self.assertTrue(shop["enabled"])
        self.assertEqual(registry["default_read_shop"], "store-a")
        self.assertEqual(shop["connection_status"], "error")

    def test_known_expired_credentials_are_not_selected_for_reads(self):
        self.authorize()
        registry = stores.load_registry()
        next(item for item in registry["shops"] if item["id"] == "store-a")["expires_at"] = "2000-01-01T00:00:00Z"
        with self.assertRaisesRegex(authorization.ShopAuthorizationError, "已过期"):
            authorization.select_read_shop(registry)

    def test_missing_or_tampered_ciphertext_is_not_ready(self):
        self.authorize()
        registry = stores.load_registry()
        shop = next(item for item in stores.list_shops(registry) if item["id"] == "store-a")
        encrypted = self.vault_root / (shop["credential_ref"] + ".fernet")
        encrypted.write_bytes(b"tampered")
        self.assertFalse(stores.resolve_credentials(shop)["ready"])
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.select_read_shop(registry)
        encrypted.unlink()
        self.assertFalse(stores.resolve_credentials(shop)["ready"])

    def test_wrong_reference_or_other_shop_ciphertext_is_rejected(self):
        self.authorize()
        shop = next(item for item in stores.list_shops(stores.load_registry()) if item["id"] == "store-a")
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.read_vault_credentials({**shop, "credential_ref": "../../outside"})
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.read_vault_credentials({**shop, "id": "store-b"}, vault_root=self.vault_root)

    def test_missing_master_key_does_not_generate_new_key_on_read(self):
        self.authorize()
        (self.vault_root / "master.key").unlink()
        shop = next(item for item in stores.list_shops(stores.load_registry()) if item["id"] == "store-a")
        self.assertFalse(stores.resolve_credentials(shop)["ready"])
        self.assertFalse((self.vault_root / "master.key").exists())

    def test_new_authorization_does_not_replace_lost_master_key_with_existing_ciphertexts(self):
        self.authorize()
        old_registry = self.registry_path.read_bytes()
        (self.vault_root / "master.key").unlink()
        with self.assertRaisesRegex(authorization.ShopAuthorizationError, "主密钥已遗失"):
            self.authorize("store-b")
        self.assertFalse((self.vault_root / "master.key").exists())
        self.assertEqual(old_registry, self.registry_path.read_bytes())

    def test_registry_rejects_any_plaintext_credential_fields_even_short_values(self):
        for field in ("client_id", "api_key", "credentials", "client_secret"):
            registry = stores.example_registry()
            registry["shops"][0][field] = "tiny"
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    stores.save_registry(registry, self.registry_path)

    def test_invalid_input_fails_before_any_network_request(self):
        for kwargs in (
            {"shop_id": "../evil"}, {"shop_id": ".."}, {"client_id": "bad"},
            {"api_key": "bad key"}, {"default_currency_code": "INVALID"},
            {"display_name": SECRET_KEY}, {"shop_id": SECRET_KEY},
        ):
            with self.subTest(kwargs=list(kwargs)):
                with self.assertRaises(authorization.ShopAuthorizationError):
                    self.authorize(**kwargs)
        self.assertEqual(self.transport.calls, [])

    def test_corrupt_existing_registry_is_not_overwritten(self):
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(authorization.ShopAuthorizationError):
            self.authorize()
        self.assertEqual(self.registry_path.read_text(), "{corrupt")
        self.assertFalse(self.vault_root.exists())

    def test_save_failure_keeps_previous_registry_and_reclaims_only_uncommitted_cipher(self):
        self.authorize()
        old_registry = self.registry_path.read_bytes()
        old_files = set(self.vault_root.iterdir())
        with patch.object(stores, "save_registry", side_effect=OSError("disk full")):
            with self.assertRaises(authorization.ShopAuthorizationError):
                self.authorize(api_key="replacement-secret-key")
        self.assertEqual(old_registry, self.registry_path.read_bytes())
        self.assertEqual(old_files, set(self.vault_root.iterdir()))

    def test_concurrent_authorizations_keep_both_shops_and_default(self):
        barrier = threading.Barrier(2)
        def factory(credentials):
            barrier.wait(timeout=10)
            return FakeTransport()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda shop_id: self.authorize(shop_id, transport_factory=factory), ["store-a", "store-b"]))
        registry = stores.load_registry()
        self.assertEqual(set(stores.enabled_shop_ids(registry)), {"store-a", "store-b"})
        self.assertEqual(sum(bool(row["is_default"]) for row in results), 1)
        self.assertEqual(len(list(self.vault_root.glob("*.fernet"))), 2)

    def test_no_silent_fallback_from_disabled_or_absent_default(self):
        self.authorize()
        registry = stores.load_registry()
        registry["default_read_shop"] = None
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.select_read_shop(registry)
        registry["default_read_shop"] = "default"  # disabled example
        with self.assertRaises(authorization.ShopAuthorizationError):
            authorization.select_read_shop(registry)
        self.assertEqual(authorization.select_read_shop(registry, "store-a")["id"], "store-a")

    def test_legacy_environment_credentials_still_work(self):
        registry = stores.example_registry()
        registry["shops"][0]["enabled"] = True
        stores.save_registry(registry, self.registry_path)
        with patch.dict(os.environ, {"OZON_DEFAULT_CLIENT_ID": SECRET_CLIENT, "OZON_DEFAULT_API_KEY": SECRET_KEY}):
            result = authorization.test_shop_connection("default", transport_factory=lambda credentials: FakeTransport())
            self.assertTrue(result["credentials_ready"])
            self.assertEqual(result["credential_storage"], "environment")
        self.assertFalse(self.vault_root.exists())


if __name__ == "__main__":
    unittest.main()
