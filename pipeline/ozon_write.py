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
import ipaddress
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .ozon_http import OzonCredentials, OzonHttpError, Transport, UrllibTransport
from .source_videos import validate_listing_video_url

SCHEMA_VERSION = "1.0.0"
PATH_IMPORT = "/v3/product/import"
PATH_IMPORT_INFO = "/v1/product/import/info"

WEIGHT_UNIT = "g"
DIMENSION_UNIT = "mm"
MAX_IMAGES_PER_ITEM = 50
MAX_NAME_LENGTH = 200
DESCRIPTION_ATTRIBUTE_ID = 4191
HASHTAGS_ATTRIBUTE_ID = 23171

RETRY_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0

#: 一旦出现在请求体里就说明我们提交了库存字段
FORBIDDEN_FIELD_PATTERN = re.compile(r'"(stock|stocks|inventory|warehouse|warehouses)"\s*:', re.IGNORECASE)


class OzonWriteError(RuntimeError):
    """写请求的确定性失败（已重试或不该重试）。"""

    def __init__(self, message: str, *, ambiguous: bool = False, attempts: int = 1,
                 raw: Any = None, http_status: int | None = None) -> None:
        super().__init__(message)
        #: True 表示"结果未知"（连接层异常），调用方必须人工核对而不是重试
        self.ambiguous = ambiguous
        self.attempts = attempts
        self.raw = raw
        self.http_status = http_status


# --------------------------------------------------------------------- 请求构建


def _money(value: Any) -> str:
    try:
        number = float(value)
        if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
            raise ValueError
        formatted = f"{number:.2f}"
        if float(formatted) <= 0:
            raise ValueError
        return formatted
    except (TypeError, ValueError) as error:
        raise OzonWriteError("每个规格必须填写有限正数售价") from error


def _item_measurements(payload: Mapping[str, Any], sku_id: str) -> dict[str, Any]:
    """只取载荷中已确认的含包装尺寸重量，不用商品本体尺寸代替。"""
    surface = payload.get("sku_measurements") if isinstance(payload.get("sku_measurements"), Mapping) else {}
    package = surface.get("package_dimensions") if isinstance(surface.get("package_dimensions"), Mapping) else None
    result: dict[str, Any] = {}
    if not package:
        return result
    for key, field in (("length_mm", "depth"), ("width_mm", "width"), ("height_mm", "height")):
        value = package.get(key)
        if type(value) is int and value > 0:
            result[field] = value
    weight = package.get("weight_g")
    if type(weight) is int and weight > 0:
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


def normalize_hashtags(value: Any) -> str:
    """Validate official hashtag syntax; never convert SEO keywords into tags."""
    if value in (None, "", []):
        return ""
    if isinstance(value, str):
        tags = value.split()
    elif isinstance(value, (list, tuple)) and all(isinstance(tag, str) for tag in value):
        tags = list(value)
    else:
        raise OzonWriteError("主题标签必须是字符串或字符串数组")
    if len(tags) > 30:
        raise OzonWriteError("主题标签最多30个")
    for tag in tags:
        if len(tag) > 30 or not tag.startswith("#") or len(tag) < 2 or not all(
            char.isalpha() or char.isdecimal() or char == "_" for char in tag[1:]
        ):
            raise OzonWriteError("主题标签需以#开头，仅字母、数字和下划线，每个最多30字符")
    if len(set(tags)) != len(tags):
        raise OzonWriteError("主题标签不能重复")
    return " ".join(tags)


def _media_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
        except ValueError:
            if re.fullmatch(r"[0-9a-fA-F:.]+", host):
                raise
    except ValueError as error:
        raise OzonWriteError("媒体地址必须为公开HTTPS地址，不能含登录凭据或本地地址") from error
    return url


def _video_entry(raw: Any, *, cover: bool = False) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise OzonWriteError("视频必须包含url和title等结构化字段")
    url = _media_url(raw.get("url"))
    if not cover:
        try:
            url = validate_listing_video_url(url, publication=raw.get("publication"))
        except ValueError as error:
            raise OzonWriteError(str(error)) from error
    format_value = str(raw.get("format") or "").lower().lstrip(".")
    path_format = Path(urlsplit(url).path).suffix.lower().lstrip(".")
    if (format_value and format_value not in ("mp4", "mov")) or (path_format and path_format not in ("mp4", "mov")):
        raise OzonWriteError("视频及视频封面必须为MP4/MOV，视频封面不是静态图片")
    for key, lower, upper in (("duration_seconds", 8, 30 if cover else 300), ("size_bytes", 1, 20 * 1024**2 if cover else 5 * 1024**3)):
        if raw.get(key) is not None:
            number = raw[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not lower <= number <= upper:
                raise OzonWriteError(f"视频{key}超出允许范围")
    if cover:
        return {"attributes": [{"id": 21845, "complex_id": 100002, "values": [{"value": url}]}]}
    title = str(raw.get("title") or "").strip()
    if not title:
        raise OzonWriteError("视频缺少标题，不能静默生成视频名称")
    return {"attributes": [
        {"id": 21841, "complex_id": 100001, "values": [{"value": url}]},
        {"id": 21837, "complex_id": 100001, "values": [{"value": title}]},
    ]}


def media_for_variant(payload: Mapping[str, Any], variant: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:
    """Explicit SKU overrides (including []/None) suppress common media."""
    videos = variant.get("videos") if "videos" in variant else payload.get("videos", [])
    if videos is None:
        videos = []
    if not isinstance(videos, list) or any(not isinstance(row, Mapping) for row in videos):
        raise OzonWriteError("videos必须是视频对象数组")
    sku_id = str(variant.get("source_sku_id") or "")
    selected = [row for row in videos if not row.get("source_sku_id") or str(row["source_sku_id"]) == sku_id]
    if len(selected) > 5:
        raise OzonWriteError("每个规格最多5个视频")
    cover = variant.get("video_cover") if "video_cover" in variant else payload.get("video_cover")
    if cover is not None and not isinstance(cover, Mapping):
        raise OzonWriteError("video_cover必须是视频对象或null")
    if cover and cover.get("source_sku_id") and str(cover["source_sku_id"]) != sku_id:
        cover = None
    return selected, cover


def build_import_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把上传载荷翻译成 ``/v3/product/import`` 的请求体（不含库存字段）。"""
    if payload.get("production_blockers"):
        raise OzonWriteError("商品存在上架阻断项，禁止构造可提交请求")
    if payload.get("complex_attributes"):
        raise OzonWriteError("未支持的complex_attributes不能静默丢弃，请使用规范videos/video_cover字段")
    category = payload.get("category") if isinstance(payload.get("category"), Mapping) else {}
    category_id = int(category.get("category_id") or 0)
    type_id = int(category.get("type_id") or 0)
    if category_id < 1 or type_id < 1:
        raise OzonWriteError("载荷里的类目不完整（description_category_id / type_id 必须 > 0）")

    variants = payload.get("variants")
    if not isinstance(variants, list) or not variants or any(not isinstance(item, Mapping) for item in variants):
        raise OzonWriteError("载荷里没有可提交的变体（variants 为空）")
    sku_ids = {str(row.get("source_sku_id") or f"S{index}") for index, row in enumerate(variants, 1)}
    for row in payload.get("videos") or []:
        if isinstance(row, Mapping) and row.get("source_sku_id") and str(row["source_sku_id"]) not in sku_ids:
            raise OzonWriteError("视频关联的规格不在本次选中规格中，不能静默丢弃")
    shared_cover = payload.get("video_cover")
    if isinstance(shared_cover, Mapping) and shared_cover.get("source_sku_id") and str(shared_cover["source_sku_id"]) not in sku_ids:
        raise OzonWriteError("视频封面关联的规格不在本次选中规格中，不能静默丢弃")

    common_attributes = [item for item in (payload.get("attributes") or []) if isinstance(item, Mapping)]
    images = [item for item in (payload.get("images") or []) if isinstance(item, Mapping)]
    detail_urls = [_media_url(item.get("url")) for item in images if item.get("role") == "detail"]
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
        # Confirmed attribute values win over legacy generated-copy aliases.
        if DESCRIPTION_ATTRIBUTE_ID not in grouped and description:
            grouped[DESCRIPTION_ATTRIBUTE_ID] = {"id": DESCRIPTION_ATTRIBUTE_ID, "values": [{"value": description}]}
        tags_input = variant.get("hashtags") if "hashtags" in variant else payload.get("hashtags")
        if HASHTAGS_ATTRIBUTE_ID not in grouped and tags_input is not None:
            tags = normalize_hashtags(tags_input)
            if tags:
                grouped[HASHTAGS_ATTRIBUTE_ID] = {"id": HASHTAGS_ATTRIBUTE_ID, "values": [{"value": tags}]}
        if HASHTAGS_ATTRIBUTE_ID in grouped:
            tags = normalize_hashtags(" ".join(str(v.get("value") or "") for v in grouped[HASHTAGS_ATTRIBUTE_ID]["values"]))
            grouped[HASHTAGS_ATTRIBUTE_ID]["values"] = [{"value": tags}]
        if DESCRIPTION_ATTRIBUTE_ID in grouped:
            if any(len(str(value.get("value") or "")) > 6000 for value in grouped[DESCRIPTION_ATTRIBUTE_ID]["values"]):
                raise OzonWriteError("简介最多6000字符")
        attributes = list(grouped.values())

        primary = _media_url(variant["color_image"]) if variant.get("color_image") else ""
        gallery: list[str] = []
        for url in [str(item) for item in ([primary] if primary else []) + detail_urls]:
            if url.startswith("https://") and url not in gallery:
                gallery.append(url)

        name = str(variant.get("display_name_ru") or payload.get("title") or "")
        if not name.strip() or len(name) > MAX_NAME_LENGTH or any(len(word) > 27 for word in name.split()):
            raise OzonWriteError("商品标题不能为空，最多200字符，单词最多27字符；禁止静默截断")
        if len(gallery) > MAX_IMAGES_PER_ITEM:
            raise OzonWriteError("每个商品最多50张图片，不能静默丢弃图片")
        videos, cover = media_for_variant(payload, variant)
        if "videos" in variant and any(row.get("source_sku_id") and str(row["source_sku_id"]) != sku_id for row in variant.get("videos") or []):
            raise OzonWriteError("规格视频不能引用另一规格")
        if "video_cover" in variant and variant.get("video_cover") and variant["video_cover"].get("source_sku_id") and str(variant["video_cover"]["source_sku_id"]) != sku_id:
            raise OzonWriteError("规格视频封面不能引用另一规格")
        complex_attributes = [_video_entry(video) for video in videos]
        if cover:
            complex_attributes.append(_video_entry(cover, cover=True))
        item: dict[str, Any] = {
            "offer_id": str(variant.get("offer_id") or ""),
            "name": name,
            "description_category_id": category_id,
            "type_id": type_id,
            "price": _money(variant.get("price")),
            "currency_code": str(variant.get("currency_code") or "RUB"),
            "vat": "0",
            "attributes": attributes,
            "images": gallery[1:] if primary.startswith("https://") else gallery,
            "complex_attributes": complex_attributes,
            "promotions": [{"type": "REVIEWS_PROMO", "operation": "DISABLE"}],
        }
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
        http_status=last_error.status,
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
                "ambiguous": error.ambiguous,
                "http_status": error.http_status,
                "items": [],
                "errors": [{"code": "AMBIGUOUS" if error.ambiguous else "OZON_HTTP_ERROR", "message": str(error)}],
                "raw_response": {"ambiguous": error.ambiguous, "attempts": error.attempts,
                                 "http_status": error.http_status, "raw": error.raw},
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
