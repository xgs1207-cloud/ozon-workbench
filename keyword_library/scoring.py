"""高热度低竞争打分（按类目分位数归一）。

为什么不用全站固定阈值：不同 Ozon 类目的搜索量与竞品数量级能差好几个数量级，
固定阈值会让冷门类目永远不入库、热门类目永远超标。这里按 ``(category_id, type_id)``
分组做秩百分位归一，再合成一个可解释的分数：

    score = heat_percentile - lambda * competition_percentile

- ``heat_percentile``：该词在本类目内搜索量的百分位（0~1，越大越热）
- ``competition_percentile``：该词在本类目内竞品数的百分位（0~1，**越小竞争越弱**）
- ``lambda``：竞争惩罚权重，默认 0.6，可调

指标缺失不猜测：``search_volume`` 或 ``competitor_count`` 缺失时 ``score=None``，
记录保持 ``candidate``，由人工或数据源补齐后再打分。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

HEAT_FIELD = "search_volume"
COMPETITION_FIELD = "competitor_count"

#: 同组样本少于该数量时分位数参考性弱，仅在 reasons 里提示，不阻断打分
MIN_GROUP_SIZE_FOR_PERCENTILE = 3


@dataclass(frozen=True)
class ScoreConfig:
    """打分参数。默认值偏保守：热度须进类目前 40%，竞争须落在类目前 60%。"""

    lam: float = 0.6
    min_heat_percentile: float = 0.6
    max_competition_percentile: float = 0.6
    min_search_volume: int | None = None
    max_competitor_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreResult:
    score: float | None = None
    heat_percentile: float | None = None
    competition_percentile: float | None = None
    qualified: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_number(value: Any) -> float | None:
    """把指标值转成非负 float；空值、布尔、非数字、负数都返回 None（不猜）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace(" ", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number) or number < 0:
            return None
        return number
    return None


def _percentile_of(sorted_values: Sequence[float], value: float) -> float:
    """值在已排序序列中的中位秩百分位（并列共享平均秩），范围 (0, 1)。"""
    size = len(sorted_values)
    if size == 0:
        return 0.0
    if size == 1:
        return 0.5
    left = bisect_left(sorted_values, value)
    right = bisect_right(sorted_values, value)
    average_rank = (left + right - 1) / 2.0
    return (average_rank + 0.5) / size


def score_records(
    records: Iterable[Mapping[str, Any]],
    config: ScoreConfig | None = None,
) -> dict[str, ScoreResult]:
    """给每条记录打分，返回 ``{记录主键: ScoreResult}``。

    记录必须带 ``key``、``category_id``、``type_id``；缺 ``key`` 时用下标占位键。
    """
    config = config or ScoreConfig()
    items = list(records)
    results: dict[str, ScoreResult] = {}
    groups: dict[tuple[str, str], list[tuple[str, Mapping[str, Any]]]] = {}

    for position, record in enumerate(items):
        key = str(record.get("key") or f"#{position}")
        result = ScoreResult()
        heat = _to_number(record.get(HEAT_FIELD))
        competition = _to_number(record.get(COMPETITION_FIELD))
        if heat is None:
            result.reasons.append(f"缺少搜索量（{HEAT_FIELD}），无法判定热度")
        if competition is None:
            result.reasons.append(f"缺少竞品数（{COMPETITION_FIELD}），无法判定竞争")
        results[key] = result
        if heat is not None and competition is not None:
            group = (str(record.get("category_id") or ""), str(record.get("type_id") or ""))
            groups.setdefault(group, []).append((key, record))

    for members in groups.values():
        heat_values = sorted(_to_number(record.get(HEAT_FIELD)) for _, record in members)
        competition_values = sorted(
            _to_number(record.get(COMPETITION_FIELD)) for _, record in members
        )
        group_size = len(members)
        for key, record in members:
            result = results[key]
            heat = _to_number(record.get(HEAT_FIELD))
            competition = _to_number(record.get(COMPETITION_FIELD))
            heat_percentile = _percentile_of(heat_values, heat)
            competition_percentile = _percentile_of(competition_values, competition)
            result.heat_percentile = round(heat_percentile, 4)
            result.competition_percentile = round(competition_percentile, 4)
            result.score = round(heat_percentile - config.lam * competition_percentile, 4)

            blocked = False
            if heat_percentile < config.min_heat_percentile:
                blocked = True
                result.reasons.append(
                    f"热度分位 {heat_percentile:.2f} 低于门槛 {config.min_heat_percentile:.2f}"
                )
            if competition_percentile > config.max_competition_percentile:
                blocked = True
                result.reasons.append(
                    f"竞争分位 {competition_percentile:.2f} 高于门槛 "
                    f"{config.max_competition_percentile:.2f}"
                )
            if config.min_search_volume is not None and heat < config.min_search_volume:
                blocked = True
                result.reasons.append(
                    f"搜索量 {heat:.0f} 低于绝对下限 {config.min_search_volume}"
                )
            if config.max_competitor_count is not None and competition > config.max_competitor_count:
                blocked = True
                result.reasons.append(
                    f"竞品数 {competition:.0f} 高于绝对上限 {config.max_competitor_count}"
                )
            if group_size < MIN_GROUP_SIZE_FOR_PERCENTILE:
                result.reasons.append(f"本类目样本仅 {group_size} 条，分位数参考性弱")
            result.qualified = not blocked
            if not result.reasons:
                result.reasons.append("热度与竞争均达标")

    return results
