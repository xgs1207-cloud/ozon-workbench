"""独立的 Ozon Performance 只读授权、活动读取与可恢复报表。

凭据只保存为 Fernet 密文，Token 仅驻留内存。允许的网络请求为授权、
活动列表及统计导出，绝无创建活动、开关广告、预算或出价更新入口。
接口依据 2026-10-03 官方文档快照；实际账号权限仍须授权后验证。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import error, parse, request

BASE_URL = "https://api-performance.ozon.ru"
DOCUMENTATION = "https://docs.ozon.ru/api/performance/"
DOCUMENTATION_SNAPSHOT = "2026-10-03"
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_CSV_ROWS = 50_000
_SHOP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_ID = re.compile(r"[0-9]{1,20}\Z")
_TOKENS: dict[tuple[str, str, str], tuple[str, float]] = {}
_TOKEN_LOCK = threading.RLock()


class PerformanceAccessError(ValueError):
    """Only sanitized messages cross the API boundary; never upstream error bodies."""

    def __init__(self, message: str, *, http_status: int | None = None, code: str = "performance_error"):
        super().__init__(message)
        self.http_status = http_status
        self.code = code


@dataclass(frozen=True)
class WireResponse:
    data: bytes
    content_type: str = "application/json"


def _shop(value: str) -> str:
    if not isinstance(value, str) or not _SHOP.fullmatch(value):
        raise PerformanceAccessError("请选择有效的店铺 ID", code="invalid_shop")
    return value


def _report_id(value: str) -> str:
    value = str(value or "").lower()
    if not _UUID.fullmatch(value):
        raise PerformanceAccessError("广告报表 ID 无效", code="invalid_report")
    return value


def _credentials(client_id: str, client_secret: str) -> tuple[str, str]:
    client = str(client_id or "").strip()
    secret = str(client_secret or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._@+\-]{3,512}", client) or client.isdecimal():
        raise PerformanceAccessError("请填写 Performance 服务账号的 client_id，不是 Seller API 数字 Client-Id", code="invalid_credentials")
    if not 8 <= len(secret) <= 1024 or not re.fullmatch(r"[!-~]+", secret):
        raise PerformanceAccessError("请填写有效的 Performance client_secret，不能包含空格或换行", code="invalid_credentials")
    return client, secret


def _allowed_request(method: str, path: str, query: Mapping[str, Any] | None) -> None:
    """Whitelisting must precede token use; GET is not necessarily read-only in Ozon."""
    allowed = {
        ("POST", "/api/client/token"): set(),
        ("GET", "/api/client/campaign"): {"page", "pageSize", "campaignIds", "state"},
        ("POST", "/api/client/statistics"): set(),
        ("GET", "/api/client/statistics/report"): {"UUID"},
    }
    keys = allowed.get((method, path))
    if keys is None and method == "GET" and path.startswith("/api/client/statistics/"):
        identifier = path.removeprefix("/api/client/statistics/")
        if _UUID.fullmatch(identifier):
            keys = set()
    if keys is None or set(query or {}) - keys:
        raise PerformanceAccessError("只读广告连接禁止访问该接口", code="readonly_endpoint")
    if path == "/api/client/statistics/report":
        _report_id(str((query or {}).get("UUID", "")))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class ReadonlyPerformanceTransport:
    """Fixed HTTPS authority, no redirects, bounded bodies, sanitized exceptions."""

    def __init__(self, *, timeout: float = 20):
        self.timeout = max(1.0, min(float(timeout), 30.0))

    def request(self, method: str, path: str, *, body: Mapping[str, Any] | None = None,
                query: Mapping[str, Any] | None = None, token: str | None = None,
                max_bytes: int = MAX_DOWNLOAD_BYTES) -> WireResponse:
        _allowed_request(method, path, query)
        encoded_query = parse.urlencode(query or {}, doseq=True)
        url = BASE_URL + path + ("?" + encoded_query if encoded_query else "")
        endpoint = parse.urlsplit(url)
        if endpoint.scheme != "https" or endpoint.hostname != "api-performance.ozon.ru" or endpoint.username or endpoint.password:
            raise PerformanceAccessError("广告接口地址不安全", code="unsafe_host")
        headers = {"Accept": "application/json, text/csv, application/zip", "User-Agent": "OzonWorkbench/readonly-operations"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=self.timeout) as response:
                final = parse.urlsplit(response.geturl())
                if final.scheme != "https" or final.netloc != "api-performance.ozon.ru":
                    raise PerformanceAccessError("广告接口返回不安全跳转", code="unsafe_host")
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise PerformanceAccessError("广告报表超过安全大小限制，请减少活动或日期", code="report_too_large")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise PerformanceAccessError("广告报表超过安全大小限制，请减少活动或日期", code="report_too_large")
                return WireResponse(payload, response.headers.get("Content-Type", ""))
        except PerformanceAccessError:
            raise
        except error.HTTPError as failure:
            status = failure.code
            failure.close()
            if status in (401, 403):
                message = f"Performance 授权或读取权限不足（HTTP {status}），请检查服务账号"
            elif status == 429:
                message = "Performance 请求或报表额度受限（HTTP 429），请稍后重试"
            elif 300 <= status < 400:
                message = "广告接口返回跳转，已安全拒绝"
            else:
                message = f"Performance 读取失败（HTTP {status}），请稍后重试"
            raise PerformanceAccessError(message, http_status=status, code="upstream_http") from None
        except Exception:
            raise PerformanceAccessError("无法连接 Performance，请检查服务器网络后重试", code="network_error") from None


def _json(response: WireResponse) -> dict[str, Any]:
    try:
        value = json.loads(response.data.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise PerformanceAccessError("Performance 响应格式异常", code="invalid_response") from None
    if not isinstance(value, dict):
        raise PerformanceAccessError("Performance 未返回有效对象", code="invalid_response")
    return value


def _safe_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[已隐藏]")
        return value[:4096]
    if isinstance(value, list):
        return [_safe_value(item, secrets) for item in value[:100]]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def _numeric(value: Any) -> float | None:
    try:
        number = Decimal(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        if not number.is_finite() or number < 0:
            return None
        result = float(number)
        return result if result != float("inf") else None
    except (ValueError, InvalidOperation):
        return None


def parse_report(data: bytes, content_type: str, *, campaigns: list[str]) -> dict[str, Any]:
    """Keep exact report headings/units; don't guess spend or revenue aliases.

    No files are extracted to disk, CSV formulas are inert JSON strings. Only CTR
    is derived when the exact same row exposes both official Показы and Клики.
    Ambiguous money headings must be examined before ACOS/ROAS conversion.
    """
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise PerformanceAccessError("广告报表超过安全大小限制", code="report_too_large")
    files: list[tuple[str, bytes]] = []
    if data.startswith(b"PK") or "zip" in content_type.lower():
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if not 1 <= len(entries) <= 10 or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                    raise PerformanceAccessError("广告 ZIP 超出安全解压限制", code="unsafe_archive")
                if len({entry.filename for entry in entries}) != len(entries):
                    raise PerformanceAccessError("广告 ZIP 出现重复文件名", code="unsafe_archive")
                for entry in entries:
                    if entry.filename not in {f"{campaign}.csv" for campaign in campaigns} or entry.flag_bits & 1:
                        raise PerformanceAccessError("广告 ZIP 文件名不匹配所选活动", code="unsafe_archive")
                    if entry.file_size > MAX_EXPANDED_BYTES or (entry.compress_size and entry.file_size / entry.compress_size > 250):
                        raise PerformanceAccessError("广告 ZIP 压缩比例异常", code="unsafe_archive")
                    with archive.open(entry) as handle:
                        content = handle.read(min(entry.file_size, MAX_EXPANDED_BYTES) + 1)
                    if len(content) != entry.file_size:
                        raise PerformanceAccessError("广告 ZIP 内容长度异常", code="unsafe_archive")
                    files.append((entry.filename, content))
        except PerformanceAccessError:
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
            raise PerformanceAccessError("广告 ZIP 无法安全读取", code="unsafe_archive") from None
    else:
        if "text/html" in content_type.lower() or data.lstrip().startswith((b"<", b"{")):
            raise PerformanceAccessError("广告接口未返回 CSV 报表", code="invalid_report_content")
        files = [(f"{campaigns[0]}.csv" if len(campaigns) == 1 else "report.csv", data)]
    rows: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for filename, content in files:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError:
            raise PerformanceAccessError("广告 CSV 编码无法确认，请使用官方 UTF-8 报表", code="invalid_report_content") from None
        lines = text.splitlines()
        # The official CSV example has one preamble line before the header.
        start = next((index for index, line in enumerate(lines[:8])
                      if any(token in line.casefold() for token in ("sku;", "показы", "клики", "impressions", "clicks", "дата;", "date;"))), None)
        if start is None:
            raise PerformanceAccessError("广告 CSV 表头无法识别，未猜测字段含义", code="unknown_report_schema")
        sample = lines[start]
        delimiter = ";" if sample.count(";") >= sample.count("\t") and sample.count(";") >= sample.count(",") else ("\t" if "\t" in sample else ",")
        try:
            reader = csv.reader(io.StringIO("\n".join(lines[start:])), delimiter=delimiter)
            headings = [name.strip() for name in next(reader)]
            if len(headings) > 100 or len(set(headings)) != len(headings) or not all(headings):
                raise PerformanceAccessError("广告 CSV 表头重复或缺失，未合并不确定指标", code="unknown_report_schema")
            count = 0
            for cells in reader:
                if not cells or not any(cell.strip() for cell in cells):
                    continue
                if len(cells) != len(headings) or any(len(cell) > 4096 for cell in cells):
                    raise PerformanceAccessError("广告 CSV 行结构异常", code="invalid_report_content")
                if len(rows) >= MAX_CSV_ROWS:
                    raise PerformanceAccessError("广告报表行数过多，请缩小统计范围", code="report_too_large")
                raw = dict(zip(headings, (cell.strip() for cell in cells)))
                mapping = {key.casefold(): value for key, value in raw.items()}
                impressions = _numeric(mapping.get("показы", mapping.get("impressions")))
                clicks = _numeric(mapping.get("клики", mapping.get("clicks")))
                derived = {"ctr_percent": round(clicks / impressions * 100, 6) if impressions and clicks is not None else None}
                rows.append({"file": filename, "raw": raw, "derived": derived})
                count += 1
            metadata.append({"name": filename, "headings": headings, "row_count": count})
        except (csv.Error, StopIteration):
            raise PerformanceAccessError("广告 CSV 无法安全解析", code="invalid_report_content") from None
    return {"files": metadata, "rows": rows, "metric_scope": "advertising_attributed_report_not_incremental_sales",
            "units": "保留官方原始列名及单位；未确认币种和归因口径时不计算 ACOS/ROAS"}


class PerformanceAccess:
    def __init__(self, root: Path | str, *, transport: Any = None, clock: Callable[[], float] | None = None):
        self.root = Path(root)
        self.path = self.root / "performance-access.sqlite3"
        self.transport = transport or ReadonlyPerformanceTransport()
        self.clock = clock or time.time

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    def _db(self, *, create: bool = False) -> sqlite3.Connection | None:
        if not create and not self.path.exists():
            return None
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
        try:
            connection = sqlite3.connect(self.path, timeout=10) if create else sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            connection.row_factory = sqlite3.Row
            if create:
                os.chmod(self.path, 0o600)
                connection.executescript("""
                CREATE TABLE IF NOT EXISTS credentials (
                    shop TEXT PRIMARY KEY, generation TEXT NOT NULL, account TEXT NOT NULL,
                    ciphertext BLOB NOT NULL, checked_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY, shop TEXT NOT NULL, generation TEXT NOT NULL,
                    account TEXT NOT NULL, uuid TEXT UNIQUE, state TEXT NOT NULL,
                    request_json TEXT NOT NULL, requested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    result_json TEXT, last_polled REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS export_locks (account TEXT PRIMARY KEY, report_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reports_by_shop ON reports (shop, requested_at);
                """)
            return connection
        except (OSError, sqlite3.Error):
            raise PerformanceAccessError("广告授权数据库不可用，请检查服务器权限与备份", code="storage_error") from None

    def _cipher(self, *, create: bool):
        from cryptography.fernet import Fernet
        key_path = self.root / "master.key"
        try:
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
                os.chmod(self.root, 0o700)
                if not key_path.exists() and self.path.exists():
                    existing = self._db()
                    try:
                        if existing and existing.execute("SELECT 1 FROM credentials LIMIT 1").fetchone():
                            raise PerformanceAccessError("广告主密钥遗失，请恢复完整备份，未替换旧凭据", code="missing_key")
                    finally:
                        if existing:
                            existing.close()
                try:
                    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(Fernet.generate_key())
                        handle.flush()
                        os.fsync(handle.fileno())
            if not key_path.is_file() or key_path.stat().st_size > 128:
                raise PerformanceAccessError("广告主密钥缺失或损坏，请恢复完整备份", code="missing_key")
            if os.name != "nt" and key_path.stat().st_mode & 0o077:
                raise PerformanceAccessError("广告主密钥权限应为 0600", code="unsafe_key_permissions")
            return Fernet(key_path.read_bytes())
        except PerformanceAccessError:
            raise
        except Exception:
            raise PerformanceAccessError("广告授权主密钥不可用，请检查完整备份", code="invalid_key") from None

    def _loaded(self, shop: str) -> dict[str, Any]:
        shop = _shop(shop)
        connection = self._db()
        try:
            row = connection.execute("SELECT * FROM credentials WHERE shop=?", (shop,)).fetchone() if connection else None
        finally:
            if connection:
                connection.close()
        if row is None:
            raise PerformanceAccessError("该店铺尚未授权 Performance API，请在运营中心安全填写服务账号", code="not_authorized")
        try:
            value = json.loads(self._cipher(create=False).decrypt(row["ciphertext"]).decode("utf-8"))
            if value.get("shop") != shop or value.get("generation") != row["generation"]:
                raise ValueError("mismatch")
            client, secret = _credentials(value.get("client_id"), value.get("client_secret"))
            return {**dict(row), "client_id": client, "client_secret": secret}
        except PerformanceAccessError:
            raise
        except Exception:
            raise PerformanceAccessError("该店铺广告授权密文无法读取，请重新授权或恢复备份", code="invalid_ciphertext") from None

    def _call(self, method: str, path: str, *, body=None, query=None, token=None, max_bytes=MAX_DOWNLOAD_BYTES) -> WireResponse:
        _allowed_request(method, path, query)
        try:
            response = self.transport.request(method, path, body=body, query=query, token=token, max_bytes=max_bytes)
            if not isinstance(response, WireResponse) or len(response.data) > max_bytes:
                raise PerformanceAccessError("广告响应异常或过大", code="invalid_response")
            return response
        except PerformanceAccessError:
            raise
        except Exception as failure:
            # Do not echo arbitrary transport exceptions or chain secret-bearing context.
            status = getattr(failure, "http_status", getattr(failure, "status", None))
            status = status if isinstance(status, int) and 100 <= status <= 599 else None
            raise PerformanceAccessError(f"Performance 读取失败（HTTP {status}）" if status else "无法连接 Performance，请稍后重试", http_status=status, code="transport_error") from None

    def _get_token(self, credentials: dict[str, Any], *, force: bool = False) -> str:
        key = (str(self.root.resolve()), credentials["shop"], credentials["generation"])
        with _TOKEN_LOCK:
            cached = _TOKENS.get(key)
            if not force and cached and cached[1] > self.clock():
                return cached[0]
            response = _json(self._call("POST", "/api/client/token", body={
                "client_id": credentials["client_id"], "client_secret": credentials["client_secret"],
                "grant_type": "client_credentials"}, max_bytes=128 * 1024))
            token = response.get("access_token")
            expiry = _numeric(response.get("expires_in"))
            if not isinstance(token, str) or not 8 <= len(token) <= 16_384 or any(char.isspace() for char in token) or expiry is None or expiry <= 0 or expiry > 86400:
                raise PerformanceAccessError("Performance 未返回有效 Token 或有效期", code="invalid_token")
            if str(response.get("token_type", "Bearer")).casefold() != "bearer":
                raise PerformanceAccessError("Performance Token 类型不支持", code="invalid_token")
            _TOKENS[key] = (token, self.clock() + max(0, expiry - min(30, expiry / 10)))
            return token

    def _authenticated(self, credentials: dict[str, Any], method: str, path: str, *, body=None, query=None, max_bytes=MAX_DOWNLOAD_BYTES) -> WireResponse:
        token = self._get_token(credentials)
        try:
            return self._call(method, path, body=body, query=query, token=token, max_bytes=max_bytes)
        except PerformanceAccessError as failure:
            # 401 means rejected authentication, safe to retry readonly/export once.
            if failure.http_status != 401:
                raise
            token = self._get_token(credentials, force=True)
            return self._call(method, path, body=body, query=query, token=token, max_bytes=max_bytes)

    def public_status(self, shop: str) -> dict[str, Any]:
        shop = _shop(shop)
        try:
            credentials = self._loaded(shop)
            configured, checked_at, state = True, credentials["checked_at"], "connected"
        except PerformanceAccessError as failure:
            configured, checked_at, state = False, None, "not_configured" if failure.code == "not_authorized" else "credential_error"
        return {"shop": shop, "configured": configured, "connection_status": state, "checked_at": checked_at,
                "scope": "read_only", "ad_writes_enabled": False, "credential_storage": "encrypted_server_vault",
                "credentials_location": "Ozon 卖家后台 → 设置 → API 密钥 → Performance API → 服务账号",
                "documentation": DOCUMENTATION, "documentation_snapshot": DOCUMENTATION_SNAPSHOT}

    def authorize(self, shop: str, client_id: str, client_secret: str) -> dict[str, Any]:
        shop = _shop(shop)
        client, secret = _credentials(client_id, client_secret)
        if client in shop or secret in shop:
            raise PerformanceAccessError("店铺 ID 不得包含广告凭据", code="invalid_shop")
        generation = str(uuid.uuid4())
        credentials = {"shop": shop, "generation": generation, "client_id": client, "client_secret": secret}
        try:
            self._campaigns(credentials, page=1, page_size=1)
        except Exception:
            with _TOKEN_LOCK:
                _TOKENS.pop((str(self.root.resolve()), shop, generation), None)
            raise
        cipher = self._cipher(create=True)
        encrypted = cipher.encrypt(json.dumps(credentials, ensure_ascii=True).encode("utf-8"))
        # OAuth client IDs are opaque and may be case-sensitive. Normalizing
        # them could accidentally grant access to another account's report.
        account = hashlib.sha256(client.encode()).hexdigest()
        connection = self._db(create=True)
        try:
            with connection:
                connection.execute("INSERT INTO credentials(shop,generation,account,ciphertext,checked_at) VALUES(?,?,?,?,?) ON CONFLICT(shop) DO UPDATE SET generation=excluded.generation,account=excluded.account,ciphertext=excluded.ciphertext,checked_at=excluded.checked_at", (shop, generation, account, encrypted, self._now()))
        finally:
            connection.close()
        self._invalidate_tokens(shop, keep=generation)
        return self.public_status(shop)

    def _invalidate_tokens(self, shop: str, *, keep: str | None = None) -> None:
        with _TOKEN_LOCK:
            for key in list(_TOKENS):
                if key[0] == str(self.root.resolve()) and key[1] == shop and key[2] != keep:
                    del _TOKENS[key]

    def remove(self, shop: str) -> dict[str, Any]:
        shop = _shop(shop)
        connection = self._db(create=True)
        try:
            with connection:
                connection.execute("DELETE FROM credentials WHERE shop=?", (shop,))
        finally:
            connection.close()
        self._invalidate_tokens(shop)
        return self.public_status(shop)

    def _campaigns(self, credentials: dict[str, Any], *, page=1, page_size=50, campaign_ids=None, state=None) -> dict[str, Any]:
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10_000 or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise PerformanceAccessError("活动分页必须从第 1 页开始，每页 1–100 条", code="invalid_pagination")
        query: dict[str, Any] = {"page": page, "pageSize": page_size}
        if campaign_ids is not None:
            query["campaignIds"] = self._campaign_ids(campaign_ids)
        if state is not None:
            valid_states = {"UNKNOWN", "RUNNING", "PLANNED", "STOPPED", "INACTIVE", "ARCHIVED", "MODERATION_DRAFT", "MODERATION_IN_PROGRESS", "MODERATION_FAILED", "FINISHED"}
            if state not in {"CAMPAIGN_STATE_" + value for value in valid_states}:
                raise PerformanceAccessError("广告活动状态无效", code="invalid_campaign_state")
            query["state"] = state
        value = _json(self._authenticated(credentials, "GET", "/api/client/campaign", query=query, max_bytes=2 * 1024 * 1024))
        token = self._get_token(credentials)
        rows = value.get("list")
        if not isinstance(rows, list) or len(rows) > page_size or any(not isinstance(row, dict) for row in rows):
            raise PerformanceAccessError("Performance 活动列表格式异常", code="invalid_response")
        fields = {"id", "paymentType", "title", "state", "advObjectType", "fromDate", "toDate", "dailyBudget", "weeklyBudget", "budget", "placement", "productAutopilotStrategy", "productCampaignMode", "createdAt", "updatedAt"}
        secrets = (credentials["client_id"], credentials["client_secret"], token)
        result = [{key: _safe_value(item, secrets) for key, item in row.items() if key in fields} for row in rows]
        return {"items": result, "page": page, "page_size": page_size, "has_more": len(rows) == page_size,
                "source": "Performance API /api/client/campaign", "read_only": True,
                "budget_unit": "millionth_RUB", "budget_divisor": 1_000_000, "observed_at": self._now()}

    def list_campaigns(self, shop: str, **kwargs) -> dict[str, Any]:
        return self._campaigns(self._loaded(shop), **kwargs)

    @staticmethod
    def _campaign_ids(campaigns: Any) -> list[str]:
        if not isinstance(campaigns, (list, tuple)) or not 1 <= len(campaigns) <= 10:
            raise PerformanceAccessError("每次报表请选择 1–10 个广告活动", code="invalid_campaigns")
        result = [str(value) for value in campaigns]
        if any(not _ID.fullmatch(value) or int(value) < 1 for value in result) or len(set(result)) != len(result):
            raise PerformanceAccessError("活动 ID 必须为不重复的正整数", code="invalid_campaigns")
        return result

    def request_report(self, shop: str, campaigns: Any, date_from: str, date_to: str, *, group_by: str = "DATE") -> dict[str, Any]:
        credentials = self._loaded(shop)
        selected = self._campaign_ids(campaigns)
        try:
            beginning, ending = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if str(beginning) != date_from or str(ending) != date_to:
                raise ValueError("format")
        except (TypeError, ValueError):
            raise PerformanceAccessError("广告统计日期应为 YYYY-MM-DD", code="invalid_date") from None
        today = datetime.fromtimestamp(self.clock(), timezone.utc).date()
        if not 0 <= (ending - beginning).days <= 61 or ending > today:
            raise PerformanceAccessError("广告统计须为过去 1–62 天，不能查询未来日期", code="invalid_date_range")
        if group_by not in {"DATE", "NO_GROUP_BY"}:
            raise PerformanceAccessError("本版报表仅支持按日期或不分组", code="invalid_group_by")
        # Resolve campaign ownership before any statistics request. No arbitrary
        # supplied campaign may be silently added to the authorized report.
        visible = self._campaigns(credentials, page=1, page_size=10, campaign_ids=selected)
        if set(str(row.get("id")) for row in visible["items"]) != set(selected):
            raise PerformanceAccessError("部分活动不属于该广告账号或不可读取", code="campaign_not_owned")
        report_id = str(uuid.uuid4())
        payload = {"campaigns": selected, "dateFrom": date_from, "dateTo": date_to, "groupBy": group_by}
        connection = self._db(create=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT generation FROM credentials WHERE shop=?", (shop,)).fetchone()
            if not current or current["generation"] != credentials["generation"]:
                raise PerformanceAccessError("广告授权已更新，请重新请求报表", code="credentials_changed")
            lock = connection.execute("SELECT report_id FROM export_locks WHERE account=?", (credentials["account"],)).fetchone()
            if lock:
                raise PerformanceAccessError("该广告账号已有报表正在生成，请先刷新已有任务", code="export_busy")
            # Keep below the documented account maximum; Ozon's organization
            # and active-campaign quotas remain authoritative (HTTP 429).
            recent = connection.execute("SELECT request_json FROM reports WHERE account=? AND requested_at>=?", (credentials["account"], datetime.fromtimestamp(self.clock() - 86400, timezone.utc).isoformat().replace("+00:00", "Z"))).fetchall()
            if sum(len(json.loads(row["request_json"])["campaigns"]) for row in recent) + len(selected) > 2000:
                raise PerformanceAccessError("该账号 24 小时报表安全额度已用完", code="export_quota")
            now = self._now()
            connection.execute("INSERT INTO reports(report_id,shop,generation,account,state,request_json,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (report_id, shop, credentials["generation"], credentials["account"], "SUBMITTING", json.dumps(payload), now, now))
            connection.execute("INSERT INTO export_locks(account,report_id) VALUES(?,?)", (credentials["account"], report_id))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        try:
            response = _json(self._authenticated(credentials, "POST", "/api/client/statistics", body=payload, max_bytes=128 * 1024))
            identifier = _report_id(response.get("UUID", ""))
            self._set_report(report_id, state="NOT_STARTED", identifier=identifier)
        except PerformanceAccessError as failure:
            # A lost response may still have started the export. Do not submit
            # another report or release its account lock automatically.
            definitely_rejected = failure.http_status is not None and 400 <= failure.http_status < 500
            self._set_report(report_id, state="ERROR" if definitely_rejected else "SUBMISSION_UNCERTAIN", release=definitely_rejected)
            raise PerformanceAccessError("广告报表请求未完成；上游是否已接收未知，已保留任务防止重复导出" if not definitely_rejected else str(failure), http_status=failure.http_status, code="submission_uncertain" if not definitely_rejected else failure.code) from None
        return self._public_report(self._owned_report(shop, report_id, credentials))

    def _owned_report(self, shop: str, identifier: str, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
        credentials = credentials or self._loaded(shop)
        identifier = _report_id(identifier)
        connection = self._db()
        try:
            # Rotating a secret for the SAME account must not strand its active
            # export lock. Account changes still make old reports inaccessible.
            row = connection.execute("SELECT * FROM reports WHERE shop=? AND account=? AND (report_id=? OR uuid=?)", (shop, credentials["account"], identifier, identifier)).fetchone() if connection else None
        finally:
            if connection:
                connection.close()
        if not row:
            raise PerformanceAccessError("该报表不属于当前店铺授权", code="report_not_owned")
        return dict(row)

    @staticmethod
    def _public_report(row: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(row["request_json"])
        return {"report_id": row["report_id"], "uuid": row["uuid"], "shop": row["shop"], "state": row["state"],
                "campaigns": payload["campaigns"], "date_from": payload["dateFrom"], "date_to": payload["dateTo"], "group_by": payload["groupBy"],
                "requested_at": row["requested_at"], "updated_at": row["updated_at"], "poll_after_seconds": 5,
                "downloaded": row["result_json"] is not None, "read_only": True,
                "recovery_required": row["state"] in {"SUBMITTING", "SUBMISSION_UNCERTAIN"},
                "recovery_instructions": "提交响应遗失，可能已在 Ozon 开始生成。请让管理员核对 Ozon 报表记录；确认任务状态前不会重复导出，也不会自动解除账号锁。" if row["state"] in {"SUBMITTING", "SUBMISSION_UNCERTAIN"} else None,
                "error_message": "Ozon 报表生成失败，可以重新请求；未返回上游原始错误内容。" if row["state"] == "ERROR" else None}

    def list_reports(self, shop: str, *, limit: int = 30) -> dict[str, Any]:
        credentials = self._loaded(shop)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise PerformanceAccessError("报表列表数量须为 1–100", code="invalid_pagination")
        connection = self._db()
        try:
            rows = connection.execute("SELECT * FROM reports WHERE shop=? AND account=? ORDER BY requested_at DESC LIMIT ?", (shop, credentials["account"], limit)).fetchall() if connection else []
            return {"items": [self._public_report(dict(row)) for row in rows], "read_only": True}
        finally:
            if connection:
                connection.close()

    def _set_report(self, report_id: str, *, state: str, identifier: str | None = None, release: bool = False, result: dict[str, Any] | None = None, polled: bool = False) -> None:
        connection = self._db(create=True)
        try:
            with connection:
                connection.execute("UPDATE reports SET state=?,uuid=COALESCE(?,uuid),updated_at=?,result_json=COALESCE(?,result_json),last_polled=CASE WHEN ? THEN ? ELSE last_polled END WHERE report_id=?", (state, identifier, self._now(), json.dumps(result, ensure_ascii=False) if result is not None else None, polled, self.clock(), report_id))
                if release:
                    connection.execute("DELETE FROM export_locks WHERE report_id=?", (report_id,))
        finally:
            connection.close()

    def poll_report(self, shop: str, identifier: str) -> dict[str, Any]:
        credentials = self._loaded(shop)
        row = self._owned_report(shop, identifier, credentials)
        if row["state"] in {"OK", "ERROR", "SUBMISSION_UNCERTAIN", "SUBMITTING"} or self.clock() - row["last_polled"] < 5:
            return self._public_report(row)
        response = _json(self._authenticated(credentials, "GET", "/api/client/statistics/" + row["uuid"], max_bytes=256 * 1024))
        if response.get("UUID", row["uuid"]) != row["uuid"] or response.get("state") not in {"NOT_STARTED", "IN_PROGRESS", "ERROR", "OK"}:
            raise PerformanceAccessError("广告报表状态响应不匹配", code="invalid_report_state")
        # Ignore link/error/request strings from Ozon: no arbitrary URL and no
        # reflected upstream secrets can enter public output or persistence.
        state = response["state"]
        self._set_report(row["report_id"], state=state, release=state in {"OK", "ERROR"}, polled=True)
        return self._public_report(self._owned_report(shop, row["report_id"], credentials))

    def download_report(self, shop: str, identifier: str) -> dict[str, Any]:
        credentials = self._loaded(shop)
        row = self._owned_report(shop, identifier, credentials)
        if row["result_json"] is not None:
            return json.loads(row["result_json"])
        if row["state"] != "OK":
            raise PerformanceAccessError("广告报表尚未生成完成，请先刷新任务状态", code="report_not_ready")
        response = self._authenticated(credentials, "GET", "/api/client/statistics/report", query={"UUID": row["uuid"]})
        request_payload = json.loads(row["request_json"])
        # CSV is provider-controlled text too. Remove literal credential/token
        # reflection before any persistence, while preserving report headings.
        result = parse_report(response.data, response.content_type, campaigns=request_payload["campaigns"])
        token = self._get_token(credentials)
        secrets = (credentials["client_id"], credentials["client_secret"], token)
        for record in result["rows"]:
            record["raw"] = {_safe_value(key, secrets): _safe_value(value, secrets) for key, value in record["raw"].items()}
        for file in result["files"]:
            file["headings"] = [_safe_value(key, secrets) for key in file["headings"]]
        result.update({"report_id": row["report_id"], "shop": shop, "campaigns": request_payload["campaigns"],
                       "date_from": request_payload["dateFrom"], "date_to": request_payload["dateTo"],
                       "provenance": {"source": "Ozon Performance API", "endpoint": "/api/client/statistics", "downloaded_at": self._now(), "documentation_snapshot": DOCUMENTATION_SNAPSHOT},
                       "ad_writes_performed": False, "library_writes_performed": False})
        self._set_report(row["report_id"], state="OK", result=result, release=True)
        return result
