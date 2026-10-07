"""定价与尺寸重量（``measurements`` 步骤）：把采购价算成可提交的卢布售价，并整理确认过的尺寸重量。

**为什么不用上游的 pricing-result 契约**：那份契约绑死了作者本机的 Excel 运费表
（``worksheet`` 常量 ``RETS``、``exchange_rate.source`` 常量 ``RETS!P2``、``workbook_sha256``），
我们没有那张表 —— 与其编一个 sha256 假装读过，不如定义自己的明确契约
（``contracts/workbench-pricing-result.schema.json``），并保留"以后接上游运费表"的位置。

**尺寸重量只接受确认值**：来自 ``input/workbench-sku-overrides.json``（人工在界面里填的）
或采集里明确存在的结构化字段；两者都没有就是"缺"，如实报出来（上传门禁会拦住），**绝不估算**。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import format_problems, validate_contract
from parsing import parse_number

from .context import PipelineGateError, StepContext

SCHEMA_VERSION = "1.0.0"
PRICING_FILE = "output/pricing-result.json"
MEASUREMENTS_FILE = "output/measurements.json"
PROFIT_FILE = "output/profit-analysis.json"
OVERRIDES_FILE = "input/workbench-sku-overrides.json"
CONFIG_PATH = Path("config") / "pricing.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "rub_per_cny": 11.6,
    "commission_rate": 0.15,
    #: 按"价格百分比"计的费用（平台佣金里的比例部分、收单、提现等）
    "logistics_commission_rate": 0.05,
    "acquiring_fee_rate": 0.015,
    "withdrawal_fee_rate": 0.01,
    #: 按**卢布金额**计的固定费（Ozon 的物流/处理费多数是这个形状，不是"价格的百分比"）：
    #: 每件固定 + 每公斤，二者相加后按 rub_per_cny 折算成人民币参与定价
    "logistics_fee_rub_per_item": 0.0,
    "logistics_fee_rub_per_kg": 0.0,
    "order_processing_fee_rub": 0.0,
    "shipping_cost_cny_per_kg": 45.0,
    "shipping_min_cny": 15.0,
    "packing_fee_cny": 1.5,
    "other_fixed_cost_cny": 0.0,
    "volumetric_divisor": 6000,
    "target_margin_rate": 0.25,
    "min_margin_rate": 0.10,
    "round_to_rub": 10,
    "price_ends_with": 90,
    #: 价格上限（RUB）；0 表示不限制。超过上限会降级为 WARNING（竞争力风险）
    "max_price_rub": 0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_pricing_config(path: Path | str | None = None) -> tuple[dict[str, Any], list[str]]:
    """读 ``config/pricing.json``，缺项用默认值补齐；返回 (配置, 警告)。"""
    target = Path(path) if path else CONFIG_PATH
    warnings: list[str] = []
    config = dict(DEFAULT_CONFIG)
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            warnings.append(f"pricing.json 解析失败，使用默认配置：{error}")
            loaded = {}
        if isinstance(loaded, Mapping):
            for key, value in loaded.items():
                if key in DEFAULT_CONFIG:
                    config[key] = value
            config["config_file"] = str(target)
    else:
        warnings.append(f"没有 {target}：使用内置默认费率（建议按自己的物流/佣金实际值配置一份）")
    return config, warnings


# --------------------------------------------------------------------- 尺寸重量


def _positive_int(value: Any) -> int | None:
    number = parse_number(value)
    if number is None or number <= 0:
        return None
    return int(math.ceil(number))


def _dimensions_from_block(block: Mapping[str, Any] | None, *, prefix: str) -> dict[str, Any] | None:
    """从 overrides 的一条记录里取尺寸重量；键名兼容 ``length_mm`` 与 ``product_length_mm`` 两种写法。"""
    if not isinstance(block, Mapping):
        return None
    def pick(*names: str) -> Any:
        for name in names:
            if block.get(name) is not None:
                return block.get(name)
        return None

    length = _positive_int(pick(f"{prefix}length_mm", "length_mm"))
    width = _positive_int(pick(f"{prefix}width_mm", "width_mm"))
    height = _positive_int(pick(f"{prefix}height_mm", "height_mm"))
    weight = _positive_int(pick(f"{prefix}weight_g", "weight_g"))
    if None in (length, width, height, weight):
        return None
    return {"length_mm": length, "width_mm": width, "height_mm": height, "weight_g": weight}


def collect_measurements(
    *,
    product_id: str,
    source: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    product_dir: Path | str | None = None,
) -> dict[str, Any]:
    """整理商品/包装/各 SKU 的尺寸重量（只认确认值）；返回 workbench-measurements 形状。"""
    overrides = dict(overrides or {})
    overrides_by_sku = overrides.get("sku_overrides") if isinstance(overrides.get("sku_overrides"), Mapping) else {}
    product_block = overrides.get("product") if isinstance(overrides.get("product"), Mapping) else None

    warnings: list[str] = []
    source_refs = ["input/source.json"]
    if overrides:
        source_refs.append(OVERRIDES_FILE)
    source_refs.append("output/pricing-result.json")

    product_dims = _dimensions_from_block(product_block, prefix="product_") or _dimensions_from_block(
        product_block, prefix=""
    )
    package_dims = _dimensions_from_block(product_block, prefix="package_")

    sku_measurements: dict[str, Any] = {}
    from .sku_selection import active_skus

    raw_skus = [item for item in (source.get("skus") or []) if isinstance(item, Mapping)]
    skus = active_skus(product_dir, raw_skus) if product_dir is not None else raw_skus
    for index, sku in enumerate(skus, start=1):
        sku_id = str(sku.get("sku_id") or f"S{index}")
        block = overrides_by_sku.get(sku_id) if isinstance(overrides_by_sku, Mapping) else None
        sku_product = _dimensions_from_block(block, prefix="product_") or _dimensions_from_block(block, prefix="")
        sku_package = _dimensions_from_block(block, prefix="package_")
        sku_measurements[sku_id] = {"product": sku_product, "package": sku_package}
        if sku_product is None and product_dims is None:
            warnings.append(f"SKU {sku_id} 缺商品尺寸重量（需要人工确认，不估算）")
        if sku_package is None and package_dims is None:
            warnings.append(f"SKU {sku_id} 缺包装尺寸重量（需要人工确认，不估算）")

    hierarchy_ok = True
    if product_dims and package_dims:
        hierarchy_ok = all(
            package_dims[axis] >= product_dims[axis] for axis in ("length_mm", "width_mm", "height_mm")
        ) and package_dims["weight_g"] >= product_dims["weight_g"]
        if not hierarchy_ok:
            warnings.append("包装尺寸/重量小于商品本体：请检查填写值（上传门禁要求包装 ≥ 商品）")
    elif isinstance(product_block, Mapping):
        # Optional partial item measurements still constrain the corresponding
        # known shipping axis; missing item axes are never inferred from package.
        for axis in ("length_mm", "width_mm", "height_mm", "weight_g"):
            item_value = _positive_int(product_block.get(f"product_{axis}"))
            package_value = _positive_int(product_block.get(f"package_{axis}")) or (package_dims or {}).get(axis)
            if item_value is not None and package_value is not None and item_value > package_value:
                hierarchy_ok = False
                warnings.append("已确认商品尺寸/重量大于对应包装值，请核对实测资料")

    if product_dims and package_dims:
        origin = "user_confirmed" if overrides else "capture_structured"
    elif product_dims or package_dims or any(item["product"] or item["package"] for item in sku_measurements.values()):
        origin = "mixed"
    else:
        origin = "missing"

    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "source": origin,
        "source_refs": source_refs,
        "product": product_dims,
        "package": package_dims,
        "sku_measurements": sku_measurements,
        "hierarchy_ok": hierarchy_ok,
        "warnings": warnings,
        "generated_at": now_iso(),
    }


def load_measurements(product_dir: Path | str) -> dict[str, Any]:
    """读 ``output/measurements.json``（没有就返回空结构），也兼容旧的 cost-analysis.json。"""
    directory = Path(product_dir)
    path = directory / MEASUREMENTS_FILE
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, Mapping):
            return dict(payload)
    legacy = directory / "output" / "cost-analysis.json"
    if legacy.is_file():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, Mapping):
            product = payload.get("product_dimensions") or payload.get("dimensions")
            package = payload.get("package_dimensions")
            return {
                "schema_version": SCHEMA_VERSION,
                "product_id": directory.name,
                "source": "legacy_cost_analysis",
                "source_refs": payload.get("source_refs") or ["output/cost-analysis.json"],
                "product": _normalize_legacy(product),
                "package": _normalize_legacy(package),
                "sku_measurements": {},
                "hierarchy_ok": bool((payload.get("measurement_hierarchy") or {}).get("valid", True)),
                "warnings": ["尺寸重量来自旧的 cost-analysis.json（建议改用 measurements.json）"],
                "generated_at": payload.get("generated_at") or now_iso(),
            }
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "source": "missing",
        "source_refs": ["input/source.json"],
        "product": None,
        "package": None,
        "sku_measurements": {},
        "hierarchy_ok": False,
        "warnings": ["没有 measurements.json：尺寸重量未确认"],
        "generated_at": now_iso(),
    }


def _normalize_legacy(block: Any) -> dict[str, Any] | None:
    """把旧 cost-analysis 的 cm/g 或 mm 写法统一成我们的 mm/g 整数。"""
    if not isinstance(block, Mapping):
        return None
    if all(block.get(key) for key in ("length_mm", "width_mm", "height_mm", "weight_g")):
        return {
            "length_mm": int(block["length_mm"]),
            "width_mm": int(block["width_mm"]),
            "height_mm": int(block["height_mm"]),
            "weight_g": int(block["weight_g"]),
        }
    unit = str(block.get("unit") or "").casefold()
    if unit in {"cm", "см"} and all(block.get(key) for key in ("length", "width", "height")):
        weight = block.get("weight_g") or block.get("value_g") or (block.get("weight") or {}).get("value")
        return {
            "length_mm": int(round(float(block["length"]) * 10)),
            "width_mm": int(round(float(block["width"]) * 10)),
            "height_mm": int(round(float(block["height"]) * 10)),
            "weight_g": int(round(float(weight or 0))),
        }
    return None


# --------------------------------------------------------------------- 定价


def volumetric_weight_g(dimensions: Mapping[str, Any] | None, *, divisor: float) -> int | None:
    if not dimensions:
        return None
    volume_cm3 = (dimensions["length_mm"] / 10) * (dimensions["width_mm"] / 10) * (dimensions["height_mm"] / 10)
    return int(round(volume_cm3 / float(divisor) * 1000))


def nice_price(raw_rub: float, *, ends_with: int, step: int) -> int:
    """把裸价抬到"好看"的整数价（例如 1478 → 1490），只向上取。"""
    base = math.floor(raw_rub / 100) * 100
    candidate = int(base + ends_with)
    while candidate < raw_rub:
        candidate += 100
    if step > 1 and candidate % step != 0:
        candidate += step - (candidate % step)
    return candidate


def compute_sku_pricing(
    *,
    sku: Mapping[str, Any],
    sku_id: str,
    config: Mapping[str, Any],
    dimensions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    purchase = parse_number(sku.get("purchase_price_cny") or sku.get("cost_cny") or sku.get("purchase_price"))
    errors: list[str] = []
    missing_purchase = purchase is None or purchase <= 0
    if missing_purchase:
        errors.append("缺少采购价（purchase_price_cny）")

    actual_weight = (dimensions or {}).get("weight_g") if dimensions else _positive_int(sku.get("weight_g"))
    volumetric = volumetric_weight_g(dimensions, divisor=config["volumetric_divisor"])
    candidates = [value for value in (actual_weight, volumetric) if value]
    billable_weight = max(candidates) if candidates else 0
    shipping_from_weight = billable_weight / 1000 * float(config["shipping_cost_cny_per_kg"])
    shipping = max(float(config["shipping_min_cny"]), shipping_from_weight)

    base_cost = (purchase or 0) + shipping + float(config["packing_fee_cny"]) + float(config["other_fixed_cost_cny"])
    # Ozon 的物流/处理费是"每件 + 每公斤"的卢布固定费（不是价格百分比）→ 折成人民币后进成本
    rub_per_cny = float(config["rub_per_cny"]) or 1.0
    logistics_fixed_rub = (
        float(config.get("logistics_fee_rub_per_item") or 0)
        + float(config.get("logistics_fee_rub_per_kg") or 0) * (billable_weight / 1000)
        + float(config.get("order_processing_fee_rub") or 0)
    )
    logistics_fixed_cny = logistics_fixed_rub / rub_per_cny
    base_cost += logistics_fixed_cny

    fee_rate = (
        float(config["commission_rate"])
        + float(config["logistics_commission_rate"])
        + float(config["acquiring_fee_rate"])
        + float(config["withdrawal_fee_rate"])
    )
    denominator = 1 - float(config["target_margin_rate"]) - fee_rate
    selling_cny = None
    if missing_purchase:
        # 成本未知时不报价：给一个"价格"会误导（宁可 REJECT 让人补资料）
        selling_cny = None
    elif denominator <= 0.05:
        errors.append("费率与目标利润率之和过高，无法定价（请调小 target_margin_rate 或费率）")
        selling_cny = None
    else:
        selling_cny = base_cost / denominator

    selling_rub = None
    profit_rub = None
    margin_rate = None
    if selling_cny is not None:
        raw_rub = selling_cny * float(config["rub_per_cny"])
        selling_rub = nice_price(
            raw_rub, ends_with=int(config["price_ends_with"]), step=int(config["round_to_rub"])
        )
        revenue_after_fees = selling_rub * (1 - fee_rate)
        cost_rub = base_cost * float(config["rub_per_cny"])
        profit_rub = round(revenue_after_fees - cost_rub, 2)
        margin_rate = round(profit_rub / selling_rub, 4) if selling_rub else None

    status = "REJECT"
    max_price = parse_number(config.get("max_price_rub")) or 0
    if errors:
        status = "REJECT"
    elif margin_rate is not None and margin_rate >= float(config["min_margin_rate"]):
        status = "UPLOAD"
    elif margin_rate is not None and margin_rate >= float(config["min_margin_rate"]) * 0.5:
        status = "WARNING"
        errors.append(f"利润率 {margin_rate:.1%} 低于目标下限 {float(config['min_margin_rate']):.1%}")
    else:
        status = "REJECT"
        if margin_rate is not None:
            errors.append(f"利润率 {margin_rate:.1%} 过低（低于下限的一半）")

    if status == "UPLOAD" and max_price and selling_rub and selling_rub > max_price:
        status = "WARNING"
        errors.append(f"价格 {selling_rub} RUB 超过上限 {int(max_price)}：成本加成定价会失去竞争力，建议换 SKU 或压物流")

    return {
        "sku_id": sku_id,
        "sku_name": sku.get("name_zh") or sku.get("spec_zh"),
        "purchase_cost_cny": purchase,
        "purchase_cost_source": "input/source.json",
        "shipping_cost_cny": round(shipping, 2),
        "logistics_fee_rub": round(logistics_fixed_rub, 2),
        "logistics_fee_cny": round(logistics_fixed_cny, 2),
        "actual_weight_g": actual_weight,
        "volumetric_weight_g": volumetric,
        "billable_weight_g": billable_weight,
        "base_cost_cny": round(base_cost, 2),
        "total_fee_rate": round(fee_rate, 4),
        "selling_price_cny": round(selling_cny, 2) if selling_cny is not None else None,
        "selling_price_rub": selling_rub,
        "estimated_profit_rub": profit_rub,
        "margin_rate": margin_rate,
        "status": status,
        "errors": errors,
        # 钱花在哪：定价明细（人审与事后对账都要看这个）
        "breakdown_cny": {
            "purchase": round(purchase or 0, 2),
            "shipping_to_ozon": round(shipping, 2),
            "packing": round(float(config["packing_fee_cny"]), 2),
            "other_fixed": round(float(config["other_fixed_cost_cny"]), 2),
            "ozon_logistics_fixed": round(logistics_fixed_cny, 2),
            "total_cost": round(base_cost, 2),
        },
        "breakdown_rub": {
            "selling_price": selling_rub,
            "platform_fees": round(selling_rub * fee_rate, 2) if selling_rub else None,
            "cost": round(base_cost * rub_per_cny, 2) if selling_rub else None,
            "profit": profit_rub,
        },
    }


def compute_pricing(
    *,
    product_id: str,
    source: Mapping[str, Any],
    config: Mapping[str, Any],
    measurements: Mapping[str, Any] | None = None,
    product_dir: Path | str | None = None,
) -> dict[str, Any]:
    from .sku_selection import active_skus

    raw_skus = [item for item in (source.get("skus") or []) if isinstance(item, Mapping)]
    # 只为"要上架"的 SKU 定价：没选的规格缺价格不该把整单卡住
    skus = active_skus(product_dir, raw_skus) if product_dir is not None else raw_skus
    if not skus:
        raise ValueError("没有已选 SKU，无法定价")
    warnings: list[str] = []

    package = (measurements or {}).get("package")
    if not package:
        warnings.append("缺包装尺寸重量：运费只能按最低运费估算（上传门禁仍会拦住缺尺寸的商品）")

    rows: list[dict[str, Any]] = []
    for index, sku in enumerate(skus, start=1):
        sku_id = str(sku.get("sku_id") or f"S{index}")
        sku_dims = None
        sku_block = ((measurements or {}).get("sku_measurements") or {}).get(sku_id) or {}
        sku_dims = sku_block.get("package") or (measurements or {}).get("package")
        rows.append(compute_sku_pricing(sku=sku, sku_id=sku_id, config=config, dimensions=sku_dims))

    manual_required = product_dir is not None and (Path(product_dir) / "input" / "manual-pricing-required.json").is_file()
    if manual_required:
        manual_file = Path(product_dir) / "input" / "manual-prices.json"
        try:
            manual = json.loads(manual_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            manual = {}
        except ValueError as error:
            raise ValueError("人工售价文件不是有效 JSON") from error
        entered = manual.get("prices") if isinstance(manual, Mapping) else {}
        entered = entered if isinstance(entered, Mapping) else {}
        for row in rows:
            choice = entered.get(row["sku_id"])
            choice = choice if isinstance(choice, Mapping) else {}
            amount = parse_number(choice.get("price"))
            currency = str(choice.get("currency") or "").upper()
            # Ignore all auto-pricing rejections: the user's number is the
            # source of truth for the offer, while costs remain diagnostics.
            row["errors"] = []
            if not amount or amount <= 0 or currency not in {"CNY", "RUB"}:
                row["selling_price_cny"] = None
                row["selling_price_rub"] = None
                row["estimated_profit_rub"] = None
                row["margin_rate"] = None
                row["status"] = "REJECT"
                row["errors"].append("缺少人工确认售价（CNY 或 RUB）")
                continue
            rub_per_cny = float(config["rub_per_cny"])
            cny = amount if currency == "CNY" else amount / rub_per_cny
            rub = amount if currency == "RUB" else amount * rub_per_cny
            row["selling_price_cny"] = round(cny, 2)
            row["selling_price_rub"] = round(rub, 2)
            row["estimated_profit_rub"] = round(rub * (1 - row["total_fee_rate"]) - row["base_cost_cny"] * rub_per_cny, 2) if row["purchase_cost_cny"] else None
            row["margin_rate"] = round(row["estimated_profit_rub"] / rub, 4) if row["estimated_profit_rub"] is not None else None
            row["status"] = "UPLOAD"
            row["breakdown_rub"] = {
                "selling_price": row["selling_price_rub"],
                "platform_fees": round(rub * row["total_fee_rate"], 2),
                "cost": round(row["base_cost_cny"] * rub_per_cny, 2) if row["purchase_cost_cny"] else None,
                "profit": row["estimated_profit_rub"],
            }
            if row["purchase_cost_cny"] is None:
                row["errors"].append("缺采购价，无法评估利润；人工售价仍保留")

    statuses = {row["status"] for row in rows}
    if "REJECT" in statuses:
        recommendation = "REJECT"
    elif "WARNING" in statuses:
        recommendation = "WARNING"
    else:
        recommendation = "UPLOAD"
    warnings.extend(
        f"{row['sku_id']}: {item}" for row in rows for item in row["errors"] if row["status"] != "UPLOAD"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "pricing_source": "user_manual" if manual_required else "workbench-pricing-engine",
        "config": {
            key: config[key]
            for key in (
                "rub_per_cny",
                "commission_rate",
                "logistics_commission_rate",
                "acquiring_fee_rate",
                "withdrawal_fee_rate",
                "shipping_cost_cny_per_kg",
                "shipping_min_cny",
                "packing_fee_cny",
                "other_fixed_cost_cny",
                "volumetric_divisor",
                "target_margin_rate",
                "min_margin_rate",
                "round_to_rub",
                "price_ends_with",
                "max_price_rub",
            )
        }
        | ({"config_file": config["config_file"]} if config.get("config_file") else {}),
        "skus": rows,
        "recommendation": recommendation,
        "warnings": warnings,
        "generated_at": now_iso(),
    }


# --------------------------------------------------------------------- handler


def handle_measurements(ctx: StepContext) -> dict[str, Any]:
    """定价 + 尺寸重量：写 pricing-result.json / measurements.json / profit-analysis.json。"""
    source = ctx.require_json("input/source.json")
    overrides = ctx.read_json(OVERRIDES_FILE)
    config, config_warnings = load_pricing_config(ctx.path("config/pricing.json"))

    measurements = collect_measurements(
        product_id=ctx.product_dir.name, source=source, overrides=overrides, product_dir=ctx.product_dir
    )
    problems = validate_contract("workbench-measurements", measurements)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "尺寸重量结果不符合 workbench-measurements 契约",
            {"problems": problems[:8], "summary": format_problems(problems)},
        )
    ctx.write_json(MEASUREMENTS_FILE, measurements)

    pricing = compute_pricing(
        product_id=ctx.product_dir.name,
        source=source,
        config=config,
        measurements=measurements,
        product_dir=ctx.product_dir,
    )
    problems = validate_contract("workbench-pricing-result", pricing)
    if problems:
        raise PipelineGateError(
            ctx.step,
            "定价结果不符合 workbench-pricing-result 契约",
            {"problems": problems[:8], "summary": format_problems(problems)},
        )
    ctx.write_json(PRICING_FILE, pricing)

    ctx.write_json(
        PROFIT_FILE,
        {
            "schema_version": SCHEMA_VERSION,
            "product_id": ctx.product_dir.name,
            "generated_at": pricing["generated_at"],
            "config_file": config.get("config_file"),
            "skus": [
                {
                    "sku_id": row["sku_id"],
                    "selling_price_rub": row["selling_price_rub"],
                    "estimated_profit_rub": row["estimated_profit_rub"],
                    "margin_rate": row["margin_rate"],
                    "status": row["status"],
                }
                for row in pricing["skus"]
            ],
            "recommendation": pricing["recommendation"],
        },
    )

    warnings = [*config_warnings, *measurements["warnings"], *pricing["warnings"]]
    if pricing["recommendation"] == "REJECT":
        warnings.append("存在无法定价或利润过低的 SKU：建议调价或换 SKU（上传门禁会拦住）")
    return {
        "warnings": warnings,
        "artifacts": [PRICING_FILE, MEASUREMENTS_FILE, PROFIT_FILE],
        "recommendation": pricing["recommendation"],
        "priced_skus": len([row for row in pricing["skus"] if row["selling_price_rub"]]),
        "measurement_source": measurements["source"],
    }


MEASUREMENT_HANDLERS = {"measurements": handle_measurements}


def quote(
    *,
    cost_cny: float,
    weight_g: int | None = None,
    length_mm: int | None = None,
    width_mm: int | None = None,
    height_mm: int | None = None,
    sku_id: str = "QUOTE",
    config_path: Path | str | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """给一个假想 SKU 试算价格（上线前 sanity check，不用建商品）。"""
    config, warnings = load_pricing_config(config_path)
    for key, value in (config_overrides or {}).items():
        if value is not None and key in config:
            config[key] = value
    dimensions = None
    if length_mm and width_mm and height_mm:
        dimensions = {
            "length_mm": int(length_mm),
            "width_mm": int(width_mm),
            "height_mm": int(height_mm),
            "weight_g": int(weight_g) if weight_g else None,
        }
    row = compute_sku_pricing(
        sku={"sku_id": sku_id, "purchase_price_cny": cost_cny, "weight_g": weight_g},
        sku_id=sku_id,
        config=config,
        dimensions=dimensions,
    )
    return {"config": config, "config_warnings": warnings, "quote": row}


def main(argv: Sequence[str] | None = None) -> int:
    """报价试算 CLI：``python -m pipeline.measurements --quote --cost-cny 18.5 --weight-g 430``"""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="定价/尺寸重量：报价试算与配置查看")
    parser.add_argument("--quote", action="store_true", help="试算一个假想 SKU 的售价与利润")
    parser.add_argument("--cost-cny", type=float, default=None)
    parser.add_argument("--weight-g", type=int, default=None)
    parser.add_argument("--length-mm", type=int, default=None)
    parser.add_argument("--width-mm", type=int, default=None)
    parser.add_argument("--height-mm", type=int, default=None)
    parser.add_argument("--config", default=None, help="pricing.json 路径（默认 config/pricing.json）")
    parser.add_argument("--set", action="append", dest="overrides", help="临时覆盖配置，如 --set commission_rate=0.17")
    parser.add_argument("--show-config", action="store_true", help="打印当前生效的定价配置")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.show_config:
        config, warnings = load_pricing_config(args.config)
        if args.json:
            print(_json.dumps({"ok": True, "config": config, "warnings": warnings}, ensure_ascii=False, indent=2))
        else:
            print("\n".join(f"{key} = {value}" for key, value in config.items()))
            for item in warnings:
                print(f"⚠️ {item}")
        return 0

    if not args.quote:
        parser.error("要么 --quote，要么 --show-config")
    if args.cost_cny is None:
        parser.error("--quote 需要 --cost-cny")

    overrides: dict[str, Any] = {}
    for item in args.overrides or []:
        key, _, value = str(item).partition("=")
        try:
            overrides[key.strip()] = float(value)
        except ValueError:
            overrides[key.strip()] = value.strip()
    report = quote(
        cost_cny=args.cost_cny,
        weight_g=args.weight_g,
        length_mm=args.length_mm,
        width_mm=args.width_mm,
        height_mm=args.height_mm,
        config_path=args.config,
        config_overrides=overrides,
    )
    row = report["quote"]
    if args.json:
        print(_json.dumps({"ok": row["status"] != "REJECT", **report}, ensure_ascii=False, indent=2))
    else:
        breakdown = row["breakdown_cny"]
        print(
            f"采购 {breakdown['purchase']} CNY → 建议售价 {row['selling_price_rub']} RUB"
            f"（≈{row['selling_price_cny']} CNY，计费重 {row['billable_weight_g']} g）"
        )
        print("成本构成（CNY）：" + "，".join(f"{key} {value}" for key, value in breakdown.items()))
        margin = "—" if row["margin_rate"] is None else format(row["margin_rate"], ".1%")
        print(
            f"平台费率合计 {row['total_fee_rate']:.2%}｜预计利润 {row['estimated_profit_rub']} RUB｜利润率 {margin}"
        )
        print(f"判定：{row['status']}" + (f"（{'; '.join(row['errors'])}）" if row["errors"] else ""))
        for item in report["config_warnings"]:
            print(f"⚠️ {item}")
    return 0 if row["status"] != "REJECT" else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())
