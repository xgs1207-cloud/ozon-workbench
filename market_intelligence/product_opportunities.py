"""Read-only, explainable competitor product screening from Seerfar snapshots.

Sales and review barriers are not competitor counts. Recommendations require
independent category competition evidence as well as same-category peers.
This is a research shortlist, not a profitability or delivery prediction.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .store import connect


def number(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if value is None or isinstance(value, bool):
            continue
        # Growth figures on a second line must never be added to sales.
        text = str(value).splitlines()[0].strip() if str(value).strip() else ""
        text = re.sub(r"[\s\u00a0₽￥¥]", "", text)
        if re.fullmatch(r"[+-]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?%?", text):
            text = text.replace(",", "")
        if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?%?", text):
            continue
        result = float(text.rstrip("%").replace(",", "."))
        if math.isfinite(result):
            return result
    return None


def text(raw: dict[str, Any], *keys: str) -> str:
    return next((str(raw[k]).strip() for k in keys if raw.get(k)), "")


def captured_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def listing_age(raw: dict[str, Any], captured_at: str, today: date) -> tuple[int | None, str]:
    for key in ("上架时间", "上架日期", "创建日期", "首次上架日期", "listing_date", "upTime"):
        value = text(raw, key)
        match = re.search(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", value)
        if match:
            try:
                listed = date(*map(int, match.groups()))
                age = (today - listed).days
                return (age, f"{key} {listed.isoformat()}") if age >= 0 else (None, "上架日期晚于今天")
            except ValueError:
                return None, "上架日期无效"
    age = number(raw, "上架天数", "在售天数", "listing_days")
    capture_day = captured_date(captured_at)
    if age is not None and age >= 0 and age.is_integer() and capture_day and capture_day <= today:
        return int(age) + (today - capture_day).days, "采集时上架天数 + 已过去天数"
    return None, "缺少可核验上架日期/天数"


def product_url(raw: dict[str, Any], sku: str) -> str | None:
    candidates = []
    for key, value in raw.items():
        if "链接" in key or key in {"url", "product_url"}:
            candidates.extend(value if isinstance(value, list) else [value])
    for value in candidates:
        try:
            parsed = urlparse(str(value))
            if (parsed.scheme == "https" and parsed.hostname in {"ozon.ru", "www.ozon.ru"}
                    and parsed.path.startswith("/product/") and not parsed.username and not parsed.password):
                return str(value)
        except ValueError:
            pass
    return f"https://www.ozon.ru/product/{sku}/" if re.fullmatch(r"\d{5,20}", sku) else None


def percentile(value: float, peers: list[float]) -> float:
    """Midrank; equal observations have no spurious first/last advantage."""
    return (sum(x < value for x in peers) + .5 * sum(x == value for x in peers)) / len(peers)


def _latest(conn, dataset: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM (SELECT o.*,b.page_url,ROW_NUMBER() OVER "
        "(PARTITION BY o.entity_key ORDER BY o.captured_at DESC,o.id DESC) AS rn "
        "FROM observations o JOIN ingest_batches b ON b.id=o.batch_id "
        "WHERE o.source='seerfar' AND o.dataset=?) WHERE rn=1", (dataset,),
    ).fetchall()
    return [dict(row) | {"raw": json.loads(row["raw_json"])} for row in rows]


def list_opportunities(path: Path | str, *, q: str = "", category_key: str = "",
                       status: str = "recommended", limit: int = 50, offset: int = 0,
                       today: date | None = None) -> dict[str, Any]:
    if status not in {"all", "recommended", "pending", "watch", "excluded"}:
        raise ValueError("无效的商品筛选状态")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("无效的分页参数")
    today = today or datetime.now(timezone.utc).date()
    with closing(connect(path)) as conn:
        products = _latest(conn, "products")
        categories = _latest(conn, "categories")
    category_data = {}
    density_peers = defaultdict(list)
    for row in categories:
        raw = row["raw"]
        competitors = number(raw, "竞品数", "在售商品数", "products_count")
        volume = number(raw, "销量", "月销量", "sale_count")
        capture_day = captured_date(row["captured_at"])
        if (competitors is None or competitors < 0 or volume is None or volume <= 0
                or not capture_day or not 0 <= (today - capture_day).days <= 62):
            continue
        density = competitors / volume
        item = {"density": density, "evidence_id": row["id"], "period": row["period"],
                "period_kind": row["period_kind"], "competitors": competitors, "sales": volume}
        category_data[row["category_key"]] = item
        density_peers[(row["period_kind"], row["period"])].append(density)
    normalized = []
    review_peers = defaultdict(list)
    sales_peers = defaultdict(list)
    for row in products:
        raw = row["raw"]
        sku = text(raw, "SKU", "sku", "商品ID", "product_id") or row["entity_key"]
        age, age_basis = listing_age(raw, row["captured_at"], today)
        sales = number(raw, "销量", "月销量", "sales", "sale_count")
        reviews = number(raw, "评论数", "评价数", "reviewCount", "reviews")
        capture_day = captured_date(row["captured_at"])
        fresh = bool(capture_day and 0 <= (today - capture_day).days <= 14)
        images = next((v for k, v in raw.items() if k.endswith("图片") and isinstance(v, list) and v), [])
        title = text(raw, "商品", "商品信息 / SKU", "商品 / SKU", "商品/SKU", "商品名称", "标题", "title")
        title = title.splitlines()[0] if title else ""
        item = {"id": row["id"], "sku": sku, "title": title,
                "category_key": row["category_key"], "age_days": age, "age_basis": age_basis,
                "sales": sales, "reviews": reviews, "price": number(raw, "售价", "价格", "price"),
                "revenue": number(raw, "销售额", "revenue"), "rating": number(raw, "评分", "reviewRating"),
                "return_rate": number(raw, "退货取消率", "退货率", "returnCancellationRate"),
                "product_url": product_url(raw, sku), "image_url": str(images[0]) if images else None,
                "captured_at": row["captured_at"], "period": row["period"], "period_kind": row["period_kind"],
                "source_url": row["page_url"], "raw": raw, "fresh": fresh}
        normalized.append(item)
        # Peer comparisons use the full captured category, not just the result
        # after the user's age/query filters. Never compare monthly/rolling sales.
        group = (row["category_key"], row["period_kind"], row["period"])
        if fresh and row["category_key"]:
            if reviews is not None and reviews >= 0:
                review_peers[group].append(reviews)
            if sales is not None and sales >= 0:
                sales_peers[group].append(sales)
    for item in normalized:
        reasons, warnings, excluded = [], [], []
        age, sales = item["age_days"], item["sales"]
        if age is None:
            warnings.append(item["age_basis"])
        elif age >= 90:
            excluded.append(f"上架 {age} 天，不满足不足 90 天")
        else:
            reasons.append(f"上架 {age} 天（不足 90 天）")
        if sales is None or sales < 0:
            warnings.append("缺少有效销量")
        elif sales == 0:
            excluded.append("销量为 0")
        else:
            reasons.append(f"报表销量 {sales:g}")
        if not item["fresh"]:
            warnings.append("商品快照超过 14 天或时间无效，请重新采集核验销量")
        cat = category_data.get(item["category_key"])
        low_competition = False
        item["competition_density"] = cat["density"] if cat else None
        item["competition_evidence_id"] = cat["evidence_id"] if cat else None
        competition_rank = None
        if cat and len(density_peers[(cat["period_kind"], cat["period"])]) >= 5:
            competition_rank = percentile(cat["density"], density_peers[(cat["period_kind"], cat["period"])])
            low_competition = competition_rank <= .4
            reasons.append(f"类目竞争密度 {cat['density']:.3f}（竞品数÷销量），同口径样本分位 {competition_rank:.0%}")
            if not low_competition:
                reasons.append("类目竞争密度未进入较低的 40%")
        else:
            warnings.append("缺少近期类目竞品数/销量，或可比较类目不足 5 个；不能确认低竞争")
        group = (item["category_key"], item["period_kind"], item["period"])
        peers = review_peers[group]
        review_rank = None
        if item["reviews"] is not None and item["reviews"] >= 0 and len(peers) >= 5:
            review_rank = percentile(item["reviews"], peers)
            reasons.append(f"评论数 {item['reviews']:g}，同类目样本分位 {review_rank:.0%}（评价壁垒，不是竞争度）")
        else:
            warnings.append("缺少评论数或同类目有效商品不足 5 个，评价壁垒待核验")
        demand_rank = percentile(sales, sales_peers[group]) if sales is not None and sales >= 0 and sales_peers[group] else 0
        score = (40 * (1 - competition_rank) if competition_rank is not None else 0)
        score += 30 * (1 - review_rank) if review_rank is not None else 0
        score += 30 * demand_rank
        returns = item["return_rate"]
        if returns is not None and 0 <= returns <= 100:
            score -= 8 * returns / 100
            reasons.append(f"退货取消率 {returns:g}%，仅作扣分")
        item["score"] = round(max(0, min(100, score)), 1)
        item["reasons"], item["warnings"] = reasons + excluded, warnings
        item["status"] = ("excluded" if excluded else "pending" if warnings else
                          "recommended" if low_competition and review_rank <= .5 and score >= 60 else "watch")
    counts = {name: sum(x["status"] == name for x in normalized) for name in ("recommended", "pending", "watch", "excluded")}
    category_options = sorted({x["category_key"] for x in normalized if x["category_key"]})
    query = q.strip().casefold()
    items = [x for x in normalized if (status == "all" or x["status"] == status)
             and (not category_key or x["category_key"] == category_key)
             and (not query or query in f"{x['sku']} {x['title']} {x['category_key']}".casefold())]
    order = {"recommended": 0, "watch": 1, "pending": 2, "excluded": 3}
    items.sort(key=lambda x: (order[x["status"]], -x["score"], x["sku"]))
    return {"items": items[offset:offset + limit], "total": len(items), "all_count": len(normalized),
            "counts": counts, "categories": category_options, "limit": limit, "offset": offset,
            "as_of": today.isoformat(), "rules": {"age_days_lt": 90, "sales_gt": 0,
                "competition_bottom_fraction": .4, "review_bottom_fraction": .5, "min_peers": 5,
                "min_score": 60, "product_freshness_days": 14, "category_freshness_days": 62}}
