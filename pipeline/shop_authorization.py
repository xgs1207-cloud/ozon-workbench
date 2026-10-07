"""店铺授权与加密凭据保险库（只读验证，不创建/修改任何 Ozon 商品）。

注册表只存不可猜测的 ``credential_ref``；对应密文和本机加密主密钥位于
``config/shop-vault``（或 WORKBENCH_SHOP_VAULT_ROOT）。需同时备份密文及主密钥。
Linux 文件权限 0600、保险库目录 0700；Windows 继承当前用户的目录 ACL。
所有公开函数仅返回白名单元数据，既不返回 Client-Id，也不返回 Api-Key。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import stores

PATH_ROLES = "/v1/roles"
PATH_TREE = "/v1/description-category/tree"
_SHOP_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_REFERENCE = re.compile(r"[a-f0-9]{32}\Z")
TransportFactory = Callable[[Any], Any]


class ShopAuthorizationError(ValueError):
    """可直接向前端显示的脱敏错误，不保留上游响应体。"""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_shop_id(shop_id: str) -> str:
    value = str(shop_id or "")
    if not _SHOP_ID.fullmatch(value) or value in (".", ".."):
        raise ShopAuthorizationError("店铺 ID 只能使用 1–64 位字母、数字、点、下划线或连字符")
    return value


def _root(value: Path | str | None = None, registry_path: Path | str | None = None) -> Path:
    return Path(value) if value is not None else stores.vault_root(registry_path)


def _fernet(root: Path, *, create: bool) -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ShopAuthorizationError("店铺授权加密依赖尚未安装，请先安装 requirements.txt") from None
    key_file = root / "master.key"
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            if not key_file.exists() and any(root.glob("*.fernet")):
                # 遗失主密钥时不可偷偷生成另一把钥匙，让其他店铺的密文永久失效。
                raise ShopAuthorizationError("店铺加密主密钥已遗失，请恢复完整凭据备份后重新授权")
            try:
                descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(Fernet.generate_key())
                    handle.flush()
                    os.fsync(handle.fileno())
        if not key_file.is_file() or key_file.stat().st_size > 128:
            raise ShopAuthorizationError("店铺加密主密钥缺失或损坏，请恢复服务器凭据备份")
        if os.name != "nt" and key_file.stat().st_mode & 0o077:
            raise ShopAuthorizationError("店铺加密主密钥权限不安全，请将其设为仅服务账号可读写（0600）")
        return Fernet(key_file.read_bytes())
    except ShopAuthorizationError:
        raise
    except (OSError, ValueError):
        raise ShopAuthorizationError("无法读取店铺加密主密钥，请检查服务器文件权限及凭据备份") from None


def _write_vault_credentials(root: Path, shop_id: str, client_id: str, api_key: str) -> str:
    cipher = _fernet(root, create=True)
    reference = uuid.uuid4().hex
    encoded = json.dumps(
        {"version": 1, "shop_id": shop_id, "client_id": client_id, "api_key": api_key},
        ensure_ascii=True,
    ).encode("utf-8")
    encrypted = cipher.encrypt(encoded)
    descriptor, name = tempfile.mkstemp(prefix=".vault-", dir=root)
    temporary = Path(name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        destination = root / f"{reference}.fernet"
        os.replace(temporary, destination)
    except OSError:
        raise ShopAuthorizationError("无法保存加密店铺凭据，请检查服务器目录权限") from None
    finally:
        if temporary.exists():
            temporary.unlink()
    return reference


def read_vault_credentials(
    shop: Mapping[str, Any], *, vault_root: Path | str | None = None
) -> tuple[str, str]:
    """仅供 stores.credential_values / HTTP 传输层调用，不属于对外 API。"""
    reference = str(shop.get("credential_ref") or "")
    if not _REFERENCE.fullmatch(reference):
        raise ShopAuthorizationError("店铺加密凭据引用无效，请重新授权")
    root = _root(vault_root)
    target = root / f"{reference}.fernet"
    try:
        if not target.is_file() or target.stat().st_size > 16_384:
            raise ShopAuthorizationError("店铺加密凭据缺失或损坏，请重新授权")
        if os.name != "nt" and target.stat().st_mode & 0o077:
            raise ShopAuthorizationError("店铺加密凭据权限不安全，请设置文件权限为 0600")
        cipher = _fernet(root, create=False)
        from cryptography.fernet import InvalidToken

        try:
            value = json.loads(cipher.decrypt(target.read_bytes()).decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError):
            raise ShopAuthorizationError("店铺加密凭据无法解密，请恢复完整备份或重新授权") from None
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("shop_id") != shop.get("id"):
            raise ShopAuthorizationError("店铺加密凭据与店铺不匹配，请重新授权")
        client_id, api_key = _validate_credentials(value.get("client_id"), value.get("api_key"))
        return client_id, api_key
    except OSError:
        raise ShopAuthorizationError("无法读取加密店铺凭据，请检查服务器文件权限") from None


def _validate_credentials(client_id: Any, api_key: Any) -> tuple[str, str]:
    client = str(client_id or "").strip()
    key = str(api_key or "").strip()
    if not re.fullmatch(r"[0-9]{1,20}", client):
        raise ShopAuthorizationError("Client-Id 应为 Ozon Seller API 页面显示的数字 ID")
    if not 8 <= len(key) <= 512 or not re.fullmatch(r"[!-~]+", key):
        raise ShopAuthorizationError("请填写有效的 Ozon Seller API Key，不能包含空格或换行")
    return client, key


def _safe_text(value: Any, client_id: str, api_key: str, *, limit: int = 120) -> str:
    text = str(value or "")
    for secret in (api_key, client_id):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return " ".join(text.split())[:limit]


def _read_registry(path: Path | str | None, vault_root: Path | str | None = None) -> dict[str, Any]:
    target = stores.registry_path(path)
    if target.exists():
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ShopAuthorizationError("店铺注册表无法读取，请先修复配置文件；未覆盖已有配置") from None
        if not isinstance(value, dict) or stores.validate_registry(value):
            raise ShopAuthorizationError("店铺注册表格式不正确，请先修复配置文件；未覆盖已有配置")
    else:
        value = stores.example_registry()
    for shop in value.get("shops") or []:
        if shop.get("credential_ref"):
            shop["_vault_root"] = str(_root(vault_root, path).resolve())
    return value


def _shop(registry: Mapping[str, Any], shop_id: str) -> dict[str, Any]:
    valid = _validate_shop_id(shop_id)
    selected = next((row for row in stores.list_shops(registry) if row.get("id") == valid), None)
    if selected is None:
        raise ShopAuthorizationError("店铺不存在，请先添加授权")
    return selected


def _tree_count(rows: list[Any], *, inherited_disabled: bool = False) -> int:
    total = 0
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        disabled = inherited_disabled or bool(item.get("disabled"))
        children = item.get("children") or []
        if not disabled and item.get("type_id") and not children:
            total += 1
        if isinstance(children, list):
            total += _tree_count(children, inherited_disabled=disabled)
    return total


def _check_credentials(client_id: str, api_key: str, factory: TransportFactory | None) -> dict[str, Any]:
    from .ozon_http import OzonCredentials, UrllibTransport

    try:
        transport = (factory or (lambda credentials: UrllibTransport(credentials, timeout=15)))(
            OzonCredentials(client_id=client_id, api_key=api_key)
        )
    except Exception:
        raise ShopAuthorizationError("无法初始化 Ozon 连接，请检查服务器网络配置") from None
    responses: dict[str, Any] = {}
    for path, body in ((PATH_ROLES, {}), (PATH_TREE, {"language": "ZH_HANS"})):
        try:
            response = transport.post(path, body)
        except Exception as error:
            status = getattr(error, "status", None)
            status = status if isinstance(status, int) and 100 <= status <= 599 else None
            stage = "API 密钥权限" if path == PATH_ROLES else "真实类目"
            if status in (401, 403):
                message = f"Ozon {stage}读取失败（HTTP {status}），请检查密钥有效期及对应只读权限"
            elif status == 429:
                message = "Ozon 请求过于频繁（HTTP 429），请稍后重试"
            elif status:
                message = f"Ozon {stage}读取失败（HTTP {status}），请稍后重试或检查店铺授权"
            else:
                message = f"无法连接 Ozon 读取{stage}，请检查服务器网络后重试"
            # 上游错误可能反射请求头；绝不保留 error/body，亦不串联异常上下文。
            raise ShopAuthorizationError(message, http_status=status) from None
        if not isinstance(response, Mapping):
            raise ShopAuthorizationError("Ozon 授权验证响应格式异常，请稍后重试")
        responses[path] = response
    roles_response = responses[PATH_ROLES]
    roles = roles_response.get("roles")
    tree_rows = responses[PATH_TREE].get("result")
    if not isinstance(roles, list) or not isinstance(tree_rows, list):
        raise ShopAuthorizationError("Ozon 未返回有效的权限列表或类目树，请检查 API Key 权限")
    category_count = _tree_count(tree_rows)
    if category_count == 0:
        raise ShopAuthorizationError("Ozon 未返回可用的末级类目，请检查店铺权限")
    methods: set[str] = set()
    names: list[str] = []
    for role in roles:
        if not isinstance(role, Mapping):
            continue
        name = _safe_text(role.get("name"), client_id, api_key)
        if name and name not in names:
            names.append(name)
        for method in role.get("methods") or []:
            if isinstance(method, str):
                methods.add(method)
    expires_at = roles_response.get("expires_at")
    if expires_at:
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, TypeError):
            raise ShopAuthorizationError("Ozon 返回的 API Key 有效期格式异常，请稍后重新验证") from None
        if expires <= datetime.now(timezone.utc):
            raise ShopAuthorizationError("Ozon API Key 已过期，请在 Seller API 页面创建新密钥后重新授权", http_status=401)
        expires_at = expires.isoformat().replace("+00:00", "Z")
    return {
        "connection_status": "connected",
        "checked_at": _now(),
        "expires_at": expires_at or None,
        "roles": names[:100],
        "category_count": category_count,
        "capabilities": {
            "read_categories": True,
            "read_attributes": "/v1/description-category/attribute" in methods,
            "read_dictionary_values": "/v1/description-category/attribute/values" in methods,
            "read_dictionary_search": "/v1/description-category/attribute/values/search" in methods,
            "import_products": bool(methods & {"/v3/product/import", "/v2/product/import"}),
        },
    }


def _public_shop(registry: Mapping[str, Any], shop_id: str) -> dict[str, Any]:
    row = next(row for row in stores.shop_summary(registry) if row.get("id") == shop_id)
    return {**row, "api_writes_performed": False}


def authorize_shop(
    shop_id: str,
    display_name: str,
    client_id: str,
    api_key: str,
    *,
    default_currency_code: str = "CNY",
    currency: str | None = None,
    registry_path: Path | str | None = None,
    vault_root: Path | str | None = None,
    transport_factory: TransportFactory | None = None,
    make_default: bool | None = None,
) -> dict[str, Any]:
    """验证新凭据后才替换密文及启用店铺，验证失败不覆盖原来的可用授权。"""
    valid_id = _validate_shop_id(shop_id)
    client, key = _validate_credentials(client_id, api_key)
    if key in valid_id or client in valid_id:
        raise ShopAuthorizationError("店铺 ID 不能包含 Client-Id 或 API Key，请填写自定义代号")
    name = str(display_name or "").strip()
    if not name or len(name) > 100 or key in name or client in name:
        raise ShopAuthorizationError("请输入 1–100 字的店铺名称，名称不能包含凭据")
    currency = currency or default_currency_code
    if currency not in ("CNY", "RUB", "USD", "EUR", "KZT", "BYN"):
        raise ShopAuthorizationError("不支持该默认币种，请选择 CNY、RUB、USD、EUR、KZT 或 BYN")
    # 先验证网络；锁内再读取最新注册表，避免并发添加覆盖彼此。
    status = _check_credentials(client, key, transport_factory)
    root = _root(vault_root, registry_path)
    with stores.registry_lock(registry_path):
        registry = _read_registry(registry_path, vault_root)
        existing = next((row for row in stores.list_shops(registry) if row.get("id") == valid_id), {})
        # 随机密文引用同时充当环境变量名后缀，避免不同合法 ID 规范化后发生冲突。
        reference = _write_vault_credentials(root, valid_id, client, key)
        prefix = "OZON_VAULT_" + reference.upper()
        shop = {
            **existing,
            "id": valid_id,
            "name": valid_id,
            "display_name": name,
            "enabled": True,
            "client_id_env": prefix + "_CLIENT_ID",
            "api_key_env": prefix + "_API_KEY",
            "credential_storage": "vault",
            "credential_ref": reference,
            "_vault_root": str(root.resolve()),
            "default_currency_code": currency,
            "default_vat": existing.get("default_vat") or "0",
            "default_unbranded_value": existing.get("default_unbranded_value") or "Нет бренда",
            **status,
        }
        stores.upsert_shop(registry, shop)
        default = registry.get("default_read_shop")
        enabled = set(stores.enabled_shop_ids(registry))
        if make_default or default not in enabled:
            registry["default_read_shop"] = valid_id
        try:
            stores.save_registry(registry, registry_path)
        except (OSError, ValueError):
            # 尚未提交到注册表的单个新密文可以安全回收；旧引用完全保留。
            try:
                (root / f"{reference}.fernet").unlink(missing_ok=True)
            except OSError:
                pass  # 未被任何店铺引用的孤立密文不影响原授权，避免覆盖真正的保存错误。
            raise ShopAuthorizationError("店铺授权验证成功，但无法保存配置，请检查服务器目录权限") from None
        return _public_shop(registry, valid_id)


def list_authorized_shops(
    *, registry_path: Path | str | None = None, vault_root: Path | str | None = None
) -> list[dict[str, Any]]:
    return stores.shop_summary(_read_registry(registry_path, vault_root))


def test_shop_connection(
    shop_id: str,
    *,
    registry_path: Path | str | None = None,
    vault_root: Path | str | None = None,
    transport_factory: TransportFactory | None = None,
) -> dict[str, Any]:
    """复查已有凭据，失败仅更新安全状态，不删除或替换原密文。"""
    valid_id = _validate_shop_id(shop_id)
    with stores.registry_lock(registry_path):
        registry = _read_registry(registry_path, vault_root)
        shop = _shop(registry, valid_id)
        loaded = False
        try:
            client, key = stores.credential_values(shop)
            client, key = _validate_credentials(client, key)
            loaded = True
            status = _check_credentials(client, key, transport_factory)
        except ShopAuthorizationError as error:
            # 不把错误响应体写进配置。禁用已失效的授权，避免后续误选。
            invalid_credentials = not loaded or error.http_status in (401, 403)
            stores.upsert_shop(registry, {
                "id": valid_id, "connection_status": "error", "checked_at": _now(),
                "enabled": False if invalid_credentials else bool(shop.get("enabled")),
            })
            if invalid_credentials and registry.get("default_read_shop") == valid_id:
                registry["default_read_shop"] = next(iter(stores.enabled_shop_ids(registry)), None)
            stores.save_registry(registry, registry_path)
            raise
        stores.upsert_shop(registry, {"id": valid_id, **status})
        stores.save_registry(registry, registry_path)
        return _public_shop(registry, valid_id)


def set_shop_enabled(
    shop_id: str,
    enabled: bool,
    *,
    registry_path: Path | str | None = None,
    vault_root: Path | str | None = None,
    transport_factory: TransportFactory | None = None,
) -> dict[str, Any]:
    valid_id = _validate_shop_id(shop_id)
    with stores.registry_lock(registry_path):
        registry = _read_registry(registry_path, vault_root)
        shop = _shop(registry, valid_id)
        if enabled:
            client, key = stores.credential_values(shop)
            client, key = _validate_credentials(client, key)
            stores.upsert_shop(registry, {"id": valid_id, **_check_credentials(client, key, transport_factory)})
        stores.set_enabled(registry, valid_id, enabled)
        if not enabled and registry.get("default_read_shop") == valid_id:
            registry["default_read_shop"] = next(iter(stores.enabled_shop_ids(registry)), None)
        elif enabled and registry.get("default_read_shop") not in stores.enabled_shop_ids(registry):
            registry["default_read_shop"] = valid_id
        stores.save_registry(registry, registry_path)
        return _public_shop(registry, valid_id)


def set_default_shop(
    shop_id: str,
    *, registry_path: Path | str | None = None,
    vault_root: Path | str | None = None,
) -> dict[str, Any]:
    valid_id = _validate_shop_id(shop_id)
    with stores.registry_lock(registry_path):
        registry = _read_registry(registry_path, vault_root)
        shop = _shop(registry, valid_id)
        if not shop.get("enabled") or not stores.resolve_credentials(shop)["ready"]:
            raise ShopAuthorizationError("请先启用并验证店铺授权，再设为默认读取店铺")
        registry["default_read_shop"] = valid_id
        stores.save_registry(registry, registry_path)
        return _public_shop(registry, valid_id)


def select_read_shop(registry: Mapping[str, Any], shop_id: str | None = None) -> dict[str, Any]:
    """显式选择已启用店铺；未指定时尊重 default_read_shop，不静默选另一家。"""
    selected = shop_id or registry.get("default_read_shop")
    if not selected:
        raise ShopAuthorizationError("没有默认读取店铺，请先在店铺授权中选择一个已启用店铺")
    shop = _shop(registry, str(selected))
    if not shop.get("enabled"):
        raise ShopAuthorizationError("选中的 Ozon 店铺已停用，请在店铺授权中启用或更换默认店铺")
    if not stores.resolve_credentials(shop)["ready"]:
        raise ShopAuthorizationError("选中的 Ozon 店铺凭据未就绪，请重新授权")
    if shop.get("expires_at"):
        expired = False
        try:
            expiry = datetime.fromisoformat(str(shop["expires_at"]).replace("Z", "+00:00"))
            expired = expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            pass
        if expired:
            raise ShopAuthorizationError("选中的 Ozon 店铺 API Key 已过期，请重新授权")
    return shop
