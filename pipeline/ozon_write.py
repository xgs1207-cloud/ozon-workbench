"""Ozon Seller API **写**适配器：``/v3/product/import`` 提交（可离线测试）。

设计要点：

- **注入式传输层**：复用只读适配器那套 ``Transport`` 协议（``post(path, body) -> dict``），
  真实网络是 ``UrllibTransport``，测试/演练是 ``FixtureTransport``；
- **绝不提交库存字段**：请求体里不出现 ``stock/stocks/warehouse/warehouses``，
  有正则断言 + 测试守着（原项目硬禁令）；
- **写请求的重试策略很保守**：
  * 收到明确 HTTP 状态（429/5xx）→ 可重试（Ozon 的 import 以 ``offer_id`` 为键，属更新语义，重试不会多建卡片）；
  * 连接层异常（超时/断网）→ **绝不自动重试**：结果未知，自动重试可能造成重复提交，
    这里如实返回"结果未知，请人工核对 import 任务列表"；
- **字典属性必须走 ``dictionary_value_id``**：有字典 id 的属性不能只传文本值（否则 Ozon 会拒）；
- ⚠️ 请求字段按 Ozon 官方 v3 文档构造，**未用真实凭据验证过**；上线前请用
  ``python -m pipeline.ozon_write --payload <payload.json> --show-request`` 检查请求体，
  再用 ``--send`` 小批量试单。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .ozon_http import OzonCredentials, OzonHttpError, Transport, UrllibTransport

SCHEMA_VERSION = "1.0.0"
PATH_IMPORT = "/v3/product/import"
PATH_IMPORT_INFO = "/v1/product/import/info"

WEIGHT_UNIT = "g"
DIMENSION_UNIT = "mm"
MAX_IMAGES_PER_ITEM = 15
MAX_NAME_LENGTH = 255

RETRY_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0

#: 一旦出现在请求体里就说明我们提交了库存字段
FORBIDDEN_FIELD_PATTERN = re.compile(r'"(stock|stocks|inventory|warehouse|warehouses)"\s*:', re.IGNORECASE)


class OzonWriteError(RuntimeError):
    """写请求的确定性失败（已重试或不该重试）。"""

    def __init__(self, message: str, *, ambiguous: bool = False, attempts: int = 1, raw: Any = None) -> None:
        super().__init__(message)
        #: True 表示"结果未知"（连接层异常），调用方必须人工核对而不是重试
        self.ambiguous = ambiguous
        self.attempts = attempts
        self.raw = raw


# --------------------------------------------------------------------- 请求构建


def _money(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _item_measurements(payload: Mapping[str, Any], sku_id: str) -> dict[str, Any]:
    """取该 SKU 的包装尺寸重量（优先 SKU 级，回退商品级）；没有就不填（不编造）。"""
    surface = payload.get("sku_measurements") if isinstance(payload.get("sku_measurements"), Mapping) else {}
    package = surface.get("package_dimensions") if isinstance(surface.get("package_dimensions"), Mapping) else None
    result: dict[str, Any] = {}
    if not package:
        return result
    for key, field in (("length_mm", "depth"), ("width_mm", "width"), ("height_mm", "height")):
        value = package.get(key)
        if isinstance(value, int) and value > 0:
            result[field] = value
    weight = package.get("weight_g")
    if isinstance(weight, int) and weight > 0:
        result["weight"] = weight
    if result:
        if "weight" in result:
            result["weight_unit"] = WEIGHT_UNIT
        if any(field in result for field in ("depth", "width", "height")):
            result["dimension_unit"] = DIMENSION_UNIT
    return result


def _attribute_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """翻译成 ``/v3/product/import`` 的 attributes 形状。

    ⚠️ 真机踩坑：Ozon 的 import 接口要求值放在 ``values`` 数组里::

        {"id": 8229, "values": [{"dictionary_value_id": 92612, "value": "床单"}]}

    早先写成 ``{"id": 8229, "dictionary_value_id": 92612}``（把"字典查值接口"的返回形状
    当成了提交形状）→ 请求里这两条必填属性的值变成 None，真提交必被拒。
    """
    attribute_id = item.get("attribute_id")
    if not attribute_id:
        return None
    dictionary_id = item.get("dictionary_value_id")
    value = item.get("value")
    entry: dict[str, Any] = {"id": int(attribute_id), "values": []}
    payload_value: dict[str, Any] = {}
    if isinstance(dictionary_id, int) and dictionary_id > 0:
        payload_value["dictionary_value_id"] = int(dictionary_id)
    if value not in (None, ""):
        payload_value["value"] = str(value).lower() if isinstance(value, bool) else str(value)
    if not payload_value:
        return None
    entry["values"].append(payload_value)
    return entry


def build_import_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把上传载荷翻译成 ``/v3/product/import`` 的请求体（不含库存字段）。"""
    category = payload.get("category") if isinstance(payload.get("category"), Mapping) else {}
    category_id = int(category.get("category_id") or 0)
    type_id = int(category.get("type_id") or 0)
    if category_id < 1 or type_id < 1:
        raise OzonWriteError("载荷里的类目不完整（description_category_id / type_id 必须 > 0）")

    variants = [item for item in (payload.get("variants") or []) if isinstance(item, Mapping)]
    if not variants:
        raise OzonWriteError("载荷里没有可提交的变体（variants 为空）")

    common_attributes = [item for item in (payload.get("attributes") or []) if isinstance(item, Mapping)]
    images = [item for item in (payload.get("images") or []) if isinstance(item, Mapping)]
    detail_urls = [
        str(item.get("url"))
        for item in images
        if item.get("role") == "detail" and str(item.get("url") or "").startswith("https://")
    ]
    description = str(payload.get("description") or "")

    items: list[dict[str, Any]] = []
    for index, variant in enumerate(variants, start=1):
        sku_id = str(variant.get("source_sku_id") or f"S{index}")
        attributes: list[dict[str, Any]] = []
        sku_attributes = [row for row in variant.get("attributes") or [] if isinstance(row, Mapping)]
        override_ids = {row.get("attribute_id") for row in sku_attributes}
        effective = [row for row in common_attributes if row.get("attribute_id") not in override_ids] + sku_attributes
        grouped: dict[int, dict[str, Any]] = {}
        for item in effective:
            if not isinstance(item, Mapping):
                continue
            entry = _attribute_entry(item)
            if entry:
                aggregate = grouped.setdefault(entry["id"], {"id": entry["id"], "values": []})
                for value in entry["values"]:
                    if value not in aggregate["values"]:
                        aggregate["values"].append(value)
        attributes = list(grouped.values())

        primary = str(variant.get("color_image") or "")
        gallery: list[str] = []
        for url in [str(item) for item in ([primary] if primary.startswith("https://") else []) + detail_urls]:
            if url.startswith("https://") and url not in gallery:
                gallery.append(url)

        item: dict[str, Any] = {
            "offer_id": str(variant.get("offer_id") or ""),
            "name": str(variant.get("display_name_ru") or "")[:MAX_NAME_LENGTH],
            "description_category_id": category_id,
            "type_id": type_id,
            "price": _money(variant.get("price")),
            "currency_code": str(variant.get("currency_code") or "RUB"),
            "vat": "0",
            "attributes": attributes,
            "images": gallery[1:MAX_IMAGES_PER_ITEM] if len(gallery) > 1 else gallery,
        }
        if description:
            item["description"] = description
        if primary.startswith("https://"):
            item["primary_image"] = primary
        item.update(_item_measurements(payload, sku_id))
        items.append(item)

    request = {"items": items}
    if FORBIDDEN_FIELD_PATTERN.search(json.dumps(request, ensure_ascii=False)):
        raise OzonWriteError("请求体里出现库存字段：原项目硬禁令，拒绝提交")
    return request


# --------------------------------------------------------------------- 响应解析


def parse_import_response(
    response: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """解析 ``/v3/product/import`` 响应 → 任务号 + 逐项结果。"""
    result = response.get("result") if isinstance(response.get("result"), Mapping) else response
    task_id = result.get("task_id")
    errors: list[dict[str, Any]] = []
    raw_items = [item for item in (result.get("items") or []) if isinstance(item, Mapping)]

    offer_to_sku: dict[str, str] = {}
    for index, variant in enumerate(((payload or {}).get("variants") or []), start=1):
        if isinstance(variant, Mapping):
            offer_to_sku[str(variant.get("offer_id"))] = str(variant.get("source_sku_id") or f"S{index}")

    items: list[dict[str, Any]] = []
    for raw in raw_items:
        offer_id = str(raw.get("offer_id") or "")
        item_errors = [
            {
                "code": str(item.get("code") or "OZON_ITEM_ERROR"),
                "message": str(item.get("message") or ""),
                "attribute_id": item.get("attribute_id"),
            }
            for item in (raw.get("errors") or [])
            if isinstance(item, Mapping)
        ]
        product_id = raw.get("product_id")
        items.append(
            {
                "source_sku_id": offer_to_sku.get(offer_id) or offer_id or "unknown",
                "offer_id": offer_id or "unknown",
                "product_id": int(product_id) if isinstance(product_id, int) and product_id > 0 else None,
                "status": "failed" if item_errors else "submitted",
                "errors": item_errors,
            }
        )
        errors.extend(item_errors)

    if not task_id and not items:
        errors.append({"code": "OZON_EMPTY_RESPONSE", "message": "Ozon 既没有返回 task_id 也没有返回 items"})

    return {
        "task_id": str(task_id) if task_id not in (None, "", 0) else None,
        "items": items,
        "errors": errors,
    }


def fetch_import_status(transport: Transport, task_id: str | int) -> dict[str, Any]:
    """查 import 任务状态（只读，用于上传后确认）。"""
    return transport.post(PATH_IMPORT_INFO, {"task_id": int(task_id)})


# --------------------------------------------------------------------- 发送（含重试）


def post_import(
    transport: Transport,
    body: Mapping[str, Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    """发送 import 请求；返回 (响应, 实际尝试次数)。

    - 明确的 429/5xx：按退避重试；
    - 连接层异常（``status is None``）：**不重试**，抛 ``ambiguous=True`` 的错误让调用方人工核对。
    """
    attempts = 0
    last_error: OzonHttpError | None = None
    while attempts < max(1, int(max_attempts)):
        attempts += 1
        try:
            return dict(transport.post(PATH_IMPORT, body)), attempts
        except OzonHttpError as error:
            last_error = error
            if error.status is None:
                raise OzonWriteError(
                    f"提交结果未知（连接层异常：{error}）：不要自动重试，请到 Ozon 后台核对 import 任务列表",
                    ambiguous=True,
                    attempts=attempts,
                ) from error
            if error.status not in RETRY_STATUSES or attempts >= max(1, int(max_attempts)):
                break
            sleep(backoff_seconds * attempts)
    assert last_error is not None
    raise OzonWriteError(
        f"提交失败（HTTP {last_error.status}，尝试 {attempts} 次）：{last_error}",
        attempts=attempts,
        raw=last_error.body,
    )


# --------------------------------------------------------------------- uploader


def _default_transport_factory(credentials: OzonCredentials) -> Transport:
    return UrllibTransport(credentials)


class OzonWriteUploader:
    """真实提交器：每个店铺用自己的凭据与传输层（凭据只从环境变量读）。"""

    name = "ozon-api"
    performs_api_writes = True

    def __init__(
        self,
        *,
        transport_factory: Callable[[OzonCredentials], Transport] | None = None,
        registry_path: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport_factory = transport_factory or _default_transport_factory
        self.registry_path = registry_path
        self.env = env
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep

    def transport_for(self, store_id: str) -> Transport:
        from .stores import list_shops, load_registry

        registry = load_registry(self.registry_path)
        shop = next((item for item in list_shops(registry) if str(item.get("id")) == str(store_id)), None)
        if not shop:
            raise OzonWriteError(f"店铺 {store_id} 不在注册表里（config/shops.json）")
        try:
            credentials = OzonCredentials.from_shop(shop, self.env)
        except OzonHttpError as error:
            raise OzonWriteError(str(error)) from error
        return self.transport_factory(credentials)

    def submit(self, payload: Mapping[str, Any], *, store_id: str) -> dict[str, Any]:
        body = build_import_request(payload)
        transport = self.transport_for(store_id)
        try:
            response, attempts = post_import(
                transport,
                body,
                max_attempts=self.max_attempts,
                backoff_seconds=self.backoff_seconds,
                sleep=self.sleep,
            )
        except OzonWriteError as error:
            return {
                "status": "failed",
                "task_id": None,
                "api_writes_performed": True,
                "api_writes": int(error.attempts),
                "items": [],
                "errors": [{"code": "AMBIGUOUS" if error.ambiguous else "OZON_HTTP_ERROR", "message": str(error)}],
                "raw_response": {"ambiguous": error.ambiguous, "attempts": error.attempts, "raw": error.raw},
                "note": "结果未知：请人工核对" if error.ambiguous else "Ozon 拒绝了本次提交（已按策略重试）",
            }

        parsed = parse_import_response(response, payload=payload)
        failed_items = [item for item in parsed["items"] if item["status"] == "failed"]
        if parsed["task_id"]:
            status = "processing"  # import 返回任务号，最终结果要查 import/info
        elif failed_items:
            status = "failed"
        else:
            status = "submitted"
        return {
            "status": status,
            "task_id": parsed["task_id"],
            "api_writes_performed": True,
            "api_writes": attempts,
            "items": parsed["items"],
            "errors": parsed["errors"],
            "raw_response": {"response": response, "attempts": attempts, "api": f"POST {PATH_IMPORT}"},
            "note": (
                f"已提交 import 任务（task_id={parsed['task_id']}），最终状态需查 {PATH_IMPORT_INFO}"
                if parsed["task_id"]
                else "Ozon 未返回 task_id"
            ),
        }


    def confirm(
        self,
        task_id: str | int,
        *,
        store_id: str,
        payload: Mapping[str, Any] | None = None,
        max_attempts: int = 6,
        interval_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """查询 import 任务最终状态（**只读**：/v1/product/import/info）。"""
        from .ozon_status import confirm_task

        return confirm_task(
            self.transport_for(store_id),
            task_id,
            payload=payload,
            max_attempts=max_attempts,
            interval_seconds=interval_seconds,
            sleep=self.sleep,
        )


# --------------------------------------------------------------------- CLI


def _load_payload(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OzonWriteError(f"载荷不是 JSON 对象：{path}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ozon 写适配器：查看/发送 /v3/product/import 请求")
    parser.add_argument("--payload", required=True, help="output/store-runs/<店铺>/payload.json")
    parser.add_argument("--show-request", action="store_true", help="只打印将发送的请求体（默认行为，不联网）")
    parser.add_argument("--send", action="store_true", help="真正提交（会写 Ozon）")
    parser.add_argument("--store", default=None, help="店铺 id（--send 必填）")
    parser.add_argument("--registry", default=None)
    parser.add_argument("--i-understand-this-hits-ozon", action="store_true", help="--send 的安全确认")
    args = parser.parse_args(argv)

    try:
        payload = _load_payload(args.payload)
        request = build_import_request(payload)
    except (OSError, ValueError, OzonWriteError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if not args.send:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "show-request",
                    "api": f"POST {PATH_IMPORT}",
                    "items": len(request["items"]),
                    "api_writes_performed": False,
                    "request": request,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.i_understand_this_hits_ozon or not args.store:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "拒绝发送：需要 --store <店铺id> 与 --i-understand-this-hits-ozon（这会真的写 Ozon）",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    uploader = OzonWriteUploader(registry_path=args.registry)
    receipt = uploader.submit(payload, store_id=args.store)
    print(json.dumps({"ok": receipt["status"] != "failed", "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
