"""Separate draft preparation from publication without suppressing risk evidence."""
from __future__ import annotations

import re
from typing import Any, Mapping

_MISSING = re.compile(
    r"缺少|缺失|未提供|未确认|未注明|没有.{0,12}(?:信息|数据|参数|证明|认证)|"
    r"отсутств|не\s+указ|нет\s+данн|недостаточ|missing|not\s+(?:provided|specified|confirmed)", re.I)
_CATEGORY_MISSING = re.compile(
    r"(?:没有|未|尚未|未能).{0,8}(?:选择|确定|确认)|не\s+выбран|not\s+(?:selected|chosen)|no\s+category", re.I)
_CATEGORY_CONFLICT = re.compile(r"不符|不匹配|矛盾|错分|mismatch|does\s+not\s+match|не\s+соответств", re.I)


def risk_gates(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """AI lack-of-data warnings are not a ban on composing a truthful draft.

    Keep compliance evidence unresolved at the publication boundary. Known
    prohibited/unsafe products, explicit rejection and unfamiliar blockers stay
    blocking even in preparation. No risk artifact or verified fact is rewritten.
    """
    preparation, publication, deferred = [], [], []
    if (payload.get("recommendation") or {}).get("decision") == "reject":
        preparation.append("商品分析建议停止，请先解决商品或合规问题")
    for row in payload.get("risks") or []:
        if not isinstance(row, Mapping) or row.get("blocking") is not True:
            continue
        area = str(row.get("area") or "").casefold()
        message = str(row.get("message") or "未解决的商品风险")
        missing = bool(_MISSING.search(message))
        if area == "category" and (missing or _CATEGORY_MISSING.search(message)) and not _CATEGORY_CONFLICT.search(message):
            # Only missing category data can defer; a known mismatch is a real
            # product-identity blocker, not cured by selecting any official type.
            deferred.append(message)
        elif missing and area in {"product_info", "logistics", "material", "materials", "dimensions", "weight", "brand", "package_quantity"}:
            # Mandatory official fields, shipping facts and truthful claims are
            # independently checked; unknown OPTIONAL data stays blank.
            deferred.append(message)
        elif missing and area in {"compliance", "certification", "certifications", "safety"}:
            deferred.append(message)
            publication.append(message)
        else:
            preparation.append(message)
            publication.append(message)
    return {"preparation": list(dict.fromkeys(preparation)),
            "publication": list(dict.fromkeys(publication)),
            "deferred": list(dict.fromkeys(deferred))}
