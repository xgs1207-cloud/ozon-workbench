"""Explainable, source-aware category and keyword recommendations.

Only Seerfar observations are scored. Ozon observations are independent
corroborating evidence; the two vendors' demand figures are never added.
Missing measures stay missing rather than becoming zero.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Mapping

from .store import connect


ALIASES = {
    "category_demand": ("销售额", "月销售额", "gmv", "metric_gmv", "销量", "月销量", "sale_count"),
    "keyword_demand": ("月搜热度", "搜索热度", "搜索量", "月搜索量", "搜索人数", "search_volume", "search_heat"),
    "competitors": ("竞对数", "卖家数", "商家数", "sellers_count", "metric_sellers", "competitor_count"),
    "items": ("竞品数", "商品数", "在售商品数", "products_count", "metric_items", "items_count"),
    "conversion": ("加购转化率", "转化率", "conversion_to_cart", "cart_conversion"),
    "return_rate": ("退货取消率", "退货率", "取消率", "return_cancel_rate", "return_rate"),
    "concentration": ("转换集中度", "转化集中度", "头部集中度", "头部份额", "metric_leader_share", "leader_share"),
    "category_volume": ("销量", "月销量", "sale_count"),
    "seasonality_coefficient": ("季节性系数",),
    "crossborder_share": ("跨境商品份额",),
}

DEFAULT_WEIGHTS = {
    "categories": {"stability": .35, "competition": .35, "demand": .20, "trend": .10},
    "keywords": {"stability": .30, "competition": .30, "demand": .25, "conversion": .15},
}


@dataclass(frozen=True)
class RecommendConfig:
    min_history_months: int = 3
    min_demand: float = 0
    max_competition_density: float | None = None
    return_penalty: float = 8
    weights: Mapping[str, Mapping[str, float]] = field(default_factory=lambda: DEFAULT_WEIGHTS)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RecommendConfig":
        value = value or {}
        weights = {kind: dict(row) for kind, row in DEFAULT_WEIGHTS.items()}
        for kind in weights:
            incoming = (value.get("weights") or {}).get(kind) or {}
            for name in weights[kind]:
                if name in incoming:
                    number = float(incoming[name])
                    if not 0 <= number <= 1:
                        raise ValueError("评分权重须在 0–1 之间")
                    weights[kind][name] = number
            if sum(weights[kind].values()) <= 0:
                raise ValueError("评分权重之和不能为 0")
        months = int(value.get("min_history_months", 3))
        if not 1 <= months <= 12:
            raise ValueError("历史月份门槛须在 1–12 之间")
        min_demand = float(value.get("min_demand", 0))
        density = value.get("max_competition_density")
        penalty = float(value.get("return_penalty", 8))
        if min_demand < 0 or density is not None and float(density) < 0 or not 0 <= penalty <= 30:
            raise ValueError("需求、竞争密度或退货扣分超出允许范围")
        return cls(months, min_demand, float(density) if density is not None else None, penalty, weights)

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_history_months": self.min_history_months,
            "min_demand": self.min_demand,
            "max_competition_density": self.max_competition_density,
            "return_penalty": self.return_penalty,
            "weights": {kind: dict(row) for kind, row in self.weights.items()},
        }


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value or "").strip().replace(",", "").replace("，", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(亿|万|千|%|％)?", text)
    if not match:
        return None
    scale = {"亿": 1e8, "万": 1e4, "千": 1e3, "%": .01, "％": .01}.get(match.group(2), 1)
    return float(match.group(1)) * scale


def _measure(raw: Mapping[str, Any], kind: str) -> float | None:
    for alias in ALIASES[kind]:
        if alias in raw:
            if kind == "seasonality_coefficient":
                # Seerfar often exports "旺季 1.22"; the first digit is a label,
                # not the coefficient. This field is displayed, not scored.
                numbers = re.findall(r"\d+(?:\.\d+)?", str(raw[alias] or ""))
                return float(numbers[-1]) if numbers else None
            value = _number(raw[alias])
            if value is not None:
                return value
    return None


def _observations(path: Path | str, dataset: str) -> list[dict[str, Any]]:
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT id,source,entity_key,category_key,period,captured_at,raw_json "
            "FROM observations WHERE dataset=? ORDER BY id DESC LIMIT 20000", (dataset,)
        ).fetchall()
    return [dict(row) | {"raw": json.loads(row["raw_json"])} for row in rows]


def config_path(path: Path | str) -> Path:
    return Path(path).with_name("recommend-config.json")


def load_config(path: Path | str) -> RecommendConfig:
    target = config_path(path)
    try:
        return RecommendConfig.from_mapping(json.loads(target.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return RecommendConfig()


def save_config(path: Path | str, value: Mapping[str, Any]) -> RecommendConfig:
    config = RecommendConfig.from_mapping(value)
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        json.dump(config.as_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, target)
    return config


def _percentile(value: float | None, universe: list[float], *, lower_is_better: bool = False) -> float | None:
    if value is None:
        return None
    if len(universe) < 2 or min(universe) == max(universe):
        return 50.0
    below = sum(item < value for item in universe)
    equal = sum(item == value for item in universe)
    score = 100 * (below + equal / 2) / len(universe)
    return 100 - score if lower_is_better else score


def _trend(values: list[float]) -> float | None:
    if len(values) < 2 or not values[0]:
        return None
    return (values[-1] - values[0]) / abs(values[0])


def _stability(values: list[float], min_months: int) -> float | None:
    if len(values) < min_months or mean(values) <= 0:
        return None
    return max(0.0, 1 - pstdev(values) / mean(values)) * 100


def _group(rows: list[dict[str, Any]], dataset: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for row in rows:  # newest first; keep one snapshot per comparable month
        if row["source"] != "seerfar":
            continue
        entity = (str(row["raw"].get("关键词") or row["entity_key"]).splitlines()[0].strip().casefold()
                  if dataset == "keywords" else str(row["entity_key"]).casefold())
        category = str(row["category_key"]).casefold()
        key = (category, entity) if dataset == "keywords" else (entity, entity)
        period = str(row["period"] or "")
        if not period or (key[0] + ":" + key[1], period, row["source"]) in seen:
            continue
        seen.add((key[0] + ":" + key[1], period, row["source"]))
        groups[key].append(row)
    for entries in groups.values():
        entries.sort(key=lambda row: row["period"])
    return groups


def _signal(rows: list[dict[str, Any]], dataset: str, config: RecommendConfig) -> dict[str, Any]:
    demand_kind = "category_demand" if dataset == "categories" else "keyword_demand"
    demand_pairs = [(row["period"], _measure(row["raw"], demand_kind)) for row in rows]
    demand_pairs = [(period, value) for period, value in demand_pairs if value is not None]
    demand = [value for _, value in demand_pairs]
    latest = rows[-1]["raw"]
    sellers = _measure(latest, "competitors")
    items = _measure(latest, "items")
    # Do not divide a product count by category revenue in rubles. Within the
    # same report/month, use product supply against sales volume (categories)
    # or search heat (keywords). Sellers are evidence, not additive products.
    opportunity_volume = (_measure(latest, "category_volume") if dataset == "categories"
                          else _measure(latest, "keyword_demand"))
    density = items / opportunity_volume if items is not None and opportunity_volume and opportunity_volume > 0 else None
    conversion = _measure(latest, "conversion")
    returns = _measure(latest, "return_rate")
    if returns is not None and returns > 1:
        returns /= 100
    return {
        "demand": mean(demand) if demand else None,
        "history_months": len(demand),
        "stability": _stability(demand, config.min_history_months),
        "trend": _trend(demand),
        "competition_density": density,
        "sellers": sellers,
        "items": items,
        "opportunity_volume": opportunity_volume,
        "density_unit": "竞品数/销量" if dataset == "categories" else "竞品数/月搜热度",
        "conversion": conversion,
        "return_rate": returns,
        "concentration": _measure(latest, "concentration"),
        "crossborder_share": _measure(latest, "crossborder_share"),
        "seasonality_coefficient": _measure(latest, "seasonality_coefficient"),
        "crossborder_eligible": "跨境卖家可售" in str(latest.get("销售方式") or "") if "销售方式" in latest else None,
        "periods": [period for period, _ in demand_pairs],
        "latest_period": rows[-1]["period"],
        "source": "seerfar",
        "evidence_ids": [row["id"] for row in rows],
    }


def recommend(path: Path | str, *, dataset: str, category_key: str | None = None,
              config: RecommendConfig | None = None) -> dict[str, Any]:
    if dataset not in {"categories", "keywords"}:
        raise ValueError("只能推荐类目或关键词")
    config = config or RecommendConfig()
    rows = _observations(path, dataset)
    groups = _group(rows, dataset)
    if dataset == "keywords" and category_key is not None:
        groups = {key: value for key, value in groups.items() if key[0] == category_key}
    candidates = []
    for (category, entity), snapshots in groups.items():
        signal = _signal(snapshots, dataset, config)
        original = snapshots[-1]["raw"].get("关键词" if dataset == "keywords" else "类目")
        candidates.append({"key": entity, "category_key": category,
                           "label": str(original or snapshots[-1]["entity_key"]).splitlines()[0].strip(),
                           "metrics": signal})

    measures = {
        name: [candidate["metrics"][name] for candidate in candidates if candidate["metrics"][name] is not None]
        for name in ("demand", "stability", "trend", "competition_density", "conversion")
    }
    ozon_queries = {str(row["entity_key"]).splitlines()[0].strip().casefold(): row
                    for row in rows if row["source"] == "ozon_seller_api"}
    weights = config.weights[dataset]
    for candidate in candidates:
        metric = candidate["metrics"]
        sub = {
            "demand": _percentile(metric["demand"], measures["demand"]),
            "stability": metric["stability"],
            "trend": _percentile(metric["trend"], measures["trend"]),
            "competition": _percentile(metric["competition_density"], measures["competition_density"], lower_is_better=True),
            "conversion": _percentile(metric["conversion"], measures["conversion"]),
        }
        available = {name: value for name, value in sub.items() if name in weights and value is not None}
        denominator = sum(weights[name] for name in available)
        base = sum(value * weights[name] for name, value in available.items()) / denominator if denominator else 0
        return_penalty = config.return_penalty * min(1, max(0, metric["return_rate"] or 0))
        seasonality_penalty = (min(8, max(0, (metric["seasonality_coefficient"] - 1) * 10))
                               if dataset == "categories" and metric["seasonality_coefficient"] is not None else 0)
        candidate["score"] = round(max(0, base - return_penalty - seasonality_penalty), 1)
        candidate["score_parts"] = {name: round(value, 1) if value is not None else None for name, value in sub.items() if name in weights}
        insufficient = []
        if metric["history_months"] < config.min_history_months:
            insufficient.append(f"仅 {metric['history_months']} 个有效月份，需至少 {config.min_history_months} 个月")
        if metric["demand"] is None or metric["demand"] < config.min_demand:
            insufficient.append("需求数据不足或低于门槛")
        if metric["competition_density"] is None:
            insufficient.append("缺少同月竞争供给数据")
        elif config.max_competition_density is not None and metric["competition_density"] > config.max_competition_density:
            insufficient.append("竞争密度高于设定门槛")
        if metric["crossborder_eligible"] is False:
            insufficient.append("Seerfar 报表未标为跨境卖家可售")
        candidate["recommended"] = not insufficient
        candidate["warnings"] = insufficient
        candidate["reasons"] = [
            f"需求稳定度 {metric['stability']:.0f}/100" if metric["stability"] is not None else "需求稳定度待补历史月份",
            "竞争密度处于候选低位" if sub["competition"] is not None and sub["competition"] >= 60 else "竞争优势尚不明显",
        ]
        if metric["return_rate"] is not None:
            candidate["reasons"].append(f"退货/取消率仅作扣分：-{return_penalty:.1f}")
        if seasonality_penalty:
            candidate["reasons"].append(f"季节性系数 {metric['seasonality_coefficient']:.2f}：-{seasonality_penalty:.1f}")
        if dataset == "keywords":
            official = ozon_queries.get(candidate["key"])
            candidate["ozon_evidence"] = ({"observation_id": official["id"], "captured_at": official["captured_at"],
                                           "raw": official["raw"]} if official else None)
        candidate["confidence"] = "high" if candidate["recommended"] and len(available) == len(weights) else "limited"

    candidates.sort(key=lambda item: (not item["recommended"], -item["score"], item["label"]))
    return {"dataset": dataset, "category_key": category_key, "config": config.as_dict(),
            "recommended_count": sum(item["recommended"] for item in candidates),
            "count": len(candidates), "items": candidates}
