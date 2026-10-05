"""店铺注册表与多店铺发布台账测试（幂等、凭据只认环境变量、契约合规）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from pipeline import stores  # noqa: E402
from pipeline.publications import (  # noqa: E402
    ACTION_CREATE,
    ACTION_SKIP,
    load_publications,
    plan_publications,
    record_publication,
    render_plan,
    store_has_task,
    store_status,
)

HAS_CONTRACTS = len(available_contracts()) > 0


class StoreRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "config" / "shops.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_registry_writes_disabled_example(self):
        registry = stores.ensure_registry(self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(stores.validate_registry(registry), [])
        self.assertEqual(stores.enabled_shop_ids(registry), [])  # 示例默认不启用，防误传

    def test_validate_catches_duplicate_ids_and_secrets(self):
        registry = stores.example_registry()
        registry["shops"].append(dict(registry["shops"][0]))
        self.assertTrue(any("重复" in item for item in stores.validate_registry(registry)))

        registry = stores.example_registry()
        registry["shops"][0]["api_key_env"] = "abcdef1234567890abcdef1234567890"
        self.assertTrue(any("必须是环境变量名" in item for item in stores.validate_registry(registry)))

        registry = stores.example_registry()
        registry["shops"][0]["api_key"] = "abcdef1234567890abcdef1234567890"
        self.assertTrue(any("写进了注册表" in item for item in stores.validate_registry(registry)))

    def test_uppercase_env_names_are_accepted(self):
        registry = stores.example_registry()
        self.assertEqual(stores.validate_registry(registry), [])
        registry["shops"][0]["client_id_env"] = "OZON_SHOP_A_CLIENT_ID"
        registry["shops"][0]["api_key_env"] = "OZON_SHOP_A_API_KEY"
        self.assertEqual(stores.validate_registry(registry), [])

    def test_upsert_and_toggle(self):
        registry = stores.ensure_registry(self.path)
        stores.upsert_shop(
            registry,
            {
                "id": "shop-b",
                "name": "shop-b",
                "display_name": "第二家店",
                "enabled": False,
                "client_id_env": "OZON_SHOP_B_CLIENT_ID",
                "api_key_env": "OZON_SHOP_B_API_KEY",
            },
        )
        stores.set_enabled(registry, "shop-b", True)
        saved = stores.save_registry(registry, self.path)
        self.assertEqual(stores.enabled_shop_ids(saved), ["shop-b"])
        with self.assertRaises(ValueError):
            stores.set_enabled(registry, "nope", True)

    def test_credentials_come_from_env_only(self):
        registry = stores.example_registry()
        shop = stores.list_shops(registry)[0]
        missing = stores.resolve_credentials(shop, env={})
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["missing_env"], ["OZON_DEFAULT_CLIENT_ID", "OZON_DEFAULT_API_KEY"]
        )
        ready = stores.resolve_credentials(
            shop, env={"OZON_DEFAULT_CLIENT_ID": "123", "OZON_DEFAULT_API_KEY": "secret"}
        )
        self.assertTrue(ready["ready"])
        report = stores.credential_report(registry, env={})
        self.assertEqual(len(report), 1)


class PublicationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/909090909.html",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [{"sku_id": "S1", "purchase_price_cny": 10}, {"sku_id": "S2", "purchase_price_cny": 11}],
            },
        )
        self.product_dir = self.products / self.summary["product_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_skips_disabled_and_creates_others(self):
        plan = plan_publications(
            self.product_dir,
            ["shop-a", "shop-b"],
            sku_ids=["S1", "S2"],
            enabled_store_ids=["shop-a"],
        )
        actions = {row["store_id"]: row["action"] for row in plan}
        self.assertEqual(actions, {"shop-a": ACTION_CREATE, "shop-b": ACTION_SKIP})
        self.assertIn("未启用", [row["reason"] for row in plan if row["store_id"] == "shop-b"][0])
        self.assertIn("| shop-a | create |", render_plan(plan))

    def test_plan_is_idempotent_after_task_id(self):
        self.assertFalse(store_has_task(self.product_dir, "shop-a"))
        self.assertEqual(
            [row["action"] for row in plan_publications(self.product_dir, ["shop-a"])], [ACTION_CREATE]
        )
        record_publication(
            self.product_dir, "shop-a", sku_id="S1", offer_id="OFFER-S1", task_id="TASK-1", status="submitted"
        )
        self.assertTrue(store_has_task(self.product_dir, "shop-a"))
        self.assertEqual(store_status(self.product_dir, "shop-a"), "submitted")

        plan = plan_publications(self.product_dir, ["shop-a"])
        self.assertEqual(plan[0]["action"], ACTION_SKIP)
        self.assertIn("task_id", plan[0]["reason"])

    def test_record_updates_same_sku_in_place(self):
        record_publication(self.product_dir, "shop-a", sku_id="S1", task_id="T1")
        record_publication(self.product_dir, "shop-a", sku_id="S1", task_id="T1", ozon_product_id="123")
        payload = load_publications(self.product_dir)
        rows = payload["stores"]["shop-a"]["sku_publications"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ozon_product_id"], "123")

    def test_failed_store_does_not_block_others(self):
        record_publication(self.product_dir, "shop-a", sku_id="S1", status="failed", errors=[{"code": "x"}])
        record_publication(self.product_dir, "shop-b", sku_id="S1", task_id="T2", status="submitted")
        payload = load_publications(self.product_dir)
        self.assertEqual(payload["stores"]["shop-a"]["status"], "failed")
        self.assertEqual(payload["stores"]["shop-b"]["status"], "submitted")
        plan = plan_publications(self.product_dir, ["shop-a", "shop-b"])
        actions = {row["store_id"]: row["action"] for row in plan}
        self.assertEqual(actions["shop-a"], ACTION_CREATE)  # 失败的店可以重试
        self.assertEqual(actions["shop-b"], ACTION_SKIP)

    @unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
    def test_payload_matches_contract(self):
        record_publication(self.product_dir, "shop-a", sku_id="S1", offer_id="O1", task_id="T1")
        payload = load_publications(self.product_dir)
        problems = validate_contract("store-publications", payload)
        self.assertEqual(problems, [], problems)


if __name__ == "__main__":
    unittest.main()
