"""A research decision (one category and word set) can feed many 1688 products.

This is deliberately separate from pipeline.batch: that module freezes already
collected SKU runs, while a research session starts *before* product capture.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .recommend import load_config, recommend
from .store import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_sessions (
            id TEXT PRIMARY KEY,
            category_key TEXT NOT NULL,
            primary_keyword TEXT NOT NULL,
            secondary_keywords_json TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            auto_publish_enabled INTEGER NOT NULL DEFAULT 0,
            target_store_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS research_session_products (
            session_id TEXT NOT NULL REFERENCES research_sessions(id),
            product_id TEXT NOT NULL UNIQUE,
            attached_at TEXT NOT NULL,
            PRIMARY KEY (session_id, product_id)
        );
        CREATE INDEX IF NOT EXISTS ix_research_session_products_session
            ON research_session_products(session_id);
        CREATE TABLE IF NOT EXISTS research_publish_attempts (
            product_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES research_sessions(id),
            store_id TEXT NOT NULL,
            state TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    return conn


def _row(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM research_sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise ValueError("选词批次不存在")
    data = dict(row)
    data["secondary_keywords"] = json.loads(data.pop("secondary_keywords_json"))
    data["config"] = json.loads(data.pop("config_json"))
    data["auto_publish_enabled"] = bool(data["auto_publish_enabled"])
    data["products"] = [dict(item) for item in conn.execute(
        "SELECT product_id,attached_at FROM research_session_products WHERE session_id=? ORDER BY attached_at,product_id",
        (session_id,),
    )]
    return data


def create_session(path: Path | str, *, category_key: str, primary_keyword: str,
                   secondary_keywords: Sequence[str] = ()) -> dict[str, Any]:
    category_key = category_key.strip()
    primary_keyword = primary_keyword.strip()
    secondary = list(dict.fromkeys(str(item).strip() for item in secondary_keywords if str(item).strip()))
    secondary = [item for item in secondary if item.casefold() != primary_keyword.casefold()]
    if not category_key or not primary_keyword or len(secondary) > 10:
        raise ValueError("须选一个类目、一个主词；辅词最多 10 个")
    config = load_config(path)
    categories = recommend(path, dataset="categories", config=config)["items"]
    category = next((row for row in categories if row["key"] == category_key), None)
    if category is None or not category["recommended"]:
        raise ValueError("这个类目未达到当前推荐门槛，先补数据或调整筛选规则")
    keywords = recommend(path, dataset="keywords", category_key=category_key, config=config)["items"]
    qualified = {row["label"].casefold() for row in keywords if row["recommended"]}
    if primary_keyword.casefold() not in qualified or any(item.casefold() not in qualified for item in secondary):
        raise ValueError("主词或辅词不在该类目当前推荐词中")
    session_id = "R-" + uuid.uuid4().hex[:12].upper()
    with closing(_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO research_sessions(id,category_key,primary_keyword,secondary_keywords_json,config_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, category_key, primary_keyword, json.dumps(secondary, ensure_ascii=False),
             json.dumps(config.as_dict(), ensure_ascii=False), _now()),
        )
        return _row(conn, session_id)


def get_session(path: Path | str, session_id: str) -> dict[str, Any]:
    with closing(_db(path)) as conn:
        return _row(conn, session_id)


def list_sessions(path: Path | str) -> list[dict[str, Any]]:
    with closing(_db(path)) as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM research_sessions ORDER BY created_at DESC LIMIT 200")]
        return [_row(conn, session_id) for session_id in ids]


def attach_product(path: Path | str, products_root: Path | str, *, session_id: str,
                   product_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"P[0-9]{6}", product_id):
        raise ValueError("商品编号无效")
    product_dir = (Path(products_root) / product_id).resolve()
    if not product_dir.is_relative_to(Path(products_root).resolve()):
        raise ValueError("商品路径无效")
    if not (product_dir / "status.json").is_file():
        raise ValueError("商品不存在或尚未通过 1688 插件采集")
    source_file = product_dir / "input" / "source.json"
    try:
        source = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("商品采集源不可读") from error
    if "1688.com/offer/" not in str(source.get("source_url") or ""):
        raise ValueError("只能关联 1688 采集商品")
    from pipeline.status import load_status

    if int(load_status(product_dir).get("api_write_count") or 0) > 0:
        raise ValueError("该商品已经写入 Ozon，不能加入新的研究批次")
    from pipeline.selection import set_selected_keywords

    with closing(_db(path)) as conn, conn:
        session = _row(conn, session_id)
        existing = conn.execute("SELECT session_id FROM research_session_products WHERE product_id=?", (product_id,)).fetchone()
        if existing and existing[0] != session_id:
            raise ValueError("商品已属于另一个选词批次")
        if not existing:
            conn.execute("INSERT INTO research_session_products(session_id,product_id,attached_at) VALUES (?,?,?)",
                         (session_id, product_id, _now()))
        selected = [{"keyword": session["primary_keyword"], "role": "core"}]
        selected.extend({"keyword": word, "role": "secondary"} for word in session["secondary_keywords"])
        set_selected_keywords(product_dir, selected, category={"research_category_key": session["category_key"]},
                              source=f"research_session:{session_id}")
        # Guided batches never accept the legacy cost-plus suggested price as
        # the actual listing price. A separate, explicit per-SKU entry is required.
        (product_dir / "input" / "manual-pricing-required.json").write_text(
            json.dumps({"session_id": session_id, "required": True}, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        from pipeline.guided_review import invalidate_from

        invalidate_from(product_dir, "product_analysis")
        return _row(conn, session_id)


def set_auto_publish(path: Path | str, *, session_id: str, enabled: bool,
                     target_store_id: str = "") -> dict[str, Any]:
    with closing(_db(path)) as conn, conn:
        _row(conn, session_id)
        if enabled and not target_store_id.strip():
            raise ValueError("开启自动发布须指定目标店铺")
        conn.execute("UPDATE research_sessions SET auto_publish_enabled=?,target_store_id=? WHERE id=?",
                     (int(enabled), target_store_id.strip() if enabled else "", session_id))
        return _row(conn, session_id)


def claim_publish(path: Path | str, *, session_id: str, product_id: str, store_id: str) -> bool:
    """At most one automatic attempt; ambiguous outcomes need manual review."""
    with closing(_db(path)) as conn, conn:
        session = _row(conn, session_id)
        if not session["auto_publish_enabled"] or session["target_store_id"] != store_id:
            raise ValueError("该批次未对这家店开启自动发布")
        if product_id not in {item["product_id"] for item in session["products"]}:
            raise ValueError("商品不属于此选词批次")
        inserted = conn.execute(
            "INSERT OR IGNORE INTO research_publish_attempts(product_id,session_id,store_id,state,attempted_at) "
            "VALUES (?,?,?,?,?)", (product_id, session_id, store_id, "in_flight", _now())
        )
        return inserted.rowcount == 1


def finish_publish(path: Path | str, *, product_id: str, state: str, result: dict[str, Any]) -> None:
    if state not in {"submitted", "failed_manual_review"}:
        raise ValueError("无效的发布结果")
    with closing(_db(path)) as conn, conn:
        conn.execute("UPDATE research_publish_attempts SET state=?,result_json=? WHERE product_id=?",
                     (state, json.dumps(result, ensure_ascii=False), product_id))


def list_publish_attempts(path: Path | str, session_id: str) -> list[dict[str, Any]]:
    with closing(_db(path)) as conn:
        rows = conn.execute(
            "SELECT product_id,store_id,state,attempted_at,result_json FROM research_publish_attempts "
            "WHERE session_id=? ORDER BY attempted_at", (session_id,)
        ).fetchall()
    return [{**dict(row), "result": json.loads(row["result_json"])} for row in rows]


def session_for_product(path: Path | str, product_id: str) -> str | None:
    with closing(_db(path)) as conn:
        row = conn.execute("SELECT session_id FROM research_session_products WHERE product_id=?", (product_id,)).fetchone()
    return str(row[0]) if row else None
