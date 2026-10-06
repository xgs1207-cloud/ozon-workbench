"""Performance API 真实 HTTP 传输层（urllib，可注入 ``urlopen`` 便于测试）。

实现 ``collector.search_phrases.PerformanceTransport`` 协议：

- ``token``：``POST /api/client/token``（client_id/client_secret，无 Bearer）；
- ``post_json``：带 Bearer 的 POST JSON；
- ``get_json``：带 Bearer 的 GET，返回解析对象；
- ``get_text``：带 Bearer 的 GET，返回原文（CSV）。

主机 ``https://api-performance.ozon.ru``；相对路径拼到主机后，完整 URL 直接请求。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from collector.search_phrases import PATH_TOKEN, SearchPhrasesError


class UrllibPerformanceTransport:
    def __init__(self, base_url: str, *, timeout: int = 60, urlopen: Any | None = None) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{'' if path.startswith('/') else '/'}{path}"

    def _request(
        self, path: str, *, method: str, bearer: str | None, body: Mapping[str, Any] | None
    ) -> bytes:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            self._url(path), data=data, headers=headers, method=method
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE007
                detail = ""
            raise SearchPhrasesError(
                f"Performance API 返回 HTTP {error.code}：{detail or error.reason}"
            ) from error
        except urllib.error.URLError as error:
            raise SearchPhrasesError(f"无法连接 Performance API：{error.reason}") from error

    def token(self, client_id: str, client_secret: str) -> dict[str, Any]:
        raw = self._request(
            PATH_TOKEN,
            method="POST",
            bearer=None,
            body={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
        )
        return json.loads(raw.decode("utf-8"))

    def post_json(
        self, path: str, body: Mapping[str, Any], bearer: str
    ) -> dict[str, Any]:
        raw = self._request(path, method="POST", bearer=bearer, body=body)
        return json.loads(raw.decode("utf-8"))

    def get_json(self, path: str, bearer: str) -> dict[str, Any]:
        raw = self._request(path, method="GET", bearer=bearer, body=None)
        return json.loads(raw.decode("utf-8"))

    def get_text(self, path: str, bearer: str) -> str:
        raw = self._request(path, method="GET", bearer=bearer, body=None)
        return raw.decode("utf-8")
