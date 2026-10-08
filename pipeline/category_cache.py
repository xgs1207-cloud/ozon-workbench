"""Durable SQLite Ozon metadata cache with backwards-compatible JSON mirrors.

Only authenticated metadata goes here; no Seller API keys are stored. Entries
retain their shop/category/type/language key supplied by existing consumers.
JSON files remain readable for old workers and audit tools. An external JSON
edit is respected (not masked by a newer database lookup).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ozon_metadata_cache (
 cache_key TEXT PRIMARY KEY, shop_id TEXT NOT NULL, scope TEXT NOT NULL,
 payload TEXT NOT NULL, stored_at REAL NOT NULL, mirror_mtime_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ozon_metadata_cache_shop ON ozon_metadata_cache(shop_id);
CREATE TABLE IF NOT EXISTS ozon_metadata_cache_generations (
 shop_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
"""


def database_path(cache_root: Path | str) -> Path:
    return Path(cache_root) / "ozon-category-cache.sqlite3"


class CacheIdentityChanged(ValueError):
    pass


@contextmanager
def _open(root: Path, *, create: bool = False):
    path = database_path(root)
    if not create and not path.is_file():
        yield None
        return
    if create:
        root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
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


def _put(connection: sqlite3.Connection, target: Path, value: Mapping[str, Any], *, stored_at: float, mtime: int) -> None:
    connection.execute("INSERT INTO ozon_metadata_cache VALUES (?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                       "shop_id=excluded.shop_id,scope=excluded.scope,payload=excluded.payload,"
                       "stored_at=excluded.stored_at,mirror_mtime_ns=excluded.mirror_mtime_ns",
                       (target.name, str(value.get("shop_id") or ""), str(value.get("scope") or ""),
                        json.dumps(dict(value), ensure_ascii=False, allow_nan=False), stored_at, mtime))


def _generation(connection: sqlite3.Connection | None, shop_id: str) -> int:
    row = connection.execute("SELECT generation FROM ozon_metadata_cache_generations WHERE shop_id=?", (shop_id,)).fetchone() if connection else None
    return int(row["generation"]) if row else 0


def shop_generation(cache_root: Path | str, shop_id: str) -> int:
    with _open(Path(cache_root)) as connection:
        return _generation(connection, str(shop_id))


def _valid_generation(root: Path, value: Mapping[str, Any]) -> bool:
    shop_id = str(value.get("shop_id") or "")
    return not shop_id or int(value.get("cache_generation") or 0) == shop_generation(root, shop_id)


def read_cache(target: Path | str, *, ttl: float | None = 86400) -> dict[str, Any] | None:
    target = Path(target)
    with _LOCK:
        with _open(target.parent) as connection:
            row = connection.execute("SELECT * FROM ozon_metadata_cache WHERE cache_key=?", (target.name,)).fetchone() if connection else None
        try:
            stat = target.stat()
        except OSError:
            stat = None
        # JSON modified by legacy workers/tools must not bypass receipt expiry.
        if stat is not None and (row is None or stat.st_mtime_ns != row["mirror_mtime_ns"]):
            if ttl is not None and time.time() - stat.st_mtime >= ttl:
                return None
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not _valid_generation(target.parent, value):
                    return None
            except (OSError, ValueError):
                return None
            with _open(target.parent, create=True) as connection:
                _put(connection, target, value, stored_at=stat.st_mtime, mtime=stat.st_mtime_ns)
            return value
        if not row or (ttl is not None and time.time() - row["stored_at"] >= ttl):
            return None
        try:
            value = json.loads(row["payload"])
            return value if isinstance(value, dict) and _valid_generation(target.parent, value) else None
        except (TypeError, ValueError):
            return None


def write_cache(target: Path | str, value: Mapping[str, Any]) -> None:
    target = Path(target)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _open(target.parent, create=True) as connection:
            # Serialize credential invalidation against writes across workers.
            connection.execute("BEGIN IMMEDIATE")
            shop_id = str(value.get("shop_id") or "")
            if shop_id and int(value.get("cache_generation") or 0) != _generation(connection, shop_id):
                raise CacheIdentityChanged("读取期间店铺授权已变更，请重新读取类目或字典")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
                    temporary = handle.name
                    json.dump(dict(value), handle, ensure_ascii=False, allow_nan=False)
                os.replace(temporary, target)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            stat = target.stat()
            _put(connection, target, value, stored_at=stat.st_mtime, mtime=stat.st_mtime_ns)


def invalidate_shop(cache_root: Path | str, shop_id: str) -> None:
    """Invalidate both stores, including legacy receipt files lacking shop_id."""
    root, shop_id = Path(cache_root), str(shop_id)
    with _LOCK:
        keys, scopes = set(), set()
        with _open(root, create=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO ozon_metadata_cache_generations VALUES (?,1) ON CONFLICT(shop_id) "
                               "DO UPDATE SET generation=generation+1", (shop_id,))
            rows = connection.execute("SELECT cache_key,scope FROM ozon_metadata_cache WHERE shop_id=?", (shop_id,)).fetchall()
            keys.update(row["cache_key"] for row in rows)
            scopes.update(row["scope"] for row in rows if row["scope"])
            # Discover any pre-SQLite entries before selecting their receipts.
            for target in root.glob("ozon-*.json"):
                if not target.name.startswith(("ozon-category-tree-", "ozon-category-form-", "ozon-dictionary-page-")):
                    continue
                try:
                    value = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(value, dict) and value.get("shop_id") == shop_id:
                    keys.add(target.name)
                    if value.get("scope"):
                        scopes.add(str(value["scope"]))
            for target in root.glob("ozon-dictionary-receipts-*.json"):
                try:
                    value = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(value, dict) and (value.get("shop_id") == shop_id or value.get("scope") in scopes):
                    keys.add(target.name)
            for scope in scopes:
                rows = connection.execute("SELECT cache_key FROM ozon_metadata_cache WHERE scope=?", (scope,)).fetchall()
                keys.update(row["cache_key"] for row in rows)
            if keys:
                connection.executemany("DELETE FROM ozon_metadata_cache WHERE cache_key=?", [(key,) for key in keys])
            for key in keys:
                # Keys are generated single file names, never arbitrary caller paths.
                if Path(key).name == key and key.startswith("ozon-") and key.endswith(".json"):
                    (root / key).unlink(missing_ok=True)
