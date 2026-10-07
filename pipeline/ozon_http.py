"""Ozon Seller API **只读**适配器（类目树 / 类目属性 / 字典值）。

设计要点：

- **注入式传输层**：``OzonClient`` 只依赖一个 ``Transport`` 协议（``post(path, body) -> dict``），
  真实 HTTP 是 ``UrllibTransport``，测试用 ``FixtureTransport``（离线、零网络）。
  这样"接口形状"与"网络"解耦，凭据到位后只需换传输层。
- **凭据只从环境变量读**：店铺注册表（``config/shops.json``）里存的是环境变量名；
  缺凭据时报清楚缺哪个变量，而不是抛一个模糊的 401。
- **只读**：本模块只实现读取类目元数据；创建/更新/库存接口一律不在此实现（有测试锁定）。
- ⚠️ **未用真实凭据验证过**：``contracts/fixtures/`` 里的响应样例是按 Ozon 官方 API 文档字段构造的，
  拿到凭据后应先用 `python -m pipeline.ozon_http --check` 对齐一次再上量。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

SCHEMA_VERSION = "1.0.0"
BASE_URL = "https://api-seller.ozon.ru"

PATH_TREE = "/v1/description-category/tree"
PATH_ATTRIBUTES = "/v1/description-category/attribute"
PATH_ATTRIBUTE_VALUES = "/v1/description-category/attribute/values"
PATH_ATTRIBUTE_VALUES_SEARCH = "/v1/description-category/attribute/values/search"

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
MAX_VALUES_PER_ATTRIBUTE = 200


class OzonHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, path: str | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.body = body


class Transport(Protocol):
    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass
class OzonCredentials:
    client_id: str = field(repr=False)
    api_key: str = field(repr=False)
    base_url: str = BASE_URL
    shop_id: str | None = None

    @classmethod
    def from_shop(
        cls,
        shop: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        *,
        base_url: str = BASE_URL,
    ) -> "OzonCredentials":
        from pipeline.stores import resolve_credentials

        report = resolve_credentials(shop, env)
        if not report["ready"]:
            raise OzonHttpError(
                f"店铺 {report['shop_id']} 缺少凭据环境变量：{', '.join(report['missing_env'])}"
                "（在进程环境里设置它们，不要把密钥写进注册表）"
            )
        from pipeline.stores import credential_values

        client_id, api_key = credential_values(shop, env)
        return cls(
            client_id=client_id,
            api_key=api_key,
            base_url=base_url,
            shop_id=str(report["shop_id"]) if report.get("shop_id") else None,
        )


class UrllibTransport:
    """真实 HTTP（POST JSON + Client-Id / Api-Key 头）。可注入 ``urlopen`` 便于测试。"""

    def __init__(
        self,
        credentials: OzonCredentials,
        *,
        timeout: int = 30,
        urlopen: Any | None = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen

    def _redact(self, text: Any) -> str:
        sanitized = str(text)
        for secret in (self.credentials.api_key, self.credentials.client_id):
            if secret:
                sanitized = sanitized.replace(secret, "[已隐藏]")
        return sanitized

    def build_request(self, path: str, body: Mapping[str, Any]) -> urllib.request.Request:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            f"{self.credentials.base_url.rstrip('/')}{path}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Client-Id": self.credentials.client_id,
                "Api-Key": self.credentials.api_key,
            },
            method="POST",
        )

    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        request = self.build_request(path, body)
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = self._redact(error.read().decode("utf-8"))[:300]
            except Exception:  # noqa: BLE001 - 读取错误体失败不影响主错误
                detail = ""
            raise OzonHttpError(
                f"Ozon 返回 HTTP {error.code}：{detail or self._redact(error.reason)}", status=error.code, path=path, body=detail
            ) from error
        except urllib.error.URLError as error:
            raise OzonHttpError(f"无法连接 Ozon：{self._redact(error.reason)}", path=path) from error
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise OzonHttpError(f"响应不是 JSON：{self._redact(raw)[:200]}", path=path) from error
        if not isinstance(value, dict):
            raise OzonHttpError(f"响应不是对象：{type(value).__name__}", path=path)
        return value


class FixtureTransport:
    """离线传输层：按路径返回录制样例，用于测试与 `--check`。"""

    def __init__(self, fixtures: Mapping[str, Any] | None = None, *, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else FIXTURE_DIR
        self.fixtures = dict(fixtures or {})
        self.calls: list[dict[str, Any]] = []

    def _load(self, path: str) -> Any:
        if path in self.fixtures:
            return self.fixtures[path]
        name = {
            PATH_TREE: "ozon-category-tree.json",
            PATH_ATTRIBUTES: "ozon-category-attributes.json",
            PATH_ATTRIBUTE_VALUES: "ozon-attribute-values.json",
        }.get(path)
        if not name:
            raise OzonHttpError(f"夹具里没有该路径：{path}", path=path)
        target = self.directory / name
        if not target.is_file():
            raise OzonHttpError(f"缺少夹具文件：{target}", path=path)
        return json.loads(target.read_text(encoding="utf-8"))

    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "body": dict(body)})
        value = self._load(path)
        if callable(value):
            value = value(body)
        return value if isinstance(value, dict) else {"result": value}


class OzonClient:
    """只读客户端：类目树、类目属性、字典值。"""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def fetch_category_tree(self, *, language: str = "ZH_HANS") -> dict[str, Any]:
        return self.transport.post(PATH_TREE, {"language": language})

    def fetch_category_attributes(
        self, *, category_id: int, type_id: int, language: str = "ZH_HANS"
    ) -> dict[str, Any]:
        return self.transport.post(
            PATH_ATTRIBUTES,
            {
                "description_category_id": int(category_id),
                "type_id": int(type_id),
                "language": language,
            },
        )

    def fetch_attribute_values(
        self,
        *,
        attribute_id: int,
        category_id: int,
        type_id: int,
        limit: int = 1000,
        language: str = "ZH_HANS",
        last_value_id: int = 0,
    ) -> dict[str, Any]:
        if not 1 <= int(limit) <= 2000 or int(last_value_id) < 0:
            raise ValueError("字典分页 limit 必须为 1–2000，last_value_id 不能为负数")
        return self.transport.post(
            PATH_ATTRIBUTE_VALUES,
            {
                "attribute_id": int(attribute_id),
                "description_category_id": int(category_id),
                "type_id": int(type_id),
                "language": language,
                "last_value_id": int(last_value_id),
                "limit": int(limit),
            },
        )

    def search_attribute_values(
        self,
        *,
        attribute_id: int,
        category_id: int,
        type_id: int,
        value: str,
        limit: int = 10,
        language: str = "ZH_HANS",
    ) -> dict[str, Any]:
        """在（可能几千个值的）字典里按关键词精确查值。

        用途举例：品牌字典 1000+ 且分页，但"无品牌"的官方值 ``Нет бренда`` 可以用这个端点直接查到，
        不必把整个字典拉下来。
        """
        if len(str(value).strip()) < 2 or not 1 <= int(limit) <= 100:
            raise ValueError("字典搜索至少输入 2 个字符，limit 必须为 1–100")
        return self.transport.post(
            PATH_ATTRIBUTE_VALUES_SEARCH,
            {
                "attribute_id": int(attribute_id),
                "description_category_id": int(category_id),
                "type_id": int(type_id),
                "value": str(value),
                "limit": int(limit),
            },
        )


# --------------------------------------------------------------------- 规范化


def find_category_in_tree(
    tree_response: Mapping[str, Any], *, category_id: int, type_id: int
) -> dict[str, Any] | None:
    """在类目树里找 (category_id, type_id)，返回名称与路径（找不到返回 None）。

    Ozon 的类目树里 ``description_category_id`` 在上一层、``type_id`` 在叶子层，
    所以要带着父级的 category_id 往下走。
    """

    def walk(node: Mapping[str, Any], path: list[str], inherited_category_id: int | None) -> dict[str, Any] | None:
        if node.get("disabled") is True:
            return None
        name = str(node.get("category_name") or node.get("type_name") or node.get("name") or "")
        here = [*path, name] if name else list(path)
        current_category_id = _to_int_or_none(node.get("description_category_id") or node.get("category_id")) or inherited_category_id
        node_type_id = _to_int_or_none(node.get("type_id"))
        if (
            node_type_id is not None
            and current_category_id is not None
            and int(node_type_id) == int(type_id)
            and int(current_category_id) == int(category_id)
            and not node.get("children")
        ):
            return {
                "category_id": int(current_category_id),
                "type_id": int(node_type_id),
                "name": name,
                "path": here,
            }
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                found = walk(child, here, current_category_id)
                if found:
                    return found
        return None

    for item in tree_response.get("result") or []:
        if isinstance(item, Mapping):
            found = walk(item, [], None)
            if found:
                return found
    return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_attribute(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把 Ozon 的原始属性对象整理成我们 ``ozon-category-attributes`` 契约的形状。"""
    return {
        "attribute_id": int(raw.get("id") or raw.get("attribute_id") or 0),
        "attribute_name": str(raw.get("name") or raw.get("attribute_name") or "unknown"),
        "required": bool(raw.get("is_required") if raw.get("is_required") is not None else raw.get("required")),
        "type": str(raw.get("type") or "String"),
        "dictionary_id": _to_int_or_none(raw.get("dictionary_id")),
        "complex_id": _to_int_or_none(raw.get("attribute_complex_id") or raw.get("complex_id")),
        "is_collection": bool(raw.get("is_collection") or False),
        "allowed_values": [
            {"id": int(item.get("id")), "value": str(item.get("value"))}
            for item in (raw.get("allowed_values") or [])
            if isinstance(item, Mapping) and item.get("id") and item.get("value")
        ],
        "values_truncated": bool(raw.get("values_truncated") or False),
    }


def build_category_snapshot(
    *,
    product_id: str,
    category_id: int,
    type_id: int,
    category_name: str,
    attributes_response: Mapping[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    """产出 ``ozon-category-attributes.schema.json`` 形状的快照。"""
    raw_attributes = [item for item in (attributes_response.get("result") or []) if isinstance(item, Mapping)]
    attributes = [normalize_attribute(item) for item in raw_attributes]
    attributes = [item for item in attributes if item["attribute_id"] > 0]
    warnings: list[str] = []
    if attributes_response.get("has_next"):
        warnings.append("类目属性分页未取完（has_next=true）：当前实现只取第一页")
    if not attributes:
        warnings.append("Ozon 返回的属性列表为空")
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "fetched_at": fetched_at,
        "api_endpoint": PATH_ATTRIBUTES,
        "category_id": int(category_id),
        "category_name": category_name or "unknown",
        "type_id": int(type_id),
        "attributes": attributes,
        "warnings": warnings,
    }


def attach_dictionary_values(
    snapshot: dict[str, Any],
    *,
    client: OzonClient,
    max_values: int = MAX_VALUES_PER_ATTRIBUTE,
) -> dict[str, Any]:
    """给带字典的属性补上可选值（用于品牌这类必须走字典的字段）。"""
    filled = 0
    for attribute in snapshot.get("attributes") or []:
        dictionary_id = attribute.get("dictionary_id")
        if not dictionary_id:
            continue
        response = client.fetch_attribute_values(
            attribute_id=int(attribute["attribute_id"]),
            category_id=int(snapshot["category_id"]),
            type_id=int(snapshot["type_id"]),
            limit=max_values,
        )
        values = [
            {"id": int(item.get("id")), "value": str(item.get("value"))}
            for item in (response.get("result") or [])
            if isinstance(item, Mapping) and item.get("id") and item.get("value")
        ][:max_values]
        if values:
            attribute["allowed_values"] = values
            attribute["values_truncated"] = bool(response.get("has_next"))
            filled += 1
    snapshot.setdefault("warnings", []).append(f"已补充 {filled} 个字典属性的可选值")
    return snapshot


def check_connectivity(client: OzonClient, *, category_id: int, type_id: int) -> dict[str, Any]:
    """`--check`：只做只读连通性检查，返回摘要（不写任何文件、不发写请求）。"""
    tree = client.fetch_category_tree()
    found = find_category_in_tree(tree, category_id=category_id, type_id=type_id)
    attributes = client.fetch_category_attributes(category_id=category_id, type_id=type_id)
    raw_attributes = attributes.get("result") or []
    required = [item for item in raw_attributes if isinstance(item, Mapping) and item.get("is_required")]
    return {
        "category_found_in_tree": bool(found),
        "category_path": (found or {}).get("path"),
        "attributes_total": len(raw_attributes),
        "attributes_required": len(required),
        "dictionary_attributes": len(
            [item for item in raw_attributes if isinstance(item, Mapping) and _to_int_or_none(item.get("dictionary_id"))]
        ),
        "api_writes_performed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ozon 只读接口检查（类目树/属性）")
    parser.add_argument("--check", action="store_true", help="只读连通性检查")
    parser.add_argument("--shop", default=None, help="店铺 id（从 config/shops.json 读环境变量名）")
    parser.add_argument("--registry", default=None)
    parser.add_argument("--category-id", type=int, required=False)
    parser.add_argument("--type-id", type=int, required=False)
    parser.add_argument("--fixture-dir", default=None, help="用夹具代替真实网络（离线自检）")
    args = parser.parse_args(argv)

    from pipeline.stores import list_shops, load_registry

    try:
        if args.fixture_dir:
            transport: Transport = FixtureTransport(directory=Path(args.fixture_dir))
            shop_label = args.shop or "fixture"
        else:
            registry = load_registry(args.registry)
            shops = list_shops(registry)
            shop = next((item for item in shops if str(item.get("id")) == str(args.shop)), None) if args.shop else (shops[0] if shops else None)
            if not shop:
                raise OzonHttpError("没有可用店铺：先配置 config/shops.json")
            credentials = OzonCredentials.from_shop(shop)
            transport = UrllibTransport(credentials)
            shop_label = str(shop.get("id"))
        client = OzonClient(transport)
        if not args.check:
            parser.error("目前只支持 --check")
        summary = check_connectivity(
            client, category_id=args.category_id or 0, type_id=args.type_id or 0
        )
    except OzonHttpError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "shop": shop_label, **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
