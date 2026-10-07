"""Serialize local editor writes across worker threads and processes."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


@contextmanager
def product_edit_lock(directory: Path):
    key = hashlib.sha256(str(Path(directory).resolve()).encode("utf-8")).hexdigest()
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=8):
        raise ValueError("另一项商品保存尚未完成，请稍后重试")
    handle = None
    locked = False
    try:
        root = Path(tempfile.gettempdir()) / "ozon-workbench-product-edit-locks"
        root.mkdir(exist_ok=True)
        handle = (root / f"{key}.lock").open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 8
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("另一项商品保存尚未完成，请稍后重试")
                time.sleep(.04)
        yield
    finally:
        if handle:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        lock.release()


def serialized_product_edit(function):
    @wraps(function)
    def wrapped(directory, *args, **kwargs):
        with product_edit_lock(directory):
            return function(directory, *args, **kwargs)
    return wrapped


@contextmanager
def product_file_transaction(directory: Path, relative_paths):
    """Roll back an editor's small input-file group after a normal write error."""
    directory = directory.resolve()
    paths = [(directory / name).resolve() for name in relative_paths]
    if any(not path.is_relative_to(directory) for path in paths):
        raise ValueError("事务文件必须位于当前商品目录")
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        yield
    except Exception:
        for path, value in before.items():
            if value is None:
                path.unlink(missing_ok=True)
                continue
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
                handle.write(value)
                temporary = handle.name
            os.replace(temporary, path)
        raise
