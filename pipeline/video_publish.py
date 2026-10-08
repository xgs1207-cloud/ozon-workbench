"""Explicit source-video publication to verified, immutable COS ordinary-video URLs.

This service never imports a product into Ozon. The official import method
documents MP4/MOV links, but its acceptance/moderation of a particular COS object
is not claimed until an explicitly authorized real import is observed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from .listing_form import read_json, write_json
from .product_edit_lock import serialized_product_edit

PUBLICATIONS_FILE = "runtime/video-publications.json"
SELECTION_FILE = "input/listing-media.json"
PUBLICATION_FIELDS = (
    "storage", "product_id", "video_id", "url", "key", "sha256", "size_bytes",
    "content_type", "remote_verified", "anonymous_verified",
)
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(directory: Path | str, relative: str) -> Path:
    root = Path(directory).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("视频发布资料必须位于当前商品目录")
    return path


def validate_cos_video_publication(url: str, publication: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate canonical compiler proof, not an arbitrary URL's claimed identity.

    Directory-level production validation MUST obtain this proof from the local
    publication ledger and rehash the actual source video. A compiler has no
    product directory, so it can only check this canonical record's structure.
    """
    if not isinstance(publication, Mapping) or not isinstance(url, str):
        raise ValueError("COS 视频链接缺少本工作台发布凭据")
    proof = {key: publication.get(key) for key in PUBLICATION_FIELDS}
    digest, video_id, product_id, size = (proof[key] for key in
                                         ("sha256", "video_id", "product_id", "size_bytes"))
    if (proof["storage"] != "tencent-cos" or proof["remote_verified"] is not True
            or proof["anonymous_verified"] is not True or proof["url"] != url
            or not isinstance(digest, str) or not _SHA.fullmatch(digest)
            or not isinstance(video_id, str) or not _ID.fullmatch(video_id)
            or not isinstance(product_id, str) or not _ID.fullmatch(product_id)
            or isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 100 * 1024 * 1024):
        raise ValueError("COS 视频发布凭据无效或未完成 SHA/公开访问核验")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or len(url) > 2048
                or "\\" in url or any(ord(c) < 33 or ord(c) == 127 for c in url)):
            raise ValueError
        host = parsed.hostname.casefold()
        host.encode("ascii")
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError
        if host == "alicdn.com" or host.endswith(".alicdn.com") or host == "cloud.video.taobao.com":
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or address.is_multicast or address.is_reserved):
            raise ValueError
        key = proof["key"]
        if (not isinstance(key, str) or not key or len(key) > 1600 or "\\" in key
                or any(ord(c) < 33 or ord(c) == 127 for c in key)):
            raise ValueError
        key_path = PurePosixPath(key)
        public_path = unquote(parsed.path)
        if (key_path.is_absolute() or ".." in key_path.parts or ":" in key
                or ".." in PurePosixPath(public_path).parts or "\\" in public_path
                or not public_path.endswith("/" + key) or len(key_path.parts) < 3
                or key_path.parts[-3:-1] != (product_id, "videos")):
            raise ValueError
        suffix = key_path.suffix
        expected_type = {".mp4": "video/mp4", ".mov": "video/quicktime"}.get(suffix)
        if (not expected_type or proof["content_type"] != expected_type
                or key_path.name != f"{video_id}-{digest}{suffix}"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("COS 视频必须是本工作台 SHA 内容寻址的稳定 HTTPS MP4/MOV 直链") from None
    return proof


def publication_for(directory: Path | str, video_id: str | None, sha256: str | None,
                    url: str, *, required: bool = False) -> dict[str, Any] | None:
    """Verify an exact ledger record against owned local or RAM-upload metadata."""
    root = Path(directory).resolve()
    data = read_json(_path(root, PUBLICATIONS_FILE))
    entries = data.get("entries")
    matches = [row for row in entries if isinstance(row, Mapping)
               and row.get("video_id") == video_id and row.get("sha256") == sha256
               and row.get("url") == url] if isinstance(entries, list) else []
    if not matches and not required:
        return None
    if len(matches) != 1 or data.get("product_id") != root.name:
        raise ValueError("视频直链未在当前商品的工作台发布台账中找到")
    entry = matches[0]
    if entry.get("status") not in {"uploaded", "unchanged"} or entry.get("rights_confirmed") is not True:
        raise ValueError("视频发布未成功核验或未确认使用权")
    proof = validate_cos_video_publication(url, entry)
    if proof["product_id"] != root.name:
        raise ValueError("视频发布链接属于其他商品")
    from .source_videos import _load_manifest, _row, video_file
    owned = _row(_load_manifest(root), str(video_id))
    if owned.get("stored_path"):
        path, mime = video_file(root, str(video_id), verify_hash=True)
        if (path.stem != sha256 or path.stat().st_size != proof["size_bytes"] or mime != proof["content_type"]):
            raise ValueError("视频发布链接与当前源文件 SHA/大小/媒体类型不一致")
    elif (entry.get("source_mode") != "source_memory" or owned.get("status") != "published"
          or owned.get("local_persistence") is not False or owned.get("media_verified") is not True
          or owned.get("published_url") != url or owned.get("sha256") != sha256
          or owned.get("size_bytes") != proof["size_bytes"] or owned.get("mime_type") != proof["content_type"]):
        raise ValueError("视频发布链接与本商品源视频内存校验台账不一致")
    if not owned.get("stored_path") and any(owned.get(key) != entry.get(key)
            for key in ("duration_seconds", "width", "height", "video_format")):
        raise ValueError("对象存储视频的技术元信息与本商品发布台账不一致")
    return proof


@serialized_product_edit
def publish_source_videos(directory: Path | str, choices: Sequence[Mapping[str, Any]],
                          rights_confirmed: bool, storage: Any = None) -> dict[str, Any]:
    """Structural gates, bounded memory transfer, recoverable ledger, atomic selection.

    ``choices`` contains video_id/title/source_sku_id only. Public URLs are
    generated by COS, never supplied by the caller. Supplier bytes are not
    persisted locally. Injection is for offline
    tests; normal execution builds the official configured COS SDK adapter.
    """
    root = Path(directory).resolve()
    if not isinstance(choices, (list, tuple)) or not choices or len(choices) > 50:
        raise ValueError("请先选择要发布的已保存视频，每个上架规格最多5段")
    if rights_confirmed is not True:
        raise ValueError("请先确认视频画面、内容及音乐的使用权，再公开发布")
    if any(not isinstance(row, Mapping) or row.get("url") or row.get("publication") for row in choices):
        raise ValueError("发布视频只接收已采集视频标识及标题，不接受外部直链或自填发布凭据")
    from .source_videos import (
        _load_manifest, _row, _write_manifest, source_video_snapshot, validate_listing_videos, video_file,
    )
    # Selected-SKU/offer/rights/title/count gates run before resolving storage.
    # Legacy files are reprobed here; each remote snapshot is measured before PUT.
    prepared = validate_listing_videos(root, {"rights_confirmed": True, "videos": list(choices)},
                                      require_url=False, reprobe=True, allow_remote_pending=True)
    if storage is None:
        from .oss_cos import _storage_from_env
        try:
            storage = _storage_from_env()
        except Exception:
            raise ValueError("视频发布需要完整的腾讯云 COS 配置；采集原地址及元信息仍保留") from None
    if getattr(storage, "name", None) != "tencent-cos" or getattr(storage, "dry_run", False):
        raise ValueError("正式视频发布必须使用腾讯云 COS，不能用占位或 dry-run 存储")
    manifest = _load_manifest(root)
    unique = {row["video_id"]: row for row in prepared}
    for video_id, row in unique.items():
        owned = _row(manifest, video_id)
        # Reprobe can promote a download saved before ffprobe was installed, but
        # only private measured metadata is refreshed; source evidence is sealed.
        if not row.get("remote_pending"):
            owned.update({key: row[key] for key in ("duration_seconds", "width", "height")})
            owned.update(media_verified=True, media_verified_at=_now())
    _write_manifest(root, manifest)
    ledger_path = _path(root, PUBLICATIONS_FILE)
    ledger = read_json(ledger_path)
    if ledger_path.exists() and not ledger:
        raise ValueError("已有视频发布台账无法读取，已停止以保留原始资料")
    if ledger and (ledger.get("product_id") != root.name or not isinstance(ledger.get("entries"), list)):
        raise ValueError("视频发布台账无效，已停止以避免覆盖其他商品资料")
    if not ledger:
        ledger = {"schema_version": 1, "product_id": root.name, "entries": []}
    published, measured_by_video = {}, {}
    for video_id, row in unique.items():
        owned = _row(manifest, video_id)
        # Source file is rechecked after preflight and the storage adapter hashes
        # its exact PUT byte snapshot again, closing the ordinary replacement race.
        try:
            if owned.get("stored_path"):
                video_file(root, video_id, verify_hash=True)
                measured_by_video[video_id] = row
                result = storage.publish_video(root, {**row, "stored_path": owned["stored_path"]})
                source_mode = "legacy_local"
            elif owned.get("status") == "published":
                previous = next((item for item in ledger["entries"] if isinstance(item, Mapping)
                                 and item.get("video_id") == video_id and item.get("sha256") == owned.get("sha256")
                                 and item.get("url") == owned.get("published_url")), None)
                publication_for(root, video_id, owned.get("sha256"), owned.get("published_url"), required=True)
                result = storage.verify_video_publication(previous)
                measured_by_video[video_id] = row
                source_mode = "source_memory"
            else:
                with source_video_snapshot(root, video_id) as snapshot:
                    measured = {**row, **{key: value for key, value in snapshot.items() if key != "body"}}
                    measured.pop("remote_pending", None)
                    if row.get("sha256") and row["sha256"] != measured["sha256"]:
                        raise ValueError("源视频内容已变化，请重新采集或校验")
                    result = storage.publish_video_bytes(root, measured, snapshot["body"])
                    measured_by_video[video_id] = measured
                del snapshot  # Do not retain the previous video's bytes during the next transfer.
                source_mode = "source_memory"
            if not isinstance(result, Mapping) or result.get("status") not in {"uploaded", "unchanged"}:
                raise ValueError
            entry = {**result, "storage": "tencent-cos", "product_id": root.name,
                     "rights_confirmed": True, "verified_at": _now(), "source_mode": source_mode}
            measured = measured_by_video[video_id]
            entry.update({key: measured[key] for key in ("duration_seconds", "width", "height")})
            entry["video_format"] = measured["format"]
            proof = validate_cos_video_publication(str(result.get("url") or ""), entry)
            if (proof["video_id"] != video_id or proof["sha256"] != measured["sha256"]
                    or proof["size_bytes"] != measured["size_bytes"]):
                raise ValueError
            ledger["entries"] = [item for item in ledger["entries"] if not (
                isinstance(item, Mapping) and item.get("video_id") == video_id
                and item.get("sha256") == measured["sha256"])]
            ledger["entries"].append(entry)
            ledger["updated_at"] = _now()
            write_json(ledger_path, ledger)
            if source_mode == "source_memory":
                owned.update({key: measured[key] for key in ("sha256", "size_bytes", "duration_seconds", "width", "height")})
                owned.update(status="published", local_persistence=False, media_verified=True,
                             media_verified_at=_now(), video_format=measured["format"],
                             mime_type=proof["content_type"], published_url=proof["url"],
                             transfer_source="source_memory_to_cos",
                             message="原视频已上传对象存储并校验；本地仅保存地址与元信息")
                _write_manifest(root, manifest)
            published[video_id] = entry
        except Exception:
            raise ValueError("视频读取、校验或对象存储发布失败；已核验对象及采集资料保留，可重试，原上架视频选择未被覆盖") from None
    selection = {"rights_confirmed": True, "videos": [
        {**row, **{key: measured_by_video[row["video_id"]][key] for key in
                  ("format", "size_bytes", "duration_seconds", "width", "height", "sha256")},
         "url": published[row["video_id"]]["url"]} for row in prepared
    ]}
    # Revalidate using the saved exact publication ledger, not the adapter's assertion.
    selection["videos"] = validate_listing_videos(root, selection)
    write_json(_path(root, SELECTION_FILE), selection)
    uploaded = sum(row["status"] == "uploaded" for row in published.values())
    reused = len(published) - uploaded
    return {"ok": True, "selection": selection, "videos": selection["videos"],
            "published": uploaded, "uploaded": uploaded, "reused": reused, "unchanged": reused,
            "api_writes_performed": False, "storage_writes_performed": bool(uploaded),
            "ozon_video_acceptance": "not_live_verified",
            "video_stage": "storage_verified_not_submitted",
            "readback_required_after_submission": True,
            "buyer_playback_verified": False,
            "warnings": ["COS 直链已完成 SHA、元数据与匿名访问核验；Ozon 的实际视频导入及审核尚未实测"]}
