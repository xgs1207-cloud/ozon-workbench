"""Durable operations jobs; no model calls, publishing or advertisement writes."""
from __future__ import annotations

import os
import threading
import uuid
from typing import Callable


class OperationsWorker:
    def __init__(self, *, store: Callable, seller_transport: Callable, shop_enabled: Callable):
        self.store = store
        self.seller_transport = seller_transport
        self.shop_enabled = shop_enabled
        self.worker_id = f"operations-{os.getpid()}-{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.last_error = ""

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="operations-readonly-worker", daemon=True)
            self._thread.start()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def tick(self):
        from pipeline.operations import run_job
        store = self.store()
        store.enqueue_due()
        job = store.claim_job(self.worker_id, lease_seconds=300)
        if not job:
            return False
        try:
            if not self.shop_enabled(job["shop"]):
                store.fail_job(job["id"], self.worker_id, safe_error="shop_disabled", retryable=False)
                return True
            try:
                transport = self.seller_transport(job["shop"])
            except Exception:
                # Missing/decrypted credential failures need operator action,
                # not repeated network retries.
                store.fail_job(job["id"], self.worker_id, safe_error="no_credentials", retryable=False)
                return True
            # Service owns completion/defer/retry so a quota wait is not
            # incorrectly overwritten as a successful job by the executor.
            run_job(store, job, transport)
            self.last_error = ""
        except Exception:
            # Provider errors may embed requests/keys. Store only safe local text.
            store.fail_job(job["id"], self.worker_id,
                           safe_error="同步失败，请检查店铺授权或网络后重新同步", retryable=True)
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                active = self.tick()
            except Exception:
                # Durable storage errors are observable without dumping credentials.
                self.last_error = "运营任务暂不可用，请检查数据库目录权限"
                active = False
            self._wake.wait(timeout=1 if active else 5)
            self._wake.clear()
