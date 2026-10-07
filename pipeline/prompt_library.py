"""A small persistent prompt library; saving prompts never invokes a model."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid


def _connect(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS image_prompts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        prompt TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.commit()
    return connection


def _item(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in ("id", "name", "prompt", "created_at", "updated_at")}


def list_prompts(path: Path | str) -> list[dict]:
    with closing(_connect(path)) as connection:
        return [_item(row) for row in connection.execute(
            "SELECT * FROM image_prompts ORDER BY updated_at DESC, id")]


def save_prompt(path: Path | str, name: str, prompt: str) -> dict:
    name, prompt = name.strip(), prompt.strip()
    if not 1 <= len(name) <= 80 or not 1 <= len(prompt) <= 4000:
        raise ValueError("名称须为 1–80 字，提示词须为 1–4000 字")
    if any(ord(char) < 32 for char in name):
        raise ValueError("提示词名称不能包含控制字符")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(_connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute("SELECT * FROM image_prompts WHERE name_key=?", (name.casefold(),)).fetchone()
        if previous:
            identity = previous["id"]
            connection.execute("UPDATE image_prompts SET name=?, prompt=?, updated_at=? WHERE id=?",
                               (name, prompt, now, identity))
        else:
            if connection.execute("SELECT COUNT(*) FROM image_prompts").fetchone()[0] >= 5000:
                raise ValueError("提示词库已达 5000 条，请先删除不再使用的提示词")
            identity = uuid.uuid4().hex
            connection.execute("INSERT INTO image_prompts VALUES (?,?,?,?,?,?)",
                               (identity, name, name.casefold(), prompt, now, now))
        return _item(connection.execute("SELECT * FROM image_prompts WHERE id=?", (identity,)).fetchone())


def delete_prompt(path: Path | str, identity: str) -> bool:
    with closing(_connect(path)) as connection, connection:
        return connection.execute("DELETE FROM image_prompts WHERE id=?", (identity,)).rowcount > 0
