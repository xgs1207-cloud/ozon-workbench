"""RightAPI third-party image adapter (the user's URL-based drawing contract).

This is not an OpenAI endpoint and ``gpt-image-2.5`` is a RightAPI model alias.
Only the supplied ``images: [https_url]`` contract is implemented: the public
RightAPI documentation inspected on 2026-10-07 did not establish data URI support.
Reference URLs must belong to sealed capture metadata; local-only captures must
be recaptured with URLs, rather than silently becoming text-to-image requests.

Every charged POST is sent once, without redirects or automatic retries. Result
downloads carry no credentials, pin a validated public IP with TLS hostname
verification, and revalidate each redirect. Errors/reports never include keys,
provider bodies, reference URLs or signed result URLs. The requested 2k result is
converted into the existing 900 x 1200 PNG working-image format by default.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import time
import warnings
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from .base import ImageRequest, ModelError
from .doubao_image import normalize_to_qc_png

DEFAULT_BASE_URL = "https://www.rightapi.ai/draw/v1"
DEFAULT_MODEL = "gpt-image-2.5"
DEFAULT_SIZE = "2k"
DEFAULT_ASPECT_RATIO = "3:4"
DEFAULT_TIMEOUT = 180
MAX_REFERENCE_IMAGES = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 50_000_000
_SLOT = re.compile(r"^[\w.-]{1,100}$", re.UNICODE)


def _https_parts(url: str) -> Any:
    if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ModelError("RightAPI 图片地址必须为无凭据的公网 HTTPS 地址")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}
                or parsed.fragment or "\\" in url):
            raise ValueError
        parsed.hostname.encode("ascii")
    except (ValueError, UnicodeError):
        raise ModelError("RightAPI 图片地址必须为无凭据的公网 HTTPS 地址") from None
    return parsed


def _public_target(url: str, resolver: Any = None) -> tuple[str, str, str]:
    parsed = _https_parts(url)
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in
            (resolver or socket.getaddrinfo)(parsed.hostname, 443, type=socket.SOCK_STREAM)))
        def public_unicast(address: str) -> bool:
            ip = ipaddress.ip_address(address)
            return (ip.is_global and not ip.is_multicast and not ip.is_reserved
                    and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified)
        if not addresses or any(not public_unicast(address) for address in addresses):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ModelError("RightAPI 图片地址未通过公网 DNS 安全校验") from None
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return parsed.hostname, path, addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: int):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _read_bounded(response: Any, limit: int, timeout: int) -> bytes:
    length = response.getheader("Content-Length")
    if length and (not str(length).isdecimal() or int(length) > limit):
        raise ModelError("RightAPI 返回内容超过安全大小限制")
    chunks, size, deadline = [], 0, time.monotonic() + timeout
    read = getattr(response, "read1", response.read)
    while True:
        if time.monotonic() > deadline:
            raise ModelError("RightAPI 返回内容读取超时")
        chunk = read(min(256 * 1024, limit - size + 1))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ModelError("RightAPI 返回内容超过安全大小限制")
        chunks.append(chunk)


def _png_bytes(data: bytes, normalize: bool) -> bytes:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ModelError("RightAPI 图片为空或超过20MB限制")
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > MAX_PIXELS:
                    raise ValueError
                image.verify()
            if normalize:
                return normalize_to_qc_png(data)
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="PNG", optimize=True)
                return buffer.getvalue()
    except ImportError:
        raise ModelError("RightAPI 图片验证需要安装 Pillow") from None
    except Exception:
        raise ModelError("RightAPI 返回的内容不是可用图片，原工作图已保留") from None


class RightApiImageTransport:
    """A synchronous, single-POST transport with injectable offline connections."""

    name = "rightapi-images"

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT,
                 resolver: Any = None, connection_factory: Any = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or any(ord(c) < 33 or ord(c) == 127 for c in api_key.strip()):
            raise ModelError("缺少或无效的 RIGHTAPI_API_KEY")
        cleaned = str(base_url).strip().rstrip("/")
        parsed = _https_parts(cleaned)
        if parsed.query:
            raise ModelError("RIGHTAPI_BASE_URL 不能包含查询参数")
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
            raise ModelError("RIGHTAPI_IMAGE_MODEL 配置无效")
        if isinstance(timeout, bool) or not 1 <= int(timeout) <= 600:
            raise ModelError("RightAPI 超时必须为1–600秒")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = cleaned
        self.timeout = int(timeout)
        self._resolver = resolver
        self._connection_factory = connection_factory or _PinnedHTTPSConnection

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/images/generations"

    def validate_url(self, url: str) -> None:
        _public_target(url, self._resolver)

    def build_body(self, *, prompt: str, images: Sequence[str] = (),
                   size: str = DEFAULT_SIZE, aspect_ratio: str = DEFAULT_ASPECT_RATIO) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
            raise ModelError("RightAPI 提示词必须为1–20000字符")
        if size not in {"1k", "2k", "4k"} or aspect_ratio not in {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"}:
            raise ModelError("RightAPI 图片尺寸或宽高比配置无效")
        if isinstance(images, (str, bytes)) or len(images) > MAX_REFERENCE_IMAGES:
            raise ModelError("RightAPI 首版最多支持3张参考图，不会静默截断")
        for url in images:
            self.validate_url(url)
        return {"model": self.model, "prompt": prompt, "images": list(images),
                "aspect_ratio": aspect_ratio, "image_size": size, "response_format": "url"}

    @contextmanager
    def _connection(self, url: str):
        host, path, address = _public_target(url, self._resolver)
        connection = self._connection_factory(host, address, self.timeout)
        try:
            yield connection, path
        finally:
            connection.close()

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_JSON_BYTES:
            raise ModelError("RightAPI 请求内容超过安全大小限制")
        try:
            with self._connection(self.endpoint) as (connection, path):
                connection.request("POST", path, body=encoded, headers={
                    "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                    "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "OzonWorkbench/1.0"})
                response = connection.getresponse()
                try:
                    if response.status != 200:
                        raise ModelError(f"RightAPI 生图返回 HTTP {int(response.status)}；未自动重试，请先核对调用记录")
                    raw = _read_bounded(response, MAX_JSON_BYTES, self.timeout)
                finally:
                    response.close()
        except ModelError:
            raise
        except Exception:
            raise ModelError("RightAPI 生图连接失败，调用结果未知；未自动重试，请先核对调用记录") from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ModelError("RightAPI 生图返回无效 JSON；未自动重试") from None
        if not isinstance(payload, dict) or payload.get("error"):
            raise ModelError("RightAPI 生图返回错误或无效结果；请在供应商后台核对调用记录")
        return payload

    def _download(self, url: str) -> bytes:
        try:
            for redirect in range(4):
                with self._connection(url) as (connection, path):
                    connection.request("GET", path, headers={"Accept": "image/png,image/jpeg,image/webp",
                        "Accept-Encoding": "identity", "User-Agent": "OzonWorkbench/1.0"})
                    response = connection.getresponse()
                    try:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.getheader("Location")
                            if not location or redirect == 3:
                                raise ModelError("RightAPI 图片下载重定向过多或无效")
                            url = urljoin(url, location)
                            continue
                        if response.status != 200:
                            raise ModelError(f"RightAPI 图片下载返回 HTTP {int(response.status)}；未重复生图")
                        mime = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
                        if mime not in {"image/png", "image/jpeg", "image/webp", "application/octet-stream", ""}:
                            raise ModelError("RightAPI 图片下载返回不支持的内容类型")
                        return _read_bounded(response, MAX_IMAGE_BYTES, self.timeout)
                    finally:
                        response.close()
        except ModelError:
            raise
        except Exception:
            raise ModelError("RightAPI 图片下载失败；未重复生图，请先核对调用记录") from None
        raise ModelError("RightAPI 图片下载失败")

    def generate(self, *, prompt: str, images: Sequence[str] = (),
                 size: str = DEFAULT_SIZE, aspect_ratio: str = DEFAULT_ASPECT_RATIO) -> list[bytes]:
        payload = self._post(self.build_body(prompt=prompt, images=images, size=size, aspect_ratio=aspect_ratio))
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping) or not isinstance(data[0].get("url"), str):
            raise ModelError("RightAPI 未返回唯一图片 URL；不支持异步任务或额外图片，未重复生图")
        return [self._download(data[0]["url"])]


def _read_json(path: Path, limit: int = MAX_METADATA_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > limit:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ModelError("缺少或无效的图片计划/采集资料，请刷新或重新采集") from None


def _relative_file(root: Path, relative: Any, *, output: bool = False) -> Path:
    if not isinstance(relative, str) or "\\" in relative or any(ord(c) < 32 for c in relative):
        raise ModelError("图片路径无效")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ":" in relative or ".." in pure.parts or not pure.parts:
        raise ModelError("图片路径不能离开当前商品目录")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or (output and (not relative.startswith("output/generated-images/")
            or pure.suffix.lower() != ".png" or not path.is_relative_to(root / "output" / "generated-images"))):
        raise ModelError("图片输出必须位于当前商品 output/generated-images 内且为 PNG")
    return path


def _verify_sealed(root: Path, manifest: Mapping[str, Any], relative: str, *, limit: int = MAX_METADATA_BYTES) -> Path:
    path = _relative_file(root, relative)
    rows = [row for row in manifest.get("files", []) if isinstance(row, Mapping) and row.get("path") == relative]
    try:
        if len(rows) != 1 or not path.is_file() or path.stat().st_size > limit:
            raise ValueError
        if rows[0].get("bytes") != path.stat().st_size or hashlib.sha256(path.read_bytes()).hexdigest() != rows[0].get("sha256"):
            raise ValueError
    except (OSError, ValueError):
        raise ModelError("采集资料或参考图已改变，未通过原始采集校验；请重新采集") from None
    return path


class RightApiImageGenerator:
    name = "rightapi"
    produces_final_images = True

    def __init__(self, transport: RightApiImageTransport, *, size: str = DEFAULT_SIZE,
                 aspect_ratio: str = DEFAULT_ASPECT_RATIO, use_reference: bool = True,
                 max_reference_images: int = MAX_REFERENCE_IMAGES, normalize: bool = True,
                 slot_filter: Sequence[str] | None = None) -> None:
        if not 1 <= int(max_reference_images) <= MAX_REFERENCE_IMAGES:
            raise ModelError("RightAPI 首版参考图上限必须为1–3")
        self.transport, self.size, self.aspect_ratio = transport, size, aspect_ratio
        self.use_reference, self.normalize = use_reference, normalize
        self.max_reference_images = int(max_reference_images)
        self.slot_filter = set(slot_filter) if slot_filter else None

    def _planned_slots(self, root: Path) -> list[dict[str, Any]]:
        plan = _read_json(_relative_file(root, "output/image-plan.json"))
        slots = list(plan.get("main_images") or []) + list(plan.get("detail_images") or [])
        if not slots or len(slots) > 18 or any(not isinstance(row, Mapping) for row in slots):
            raise ModelError("image-plan.json 需要包含1–18个有效图位")
        names = [str(row.get("slot") or "") for row in slots]
        if len(set(names)) != len(names) or any(not _SLOT.fullmatch(name) or ".." in name for name in names):
            raise ModelError("图片计划图位名称无效或重复")
        if self.slot_filter:
            if not self.slot_filter.issubset(set(names)):
                raise ModelError("选择的生图图位不在当前图片计划中")
            slots = [row for row in slots if str(row["slot"]) in self.slot_filter]
        return [dict(row) for row in slots]

    def _reference_payload(self, root: Path, slot: Mapping[str, Any], source: Mapping[str, Any],
                           manifest: Mapping[str, Any], selected: set[str]) -> list[str]:
        if not self.use_reference:
            return []
        paths = slot.get("reference_product_images") or []
        if not isinstance(paths, list) or not paths or len(paths) > self.max_reference_images:
            raise ModelError("请为图位选择1–3张有原始采集 URL 的参考图；不会自动替换或截断")
        source_mappings = source.get("image_sources") or []
        if not isinstance(source_mappings, list) or not isinstance(source.get("skus"), list):
            raise ModelError("原始采集图片关联无效，请重新采集")
        mappings = [row for row in source_mappings if isinstance(row, Mapping)]
        for sku in source["skus"]:
            if isinstance(sku, Mapping) and sku.get("image_path") and (sku.get("image_url") or sku.get("variant_image_url")):
                mappings.append({"path": sku["image_path"], "url": sku.get("image_url") or sku.get("variant_image_url"),
                                 "role": "sku", "source_sku_id": str(sku.get("sku_id") or "")})
        payload, raw = [], None
        slot_sku = str(slot.get("source_sku_id") or "")
        for relative in paths:
            if not isinstance(relative, str) or relative not in (source.get("stored_images") or []):
                raise ModelError("参考图不是当前商品原始采集图片，请重新选择")
            pure = PurePosixPath(relative)
            if (pure.parent.parent != PurePosixPath("input") or pure.parent.name not in
                    {"main-images", "sku-images", "detail-images"} or pure.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}):
                raise ModelError("参考图必须来自当前商品原始采集图片目录")
            _verify_sealed(root, manifest, relative, limit=MAX_IMAGE_BYTES)
            matches = [row for row in mappings if row.get("path") == relative]
            allowed = [row for row in matches if not row.get("source_sku_id") or
                       (str(row["source_sku_id"]) in selected and (not slot_sku or str(row["source_sku_id"]) == slot_sku))]
            urls = {row.get("url") for row in allowed if isinstance(row.get("url"), str) and row.get("url")}
            if not matches:
                # Older sealed captures: filenames retain the index AFTER the
                # collector removes empty/unsupported array entries, but BEFORE
                # download failures or byte-dedup. Mirror normalize_payload's URL
                # filtering exactly; never zip against successful stored_images.
                pure = PurePosixPath(relative)
                role = {"main-images": "main_images", "detail-images": "detail_images"}.get(pure.parent.name)
                match = re.match(r"^(\d{3})-", pure.name)
                if role and match and pure.parent.parent == PurePosixPath("input"):
                    if raw is None:
                        _verify_sealed(root, manifest, "input/raw-snapshot.json")
                        raw = _read_json(root / "input" / "raw-snapshot.json").get("raw")
                    # normalize_payload ignores main_images/detail_images if an
                    # explicit images key is present (that is a local-import path).
                    # In that branch a similarly numbered local filename proves
                    # no relationship to raw URL arrays: reject instead of guessing.
                    entries = raw.get(role) if isinstance(raw, Mapping) and "images" not in raw else None
                    index = int(match.group(1)) - 1
                    normalized_urls = []
                    if isinstance(entries, list):
                        for entry in entries:
                            url = None
                            if isinstance(entry, Mapping):
                                url = entry.get("url") or entry.get("src")
                            elif isinstance(entry, str):
                                url = entry
                            if url:
                                normalized_urls.append(str(url))
                    if 0 <= index < len(normalized_urls):
                        urls.add(normalized_urls[index])
            if len(urls) != 1:
                raise ModelError("参考图缺少唯一已采集 URL 或属于未选规格；请重新采集，不会转为纯文生图")
            url = urls.pop()
            self.transport.validate_url(url)
            payload.append(url)
        return payload

    def generate(self, request: ImageRequest) -> dict[str, Any]:
        try:
            from PIL import Image  # noqa: F401 - paid-call prerequisite
        except ImportError:
            raise ModelError("RightAPI 图片验证需要安装 Pillow；尚未调用付费接口") from None
        root = Path(request.product_dir).resolve()
        manifest = _read_json(_relative_file(root, "input/source-manifest.json"))
        _verify_sealed(root, manifest, "input/source.json")
        source = _read_json(root / "input" / "source.json")
        from pipeline.sku_selection import MAX_SELECTED, selection_state
        state = selection_state(root)
        selected = set(state.get("selected") or [])
        if state.get("unknown_in_selection") or not 1 <= len(selected) <= MAX_SELECTED:
            raise ModelError(f"请先确认1–{MAX_SELECTED}个有效上架规格，再生图；更多规格请分批选择")
        prepared, targets = [], set()
        for slot in self._planned_slots(root):
            slot_sku = str(slot.get("source_sku_id") or "")
            if slot_sku and slot_sku not in selected:
                raise ModelError("图片计划包含未选上架规格，请重新生成计划")
            prompt = slot.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ModelError("所选图位缺少提示词，请先保存提示词")
            relative = str(slot.get("output_path") or f"output/generated-images/{slot['slot']}.png")
            target = _relative_file(root, relative, output=True)
            if target in targets:
                raise ModelError("图片计划包含重复输出路径")
            targets.add(target)
            refs = self._reference_payload(root, slot, source, manifest, selected)
            # Validate ALL selected slots before the first charged request.
            self.transport.build_body(prompt=prompt, images=refs, size=self.size, aspect_ratio=self.aspect_ratio)
            prepared.append((slot, relative, target, refs))
        generated, skipped = [], []
        for slot, relative, target, refs in prepared:
            try:
                images = self.transport.generate(prompt=slot["prompt"], images=refs, size=self.size, aspect_ratio=self.aspect_ratio)
                if not isinstance(images, list) or len(images) != 1:
                    raise ModelError("RightAPI 未返回唯一图片")
                data = _png_bytes(images[0], self.normalize)
                target.parent.mkdir(parents=True, exist_ok=True)
                target = _relative_file(root, relative, output=True)
                with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as file:
                    temporary = Path(file.name)
                    try:
                        file.write(data)
                        file.flush()
                        os.fsync(file.fileno())
                        file.close()
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                generated.append({"slot": slot["slot"], "path": relative, "bytes": len(data), "reference_images": len(refs)})
            except ModelError as error:
                skipped.append({"slot": slot["slot"], "reason": str(error)})
                # Any failed charged POST may already have consumed credit. Stop
                # this batch instead of charging remaining slots after a failure.
                break
            except Exception:
                skipped.append({"slot": slot["slot"], "reason": "RightAPI 图片保存失败，原工作图已保留"})
                break
        if not generated:
            reason = skipped[0]["reason"] if skipped else "没有可出图的图位"
            raise ModelError(f"RightAPI 生图失败：{reason}")
        return {"generator": self.name, "final_images": True, "generated": generated, "skipped": skipped,
                "model": self.transport.model, "note": "RightAPI 生图：请求 " + self.size + " / " + self.aspect_ratio
                + ("，工作图统一为900×1200 PNG；不自动重试" if self.normalize else "，工作图转为PNG；不自动重试")}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> "RightApiImageGenerator":
        source = os.environ if env is None else env
        transport = RightApiImageTransport(api_key=str(source.get("RIGHTAPI_API_KEY") or ""),
            model=str(source.get("RIGHTAPI_IMAGE_MODEL") or DEFAULT_MODEL),
            base_url=str(source.get("RIGHTAPI_BASE_URL") or DEFAULT_BASE_URL))
        options = {"size": str(source.get("RIGHTAPI_IMAGE_SIZE") or DEFAULT_SIZE),
                   "aspect_ratio": str(source.get("RIGHTAPI_ASPECT_RATIO") or DEFAULT_ASPECT_RATIO)}
        options.update(overrides)
        return cls(transport, **options)
