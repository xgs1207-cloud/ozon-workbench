"""店铺注册表（多店铺）：文件里**只存环境变量名，绝不存密钥**。

与上游 ``ozon-adapter/shops.json`` 的约定一致，但换成我们自己的配置目录：

    {
      "schema_version": "1.0.0",
      "default_read_shop": "default",
      "shops": [
        {
          "id": "default", "name": "default", "display_name": "我的 Ozon 店",
          "enabled": true,
          "client_id_env": "OZON_DEFAULT_CLIENT_ID",
          "api_key_env": "OZON_DEFAULT_API_KEY",
          "connection_status": "unknown",
          "default_currency_code": "CNY", "default_vat": "0",
          "default_unbranded_value": "Нет бренда"
        }
      ]
    }

``resolve_credentials()`` 只从环境变量读密钥；写注册表时若发现疑似真密钥会被拦下。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

SCHEMA_VERSION = "1.0.0"
DEFAULT_PATH = Path("config") / "shops.json"

_SHOP_REQUIRED = ("id", "name", "enabled", "client_id_env", "api_key_env")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SECRET_HINT = re.compile(r"^[A-Za-z0-9_\-]{20,}$")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def example_registry() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "default_read_shop": "default",
        "shops": [
            {
                "id": "default",
                "name": "default",
                "display_name": "我的 Ozon 店（示例，请改成自己的）",
                "enabled": False,
                "client_id_env": "OZON_DEFAULT_CLIENT_ID",
                "api_key_env": "OZON_DEFAULT_API_KEY",
                "connection_status": "unknown",
                "default_currency_code": "CNY",
                "default_vat": "0",
                "default_unbranded_value": "Нет бренда",
            }
        ],
    }


def load_registry(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_PATH
    registry = _read_json(target)
    if not registry:
        return example_registry()
    registry.setdefault("schema_version", SCHEMA_VERSION)
    registry.setdefault("shops", [])
    return registry


def ensure_registry(path: Path | str | None = None) -> dict[str, Any]:
    """没有注册表就写一份示例（enabled=false，避免误上传）。"""
    target = Path(path) if path else DEFAULT_PATH
    if not target.is_file():
        save_registry(example_registry(), target)
    return load_registry(target)


def validate_registry(registry: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    shops = registry.get("shops")
    if not isinstance(shops, list) or not shops:
        return ["注册表里没有任何店铺"]
    seen: set[str] = set()
    for index, shop in enumerate(shops):
        if not isinstance(shop, Mapping):
            problems.append(f"shops[{index}] 不是对象")
            continue
        for key in _SHOP_REQUIRED:
            if key not in shop:
                problems.append(f"shops[{index}] 缺少 {key}")
        shop_id = str(shop.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", shop_id):
            problems.append(f"shops[{index}].id 非法：{shop_id!r}")
        if shop_id in seen:
            problems.append(f"店铺 id 重复：{shop_id}")
        seen.add(shop_id)
        for env_key in ("client_id_env", "api_key_env"):
            value = str(shop.get(env_key) or "")
            if not value:
                continue
            if not _ENV_NAME.match(value):
                # 环境变量名按约定是全大写；不像变量名的一律拒绝（防止把真密钥写进来）
                problems.append(
                    f"shops[{index}].{env_key} 必须是环境变量名（大写字母/数字/下划线）：{value[:12]}…"
                )
        if _SECRET_HINT.match(str(shop.get("client_id") or "")) or _SECRET_HINT.match(str(shop.get("api_key") or "")):
            problems.append(f"shops[{index}] 疑似把密钥写进了注册表，请改用 *_env 字段")
    default_read = registry.get("default_read_shop")
    if default_read and default_read not in seen:
        problems.append(f"default_read_shop={default_read!r} 不在店铺列表里")
    return problems


def save_registry(registry: Mapping[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    problems = validate_registry(registry)
    if problems:
        raise ValueError("注册表不合法：" + "；".join(problems[:5]))
    target = Path(path) if path else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(registry)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def list_shops(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(shop) for shop in (registry.get("shops") or []) if isinstance(shop, Mapping)]


def enabled_shop_ids(registry: Mapping[str, Any]) -> list[str]:
    return [str(shop.get("id")) for shop in list_shops(registry) if shop.get("enabled") is True]


def upsert_shop(registry: MutableMapping[str, Any], shop: Mapping[str, Any]) -> dict[str, Any]:
    shops = [dict(item) for item in (registry.get("shops") or []) if isinstance(item, Mapping)]
    shop_id = str(shop.get("id") or "")
    for index, item in enumerate(shops):
        if str(item.get("id")) == shop_id:
            merged = {**item, **dict(shop)}
            shops[index] = merged
            break
    else:
        shops.append(dict(shop))
    registry["shops"] = shops
    return dict(registry)


def set_enabled(registry: MutableMapping[str, Any], shop_id: str, enabled: bool) -> dict[str, Any]:
    for item in registry.get("shops") or []:
        if isinstance(item, MutableMapping) and str(item.get("id")) == str(shop_id):
            item["enabled"] = bool(enabled)
            return dict(registry)
    raise ValueError(f"店铺不存在：{shop_id}")


def resolve_credentials(shop: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """从环境变量取密钥；返回缺失项而不是抛错（便于界面提示"还没配密钥"）。"""
    source = env if env is not None else os.environ
    client_id_env = str(shop.get("client_id_env") or "")
    api_key_env = str(shop.get("api_key_env") or "")
    client_id = source.get(client_id_env) if client_id_env else None
    api_key = source.get(api_key_env) if api_key_env else None
    missing = [
        name
        for name, value in ((client_id_env, client_id), (api_key_env, api_key))
        if name and not value
    ]
    return {
        "shop_id": shop.get("id"),
        "client_id_env": client_id_env,
        "api_key_env": api_key_env,
        "has_client_id": bool(client_id),
        "has_api_key": bool(api_key),
        "missing_env": missing,
        "ready": not missing,
    }


def credential_report(registry: Mapping[str, Any], env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    return [resolve_credentials(shop, env) for shop in list_shops(registry)]


def shop_summary(registry: Mapping[str, Any], env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """给人和界面看的一行式摘要（**绝不包含密钥本身**，只说"有没有配"）。"""
    rows: list[dict[str, Any]] = []
    for shop, credentials in zip(list_shops(registry), credential_report(registry, env)):
        rows.append(
            {
                "id": shop.get("id"),
                "display_name": shop.get("display_name") or shop.get("name") or shop.get("id"),
                "enabled": bool(shop.get("enabled")),
                "credentials_ready": credentials["ready"],
                "missing_env": credentials["missing_env"],
                "client_id_env": credentials["client_id_env"],
                "api_key_env": credentials["api_key_env"],
                "default_currency_code": shop.get("default_currency_code"),
                "default_vat": shop.get("default_vat"),
            }
        )
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    """店铺注册表命令行：看/改 enabled、体检凭据（不打印任何密钥）。

    - ``--list``：列出店铺（含"凭据是否就绪"）
    - ``--enable <id>`` / ``--disable <id>``：开/关店铺（决定它会不会参与提交）
    - ``--check``：只体检；有任何"启用但缺凭据"或"一个都没启用"时退出码非 0
    """
    import argparse

    parser = argparse.ArgumentParser(description="店铺注册表：列出/开关/体检（密钥只在环境变量里，不会打印）")
    parser.add_argument("--registry", default=None, help="注册表路径（默认 config/shops.json）")
    parser.add_argument("--list", action="store_true", help="列出店铺")
    parser.add_argument("--enable", action="append", dest="enable", help="启用某个店铺 id（可重复）")
    parser.add_argument("--disable", action="append", dest="disable", help="停用某个店铺 id（可重复）")
    parser.add_argument("--check", action="store_true", help="体检：启用的店铺是否都有凭据")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    registry = ensure_registry(args.registry)
    changed = False
    for shop_id in args.enable or []:
        registry = set_enabled(registry, shop_id, True)
        changed = True
    for shop_id in args.disable or []:
        registry = set_enabled(registry, shop_id, False)
        changed = True
    if changed:
        save_registry(registry, args.registry)

    rows = shop_summary(registry)
    problems = validate_registry(registry)
    enabled = [row for row in rows if row["enabled"]]
    if not enabled:
        problems.append("没有任何启用的店铺（enabled=true）—— 提交前必须至少启用一个")
    for row in enabled:
        if not row["credentials_ready"]:
            problems.append(f"店铺 {row['id']} 已启用但缺凭据：{', '.join(row['missing_env'])}（去 /etc/ozon-workbench.env 配）")

    if args.json:
        print(json.dumps({"ok": not problems, "shops": rows, "problems": problems}, ensure_ascii=False, indent=2))
    else:
        print(f"店铺注册表：{args.registry or 'config/shops.json'}｜共 {len(rows)} 个")
        for row in rows:
            mark = "✅" if row["enabled"] and row["credentials_ready"] else ("⚠️" if row["enabled"] else "⏸")
            credentials = "凭据就绪" if row["credentials_ready"] else "缺 " + "、".join(row["missing_env"] or ["(未配置 *_env)"])
            print(f"  {mark} {row['id']}｜{row['display_name']}｜enabled={row['enabled']}｜{credentials}")
        print("\n结论：" + ("可以提交" if not problems else "还不能提交"))
        for item in problems:
            print(f"  - {item}")
        if not enabled:
            print("\n启用示例：python -m pipeline.stores --enable default")
    return 0 if not problems else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
