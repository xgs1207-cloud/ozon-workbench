"""Durable, inert collection jobs. Browser page lifetime ends at HTTP acceptance.

Captures contain URLs/metadata, never browser cookies or video file bytes. A
crashed running job is not blindly re-posted; only exact completed request IDs
are reconciled, otherwise the user sees an interrupted task.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from collector.ingest import (CaptureValidationError, DuplicateCaptureError, find_capture_request,
                              ingest_capture, normalize_payload, now_iso)
from pipeline.image_jobs import _pid_alive, _process_birth


class CaptureJobs:
    def __init__(self, products_root: Path, db_path: Path):
        self.products_root, self.db_path = Path(products_root), Path(db_path)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="1688-collect")
        self._initialize_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("""CREATE TABLE IF NOT EXISTS captures(
            request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL,
            payload TEXT, new_version INTEGER NOT NULL, state TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL, result TEXT, error TEXT,
            worker_pid INTEGER, worker_birth TEXT)""")
        if os.name != "nt":
            self.db_path.chmod(0o600)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self._initialize_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                # A worker restart is not proof that its previous writes failed.
                for row in connection.execute("SELECT request_id, worker_pid, worker_birth FROM captures WHERE state='running'").fetchall():
                    active = _pid_alive(row["worker_pid"]) and (
                        row["worker_birth"] is None or _process_birth(row["worker_pid"]) == row["worker_birth"])
                    if not active:
                        connection.execute("UPDATE captures SET state='interrupted', updated=?, error=? WHERE request_id=? AND state='running'",
                                           (now_iso(), "服务重启中断，先核对入库结果；不会自动重复创建商品", row["request_id"]))
                queued = [row[0] for row in connection.execute("SELECT request_id FROM captures WHERE state='queued'")]
            self._initialized = True
            for request_id in queued:
                self.pool.submit(self._run, request_id)

    @staticmethod
    def owner(device_id: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", device_id):
            raise CaptureValidationError("采集设备标识无效，请重新打开插件")
        return hashlib.sha256(device_id.encode("utf-8")).hexdigest()

    def enqueue(self, request_id: str, device_id: str, capture: dict[str, Any], allow_new_version=False):
        self.initialize()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", request_id):
            raise CaptureValidationError("采集请求标识无效")
        owner = self.owner(device_id)
        capture = {**capture, "capture_request_id": request_id, "collection_mode": "all_skus"}
        normalize_payload(capture)
        payload = json.dumps(capture, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 2_000_000:
            raise CaptureValidationError("采集数据超过2MB，请导出诊断反馈")
        digest = hashlib.sha256((str(bool(allow_new_version)) + payload).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM captures WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["owner"] != owner or prior["digest"] != digest:
                    raise CaptureValidationError("同一采集请求不能更换设备或商品数据")
                return self._public(prior)
            active = connection.execute("SELECT COUNT(*) FROM captures WHERE state IN ('queued','running')").fetchone()[0]
            if active >= 100:
                raise CaptureValidationError("采集队列已满，请等待已有商品完成")
            created = now_iso()
            connection.execute("INSERT INTO captures(request_id,owner,digest,payload,new_version,state,created,updated) VALUES(?,?,?,?,?,'queued',?,?)",
                               (request_id, owner, digest, payload, int(bool(allow_new_version)), created, created))
        self.pool.submit(self._run, request_id)
        return {"request_id": request_id, "state": "queued", "created_at": created,
                "message": "商品快照已提交后台，可关闭弹窗并采集其他商品"}

    def _run(self, request_id: str):
        with self._connect() as connection:
            changed = connection.execute("UPDATE captures SET state='running', updated=?, worker_pid=?, worker_birth=? WHERE request_id=? AND state='queued'",
                                         (now_iso(), os.getpid(), _process_birth(os.getpid()), request_id)).rowcount
            if not changed:
                return
            row = connection.execute("SELECT * FROM captures WHERE request_id=?", (request_id,)).fetchone()
        state, result, error = "failed", None, "采集未完成，请查看商品状态后重试"
        try:
            result = ingest_capture(self.products_root, json.loads(row["payload"]),
                                    allow_new_version=bool(row["new_version"]))
            state, error = "completed", None
        except DuplicateCaptureError as failure:
            state, result, error = "duplicate", failure.to_dict(), f"已有商品：{failure.existing_product_id}"
        except CaptureValidationError as failure:
            error = str(failure)
        except ValueError as failure:
            error = ("同一商品已有保存任务，请等待完成后再重新采集"
                     if "另一项商品保存" in str(failure) else "采集数据保存失败，请查看任务状态后重试")
        except Exception:
            # Exceptions may contain supplier signed URLs. Do not echo those
            # URLs or internal paths into the popup/job error.
            error = "保存失败，已停止本次任务；已采集的其他商品和上架任务不受影响"
        with self._connect() as connection:
            connection.execute("UPDATE captures SET state=?, result=?, error=?, payload=NULL, updated=? WHERE request_id=?",
                               (state, json.dumps(result, ensure_ascii=False) if result else None, error, now_iso(), request_id))

    @staticmethod
    def _public(row):
        return {"request_id": row["request_id"], "state": row["state"], "created_at": row["created"],
                "updated_at": row["updated"], "result": json.loads(row["result"]) if row["result"] else None,
                "error": row["error"]}

    def read(self, request_id: str, device_id: str):
        self.initialize()
        owner = self.owner(device_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM captures WHERE request_id=? AND owner=?", (request_id, owner)).fetchone()
            if not row:
                raise KeyError("采集任务不存在")
            if row["state"] == "interrupted":
                completed = find_capture_request(self.products_root, request_id)
                if completed:
                    connection.execute("UPDATE captures SET state='completed', result=?, error=NULL, payload=NULL, updated=? WHERE request_id=?",
                                       (json.dumps(completed, ensure_ascii=False), now_iso(), request_id))
                    row = connection.execute("SELECT * FROM captures WHERE request_id=?", (request_id,)).fetchone()
            return self._public(row)

    def close(self):
        self.pool.shutdown(wait=True)
