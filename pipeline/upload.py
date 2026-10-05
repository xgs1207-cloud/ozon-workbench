"""上传链路（M4 后半）：载荷构建 + 门禁 + 可替换的 uploader（默认只做干跑）。

关键设计：

- **载荷按店铺构建**（``shop_name`` 在载荷里），并严格对齐上游 ``ozon-upload-payload`` 契约；
- **门禁与载荷分离**：``production_blockers`` 列出所有阻断项，production 模式下 blocker 非空就拒绝提交；
- **绝不提交库存字段**：``api_request_template`` 里显式标记 ``inventory_fields_included: false``，
  并有一条断言式检查（载荷文本里不允许出现库存款式字段）；
- **干跑不写假 task_id**：``DryRunUploader`` 返回 ``status=skipped`` / ``task_id=None``，
  否则"幂等跳过"会在真正上传时把商品挡在门外；
- **单店失败不影响其他店**：逐店捕获异常并记账，最后汇总。

图片公网地址来自 ``output/image-public-urls.json``（由你的对象存储 adapter 产出，slot → https URL）。
这是替换原项目"24 小时 Cloudflare 隧道"的接入点。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from contracts import format_problems, validate_contract

from .context import PipelineGateError, StepContext
from .publications import (
    ACTION_CREATE,
    ACTION_SKIP,
    load_publications,
    plan_publications,
    record_publication,
    save_publications,
    store_has_task,
)

SCHEMA_VERSION = "1.0.0"
UPLOAD_MODE_DRY_RUN = "dry-run"
UPLOAD_MODE_PRODUCTION = "production"

IMAGE_URLS_FILE = "output/image-public-urls.json"
IMAGE_PLAN_FILE = "output/image-plan.json"
PRICING_FILE = "output/pricing-result.json"
ATTRIBUTES_FINAL = "output/ozon-attributes-final.json"
CATEGORY_FILE = "output/ozon-category.json"
CATEGORY_SNAPSHOT = "output/ozon-category-attributes.json"
COPY_FILE = "output/copy-ru.json"
ANALYSIS_FILE = "output/product-analysis.json"
GROUPING_FILE = "output/platform-grouping-result.json"

#: 一旦出现在载荷里就说明我们提交了库存字段（原项目硬禁令）
INVENTORY_FIELD_PATTERN = re.compile(r'"(stock|stocks|inventory|warehouse|warehouses)"\s*:', re.IGNORECASE)

_MAPPING_STATUS = {
    "merged_variants": "MAPPED",
    "rule_required": "RULE_REQUIRED",
    "separate_cards": "SEPARATE_CARDS_REQUIRED",
    "single_sku": "NOT_REQUIRED",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def offer_id_for(product_id: str, sku: Mapping[str, Any], index: int) -> str:
    """offer_id 必须稳定：优先用采集到的，否则由 product_id + sku_id 确定性生成。"""
    raw = str(sku.get("offer_id") or "").strip()
    if raw:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw)[:50]
    sku_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(sku.get("sku_id") or f"S{index}"))
    return f"{product_id}-{sku_id}"[:50]


# --------------------------------------------------------------------- 图片地址


def resolve_image_urls(product_dir: Path | str) -> dict[str, str]:
    """读对象存储 adapter 产出的 slot → https URL 映射。"""
    payload = _read_json(Path(product_dir) / IMAGE_URLS_FILE)
    urls = payload.get("urls") if isinstance(payload.get("urls"), Mapping) else payload
    return {
        str(key): str(value)
        for key, value in (urls or {}).items()
        if isinstance(value, str) and value.strip()
    }


def planned_slots(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for role, key in (("variant_main", "main_images"), ("detail", "detail_images")):
        for item in plan.get(key) or []:
            if isinstance(item, Mapping):
                rows.append({"slot": item.get("slot"), "role": role, "output_path": item.get("output_path")})
    return rows


# --------------------------------------------------------------------- 载荷构建


def _price_for_currency(row: Mapping[str, Any], currency: str) -> Any:
    """按**店铺合同币种**取价（真机踩坑：店铺合同是 CNY，我们提交了 RUB → Ozon 拒收
    ``currency_differs_from_contract``）。"""
    mapping = {
        "CNY": ("selling_price_cny", "breakdown_cny"),
        "RUB": ("selling_price_rub", "breakdown_rub"),
    }
    for key in mapping.get(currency, ()):  # 首选同币种字段
        value = row.get(key)
        if isinstance(value, Mapping):
            value = value.get("selling_price")
        if value not in (None, ""):
            return value
    return None


def build_upload_payload(
    product_dir: Path | str,
    *,
    shop_name: str,
    upload_mode: str = UPLOAD_MODE_DRY_RUN,
    image_urls: Mapping[str, str] | None = None,
    currency_code: str | None = None,
) -> dict[str, Any]:
    """构建单个店铺的上传载荷（并列出所有 production_blockers，不在这里抛错）。

    ``currency_code``：店铺合同币种（来自 ``config/shops.json`` 的 ``default_currency_code``）。
    真机踩坑：不传就会用默认 RUB，而店铺合同是 CNY → Ozon 报 ``currency_differs_from_contract``。
    """
    directory = Path(product_dir)
    product_id = directory.name
    blockers: list[str] = []

    source = _read_json(directory / "input" / "source.json")
    analysis = _read_json(directory / ANALYSIS_FILE)
    copy_bundle = _read_json(directory / COPY_FILE)
    attributes = _read_json(directory / ATTRIBUTES_FINAL)
    category = _read_json(directory / CATEGORY_FILE)
    snapshot = _read_json(directory / CATEGORY_SNAPSHOT)
    grouping = _read_json(directory / GROUPING_FILE)
    pricing = _read_json(directory / PRICING_FILE)
    plan = _read_json(directory / IMAGE_PLAN_FILE)
    urls = dict(image_urls) if image_urls is not None else resolve_image_urls(directory)

    from .sku_selection import active_skus, selection_state

    skus = active_skus(directory, source.get("skus") or [])
    if not skus:
        blockers.append("没有已选 SKU")
    state = selection_state(directory)
    if state.get("has_selection") and state.get("active_count") == 0:
        blockers.append("选择文件把 SKU 全部排除了（至少保留 1 个上架 SKU）")

    # 1) 类目必须来自 Ozon Seller API
    if str(category.get("metadata_source") or "") != "ozon_seller_api":
        blockers.append("类目不是来自 Ozon Seller API（metadata_source != ozon_seller_api）")
    if str(category.get("match_status") or "") not in {"api_confirmed", "api_match_needs_review"}:
        blockers.append(f"类目匹配状态不可用：{category.get('match_status')!r}")
    if str(snapshot.get("api_endpoint") or "") != "/v1/description-category/attribute":
        blockers.append("类目属性快照来源不可信（api_endpoint 不是 /v1/description-category/attribute）")

    # 2) 必填属性必须齐（提交前的硬门禁）
    summary = attributes.get("required_summary") if isinstance(attributes.get("required_summary"), Mapping) else {}
    missing = summary.get("missing")
    if missing is None:
        blockers.append("没有属性编译结果（先跑 field_completion）")
    elif int(missing) > 0:
        blockers.append(f"必需属性仍缺 {missing} 个：{summary.get('missing_attribute_ids')}")

    # 3) 文案
    title = str(copy_bundle.get("title_ru") or "").strip()
    description = str(copy_bundle.get("description_ru") or "").strip()
    if not title:
        blockers.append("缺少俄文标题（先跑 russian_copy）")
    if not description:
        blockers.append("缺少俄文简介（先跑 russian_copy）")

    # 4) 定价
    price_rows = {
        str(item.get("sku_id")): item
        for item in (pricing.get("skus") or [])
        if isinstance(item, Mapping)
    }
    pricing_missing: list[str] = []
    for index, sku in enumerate(skus, start=1):
        sku_id = str(sku.get("sku_id") or f"S{index}")
        row = price_rows.get(sku_id) or {}
        value = row.get("selling_price_rub")
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = 0.0
        if numeric <= 0:
            pricing_missing.append(sku_id)
    if pricing_missing:
        blockers.append(f"缺少卢布售价的 SKU：{pricing_missing}（先跑 measurements/pricing）")

    # 5) 尺寸重量（真实 Ozon 导入必需；我们只认可采集/确认过的数据，缺就是缺）
    from .measurements import load_measurements

    surface = load_measurements(directory)
    product_dims = surface.get("product")
    package_dims = surface.get("package")
    measurements: dict[str, Any] = {}
    if product_dims:
        measurements["product_dimensions"] = dict(product_dims)
    if package_dims:
        measurements["package_dimensions"] = dict(package_dims)
    if surface.get("source") == "missing":
        blockers.append("还没有尺寸重量数据（先跑 measurements 并人工确认尺寸重量）")
    for label, dims in (("商品", product_dims), ("包装", package_dims)):
        if not dims:
            blockers.append(f"缺少{label}尺寸（先跑 measurements，不接受编造值）")
            blockers.append(f"缺少{label}重量（先跑 measurements，不接受编造值）")
            continue
        if any(not isinstance(dims.get(axis), int) or int(dims.get(axis) or 0) <= 0 for axis in ("length_mm", "width_mm", "height_mm")):
            blockers.append(f"缺少{label}尺寸（先跑 measurements，不接受编造值）")
        if not isinstance(dims.get("weight_g"), int) or int(dims.get("weight_g") or 0) <= 0:
            blockers.append(f"缺少{label}重量（先跑 measurements，不接受编造值）")
    if product_dims and package_dims and not surface.get("hierarchy_ok", True):
        blockers.append("包装尺寸/重量小于商品本体（measurement hierarchy 不通过）")

    # 6) 图片公网地址
    slots = planned_slots(plan)
    missing_images = [str(item["slot"]) for item in slots if not str(urls.get(str(item["slot"])) or "").startswith("https://")]
    if not slots:
        blockers.append("没有图片计划（先跑 image_plan）")
    elif missing_images:
        blockers.append(f"以下图位没有可用的 https 公网地址：{missing_images}")

    # 7) 图片技术质检必须通过（本地真跑；语义维度缺失不算阻断）
    qc = _read_json(directory / "output" / "image-qc-report.json")
    qc_critical = qc.get("critical_failures")
    qc_decision = str(qc.get("decision") or "")
    if not isinstance(qc_critical, list) or not qc_decision:
        blockers.append("缺少图片质检报告（先跑 image_qc）")
        qc_critical = []
    elif qc_critical:
        blockers.append("图片质检出现致命问题：" + "、".join(str(item) for item in qc_critical))
    elif qc_decision == "reject":
        blockers.append("图片质检结论为 reject")

    # 8) 变体映射
    strategy = str(grouping.get("upload_strategy") or "")
    mapping_status = _MAPPING_STATUS.get(strategy, "RULE_REQUIRED")
    if mapping_status == "RULE_REQUIRED":
        blockers.append(f"变体映射需要人工确认：{grouping.get('reason') or '未说明'}")
    must_merge = bool(grouping.get("platform_can_merge"))
    if not grouping:
        blockers.append("没有变体规则结果（先跑 variant_rules）")

    currency = str(currency_code or category.get("default_currency_code") or "RUB").upper()

    variants: list[dict[str, Any]] = []
    by_sku = attributes.get("attributes_by_sku") if isinstance(attributes.get("attributes_by_sku"), Mapping) else {}
    for index, sku in enumerate(skus, start=1):
        sku_id = str(sku.get("sku_id") or f"S{index}")
        row = price_rows.get(sku_id) or {}
        price_value = _price_for_currency(row, currency)
        try:
            price_text = f"{float(price_value):.2f}"
        except (TypeError, ValueError):
            price_text = "0"
        sku_attributes = [
            {
                "attribute_id": item.get("attribute_id"),
                "attribute_name": item.get("attribute_name"),
                "value": item.get("value"),
                "dictionary_value_id": item.get("dictionary_value_id"),
            }
            for item in (by_sku.get(sku_id) or [])
            if isinstance(item, Mapping)
        ]
        color = next(
            (str(item.get("value")) for item in sku_attributes if "цвет" in str(item.get("attribute_name") or "").casefold()),
            str(sku.get("color_ru") or sku.get("color") or "не указан"),
        )
        color_image = next(
            (
                str(urls.get(str(item["slot"])))
                for item in slots
                if item["role"] == "variant_main"
                and str(plan_slot_sku(plan, str(item["slot"])) or "") == sku_id
                and urls.get(str(item["slot"]))
            ),
            "",
        ) or next((str(urls.get(str(item["slot"]))) for item in slots if urls.get(str(item["slot"]))), "")
        if not color_image:
            blockers.append(f"SKU {sku_id} 没有可用图片地址")
        variants.append(
            {
                "source_sku_id": sku_id,
                "offer_id": offer_id_for(product_id, sku, index),
                "sku_name": str(sku.get("name_zh") or sku.get("spec_zh") or sku_id),
                "display_name_ru": str(sku.get("name_ru") or f"{title} — {color}")[:200],
                "price": price_text,
                "currency_code": currency,
                "color": color,
                "color_image": color_image or "unknown",
                "attributes": sku_attributes,
                "variant_attribute_values": [
                    {"attribute_id": item["attribute_id"], "value": item["value"]} for item in sku_attributes
                ],
            }
        )

    images = [
        {
            "slot": item["slot"],
            "role": item["role"],
            "url": urls.get(str(item["slot"]), ""),
            "order": position,
            "output_path": item.get("output_path"),
        }
        for position, item in enumerate(slots, start=1)
    ]

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "product_name_cn": str(source.get("title_zh") or (analysis.get("facts") or {}).get("title_cn") or product_id),
        "upload_mode": upload_mode,
        "api_writes_performed": False,
        "generated_at": now_iso(),
        "shop_name": str(shop_name),
        "category": {
            "category_id": int(snapshot.get("category_id") or category.get("category_id") or 0),
            "type_id": int(snapshot.get("type_id") or category.get("type_id") or 0),
            "category_name": str(snapshot.get("category_name") or "unknown"),
            "source": "ozon_seller_api",
        },
        "product_group": {
            "product_group_id": f"{product_id}-G1",
            "selected_sku_count": max(1, len(skus)),
            "product_group_count": 1,
            "variant_count": max(1, len(variants)),
            "must_merge": must_merge,
            "variant_mapping_status": mapping_status,
            "upload_allowed": not blockers,
        },
        "title": title or "unknown",
        "description": description or "unknown",
        "images": images or [{"slot": "unknown", "role": "detail", "url": "", "order": 1}],
        "attributes": [
            {
                "attribute_id": item.get("attribute_id"),
                "attribute_name": item.get("attribute_name"),
                "value": item.get("value"),
                "dictionary_value_id": item.get("dictionary_value_id"),
            }
            for item in (attributes.get("common_attributes") or [])
            if isinstance(item, Mapping)
        ],
        "attributes_by_sku": {
            str(key): [
                {"attribute_id": item.get("attribute_id"), "value": item.get("value")}
                for item in (value or [])
                if isinstance(item, Mapping)
            ]
            for key, value in (by_sku or {}).items()
        },
        "sku_measurements": measurements,
        "variants": variants or [
            {
                "source_sku_id": "unknown",
                "offer_id": f"{product_id}-unknown",
                "sku_name": "unknown",
                "display_name_ru": "unknown",
                "price": "0",
                "currency_code": currency,
                "color": "не указан",
                "color_image": "unknown",
                "attributes": [],
                "variant_attribute_values": [],
            }
        ],
        "price": {
            "currency_code": currency,
            "source": PRICING_FILE if price_rows else "missing",
        },
        "image_upload_gate": {
            "passed": not missing_images and bool(slots),
            "planned_slots": len(slots),
            "missing_slots": missing_images,
            "https_only": True,
            "urls_source": IMAGE_URLS_FILE,
            "qc_decision": qc_decision or None,
            "qc_score": qc.get("score"),
            "qc_critical_failures": qc_critical,
            "semantic_qc": "configured" if qc.get("dimensions", {}).get("product_consistency", {}).get("score") else "not_configured",
            "note": "图片公网地址由对象存储 adapter 产出（替换原项目的 24h 隧道）；技术质检本地真跑",
        },
        "product_exists_check": {
            "status": "deferred_store_specific",
            "checked": False,
            "performed_api_calls": 0,
            "note": "重复检查按店铺在提交前进行（原项目语义）",
        },
        "production_blockers": blockers,
        "api_request_template": {
            "api": "POST /v3/product/import",
            "inventory_fields_included": False,
            "inventory_submission_enabled": False,
            "request": {
                "items": [
                    {
                        "offer_id": variant["offer_id"],
                        "name": variant["display_name_ru"],
                        "description_category_id": int(snapshot.get("category_id") or 0),
                        "type_id": int(snapshot.get("type_id") or 0),
                        "price": variant["price"],
                        "currency_code": variant["currency_code"],
                        "vat": "0",
                        "attributes": [
                            {"id": item["attribute_id"], "values": [{"value": item["value"]}]}
                            for item in variant["attributes"]
                            if item.get("attribute_id")
                        ],
                        "images": [image["url"] for image in images if image.get("url")],
                    }
                    for variant in variants
                ]
            },
        },
    }
    return payload


def plan_slot_sku(plan: Mapping[str, Any], slot: str) -> str | None:
    for item in plan.get("main_images") or []:
        if isinstance(item, Mapping) and str(item.get("slot")) == str(slot):
            return str(item.get("source_sku_id") or "") or None
    return None


def payload_problems(payload: Mapping[str, Any], *, upload_mode: str | None = None) -> list[str]:
    """提交**之前**的校验：契约 + production 模式下阻断项必须为空 + 不得含库存字段。

    （``api_writes_performed`` 是提交**之后**才由 uploader 回填的，不在这里校验。）
    """
    problems = list(validate_contract("ozon-upload-payload", payload))
    mode = upload_mode or str(payload.get("upload_mode") or UPLOAD_MODE_DRY_RUN)
    if mode == UPLOAD_MODE_PRODUCTION and payload.get("production_blockers"):
        problems.append("production 模式下存在阻断项：" + "; ".join(payload["production_blockers"][:5]))
    if INVENTORY_FIELD_PATTERN.search(json.dumps(payload, ensure_ascii=False)):
        problems.append("载荷里出现库存字段（原项目硬禁令：绝不提交库存）")
    return problems


# --------------------------------------------------------------------- uploader


class Uploader(Protocol):
    name: str

    def submit(self, payload: Mapping[str, Any], *, store_id: str) -> dict[str, Any]: ...


class DryRunUploader:
    """什么都不发，只回一份"被跳过"的回执。**不会**产生 task_id（否则会挡住真实上传）。"""

    name = "dry-run"
    performs_api_writes = False

    def submit(self, payload: Mapping[str, Any], *, store_id: str) -> dict[str, Any]:
        return {
            "status": "skipped",
            "task_id": None,
            "api_writes_performed": False,
            "items": [
                {
                    "source_sku_id": variant["source_sku_id"],
                    "offer_id": variant["offer_id"],
                    "product_id": None,
                    "status": "not_submitted",
                    "errors": [],
                }
                for variant in payload.get("variants") or []
            ],
            "errors": [],
            "raw_response": {"dry_run": True, "store_id": store_id, "api": payload.get("api_request_template", {}).get("api")},
            "note": "干跑：未向 Ozon 发送任何请求",
        }


class SimulatedUploader(DryRunUploader):
    """仅用于测试/演示：返回**明显是假**的 task_id 以验证幂等与多店分发。"""

    name = "simulated"
    performs_api_writes = True

    def __init__(self, *, fail_stores: Sequence[str] = (), api_writes: int = 1) -> None:
        self.fail_stores = set(fail_stores)
        self.api_writes = api_writes

    def submit(self, payload: Mapping[str, Any], *, store_id: str) -> dict[str, Any]:
        if store_id in self.fail_stores:
            raise RuntimeError(f"模拟失败：{store_id}")
        digest = hashlib.sha256(f"{payload.get('product_id')}:{store_id}".encode("utf-8")).hexdigest()
        # 用确定性的**数字** task_id：既满足 ozon-result 契约（整数），又能验证幂等
        numeric_task_id = int(digest[:8], 16) % 900000000 + 100000000
        return {
            "status": "submitted",
            "task_id": numeric_task_id,
            "api_writes_performed": True,
            "api_writes": self.api_writes,
            "items": [
                {
                    "source_sku_id": variant["source_sku_id"],
                    "offer_id": variant["offer_id"],
                    "product_id": None,
                    "status": "submitted",
                    "errors": [],
                }
                for variant in payload.get("variants") or []
            ],
            "errors": [],
            "raw_response": {"simulated": True, "store_id": store_id},
            "note": "模拟提交：未向 Ozon 发送任何请求",
        }


# --------------------------------------------------------------------- 分发


def upload_product(
    product_dir: Path | str,
    store_ids: Sequence[str],
    uploader: Uploader,
    *,
    upload_mode: str = UPLOAD_MODE_DRY_RUN,
    enabled_store_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """按店铺分发：构建载荷 → 门禁 → 提交 → 记账。单店失败不影响其他店。"""
    directory = Path(product_dir)
    product_id = directory.name
    if upload_mode == UPLOAD_MODE_PRODUCTION and not getattr(uploader, "performs_api_writes", False):
        raise ValueError(
            f"干跑 uploader（{getattr(uploader, 'name', 'unknown')}）不能用于 production 模式："
            "production 必须使用真正发请求的 uploader"
        )
    sku_ids = [
        str(item.get("sku_id") or "")
        for item in (_read_json(directory / "input" / "source.json").get("skus") or [])
        if isinstance(item, Mapping)
    ]
    plan = plan_publications(
        directory, store_ids, sku_ids=sku_ids, enabled_store_ids=enabled_store_ids
    )

    results: dict[str, dict[str, Any]] = {}
    submitted = skipped = failed = 0

    # 每个店铺的**合同币种**（config/shops.json 的 default_currency_code）：
    # 真机踩坑：用默认 RUB 提交给 CNY 合同店铺 → Ozon 报 currency_differs_from_contract
    from .stores import ensure_registry, list_shops

    shop_currencies = {
        str(item.get("id")): str(item.get("default_currency_code") or "").upper()
        for item in list_shops(ensure_registry(None))
        if isinstance(item, Mapping)
    }

    for row in plan:
        store_id = str(row["store_id"])
        if row["action"] == ACTION_SKIP:
            skipped += 1
            results[store_id] = {"action": "skip", "status": "skipped", "reason": row["reason"]}
            continue

        payload = build_upload_payload(directory, shop_name=store_id, upload_mode=upload_mode,
                                       currency_code=shop_currencies.get(store_id) or None)
        problems = payload_problems(payload, upload_mode=upload_mode)
        run_dir = directory / "output" / "store-runs" / store_id
        _write_json(run_dir / "payload.json", payload)

        if problems:
            failed += 1
            results[store_id] = {
                "action": "blocked",
                "status": "failed",
                "blockers": payload.get("production_blockers") or problems,
                "problems": problems[:8],
            }
            _write_json(
                run_dir / "ozon-result.json",
                _result_document(
                    product_id=product_id,
                    shop_name=store_id,
                    status="failed",
                    task_id=None,
                    items=[],
                    errors=[{"code": "PREFLIGHT_BLOCKED", "message": item} for item in problems[:8]],
                    raw_response={"blocked": True, "blockers": payload.get("production_blockers")},
                ),
            )
            record_publication(
                directory,
                store_id,
                sku_id="*",
                status="failed",
                errors=[{"code": "PREFLIGHT_BLOCKED", "message": item} for item in problems[:5]],
            )
            continue

        try:
            receipt = uploader.submit(payload, store_id=store_id)
        except Exception as error:  # 单店失败隔离
            failed += 1
            results[store_id] = {"action": "create", "status": "failed", "reason": str(error)}
            _write_json(
                run_dir / "ozon-result.json",
                _result_document(
                    product_id=product_id,
                    shop_name=store_id,
                    status="failed",
                    task_id=None,
                    items=[],
                    errors=[{"code": "UPLOADER_ERROR", "message": str(error)}],
                    raw_response=None,
                ),
            )
            record_publication(directory, store_id, sku_id="*", status="failed", errors=[{"message": str(error)}])
            continue

        task_id = receipt.get("task_id")
        status = str(receipt.get("status") or "submitted")
        if upload_mode == UPLOAD_MODE_PRODUCTION and not receipt.get("api_writes_performed"):
            # 危险配置保护：production 模式下 uploader 必须声明发生了写请求
            failed += 1
            reason = "production 模式下 uploader 未声明发生写请求（疑似把干跑 uploader 用在了 production）"
            results[store_id] = {"action": "create", "status": "failed", "reason": reason}
            _write_json(
                run_dir / "ozon-result.json",
                _result_document(
                    product_id=product_id,
                    shop_name=store_id,
                    status="failed",
                    task_id=None,
                    items=[],
                    errors=[{"code": "UPLOADER_NOT_PRODUCTION", "message": reason}],
                    raw_response=receipt.get("raw_response"),
                ),
            )
            record_publication(
                directory, store_id, sku_id="*", status="failed", errors=[{"message": reason}]
            )
            continue
        items = [
            {
                "source_sku_id": str(item.get("source_sku_id") or ""),
                "offer_id": str(item.get("offer_id") or ""),
                "product_id": item.get("product_id"),
                "status": str(item.get("status") or status),
                "errors": list(item.get("errors") or []),
            }
            for item in (receipt.get("items") or [])
        ]
        for item in items:
            record_publication(
                directory,
                store_id,
                sku_id=item["source_sku_id"],
                offer_id=item["offer_id"],
                task_id=task_id if isinstance(task_id, (str, int)) else None,
                ozon_product_id=item.get("product_id"),
                status="submitted" if task_id else status,
                errors=item.get("errors") or [],
            )
        if not items:
            record_publication(
                directory,
                store_id,
                sku_id="*",
                status="submitted" if task_id else status,
                errors=[],
            )
        _write_json(
            run_dir / "ozon-result.json",
            _result_document(
                product_id=product_id,
                shop_name=store_id,
                status=status if status in {"submitted", "processing", "created", "updated", "skipped", "failed"} else "submitted",
                task_id=task_id,
                items=items,
                errors=list(receipt.get("errors") or []),
                raw_response=receipt.get("raw_response"),
            ),
        )
        if task_id:
            submitted += 1
        else:
            skipped += 1
        results[store_id] = {
            "action": "create",
            "status": status,
            "task_id": task_id,
            "api_writes_performed": bool(receipt.get("api_writes_performed")),
            "api_writes": int(receipt.get("api_writes") or 0),
            "note": receipt.get("note"),
        }

        # 拿到 task_id 后确认最终状态（只读；失败不影响"已提交"这个事实）
        confirmer = getattr(uploader, "confirm", None)
        if task_id and callable(confirmer):
            try:
                confirmation = confirmer(task_id, store_id=store_id, payload=payload)
            except Exception as error:  # noqa: BLE001 - 确认失败不掩盖提交成功
                confirmation = {
                    "task_id": str(task_id),
                    "terminal": False,
                    "confirmed": False,
                    "error": f"确认失败：{error}",
                    "items": [],
                    "counts": {"total": 0, "imported": 0, "failed": 0, "pending": 0},
                }
            from .ozon_status import apply_confirmation

            applied = apply_confirmation(
                directory, store_id=store_id, confirmation=confirmation, task_id=str(task_id)
            )
            results[store_id]["confirmation"] = {
                "status": applied["status"],
                "terminal": applied["terminal"],
                "counts": applied["counts"],
                "error": confirmation.get("error"),
            }

    payload_ref = load_publications(directory)
    save_publications(directory, payload_ref)
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "uploader": getattr(uploader, "name", "unknown"),
        "upload_mode": upload_mode,
        "stores": results,
        "submitted": submitted,
        "skipped": skipped,
        "failed": failed,
        "api_writes": sum(int(item.get("api_writes") or 0) for item in results.values()),
    }


def _contract_task_id(value: Any) -> Any:
    """``ozon-result`` 契约只接受整数（≥1）、字符串 ``unknown`` 或 null。"""
    if isinstance(value, bool) or value is None:
        return "unknown"
    if isinstance(value, int):
        return value if value >= 1 else "unknown"
    text = str(value).strip()
    if text.isdigit() and int(text) >= 1:
        return int(text)
    return "unknown"


def _result_document(
    *,
    product_id: str,
    shop_name: str,
    status: str,
    task_id: Any,
    items: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
    raw_response: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """对齐上游 ozon-result 契约的回执。"""
    contract_task_id = _contract_task_id(task_id)
    raw = dict(raw_response) if raw_response else None
    if task_id is not None and contract_task_id == "unknown":
        # 非数字 task_id（例如本地模拟器）在契约里只能写 unknown，原值留痕在 raw_response
        raw = {**(raw or {}), "local_task_id": str(task_id)}
    return {
        "schema_version": SCHEMA_VERSION,
        "local_product_id": product_id,
        "shop_name": shop_name,
        "task_id": contract_task_id,
        "status": status,
        "moderation_status": "unknown",
        "items": list(items),
        "errors": list(errors),
        "error_code": None if not errors else "UPLOAD_BLOCKED",
        "error_message": None if not errors else str(errors[0].get("message") if isinstance(errors[0], Mapping) else errors[0]),
        "failed_step": None if not errors else "ozon_upload",
        "created_at": now_iso(),
        "raw_response": raw,
    }


# --------------------------------------------------------------------- handler


def handle_ozon_upload(ctx: StepContext) -> dict[str, Any]:
    """提交步骤：default 干跑；只有显式传入 uploader 且 app_mode=production 才会真提交。"""
    uploader = getattr(ctx, "uploader", None)
    if uploader is None:
        raise PipelineGateError(ctx.step, "未配置上传器（run_product(uploader=...)），拒绝提交")
    if ctx.app_mode != "production":
        raise PipelineGateError(ctx.step, f"app_mode={ctx.app_mode} 禁止真实上传")

    status = ctx.status if ctx.status else None
    if not status:
        from .status import load_status

        status = load_status(ctx.product_dir)
    store_ids = [str(item) for item in (status.get("target_store_ids") or [])]
    if not store_ids:
        raise PipelineGateError(ctx.step, "没有选择目标店铺（status.target_store_ids 为空）")

    upload_mode = UPLOAD_MODE_DRY_RUN if ctx.dry_run else UPLOAD_MODE_PRODUCTION
    try:
        summary = upload_product(
            ctx.product_dir,
            store_ids,
            uploader,
            upload_mode=upload_mode,
        )
    except ValueError as error:
        # 配置类错误（例如 production 模式误用干跑 uploader）：记为需要人工处理，而不是让 runner 崩掉
        raise PipelineGateError(ctx.step, str(error)) from error
    ctx.write_json("output/upload-summary.json", summary)

    if summary["api_writes"]:
        # 真实写请求才累加；干跑/模拟不写这一位，runner 的"零写请求"不变量才成立
        from .status import load_status, save_status

        current = load_status(ctx.product_dir)
        current["api_write_count"] = int(current.get("api_write_count") or 0) + int(summary["api_writes"])
        save_status(ctx.product_dir, current)

    if summary["failed"] and not summary["submitted"] and not summary["skipped"]:
        raise PipelineGateError(
            ctx.step,
            "所有目标店铺都提交失败",
            {"stores": summary["stores"]},
        )
    warnings = [f"{store}: {body.get('status')}" for store, body in summary["stores"].items() if body.get("status") == "failed"]
    return {
        "warnings": warnings,
        "artifacts": ["output/upload-summary.json"],
        "submitted": summary["submitted"],
        "skipped": summary["skipped"],
        "failed": summary["failed"],
    }


UPLOAD_HANDLERS = {"ozon_upload": handle_ozon_upload}


def upload_handlers(uploader: Any | None = None) -> dict[str, Any]:
    return dict(UPLOAD_HANDLERS) if uploader is not None else {}
