"""Unified shop form; Seller and Performance remain independent encrypted stores.

The two credential stores cannot be committed atomically.  Return explicit
partial-success flags after the first commit instead of reporting a fictitious
all-or-nothing transaction.  No product or advertising mutation is performed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import shop_authorization as authorization, stores
from .performance_access import PerformanceAccess, PerformanceAccessError


class ShopManagementError(ValueError):
    def __init__(self, message: str, *, http_status: int = 422):
        super().__init__(message)
        self.http_status = http_status


def _pair(first: str | None, second: str | None, name: str) -> tuple[str, str] | None:
    values = tuple(str(value or "").strip() for value in (first, second))
    if bool(values[0]) != bool(values[1]):
        raise ShopManagementError(f"{name} 两个凭据字段必须一起填写；都留空则保留原授权")
    return values if values[0] else None


class ShopManagement:
    def __init__(self, runtime_root: Path | str, *, registry_path=None, vault_root=None,
                 transport_factory: Callable | None = None, performance_factory: Callable | None = None,
                 invalidate_shop_cache: Callable | None = None):
        self.runtime_root = Path(runtime_root)
        self.registry_path, self.vault_root = registry_path, vault_root
        self.transport_factory = transport_factory
        self.performance = (performance_factory or PerformanceAccess)(self.runtime_root / "performance")
        self.invalidate_shop_cache = invalidate_shop_cache

    def list_shops(self) -> list[dict[str, Any]]:
        return [{**row, "advertising": self.performance.public_status(row["id"])}
                for row in authorization.list_authorized_shops(registry_path=self.registry_path,
                                                              vault_root=self.vault_root)]

    def _existing_secrets(self, existing: dict | None) -> tuple[str, ...]:
        if not existing:
            return ()
        seller_client, seller_key = stores.credential_values(existing)
        status = self.performance.public_status(existing["id"])
        advertising: tuple[str, ...] = ()
        if status.get("connection_status") == "credential_error":
            raise ShopManagementError("原广告凭据无法读取，请恢复保险库备份；本次未修改店铺资料")
        if status.get("configured"):
            # Internal-only decryption for preventing credentials from being
            # copied into public display metadata.  Never return this value.
            loaded = self.performance._loaded(existing["id"])
            advertising = (loaded["client_id"], loaded["client_secret"])
        return (seller_client, seller_key, *advertising)

    def save(self, *, mode: str, shop_id: str, display_name: str,
             default_currency_code: str = "CNY", seller_client_id: str | None = None,
             seller_api_key: str | None = None, advertising_client_id: str | None = None,
             advertising_client_secret: str | None = None, make_default: bool = False) -> dict[str, Any]:
        if mode not in ("create", "update"):
            raise ShopManagementError("请选择新增或修改店铺")
        valid_id = authorization._validate_shop_id(shop_id)
        name = str(display_name or "").strip()
        if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ShopManagementError("请输入 1–100 字的店铺名称，不能包含控制字符")
        if default_currency_code not in ("CNY", "RUB", "USD", "EUR"):
            raise ShopManagementError("请选择 CNY、RUB、USD 或 EUR 默认币种")
        seller_pair = _pair(seller_client_id, seller_api_key, "Seller API")
        advertising_pair = _pair(advertising_client_id, advertising_client_secret, "广告 API")
        if mode == "create" and seller_pair is None:
            raise ShopManagementError("新增店铺必须填写 Seller Client-Id 和 API Key")
        registry = authorization._read_registry(self.registry_path, self.vault_root)
        existing = next((row for row in stores.list_shops(registry) if row["id"] == valid_id), None)
        if mode == "create" and existing is not None:
            raise ShopManagementError("店铺 ID 已存在，请选择修改或使用新的店铺 ID", http_status=409)
        if mode == "update" and existing is None:
            raise ShopManagementError("要修改的店铺不存在，请重新读取店铺列表", http_status=404)
        existing_secrets = self._existing_secrets(existing)
        known_secrets = list(existing_secrets)
        for row in stores.list_shops(registry):
            if row["id"] == valid_id:
                continue
            try:
                known_secrets.extend(self._existing_secrets(row))
            except (ValueError, OSError):
                # A damaged unrelated vault is not an authorization to expose
                # any readable credentials, nor a reason to block other shops.
                continue
        new_secrets = (*(seller_pair or ()), *(advertising_pair or ()))
        secrets = (*known_secrets, *new_secrets)
        if any(value and (value in name or value in valid_id) for value in secrets):
            raise ShopManagementError("店铺 ID 和名称不能包含 Seller 或广告凭据")
        if existing and seller_pair and existing_secrets[0] and seller_pair[0] != existing_secrets[0]:
            raise ShopManagementError("Seller Client-Id 与原店铺不一致，请新增店铺，不能替换已有店铺账户", http_status=409)
        if existing and seller_pair and not existing_secrets[0] and (
                existing.get("enabled") or existing.get("checked_at") or existing.get("credential_ref")):
            raise ShopManagementError("无法确认原店铺 Seller Client-Id，请恢复原凭据或新增店铺，未替换现有授权", http_status=409)

        common = {"registry_path": self.registry_path, "vault_root": self.vault_root,
                  "default_currency_code": default_currency_code, "forbidden_values": secrets}
        if seller_pair:
            shop = authorization.authorize_shop(valid_id, name, *seller_pair,
                                                transport_factory=self.transport_factory,
                                                expected_mode=mode, require_same_client=True,
                                                make_default=False, **common)
        else:
            shop = authorization.update_shop_metadata(valid_id, name, **common)
        result = {"ok": True, "mode": mode, "shop": shop, "partial": False,
                  "seller_saved": bool(seller_pair), "metadata_saved": True,
                  "advertising_saved": False, "default_saved": False,
                  "api_writes_performed": False, "advertising_write_enabled": False}
        warnings, warning_codes = [], []
        if self.invalidate_shop_cache:
            try:
                self.invalidate_shop_cache(valid_id)
            except Exception:
                warning_codes.append("cache_refresh_failed")
                warnings.append("店铺资料已保存，但读取缓存未刷新，请刷新工作台后重试读取类目。")
        if advertising_pair:
            try:
                self.performance.authorize(valid_id, *advertising_pair)
                result["advertising_saved"] = True
            except Exception:
                # Even trusted integration errors may contain a reflected
                # credential; do not echo the error or pretend Seller rolled back.
                warning_codes.append("advertising_save_failed")
                warnings.append("店铺资料与 Seller 授权已保存或保留；广告授权未保存，原广告授权保持不变。请检查 Performance 服务账号、权限及网络后重新填写广告凭据。")
        if make_default:
            try:
                result["shop"] = authorization.set_default_shop(valid_id, registry_path=self.registry_path,
                                                                vault_root=self.vault_root)
                result["default_saved"] = True
            except Exception:
                warning_codes.append("default_save_failed")
                warnings.append("店铺资料已保存，但未能设置默认店铺；请先启用并验证 Seller 授权，再单独设为默认。")
        try:
            advertising_status = self.performance.public_status(valid_id)
        except Exception:
            warning_codes.append("advertising_status_unavailable")
            advertising_status = {"shop": valid_id, "configured": False,
                                  "connection_status": "credential_error", "ad_writes_enabled": False}
            warnings.append("店铺资料已保存，但广告授权状态暂时无法读取，请稍后刷新；不会开启广告。")
        result["shop"] = {**result["shop"], "advertising": advertising_status}
        result["warning_codes"] = warning_codes
        if warnings:
            result["partial"] = True
            result["warning"] = " ".join(warnings)
        return result
