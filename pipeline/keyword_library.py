"""Independent, owner-scoped employee keyword records.

This database is deliberately unrelated to the Seerfar/research libraries.
HTTP callers cannot supply an owner: the API derives it from a signed,
HttpOnly browser cookie. This is browser-profile isolation, not staff login.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

IDENTITY_TTL = 365 * 86400
_SCHEMA = """
CREATE TABLE IF NOT EXISTS keyword_identity_settings (
 name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS employee_keyword_records (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, category TEXT NOT NULL,
 text TEXT NOT NULL, note TEXT NOT NULL, category_key TEXT NOT NULL,
 text_key TEXT NOT NULL, note_key TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(owner_id,category_key,text_key));
CREATE INDEX IF NOT EXISTS employee_keyword_owner_category
 ON employee_keyword_records(owner_id,category_key);
"""


class KeywordConflict(ValueError):
    pass


class KeywordNotFound(ValueError):
    pass


class InvalidIdentity(ValueError):
    pass


@contextmanager
def _connection(db_path: Path | str):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    if os.name != "nt":
        path.chmod(0o600)  # The durable cookie signing key is not public metadata.
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(_SCHEMA)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _secret(connection: sqlite3.Connection) -> bytes:
    # INSERT OR IGNORE keeps concurrent worker startup on the same durable key.
    connection.execute("INSERT OR IGNORE INTO keyword_identity_settings VALUES (?,?)",
                       ("cookie-signing-key-v1", secrets.token_hex(32)))
    row = connection.execute("SELECT value FROM keyword_identity_settings WHERE name=?",
                             ("cookie-signing-key-v1",)).fetchone()
    return bytes.fromhex(row["value"])


def browser_identity(db_path: Path | str, token: str | None, *, now: int | None = None) -> tuple[str, str | None]:
    """Return server-verified owner and a cookie only when creating a profile."""
    current = int(time.time()) if now is None else int(now)
    with _connection(db_path) as connection:
        key = _secret(connection)
    if token:
        if not re.fullmatch(r"[a-f0-9]{64}\.[0-9]{1,12}\.[a-f0-9]{64}", token):
            raise InvalidIdentity("关键词库身份无效，请清除本站身份 Cookie 后重新打开")
        owner, issued, signature = token.split(".")
        expected = hmac.new(key, f"{owner}.{issued}".encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature) or not 0 <= current - int(issued) <= IDENTITY_TTL:
            raise InvalidIdentity("关键词库身份无效或已过期，请清除本站身份 Cookie 后重新打开")
        return owner, None
    owner = secrets.token_hex(32)
    payload = f"{owner}.{current}"
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return owner, f"{payload}.{signature}"


def _owner(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", str(value)):
        raise ValueError("关键词库身份格式无效")
    return value


def _clean(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须为文本")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ValueError(f"{label}不能包含控制字符")
    clean = value.strip() if label == "备注" else " ".join(value.split())
    if (not empty and not clean) or len(clean) > maximum:
        raise ValueError(f"{label}需为{'0' if empty else '1'}–{maximum}个字符")
    return clean


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "category", "text", "note", "created_at", "updated_at")}


def _filters(owner: str, q: str, category: str) -> tuple[str, list[Any]]:
    clauses, values = ["owner_id=?"], [_owner(owner)]
    category = _clean(category, "类目", 200, empty=True)
    if category:
        clauses.append("category_key=?")
        values.append(category.casefold())
    q = _clean(q, "搜索词", 300, empty=True).casefold()
    if q:
        literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(text_key LIKE ? ESCAPE '\\' OR note_key LIKE ? ESCAPE '\\' OR category_key LIKE ? ESCAPE '\\')")
        values.extend([f"%{literal}%"] * 3)
    return " AND ".join(clauses), values


def list_categories(db_path: Path | str, owner: str) -> list[dict[str, Any]]:
    with _connection(db_path) as connection:
        rows = connection.execute("SELECT MIN(category) AS category,COUNT(*) AS count FROM employee_keyword_records "
                                  "WHERE owner_id=? GROUP BY category_key ORDER BY category_key", (_owner(owner),)).fetchall()
    return [dict(row) for row in rows]


def list_records(db_path: Path | str, owner: str, *, q: str = "", category: str = "",
                 offset: int = 0, limit: int = 50) -> dict[str, Any]:
    if isinstance(offset, bool) or isinstance(limit, bool) or not 0 <= offset <= 10000 or not 1 <= limit <= 200:
        raise ValueError("分页范围无效，limit为1–200，offset为0–10000")
    where, values = _filters(owner, q, category)
    with _connection(db_path) as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM employee_keyword_records WHERE {where}", values).fetchone()[0]
        rows = connection.execute(f"SELECT * FROM employee_keyword_records WHERE {where} "
                                  "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", [*values, limit, offset]).fetchall()
    categories = list_categories(db_path, owner)
    return {"items": [_record(row) for row in rows], "total": total, "offset": offset, "limit": limit,
            "categories": [row["category"] for row in categories],
            "category_counts": {row["category"]: row["count"] for row in categories}}


def get_record(db_path: Path | str, owner: str, record_id: str) -> dict[str, Any]:
    with _connection(db_path) as connection:
        row = connection.execute("SELECT * FROM employee_keyword_records WHERE id=? AND owner_id=?",
                                 (str(record_id), _owner(owner))).fetchone()
    if not row:
        raise KeywordNotFound("关键词不存在或不属于当前浏览器身份")
    return _record(row)


def save_record(db_path: Path | str, owner: str, *, category: str, text: str, note: str = "",
                record_id: str | None = None) -> dict[str, Any]:
    owner = _owner(owner)
    category, text, note = (_clean(category, "类目", 200), _clean(text, "关键词", 300),
                            _clean(note, "备注", 2000, empty=True))
    timestamp = datetime.now(timezone.utc).isoformat()
    saved_id = record_id or secrets.token_hex(16)
    try:
        with _connection(db_path) as connection:
            if record_id:
                row = connection.execute("SELECT id FROM employee_keyword_records WHERE id=? AND owner_id=?",
                                         (saved_id, owner)).fetchone()
                if not row:
                    raise KeywordNotFound("关键词不存在或不属于当前浏览器身份")
                connection.execute("UPDATE employee_keyword_records SET category=?,text=?,note=?,category_key=?,"
                                   "text_key=?,note_key=?,updated_at=? WHERE id=? AND owner_id=?",
                                   (category, text, note, category.casefold(), text.casefold(), note.casefold(), timestamp, saved_id, owner))
            else:
                connection.execute("INSERT INTO employee_keyword_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (saved_id, owner, category, text, note, category.casefold(), text.casefold(), note.casefold(), timestamp, timestamp))
    except sqlite3.IntegrityError as error:
        raise KeywordConflict("当前类目中已有相同关键词，请修改现有记录") from error
    return get_record(db_path, owner, saved_id)


def delete_record(db_path: Path | str, owner: str, record_id: str) -> None:
    with _connection(db_path) as connection:
        cursor = connection.execute("DELETE FROM employee_keyword_records WHERE id=? AND owner_id=?",
                                    (str(record_id), _owner(owner)))
        if not cursor.rowcount:
            raise KeywordNotFound("关键词不存在或不属于当前浏览器身份")
