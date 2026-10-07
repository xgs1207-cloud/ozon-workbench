"""Shared, reproducible copy evidence for workflow and bounded model repair."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

from rules.validate import official_copy_checks

COPY_EVIDENCE_VERSION = 2


def safe_evidence_problems(problems: list[Any]) -> list[str]:
    """Human diagnostics only; raw model JSON never belongs in a GET response."""
    result = []
    for problem in problems[:6]:
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(problem))
        text = re.sub(r"https?://\S+", "[链接已隐藏]", text, flags=re.IGNORECASE)
        text = re.sub(r"(?:sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?)",
                      "[凭据已隐藏]", text)
        text = re.sub(r"Bearer\s+\S+", "Bearer [已隐藏]", text, flags=re.IGNORECASE)
        result.append(text[:320])
    return result


_MEASURE = re.compile(r"(?<![\w])(?P<number>\d+(?:[.,]\d+)?)[ \t]*(?P<unit>[A-Za-zА-Яа-яЁё]+|%)?")
_UNITS = {
    "ml": "ml", "мл": "ml", "миллилитр": "ml", "миллилитра": "ml", "миллилитров": "ml",
    "l": "l", "л": "l", "литр": "l", "литра": "l", "литров": "l",
    "g": "g", "г": "g", "гр": "g", "грамм": "g", "грамма": "g", "граммов": "g",
    "kg": "kg", "кг": "kg", "килограмм": "kg", "килограмма": "kg", "килограммов": "kg",
    "mm": "mm", "мм": "mm", "миллиметр": "mm", "миллиметра": "mm", "миллиметров": "mm",
    "cm": "cm", "см": "cm", "сантиметр": "cm", "сантиметра": "cm", "сантиметров": "cm",
    "м": "m", "m": "m", "метр": "m", "метра": "m", "метров": "m",
    "шт": "piece", "штук": "piece", "штуки": "piece", "pcs": "piece", "pc": "piece",
    "штука": "piece", "piece": "piece", "pieces": "piece",
    "вт": "w", "w": "w", "ватт": "w", "ватта": "w", "ваттов": "w",
    "v": "v", "в": "v", "вольт": "v", "вольта": "v", "вольтов": "v",
    # Age/duration and battery capacity are not unitless numbers. A package
    # count of two must never become evidence for an age of two years.
    "год": "year", "года": "year", "лет": "year", "year": "year", "years": "year",
    "мес": "month", "месяц": "month", "месяца": "month", "месяцев": "month",
    "month": "month", "months": "month",
    "день": "day", "дня": "day", "дней": "day", "сутки": "day", "суток": "day",
    "day": "day", "days": "day",
    "ч": "hour", "час": "hour", "часа": "hour", "часов": "hour", "h": "hour",
    "hour": "hour", "hours": "hour",
    "мин": "minute", "минута": "minute", "минуты": "minute", "минут": "minute",
    "min": "minute", "minute": "minute", "minutes": "minute",
    "с": "second", "сек": "second", "секунда": "second", "секунды": "second",
    "секунд": "second", "s": "second", "sec": "second", "second": "second", "seconds": "second",
    "мач": "mah", "mah": "mah", "ач": "ah", "ah": "ah", "%": "percent",
    "процент": "percent", "процента": "percent", "процентов": "percent", "percent": "percent",
}
_MATERIAL_CLAIMS = (
    (r"\bсиликон\w*", ("硅胶", "silicone", "силикон")),
    (r"\bхлоп\w*", ("棉", "cotton", "хлоп")),
    (r"\bполиэстер\w*", ("涤纶", "聚酯", "polyester", "полиэстер")),
    (r"\bпластик\w*", ("塑料", "plastic", "пластик", "abs", "pvc", "полипропилен")),
    (r"\b(?:нержавеющ\w*|сталь\w*|стальн\w*)", ("钢", "steel", "сталь", "стальн", "нержавеющ")),
    (r"\bшерст\w*", ("羊毛", "шерст", "wool")),
    (r"\b(?:деревян\w*|древесин\w*)", ("木", "wood", "дерев", "древес")),
)


def _measurements(value: Any, unit: str = "") -> set[tuple[str, str | None]]:
    text = f"{value} {unit}".strip()
    result: set[tuple[str, str | None]] = set()
    for match in _MEASURE.finditer(text):
        # Decimal preserves exact measurements (including large values); strip
        # fractional zeroes only, never the zeroes of an integer such as 100.
        number = format(Decimal(match["number"].replace(",", ".")), "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        token = (match["unit"] or "").casefold()
        # Unknown measure words stay distinct instead of becoming a wildcard.
        # Bare numbers only match bare facts; unit conversion is not inferred.
        canonical_unit = _UNITS.get(token, f"unrecognized:{token}" if token else None)
        result.add((number, canonical_unit))
    return result


def verified_copy_facts(analysis: Mapping[str, Any], source: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Labels prove identity only; they never bless numeric/material claims.

    A confirmed category and captured selected-SKU label remain useful when all
    specifications are unknown. Supplier titles, SKU IDs, prices and pictures
    are not specification evidence, and are deliberately excluded.
    """
    result: list[dict[str, Any]] = []
    skip = {"title_cn", "category_cn", "source", "source_refs", "image_refs", "sku_id", "name_cn", "price_cny"}

    def visit(value: Any, path: str, key: str = "") -> None:
        if key in skip or value is None or value == "unknown":
            return
        if isinstance(value, Mapping):
            for name, child in value.items():
                visit(child, f"{path}.{name}", str(name))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                identity = str(child.get("sku_id")) if isinstance(child, Mapping) and child.get("sku_id") else str(index)
                visit(child, f"{path}.{identity}", key)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            unit = "мм" if key.endswith("_mm") else "г" if key.endswith("_g") else "шт" if key == "package_quantity" else ""
            if path.startswith("facts.package_quantity") and key == "value":
                unit = "шт"
            result.append({"id": path, "value": value, "unit": unit, "verified": True,
                           "source": "confirmed_product_facts", "kind": "specification", "allow_numeric": True,
                           "evidence": [f"output/product-analysis.json#{path}"]})

    facts = analysis.get("facts") or {}
    visit(facts, "facts")
    for sku in facts.get("skus") or []:
        if not isinstance(sku, Mapping) or not sku.get("sku_id"):
            continue
        name = sku.get("name_cn")
        if isinstance(name, str) and name.strip() and name.strip() not in {"unknown", str(sku["sku_id"])}:
            path = f"facts.skus.{sku['sku_id']}.name_cn"
            result.append({"id": path, "value": name.strip(), "unit": "", "verified": True,
                           "source": "selected_sku_label", "kind": "sku_descriptor", "allow_numeric": False,
                           "usage": "仅用于已选规格的颜色、外形或名称；不能证明尺寸、材质、数量、功能或认证",
                           "evidence": [f"output/product-analysis.json#{path}"]})
    category = (source or {}).get("selected_category") or {}
    if isinstance(category, Mapping) and category.get("source") == "ozon_seller_api" and category.get("confirmed_by_user") is True:
        name = category.get("type_name") or category.get("category_name_ru") or category.get("category_name")
        if isinstance(name, str) and name.strip() and name.strip() != "unknown":
            result.append({"id": "category.type", "value": name.strip(), "unit": "", "verified": True,
                           "source": "confirmed_ozon_type", "kind": "product_type", "allow_numeric": False,
                           "usage": "仅证明商品类型；不能证明材质、尺寸、性能、适用年龄或认证",
                           "evidence": ["input/category-form.json#category_name", "input/category-selection.json"]})
    return result


def candidate_evidence(copy: Mapping[str, Any], facts: list[dict[str, Any]], keywords: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate before accepting or repairing a paid response; never drop claims."""
    if any(not isinstance(copy.get(field), str) for field in ("title_ru", "description_ru")):
        raise ValueError("title_ru 和 description_ru 必须是字符串")
    combined = f"{copy['title_ru']}\n{copy['description_ru']}"
    by_id = {row["id"]: row for row in facts if row.get("verified") is True}
    numeric_facts = {identity: fact for identity, fact in by_id.items() if fact.get("allow_numeric", True)}
    evidence = []
    claims = copy.get("claim_evidence", [])
    if not isinstance(claims, list):
        raise ValueError("claim_evidence 必须是数组；每项包含文案原文 claim 和事实 ID 数组 fact_ids")
    for row in claims:
        if not isinstance(row, Mapping):
            raise ValueError("claim_evidence 每项必须是对象")
        claim, ids = row.get("claim"), row.get("fact_ids")
        if not isinstance(claim, str) or not claim.strip() or claim.strip() not in combined:
            raise ValueError("候选声明的事实引用无效：claim 必须逐字出现在标题或简介中，不能概括改写")
        claim = claim.strip()
        if not isinstance(ids, list) or not ids or any(not isinstance(identity, str) for identity in ids):
            raise ValueError("候选声明的事实引用无效：fact_ids 必须是非空字符串数组")
        unknown = [identity for identity in ids if identity not in by_id]
        if unknown:
            raise ValueError("候选声明的事实引用无效：未知 fact_ids=" + ", ".join(safe_evidence_problems(unknown[:4]))
                             + "；只能引用 verified_facts 中的完整 ID")
        measures = {measure for identity in ids if identity in numeric_facts
                    for measure in _measurements(by_id[identity]["value"], by_id[identity].get("unit", ""))}
        if any(not any(n == fn and u == fu for fn, fu in measures) for n, u in _measurements(claim)):
            raise ValueError("候选数值/单位与引用的事实不一致；规格名称和类目不能证明数值，请去掉无依据的数值")
        evidence.append({"claim": claim, "fact_ids": ids})
    # Legacy providers can omit evidence; matching remains exact on value/unit.
    for match in _MEASURE.finditer(combined):
        raw = match.group(0).strip()
        numeric_only = match["number"] + (match["unit"] or "")
        measurement = next(iter(_measurements(numeric_only)))
        matching = [identity for identity, fact in numeric_facts.items()
                    if any(measurement[0] == n and measurement[1] == unit
                           for n, unit in _measurements(fact["value"], fact.get("unit", "")))]
        if not matching:
            raise ValueError(f"候选含未证实的数值或单位：{numeric_only}，请移除相关词或补充真实事实")
        if not any(raw in row["claim"] for row in evidence):
            evidence.append({"claim": raw, "fact_ids": matching})
    for pattern, aliases in _MATERIAL_CLAIMS:
        for match in re.finditer(pattern, combined, re.IGNORECASE):
            matching = [identity for identity, fact in by_id.items() if "material" in identity
                        and fact.get("kind") not in {"sku_descriptor", "product_type"}
                        and any(alias in str(fact["value"]).casefold() for alias in aliases)]
            if not matching:
                raise ValueError(f"候选含未证实的材质声明：{match.group(0)}，请删除或补充真实材质事实")
            if not any(match.group(0) in row["claim"] for row in evidence):
                evidence.append({"claim": match.group(0), "fact_ids": matching})
    allowed = {row["keyword"] for row in keywords}
    for field in ("primary_keywords", "secondary_keywords"):
        terms = copy.get(field, [])
        if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
            raise ValueError(f"{field} 必须是字符串数组；无已选词时使用 []")
        if set(terms) - allowed:
            raise ValueError("候选使用了未选或排除的关键词；无已选关键词时关键词数组应为空")
    usage = []
    for row in keywords:
        placement = [field for field, text in (("title", copy["title_ru"]), ("description", copy["description_ru"]))
                     if row["keyword"].casefold() in text.casefold()]
        if placement or row["role"] == "core":
            usage.append({"query": row["keyword"], "role": row["role"], "placement": placement,
                          "surface_form": row["keyword"], "matched_lemmas": []})
    checks = official_copy_checks(copy)
    return {"claim_evidence": evidence, "keyword_usage": usage, "excluded_keywords": [],
            "audit": {"title_chars": len(copy["title_ru"]), "description_chars": len(copy["description_ru"]),
                      "core_coverage": next((bool(row["placement"]) for row in usage if row["role"] == "core"), None),
                      "secondary_coverage": None, "fact_consistency": None, "readability": None,
                      "risk_flags": checks["advisory"] + ["材质、用途和俄语词形需要人工核对"], **checks}}
