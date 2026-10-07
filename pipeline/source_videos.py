"""Private, bounded source-video storage. No browser credentials or Ozon writes.

Capture only records metadata. Downloads/uploads are explicit user actions; changing
these assets never changes the sealed input/source.json or input/source-manifest.json.
Signed source URLs are private and are never returned by the public listing helper.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from typing import Any, BinaryIO, Mapping
from urllib.parse import urljoin, urlsplit
import uuid

from pipeline.product_edit_lock import product_edit_lock

MAX_VIDEO_BYTES = 100 * 1024 * 1024  # Workbench limit, not Ozon's platform limit.
MAX_PRODUCT_BYTES = 500 * 1024 * 1024
MAX_CAPTURE_VIDEOS = 30
MANIFEST_PATH = "runtime/source-video-manifest.json"
VIDEO_DIRECTORY = "runtime/source-videos"
_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_AUTH_QUERY = re.compile(r"(?:^|[&?])[^=&]*(?:token|sign|auth|credential|expires|key)[^=&]*=", re.I)
_MESSAGES = {
    "metadata_only": "视频地址已采集，尚未保存视频文件",
    "not_loaded": "页面尚未加载可读取的视频地址，请正常打开商品视频后重新采集",
    "unsupported_blob": "浏览器临时 blob 视频地址不能在服务器下载，请上传有使用权的原文件",
    "unsupported_stream": "HLS/DASH 流暂不支持；不会拼接分片或绕过保护",
    "protected_media": "受保护的视频不能自动保存，请使用有权使用的原文件",
    "unsupported_url": "视频地址不符合安全下载条件，可上传有使用权的 MP4/MOV 文件",
    "needs_refresh": "源地址已失效或要求登录，请正常刷新商品页面后重新采集，或上传原文件",
    "download_failed": "视频保存失败，商品及视频元数据已保留，可稍后重试或上传原文件",
    "too_large": "视频超过工作台文件大小或商品媒体总量上限",
    "invalid_media": "返回内容不是可识别的 MP4/MOV 视频文件，可能是登录页或错误页面",
    "downloading": "正在保存视频文件",
    "uploading": "正在保存上传视频",
    "stored": "原视频已私有保存；是否满足 Ozon 要求仍需上架前校验",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _cdn_host(host: str) -> bool:
    return host == "alicdn.com" or host.endswith(".alicdn.com") or host == "cloud.video.taobao.com"


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 12000 or any(ord(c) < 32 for c in text) or "\\" in text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    try:
        parsed = urlsplit(text)
        if parsed.scheme != "https" or not parsed.hostname or not _cdn_host(parsed.hostname.lower()):
            return None
        if parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
            return None
    except ValueError:
        return None
    return text


def normalize_videos(rows: Any, source_url: str, *, sku_ids: set[str] | None = None) -> dict[str, Any]:
    """Normalize actual page evidence; malformed video rows do not reject a product."""
    offer = re.search(r"/offer/(\d+)\.html", source_url)
    offer_id = offer.group(1) if offer else ""
    if not isinstance(rows, list):
        return {"videos": [], "warnings": ["视频元数据格式无效，商品资料已保留"] if rows else []}
    videos, warnings, seen = [], [], {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            warnings.append("已跳过无效的视频元数据行")
            continue
        if raw.get("offer_id") and str(raw["offer_id"]) != offer_id:
            warnings.append("已跳过不属于当前 1688 商品的视频")
            continue
        role = str(raw.get("role") or "product").strip().lower()
        if role in {"ad", "advertisement", "live", "livestream", "recommendation", "shop"} or raw.get("is_live") is True or raw.get("is_ad") is True:
            warnings.append("已跳过广告、直播或推荐商品视频")
            continue
        url = str(raw.get("source_url") or raw.get("url") or "").strip()
        if len(url) > 12000:
            warnings.append("视频地址长度异常，未保存该地址")
            url = ""
        provider_id = str(raw.get("provider_video_id") or raw.get("video_id") or "").strip()
        provider_id = provider_id if _ID.fullmatch(provider_id) else None
        try:
            parsed = urlsplit(url)
            identity = f"{parsed.hostname or ''}{parsed.path}"
        except ValueError:
            identity = url[:200]
        if not url and not provider_id:
            # Empty-player evidence is useful, but must not become 30 identical rows.
            identity = str(raw.get("source") or "unloaded_player")
        stable = hashlib.sha256(f"{offer_id}:{provider_id or identity}".encode("utf-8")).hexdigest()[:24]
        mime = str(raw.get("mime_type") or raw.get("type") or "")[:100]
        if raw.get("drm") is True or raw.get("protected") is True:
            status = "protected_media"
        elif not url:
            status = "not_loaded"
        elif url.startswith("blob:"):
            status = "unsupported_blob"
        elif re.search(r"\.(m3u8|mpd)(?:[?#]|$)", url, re.I) or re.search(r"mpegurl|dash\+xml", mime, re.I):
            status = "unsupported_stream"
        elif not _safe_url(url):
            status = "unsupported_url"
        else:
            status = "metadata_only"
        bindings = raw.get("sku_ids") if isinstance(raw.get("sku_ids"), list) else []
        bound = [str(item) for item in bindings if sku_ids is not None and str(item) in sku_ids]
        if len(bound) != len(bindings):
            warnings.append("视频包含无法确认的规格关联，已移除关联但保留商品视频")
        normalized = {
            "video_id": stable, "provider_video_id": provider_id, "offer_id": offer_id,
            "source_url": url or None, "poster_url": _safe_url(raw.get("poster_url") or raw.get("poster")),
            "role": role if role in {"main", "detail", "product"} else "product",
            "title": str(raw.get("title") or "商品视频")[:200],
            "source": str(raw.get("source") or "page_video")[:120],
            "network_source": "loaded_resource_timing_bound_to_product_player"
                if raw.get("network_source") == "loaded_resource_timing_bound_to_product_player" else None,
            "sku_ids": list(dict.fromkeys(bound)), "mime_type": mime or None,
            "sku_binding_unresolved": len(bound) != len(bindings),
            "duration_seconds": _finite(raw.get("duration_seconds")),
            "width": _finite(raw.get("width")), "height": _finite(raw.get("height")),
            "captured_at": str(raw.get("captured_at") or _now())[:64],
            "status": status, "message": _MESSAGES[status],
            "source_expires_at": None,  # Do not guess expiry from an opaque signature.
            "poster_is_ozon_video_cover": False,
        }
        if stable in seen:
            previous = videos[seen[stable]]
            # A later direct URL or DOM record must never erase an earlier
            # protected-player diagnostic for the same provider video.
            if status == "protected_media" or previous["status"] == "protected_media":
                previous["status"] = "protected_media"
                previous["message"] = _MESSAGES["protected_media"]
            elif status == "metadata_only" and previous["status"] != "metadata_only":
                videos[seen[stable]] = normalized
            elif not previous.get("poster_url") and normalized.get("poster_url"):
                previous["poster_url"] = normalized["poster_url"]
            continue
        seen[stable] = len(videos)
        videos.append(normalized)
    # A DOM URL can have no provider ID while the JSON record has one. Keep
    # their distinct identifiers/bindings, but never offer a direct-download
    # alias of an explicitly protected asset endpoint. Query signatures are
    # transient; only the same supported CDN host/path shares this restriction.
    def endpoint(row):
        address = _safe_url(row.get("source_url"))
        if not address:
            return None
        parsed = urlsplit(address)
        return parsed.hostname.lower(), parsed.path
    protected_endpoints = {endpoint(row) for row in videos if row["status"] == "protected_media"} - {None}
    for row in videos:
        if endpoint(row) in protected_endpoints:
            row["status"] = "protected_media"
            row["message"] = _MESSAGES["protected_media"]
    if len(videos) > MAX_CAPTURE_VIDEOS:
        warnings.append(f"页面视频超过 {MAX_CAPTURE_VIDEOS} 条，仅保存前 {MAX_CAPTURE_VIDEOS} 条；原始快照保留")
        videos = videos[:MAX_CAPTURE_VIDEOS]
    return {"videos": videos, "warnings": warnings}


def _path(directory: Path | str, relative: str) -> Path:
    root = Path(directory).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("视频文件路径必须位于当前商品目录")
    return path


def _write_manifest(directory: Path | str, data: Mapping[str, Any]) -> None:
    target = _path(directory, MANIFEST_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2)
    try:
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _load_manifest(directory: Path | str) -> dict[str, Any]:
    path = _path(directory, MANIFEST_PATH)
    if not path.exists():
        return {"schema_version": 1, "revision": 0, "videos": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list):
        raise ValueError("私有视频资料文件格式无效")
    return data


def initialize_video_manifest(directory: Path | str, videos: list[dict[str, Any]]) -> None:
    """Call after the source seal; existing transfer results must never be overwritten."""
    if not videos:
        return
    with product_edit_lock(Path(directory)):
        data = _load_manifest(directory)
        known = {row.get("video_id") for row in data["videos"]}
        data["videos"].extend(dict(row) for row in videos if row.get("video_id") not in known)
        data["revision"] = int(data.get("revision", 0)) + 1
        data["updated_at"] = _now()
        _write_manifest(directory, data)


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    source = row.get("source_url") or ""
    try:
        source_host = urlsplit(str(source)).hostname
    except ValueError:
        source_host = None
    poster = _safe_url(row.get("poster_url"))
    if poster and _AUTH_QUERY.search(poster):
        poster = None
    keys = ("video_id", "provider_video_id", "offer_id", "role", "title", "source", "network_source", "sku_ids",
            "mime_type", "duration_seconds", "width", "height", "captured_at", "status", "message",
            "sha256", "size_bytes", "media_verified", "stored_at", "transfer_source", "source_expires_at",
            "sku_binding_unresolved")
    result = {key: row.get(key) for key in keys if key in row}
    result.update({"source_host": source_host, "poster_url": poster,
                   "poster_is_ozon_video_cover": False, "has_file": bool(row.get("stored_path")),
                   "can_download": bool(_safe_url(source)) and not row.get("stored_path") and row.get("status") not in {
                       "unsupported_stream", "protected_media", "unsupported_blob", "downloading", "uploading"}})
    return result


def list_source_videos(directory: Path | str) -> dict[str, Any]:
    """No network, URL signatures, cookies, credentials or arbitrary local paths."""
    data = _load_manifest(directory)
    return {"videos": [_public_row(row) for row in data["videos"]], "revision": data.get("revision", 0),
            "max_video_bytes": MAX_VIDEO_BYTES, "max_product_bytes": MAX_PRODUCT_BYTES,
            "api_writes_performed": False}


def _row(data: Mapping[str, Any], video_id: str) -> dict[str, Any]:
    if not _ID.fullmatch(str(video_id)):
        raise ValueError("视频标识无效")
    for row in data["videos"]:
        if row.get("video_id") == video_id:
            return row
    raise ValueError("未找到当前商品的视频")


def video_file(directory: Path | str, video_id: str, *, verify_hash: bool = False) -> tuple[Path, str]:
    """Controlled preview: only a manifest-owned, content-addressed MP4/MOV file."""
    row = _row(_load_manifest(directory), video_id)
    sha = row.get("sha256")
    stored = row.get("stored_path")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or not isinstance(stored, str):
        raise ValueError("视频尚未保存")
    expected = {f"{VIDEO_DIRECTORY}/{sha}.mp4", f"{VIDEO_DIRECTORY}/{sha}.mov"}
    if stored not in expected:
        raise ValueError("视频文件索引无效")
    path = _path(directory, stored)
    if not path.is_file() or path.stat().st_size != row.get("size_bytes"):
        raise ValueError("视频文件不存在或已被改变")
    if verify_hash:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(256 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha:
            raise ValueError("视频文件校验失败，请重新保存视频")
    return path, "video/quicktime" if path.suffix == ".mov" else "video/mp4"


def validate_listing_video_url(value: Any, *, cover: bool = False,
                               directory: Path | str | None = None,
                               video_id: str | None = None, sha256: str | None = None,
                               publication: Mapping[str, Any] | None = None) -> str:
    """Stable sharing links or this workbench's verified ordinary-video COS URL.

    The official import method describes MP4/MOV links without a host whitelist.
    Generic object-storage URLs are nevertheless accepted only with an exact
    workbench publication record, never just because their suffix is .mp4.
    The directory-free compiler checks canonical proof structure; production
    preparation has already revalidated the product ledger and immutable bytes.
    This offline check does not claim live Ozon acceptance or cover support.
    """
    if cover:
        raise ValueError("首版暂不支持 Ozon 短视频封面；静态 poster 不能代替短视频封面")
    if not isinstance(value, str):
        raise ValueError("视频需要稳定的公开 HTTPS 分享链接")
    url = value.strip()
    if not url or len(url) > 2048 or re.search(r"[\x00-\x20\\]", url):
        raise ValueError("视频分享链接无效")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        if parsed.fragment or parsed.query:
            # Simple stable sharing links avoid expiring CDN signatures, private
            # credentials and ambiguous application/player routing parameters.
            raise ValueError
        host = (parsed.hostname or "").casefold()
        path = parsed.path
        vk_hosts = {"vk.com", "www.vk.com", "m.vk.com", "vkvideo.ru", "www.vkvideo.ru", "m.vkvideo.ru"}
        disk_hosts = {"disk.yandex.ru", "disk.yandex.com", "yadi.sk"}
        rutube_hosts = {"rutube.ru", "www.rutube.ru"}
        accepted = (
            host in vk_hosts and bool(re.fullmatch(r"/video-?\d+_\d+/?", path))
            or host in disk_hosts and bool(re.fullmatch(r"/(?:i|d)/[A-Za-z0-9_-]+/?", path))
            or host in rutube_hosts and bool(re.fullmatch(r"/(?:video|play/embed)/[0-9a-fA-F]{32}/?", path))
        )
        if not accepted:
            from .video_publish import publication_for, validate_cos_video_publication
            if directory is not None:
                publication = publication_for(directory, video_id, sha256, url, required=True)
            validate_cos_video_publication(url, publication)
    except ValueError:
        raise ValueError("仅支持稳定的视频平台分享链接或本工作台已核验发布的 COS 视频直链；1688临时源地址和任意外部视频域不能直接用于上架") from None
    return url


def validate_listing_videos(directory: Path | str, selection: Any, *,
                            require_url: bool = True, reprobe: bool = False) -> list[dict[str, Any]]:
    """Revalidate immutable local bytes and explicit bindings at save AND production.

    Returns safe compiler rows with measured metadata. User-supplied duration,
    resolution, size, format and SHA values are never trusted or copied through.
    Clearing videos is allowed without a rights checkbox. Covers are explicitly
    unsupported in this release, not silently dropped.
    """
    if not isinstance(selection, Mapping):
        raise ValueError("视频选择必须是对象")
    if selection.get("video_cover") is not None or selection.get("video_cover_id") is not None or selection.get("cover_video_id") is not None:
        raise ValueError("首版暂不支持 Ozon 短视频封面；静态 poster 不能代替短视频封面")
    selected = selection.get("videos", [])
    if not isinstance(selected, list) or len(selected) > 50:
        raise ValueError("视频选择必须为对象数组，每个规格最多5个视频")
    if not selected:
        return []
    if selection.get("rights_confirmed") is not True:
        raise ValueError("请先确认已取得视频内容、画面与音乐的使用权")
    root = Path(directory)
    try:
        source = json.loads(_path(root, "input/source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("未找到有效的商品采集资料") from None
    if not isinstance(source, dict) or not isinstance(source.get("skus"), list):
        raise ValueError("商品规格资料无效")
    from pipeline.sku_selection import active_skus, selection_state
    state = selection_state(root)
    if state.get("unknown_in_selection"):
        raise ValueError("上架规格选择包含未知规格，请先重新确认规格")
    active_ids = {str(row.get("sku_id")) for row in active_skus(root, source["skus"]) if row.get("sku_id")}
    if not 1 <= len(active_ids) <= 10:
        raise ValueError("请先选择1–10个上架规格，再关联视频")
    source_offer = re.search(r"/offer/(\d+)\.html", str(source.get("source_url") or ""))
    source_offer_id = source_offer.group(1) if source_offer else None
    manifest = _load_manifest(root)
    results, bindings, urls, counts = [], set(), {sku_id: set() for sku_id in active_ids}, {sku_id: 0 for sku_id in active_ids}
    verified = {}
    for item in selected:
        if not isinstance(item, Mapping):
            raise ValueError("每段视频必须包含已采集video_id、分享url及人工标题")
        video_id = item.get("video_id")
        if not isinstance(video_id, str):
            raise ValueError("视频标识无效")
        row = _row(manifest, video_id)
        if row.get("offer_id") and row.get("offer_id") != source_offer_id:
            raise ValueError("视频源不属于当前商品")
        target_sku = item.get("source_sku_id")
        if target_sku is not None and (not isinstance(target_sku, str) or target_sku not in active_ids):
            raise ValueError("视频关联规格必须是本次已选上架规格，不能关联未选或未知规格")
        key = (video_id, target_sku)
        if key in bindings:
            raise ValueError("同一视频及规格关联不能重复")
        bindings.add(key)
        targets = {target_sku} if target_sku else active_ids
        source_bindings = set(row.get("sku_ids") or [])
        if source_bindings and not targets.issubset(source_bindings):
            raise ValueError("所选视频的采集规格关联与上架规格不一致，不能当作公共视频")
        if row.get("sku_binding_unresolved") and target_sku is None:
            raise ValueError("视频原始规格关联不明确，请人工选择对应规格，不能直接用于全部规格")
        if row.get("status") != "stored" or not row.get("stored_path"):
            raise ValueError("所选视频尚未私有保存，请先保存源视频或上传有权使用的原文件")
        if not reprobe and row.get("media_verified") is not True:
            raise ValueError("视频真实时长和分辨率尚未验证；需本机ffprobe校验后才能用于上架")
        if item.get("sha256") is not None and item.get("sha256") != row.get("sha256"):
            raise ValueError("已选视频文件已被替换，请重新确认视频及其分享链接")
        if video_id not in verified:
            path, mime = video_file(root, video_id, verify_hash=True)
            if path.suffix not in {".mp4", ".mov"} or mime not in {"video/mp4", "video/quicktime"}:
                raise ValueError("普通商品视频必须为MP4或MOV")
            measured = _inspect_file(path) if reprobe else row
            if measured.get("media_verified") is not True:
                raise ValueError("视频真实时长和分辨率尚未验证；需本机ffprobe校验后才能用于上架")
            duration, width, height = (_finite(measured.get(field)) for field in ("duration_seconds", "width", "height"))
            if not duration or not 8 <= duration <= 300:
                raise ValueError("普通商品视频时长必须为8–300秒")
            if not width or not height or not 1080 <= max(width, height) <= 1920:
                raise ValueError("普通商品视频长边分辨率必须为1080–1920像素")
            size = row.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_VIDEO_BYTES:
                raise ValueError("视频文件超出工作台100MB上限")
            verified[video_id] = {"format": path.suffix.lstrip("."), "size_bytes": size,
                                  "duration_seconds": duration, "width": width, "height": height, "sha256": row["sha256"]}
        url = (validate_listing_video_url(item.get("url"), directory=root,
               video_id=video_id, sha256=verified[video_id]["sha256"]) if require_url else None)
        title = item.get("title")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200 or any(ord(c) < 32 for c in title):
            raise ValueError("请人工填写1–200字符的视频标题，不得含控制字符")
        for sku_id in targets:
            identity = url if require_url else verified[video_id]["sha256"]
            if identity in urls[sku_id]:
                raise ValueError("同一上架规格不能重复使用同一视频分享链接")
            urls[sku_id].add(identity)
            counts[sku_id] += 1
            if counts[sku_id] > 5:
                raise ValueError("每个上架规格最多5个视频（公共视频与独立视频合并计数）")
        result = {"video_id": video_id, "title": title.strip(), "source_sku_id": target_sku,
                  **verified[video_id]}
        if require_url:
            result["url"] = url
            from .video_publish import publication_for
            proof = publication_for(root, video_id, verified[video_id]["sha256"], url)
            if proof is not None:
                result["publication"] = proof
        results.append(result)
    return results


class _TransferError(Exception):
    def __init__(self, status: str):
        self.status = status


def _validated_target(url: str) -> tuple[str, str, list[str]]:
    safe = _safe_url(url)
    if not safe:
        raise _TransferError("unsupported_url")
    parsed = urlsplit(safe)
    host = parsed.hostname or ""
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise _TransferError("unsupported_url")
    except (socket.gaierror, ValueError):
        raise _TransferError("unsupported_url") from None
    return host, parsed.path + ("?" + parsed.query if parsed.query else ""), addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, 443, timeout=20, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


@contextmanager
def _open_video(url: str):
    """Pin validated public IPs, retain TLS hostname validation, validate every redirect."""
    for redirect in range(4):
        host, request_path, addresses = _validated_target(url)
        connection = _PinnedHTTPSConnection(host, addresses[0])
        response = None
        try:
            connection.request("GET", request_path, headers={"Accept": "video/mp4,video/quicktime",
                "Accept-Encoding": "identity", "User-Agent": "OzonWorkbench/1.0"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise _TransferError("download_failed")
                url = urljoin(url, location)
                continue
            if response.status in {401, 403, 410}:
                raise _TransferError("needs_refresh")
            if response.status != 200:
                raise _TransferError("download_failed")
            yield response
            return
        finally:
            if response:
                response.close()
            connection.close()
    raise _TransferError("download_failed")


def _container(head: bytes) -> tuple[str, str]:
    # ISO BMFF/QuickTime must start with a bounded, valid ftyp box, not HTML/JS.
    if len(head) < 16 or head[4:8] != b"ftyp":
        raise _TransferError("invalid_media")
    box_size = int.from_bytes(head[:4], "big")
    if box_size < 16 or box_size > 4096 or box_size > len(head):
        raise _TransferError("invalid_media")
    brands = [head[8:12]] + [head[index:index + 4] for index in range(16, box_size, 4)]
    if b"qt  " in brands:
        return ".mov", "video/quicktime"
    if not set(brands).intersection({b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash"}):
        raise _TransferError("invalid_media")
    return ".mp4", "video/mp4"


def _inspect_file(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        return {"media_verified": False}
    try:
        response = subprocess.run([executable, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration,format_name:stream=codec_type,width,height,duration",
            "-of", "json", str(path)], capture_output=True, timeout=15, check=True)
        data = json.loads(response.stdout)
        streams = [row for row in data.get("streams", []) if row.get("codec_type") == "video"]
        if not streams:
            return {"media_verified": False}
        stream = streams[0]
        duration = _finite(data.get("format", {}).get("duration")) or _finite(stream.get("duration"))
        return {"media_verified": bool(duration and _finite(stream.get("width")) and _finite(stream.get("height"))),
                "duration_seconds": duration, "width": _finite(stream.get("width")), "height": _finite(stream.get("height"))}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {"media_verified": False}


def _save_stream(directory: Path | str, stream: BinaryIO, max_bytes: int) -> dict[str, Any]:
    limit = min(int(max_bytes), MAX_VIDEO_BYTES)
    if limit <= 0:
        raise ValueError("视频大小上限无效")
    folder = _path(directory, VIDEO_DIRECTORY)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    digest, size, head = hashlib.sha256(), 0, b""
    deadline = time.monotonic() + 120
    # HTTPResponse.read(n) may wait for all n bytes while a slow sender keeps the
    # socket alive. read1(n) returns after one bounded read so our total deadline works.
    read_chunk = getattr(stream, "read1", stream.read)
    with tempfile.NamedTemporaryFile("wb", dir=folder, delete=False) as file:
        temporary = Path(file.name)
        try:
            while True:
                if time.monotonic() > deadline:
                    raise _TransferError("download_failed")
                chunk = read_chunk(min(256 * 1024, limit - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise _TransferError("too_large")
                if len(head) < 4096:
                    head += chunk[:4096 - len(head)]
                digest.update(chunk)
                file.write(chunk)
            suffix, mime = _container(head)
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
    sha = digest.hexdigest()
    target = _path(directory, f"{VIDEO_DIRECTORY}/{sha}{suffix}")
    try:
        with product_edit_lock(Path(directory)):
            # Physical-file quota and dedup, including files not yet attached to a manifest.
            used = sum(path.stat().st_size for path in folder.iterdir()
                       if path.is_file() and _SHA.fullmatch(path.stem) and path.suffix in {".mp4", ".mov"})
            if not target.exists() and used + size > MAX_PRODUCT_BYTES:
                raise _TransferError("too_large")
            if target.exists():
                if target.stat().st_size != size:
                    raise _TransferError("invalid_media")
                with target.open("rb") as existing:
                    check = hashlib.sha256()
                    for chunk in iter(lambda: existing.read(256 * 1024), b""):
                        check.update(chunk)
                if check.hexdigest() != sha:
                    raise _TransferError("invalid_media")
            else:
                temporary.chmod(0o600)
                os.replace(temporary, target)
        return {"sha256": sha, "size_bytes": size, "stored_path": f"{VIDEO_DIRECTORY}/{sha}{suffix}",
                "mime_type": mime, "stored_at": _now(), **_inspect_file(target)}
    finally:
        temporary.unlink(missing_ok=True)


def _finish(directory: Path | str, video_id: str, update: Mapping[str, Any], job_id: str | None = None) -> dict[str, Any]:
    with product_edit_lock(Path(directory)):
        data = _load_manifest(directory)
        row = _row(data, video_id)
        if job_id and row.get("transfer_job_id") != job_id:
            raise ValueError("视频任务已被替换，请刷新资料")
        row.update(update)
        row.pop("transfer_job_id", None)
        row.pop("transfer_started_at", None)
        data["revision"] = int(data.get("revision", 0)) + 1
        data["updated_at"] = _now()
        _write_manifest(directory, data)
        return _public_row(row)


def download_source_video(directory: Path | str, video_id: str, *, max_bytes: int = MAX_VIDEO_BYTES) -> dict[str, Any]:
    """Explicit action only. Never accepts an arbitrary URL or forwards login cookies."""
    with product_edit_lock(Path(directory)):
        data = _load_manifest(directory)
        row = _row(data, video_id)
        if row.get("stored_path"):
            video_file(directory, video_id)
            return _public_row(row)
        if row.get("status") in {"downloading", "uploading"}:
            started = _finite(row.get("transfer_started_at"))
            if started and time.time() - started < 180:
                raise ValueError("视频正在保存，请稍后刷新")
        if row.get("status") in {"protected_media", "unsupported_stream", "unsupported_blob", "not_loaded"}:
            return _public_row(row)
        source_url = row.get("source_url") or ""
        job_id = uuid.uuid4().hex
        row.update({"status": "downloading", "message": _MESSAGES["downloading"],
                    "transfer_job_id": job_id, "transfer_started_at": time.time()})
        _write_manifest(directory, data)
    try:
        with _open_video(source_url) as response:
            length = response.getheader("Content-Length")
            if length and (not length.isdecimal() or int(length) > min(max_bytes, MAX_VIDEO_BYTES)):
                raise _TransferError("too_large")
            content_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if content_type not in {"video/mp4", "video/quicktime", "application/octet-stream", "binary/octet-stream", ""}:
                raise _TransferError("invalid_media")
            result = _save_stream(directory, response, max_bytes)
        return _finish(directory, video_id, {**result, "status": "stored", "message": _MESSAGES["stored"],
                                            "transfer_source": "source_download"}, job_id)
    except _TransferError as error:
        return _finish(directory, video_id, {"status": error.status, "message": _MESSAGES[error.status]}, job_id)
    except (OSError, http.client.HTTPException, ValueError):
        return _finish(directory, video_id, {"status": "download_failed", "message": _MESSAGES["download_failed"]}, job_id)


def store_uploaded_video(directory: Path | str, stream: BinaryIO, filename: str, *,
                         video_id: str | None = None, max_bytes: int = MAX_VIDEO_BYTES) -> dict[str, Any]:
    """Store an authorized original file. Filenames never determine storage paths."""
    # Verify ownership and concurrency before reading a potentially large upload.
    job_id, previous = None, None
    with product_edit_lock(Path(directory)):
        data = _load_manifest(directory)
        if video_id:
            existing = _row(data, video_id)
            if existing.get("status") in {"downloading", "uploading"}:
                started = _finite(existing.get("transfer_started_at"))
                if started and time.time() - started < 180:
                    raise ValueError("视频正在保存，请稍后上传")
            previous = dict(existing)
            job_id = uuid.uuid4().hex
            existing.update({"status": "uploading", "message": _MESSAGES["uploading"],
                             "transfer_job_id": job_id, "transfer_started_at": time.time()})
            _write_manifest(directory, data)
        elif len(data["videos"]) >= MAX_CAPTURE_VIDEOS:
            raise ValueError("当前商品的视频数量已达到工作台上限")
    try:
        stored = _save_stream(directory, stream, max_bytes)
    except Exception as error:
        if job_id:
            with product_edit_lock(Path(directory)):
                data = _load_manifest(directory)
                row = _row(data, video_id)
                if row.get("transfer_job_id") == job_id:
                    row.clear()
                    row.update(previous)
                    _write_manifest(directory, data)
        if isinstance(error, _TransferError):
            raise ValueError(_MESSAGES[error.status]) from None
        raise ValueError("视频上传失败，商品资料及原视频已保留") from None
    if job_id:
        return _finish(directory, video_id, {**stored, "status": "stored", "message": _MESSAGES["stored"],
                                            "transfer_source": "manual_upload"}, job_id)
    with product_edit_lock(Path(directory)):
        data = _load_manifest(directory)
        if not video_id:
            video_id = "upload-" + stored["sha256"][:24]
            if not any(row.get("video_id") == video_id for row in data["videos"]):
                if len(data["videos"]) >= MAX_CAPTURE_VIDEOS:
                    raise ValueError("当前商品的视频数量已达到工作台上限")
                title = str(filename).replace("\\", "/").rsplit("/", 1)[-1][:200]
                data["videos"].append({"video_id": video_id, "source": "manual_upload", "role": "product",
                                       "title": title or "上传视频", "source_url": None, "sku_ids": [], "captured_at": _now()})
        row = _row(data, video_id)
        if row.get("status") == "downloading":
            raise ValueError("视频正在保存，请稍后上传")
        row.update({**stored, "status": "stored", "message": _MESSAGES["stored"], "transfer_source": "manual_upload"})
        data["revision"] = int(data.get("revision", 0)) + 1
        data["updated_at"] = _now()
        _write_manifest(directory, data)
        return _public_row(row)
