"""Editable rich content, independent of the old CLI and image-set gates.

Buyer text is plain text. Images are server-owned immutable local snapshots;
the browser cannot nominate arbitrary remote URLs or filesystem paths. AI
returns a reviewable candidate and never changes the saved draft by itself.
Official widget definitions checked 2026-10-08:
https://cdn2.ozone.ru/s3/artifact/schema.json (RichContentV02).
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import io
import json
from functools import lru_cache
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit
import uuid

from .listing_form import read_json, write_json, _require_editable
from .product_edit_lock import product_edit_lock

DRAFT_FILE = "input/rich-content.json"
ASSETS_FILE = "input/rich-content-assets.json"
CANDIDATES_FILE = "output/rich-content-candidates.json"
PUBLIC_FILE = "output/rich-content-publication.json"
IMAGE_DIRECTORY = "input/rich-content-images"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_BLOCKS = 60
_AI_GUARD = threading.Lock()
_ACTIVE_AI: set[str] = set()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_IMAGE_ID = re.compile(r"^img-[0-9a-f]{64}$")
_UNSAFE_TEXT = re.compile(
    r"(?:https?|ftp|sftp|file|mailto|tel|javascript|data):|www\.|<\s*/?\s*[a-zA-Z]|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\b(?:[a-z0-9][a-z0-9-]*\.)+(?:com|cn|ru|net|org|io|ai|su|рф)(?:\b|/)|"
    r"(?<!\w)\+\d[\d ()-]{6,}\d|"
    r"(?:тел(?:ефон)?|phone|whatsapp|微信|电话)\s*[:：]?\s*[+()\d][\d ()-]{6,}\d", re.I)


class RichContentConflict(ValueError):
    """Saved draft/product context changed since the editor loaded it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def context_fingerprint(directory: Path | str) -> str:
    root = Path(directory)
    # Refresh/default compilers update audit timestamps and provenance without
    # changing facts. Such writes must not invalidate an approved rich draft.
    audit_keys = {"source_refs", "provenance", "warnings", "cache_hit", "cache_ttl_seconds",
                  "category_form_scope", "category_form_confirmed_at", "attribute_provenance",
                  "source", "collection_id", "version", "schema_version", "input_fingerprint"}
    def semantic(value):
        if isinstance(value, Mapping):
            return {key: semantic(child) for key, child in value.items()
                    if key not in audit_keys and not str(key).endswith("_at")}
        if isinstance(value, list):
            return [semantic(child) for child in value]
        return value
    documents = {name: read_json(root / name) for name in (
        "input/source.json", "input/selected-skus.json", "input/category-selection.json",
        "input/category-form.json", "input/selected-keywords.json", "input/human-confirmations.json",
        "input/workbench-sku-overrides.json", "output/product-analysis.json", "output/copy-ru.json")}
    # The other legacy rich control cannot make this editor stale or become a
    # second owner of its value. This editor's own revision protects text edits.
    human = documents["input/human-confirmations.json"]
    for key in ("attributes", "common_attributes"):
        if isinstance(human.get(key), Mapping):
            human[key] = {k: v for k, v in human[key].items() if str(k) != "11254"}
    return _hash(semantic(documents))


def attribute_id(directory: Path | str) -> int | None:
    """Use only a rich-content field present in the selected official form."""
    form = read_json(Path(directory) / "input/category-form.json")
    selection = read_json(Path(directory) / "input/category-selection.json")
    if (form.get("source") != "ozon_seller_api" or not selection
            or any(form.get(key) != selection.get(key) for key in ("shop_id", "category_id", "type_id"))):
        return None
    for row in form.get("fields") or []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or row.get("attribute_name") or "").casefold()
        identity = row.get("attribute_id")
        if isinstance(identity, int) and (identity == 11254 or
                any(term in name for term in ("rich content", "rich-контент", "rich контент", "富内容", "丰富内容"))):
            if not row.get("complex_id") and not row.get("attribute_complex_id"):
                return identity
    return None


def _image_bytes(path: Path) -> tuple[bytes, str, int, int]:
    if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("图片不存在或超过 10 MB")
    return _check_image(path.read_bytes())


def _check_image(body: bytes) -> tuple[bytes, str, int, int]:
    from PIL import Image
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片必须不超过 10 MB")
    try:
        with Image.open(io.BytesIO(body)) as image:
            width, height = image.size
            fmt = image.format
            if fmt not in {"PNG", "JPEG", "WEBP"} or width * height > MAX_PIXELS or min(width, height) < 1:
                raise ValueError
            image.verify()
        with Image.open(io.BytesIO(body)) as image:
            if getattr(image, "is_animated", False):
                raise ValueError
            image.load()
    except Exception:
        raise ValueError("请导入完整的 PNG、JPEG 或 WebP 静态图片（最多 1600 万像素）") from None
    return body, {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[fmt], width, height


def _record(root: Path, relative: str, *, kind: str, label: str) -> dict[str, Any] | None:
    path = (root / relative).resolve()
    allowed = [root / "input" / name for name in ("main-images", "sku-images", "detail-images", "rich-content-images")]
    allowed.append(root / "output/generated-images")
    if not any(path.is_relative_to(folder.resolve()) for folder in allowed):
        return None
    try:
        body, suffix, width, height = _image_bytes(path)
    except (ValueError, OSError):
        return None
    digest = hashlib.sha256(body).hexdigest()
    identity = "img-" + digest
    return {"id": identity, "kind": kind, "label": label[:160], "relative_path": path.relative_to(root).as_posix(),
            "preview_url": f"/api/workbench/products/{quote(root.name, safe='')}/rich-content/images/{identity}",
            "sha256": digest, "size_bytes": len(body), "width": width, "height": height, "suffix": suffix}


def media_library(directory: Path | str) -> list[dict[str, Any]]:
    root = Path(directory).resolve()
    records: dict[str, dict[str, Any]] = {}
    broken_assets: set[str] = set()
    # Frozen imported/selected assets take precedence over mutable source files.
    for row in read_json(root / ASSETS_FILE).get("images") or []:
        if not isinstance(row, Mapping):
            continue
        item = _record(root, str(row.get("relative_path") or ""), kind=str(row.get("kind") or "imported"),
                       label=str(row.get("label") or "导入图片"))
        if item and item["id"] == row.get("id") and item["sha256"] == row.get("sha256"):
            records[item["id"]] = item
        elif isinstance(row.get("id"), str):
            broken_assets.add(row["id"])
    from models.image_plan import _list_reference_images
    for index, row in enumerate(_list_reference_images(root), 1):
        item = _record(root, str(row.get("path") or ""), kind="captured", label=f"采集图片 {index}")
        if item and item["id"] not in broken_assets:
            records.setdefault(item["id"], item)
    for row in read_json(root / "output/image-generation-report.json").get("files") or []:
        if not isinstance(row, Mapping) or row.get("generator") not in {"rightapi", "doubao", "captured-original"}:
            continue
        item = _record(root, str(row.get("path") or ""), kind="generated" if row.get("generator") != "captured-original" else "captured",
                       label=str(row.get("slot") or "已生成图片"))
        if item and item["id"] not in broken_assets and (not row.get("sha256") or row.get("sha256") == item["sha256"]):
            records.setdefault(item["id"], item)
    return list(records.values())


def media_path(directory: Path | str, image_id: str) -> Path:
    if not _IMAGE_ID.fullmatch(image_id):
        raise ValueError("图片标识无效")
    root = Path(directory).resolve()
    image = next((row for row in media_library(root) if row["id"] == image_id), None)
    if not image:
        raise ValueError("图片已不存在，请重新选择图片")
    return root / image["relative_path"]


def _text(value: Any, maximum: int, label: str) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}须为文本且不超过 {maximum} 字符")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value) or _UNSAFE_TEXT.search(value):
        raise ValueError(f"{label}不能包含网页链接、HTML 或联系方式")
    return value.strip()


def validate_blocks(blocks: Any, images: Sequence[Mapping[str, Any]], *, allow_incomplete: bool = False) -> list[dict[str, Any]]:
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise ValueError(f"富内容须为模块列表，一次最多 {MAX_BLOCKS} 个模块")
    available = {row["id"] for row in images}
    result, used = [], set()
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, Mapping):
            raise ValueError("富内容模块格式不正确")
        identity = block.get("id") or "block-" + uuid.uuid4().hex[:12]
        if not isinstance(identity, str) or not _IDENTIFIER.fullmatch(identity) or identity in used:
            raise ValueError("富内容模块 ID 无效或重复")
        used.add(identity)
        kind = block.get("type")
        if kind not in {"image", "image_text", "text"}:
            raise ValueError("请选择图片、图文或文字模块")
        title = _text(block.get("title") or "", 250, "模块标题")
        text = _text(block.get("text") or "", 6000, "模块文字")
        image_id = block.get("image_id")
        if kind != "text" and (not isinstance(image_id, str) or image_id not in available) and not (allow_incomplete and not image_id):
            raise ValueError(f"第 {index} 个模块的图片不存在，请重新选择或导入图片")
        if kind == "text" and not (title or text) and not allow_incomplete:
            raise ValueError(f"第 {index} 个文字模块为空")
        result.append({"id": identity, "type": kind, "image_id": image_id if kind != "text" else None,
                       "title": title if kind != "image" else "", "text": text if kind != "image" else ""})
    if sum(len(row["title"]) + len(row["text"]) for row in result) > 30000:
        raise ValueError("富内容文字合计过长，请缩短到 30000 字符以内")
    return result


def _legacy_value(root: Path) -> Any:
    identity = attribute_id(root)
    if identity is None:
        return None
    for document, key in (("input/human-confirmations.json", "attributes"),
                          ("output/ozon-attributes-final.json", "common_attributes")):
        rows = read_json(root / document).get(key) or []
        if isinstance(rows, Mapping):
            rows = [{"attribute_id": int(k), "value": v} for k, v in rows.items() if str(k).isdigit()]
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping) and row.get("attribute_id") == identity:
                value = row.get("value")
                if isinstance(value, list) and value and isinstance(value[0], Mapping):
                    return value[0].get("value")
                return value
    return None


def read_content(directory: Path | str) -> dict[str, Any]:
    root = Path(directory).resolve()
    saved = read_json(root / DRAFT_FILE)
    legacy = None if saved else _legacy_value(root)
    context = context_fingerprint(root)
    candidates = [row for row in read_json(root / CANDIDATES_FILE).get("candidates") or []
                  if isinstance(row, Mapping) and row.get("context_fingerprint") == context]
    return {"ok": True, "revision": int(saved.get("revision") or 0), "context_fingerprint": context,
            "blocks": saved.get("blocks") or [], "media": media_library(root), "candidates": candidates[-8:],
            "attribute_id": attribute_id(root), "configured": bool(saved),
            "legacy_json": legacy, "warnings": ["已有原始富内容已保留；应用新编辑内容后才替换"] if legacy else [],
            "saved_at": saved.get("saved_at"), "source_context_changed": bool(saved and saved.get("context_fingerprint") != context)}


def _require_current(root: Path, revision: int, fingerprint: str) -> None:
    current = read_json(root / DRAFT_FILE)
    if type(revision) is not int or revision != int(current.get("revision") or 0):
        raise RichContentConflict("富内容已在其他窗口更新，请刷新后再应用；本次未覆盖已保存内容")
    if fingerprint != context_fingerprint(root):
        raise RichContentConflict("商品规格、信息或关键词已变化，请刷新后重新核对富内容；本次未覆盖草稿")


def _freeze_images(root: Path, image_ids: set[str], library: Sequence[Mapping[str, Any]]) -> None:
    indexed = {row["id"]: row for row in library}
    registry = read_json(root / ASSETS_FILE)
    saved = {row["id"]: row for row in registry.get("images") or [] if isinstance(row, Mapping) and row.get("id")}
    if len(set(saved) | image_ids) > 300:
        raise ValueError("当前商品富内容图片过多，请新建商品草稿")
    for identity in image_ids:
        row = indexed[identity]
        body, suffix, width, height = _image_bytes(root / row["relative_path"])
        if hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise RichContentConflict("图片在保存期间变化，请重新选择")
        relative = f"{IMAGE_DIRECTORY}/{row['sha256']}{suffix}"
        target = root / relative
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body)
            os.replace(temporary, target)
        saved[identity] = {**dict(row), "relative_path": relative, "width": width, "height": height, "saved_at": _now()}
    write_json(root / ASSETS_FILE, {"version": 1, "images": list(saved.values())})


def save_content(directory: Path | str, *, revision: int, context_fingerprint: str,
                 blocks: list[dict[str, Any]]) -> dict[str, Any]:
    root = Path(directory).resolve()
    with product_edit_lock(root):
        _require_editable(root)
        _require_current(root, revision, context_fingerprint)
        if blocks and attribute_id(root) is None:
            raise ValueError("所选 Ozon 类目尚未返回 JSON 富内容字段，请先同步官方字段")
        library = media_library(root)
        normalized = validate_blocks(blocks, library)
        _freeze_images(root, {row["image_id"] for row in normalized if row["image_id"]}, library)
        write_json(root / DRAFT_FILE, {"version": 1, "revision": revision + 1, "blocks": normalized,
                                     "context_fingerprint": context_fingerprint, "saved_at": _now()})
        return read_content(root)


def import_image(directory: Path | str, *, filename: str, data_base64: str) -> dict[str, Any]:
    root = Path(directory).resolve()
    if not isinstance(data_base64, str) or len(data_base64) > (MAX_IMAGE_BYTES * 4 // 3 + 16):
        raise ValueError("单张图片必须不超过 10 MB")
    try:
        body = base64.b64decode(data_base64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("导入图片的 Base64 数据无效") from None
    body, suffix, width, height = _check_image(body)
    digest = hashlib.sha256(body).hexdigest()
    with product_edit_lock(root):
        _require_editable(root)
        relative = f"{IMAGE_DIRECTORY}/{digest}{suffix}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body)
            os.replace(temporary, target)
        image = _record(root, relative, kind="imported", label=Path(filename.replace("\\", "/")).name or "导入图片")
        registry = read_json(root / ASSETS_FILE)
        records = {row["id"]: row for row in registry.get("images") or [] if isinstance(row, Mapping) and row.get("id")}
        if len(records) >= 300 and image["id"] not in records:
            raise ValueError("当前商品导入图片过多")
        records[image["id"]] = image
        write_json(root / ASSETS_FILE, {"version": 1, "images": list(records.values())})
    return {"ok": True, "image": image, "media": media_library(root)}


def _ai_context(root: Path) -> dict[str, Any]:
    from .selected_source import selected_source, _model_safe
    from .copy_evidence import verified_copy_facts
    from .selection import load_selected_keywords
    from .guided_workflow import _factual_analysis, workflow_status
    source = selected_source(root, read_json(root / "input/source.json"), require_selection=True)
    # Reproject factual values from the current selection. A historical output
    # file is not evidence for a newly selected variant. Current unconfirmed
    # analysis may assist wording, but cannot invent factual specifications.
    factual = _factual_analysis(root, source)
    workflow = workflow_status(root)
    analysis_current = workflow.get("analysis", {}).get("status") in {"ready", "confirmed"}
    analysis = workflow.get("analysis", {}).get("payload") if analysis_current else {}
    approved_copy = workflow.get("copy", {}).get("payload") if workflow.get("copy", {}).get("confirmed") else {}
    keywords = (load_selected_keywords(root) or {}).get("keywords") or []
    useful = [row for row in keywords if isinstance(row, Mapping) and
              row.get("role") not in {"reject", "exclude", "ad"} and row.get("relevance") != "conflict"]
    return _model_safe({"source_title": source.get("title_zh"), "selected_skus": source.get("skus") or [],
            "verified_facts": verified_copy_facts(factual, source),
            "product_summary": {key: (analysis or {}).get(key) for key in ("summary", "selling_points")},
            "summary_confirmed": bool(analysis_current and workflow.get("analysis", {}).get("confirmed")),
            "approved_copy": approved_copy or {}, "selected_keywords": useful,
            "category": read_json(root / "input/category-selection.json")})


def generate_content(directory: Path | str, *, revision: int, context_fingerprint: str,
                     prompt: str, blocks: list[dict[str, Any]], history: list[dict[str, str]] | None = None,
                     provider: Any) -> dict[str, Any]:
    from models import ModelError
    from models.http_provider import extract_json
    root = Path(directory).resolve()
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
        raise ValueError("请输入不超过 6000 字符的修改提示词")
    with product_edit_lock(root):
        _require_editable(root)
        _require_current(root, revision, context_fingerprint)
        library = media_library(root)
        normalized = validate_blocks(blocks, library, allow_incomplete=True)
        context = _ai_context(root)
    with _AI_GUARD:
        if str(root) in _ACTIVE_AI:
            raise RichContentConflict("当前商品富内容 AI 正在生成，请等待当前请求完成")
        _ACTIVE_AI.add(str(root))
    try:
        conversation = []
        for row in (history or [])[-12:]:
            if not isinstance(row, Mapping) or row.get("role") not in {"user", "assistant"} or not isinstance(row.get("content"), str):
                raise ValueError("AI 对话记录格式无效")
            conversation.append({"role": row["role"], "content": row["content"][:6000]})
        system = (
            "你负责 Ozon 商品富内容编辑。用户以中文对话，回复 message_zh 使用中文；买家可见 title/text 使用自然俄语。"
            "只返回 JSON {message_zh:string,blocks:[{id,type,image_id,title,text}]}。type 仅 image/image_text/text。"
            "基于所选商品真实事实和已有文案；关键词必须自然融入合适的句子，忽略不相关词，不堆砌词表。"
            "不要编造材质、尺寸、性能、认证、配件或适用年龄；无证据就省略。不要加入价格、促销、联系方式、URL或HTML。"
            "图片只能引用给定 media 的 image_id；不生成图片、不修改商品、不要引用其它SKU。"
            "修改当前模块而不是无故丢弃原图。可保留、增加、删除、重新排序模块，模块id唯一。")
        user = json.dumps({"product": context, "current_blocks": normalized,
                           "media": [{key: row[key] for key in ("id", "kind", "label")} for row in library],
                           "conversation": conversation, "request_zh": prompt}, ensure_ascii=False)
        transport = getattr(provider, "transport", None)
        if not transport or not callable(getattr(transport, "complete", None)):
            raise ModelError("当前文本模型不支持富内容对话，请配置火山方舟文本模型")
        try:
            response = transport.complete(system=system, user=user, temperature=0.3)
        except Exception as error:
            raise ModelError("富内容 AI 请求失败，本次未自动重试也未修改草稿；请求可能已计费，请检查模型服务") from error
        result = extract_json(response)
        if not isinstance(result, dict):
            raise ModelError("AI 未返回有效富内容 JSON，已保留原稿；本次不自动重试")
        candidate_blocks = validate_blocks(result.get("blocks"), library)
        message = result.get("message_zh")
        if not isinstance(message, str) or len(message) > 4000:
            message = "已根据商品信息和关键词生成候选富内容，请检查后应用。"
        candidate = {"id": "rich-" + uuid.uuid4().hex[:20], "status": "candidate", "blocks": candidate_blocks,
                     "message_zh": message, "revision": revision, "context_fingerprint": context_fingerprint,
                     "prompt_zh": prompt, "created_at": _now(), "model_calls": 1}
        with product_edit_lock(root):
            _require_editable(root)
            try:
                _require_current(root, revision, context_fingerprint)
            except RichContentConflict:
                candidate["status"] = "stale"
                previous = read_json(root / CANDIDATES_FILE).get("candidates") or []
                write_json(root / CANDIDATES_FILE, {"candidates": [*previous[-19:], candidate]})
                raise RichContentConflict("AI 生成期间商品或富内容已变化，结果已作为过期候选保留，未覆盖草稿")
            previous = read_json(root / CANDIDATES_FILE).get("candidates") or []
            write_json(root / CANDIDATES_FILE, {"candidates": [*previous[-19:], candidate]})
        return {"ok": True, "candidate": candidate, "model_calls": 1, "api_writes_performed": False}
    finally:
        with _AI_GUARD:
            _ACTIVE_AI.discard(str(root))


def apply_candidate(directory: Path | str, *, candidate_id: str, revision: int,
                    context_fingerprint: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    root = Path(directory).resolve()
    candidate = next((row for row in read_json(root / CANDIDATES_FILE).get("candidates") or []
                      if row.get("id") == candidate_id), None)
    if not candidate or candidate.get("status") != "candidate":
        raise ValueError("富内容候选不存在或已过期，请重新生成")
    if candidate.get("revision") != revision or candidate.get("context_fingerprint") != context_fingerprint:
        raise RichContentConflict("候选不属于当前版本，请重新生成或手动编辑后应用")
    return save_content(root, revision=revision, context_fingerprint=context_fingerprint,
                        blocks=blocks if blocks is not None else candidate["blocks"])


def content_version(directory: Path | str) -> str | None:
    root = Path(directory).resolve()
    saved = read_json(root / DRAFT_FILE)
    if not saved:
        return None
    assets = {row["id"]: row for row in media_library(root)}
    binding = []
    for block in saved.get("blocks") or []:
        if block.get("image_id"):
            image = assets.get(block["image_id"])
            if not image:
                raise ValueError("富内容图片已变化或丢失，请重新选择并应用")
            binding.append({key: image[key] for key in ("id", "sha256", "size_bytes")})
    return _hash({"blocks": saved.get("blocks") or [], "images": binding})


def _rich_json(blocks: Sequence[Mapping[str, Any]], images: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    content = []
    for block in blocks:
        title, text = str(block.get("title") or ""), str(block.get("text") or "")
        if block["type"] == "text":
            widget: dict[str, Any] = {"widgetName": "raTextBlock", "gapSize": "m"}
            if title:
                widget["title"] = {"content": [title], "theme": "title"}
            if text:
                widget["text"] = {"content": text.splitlines() or [text], "theme": "default"}
        else:
            image = images[block["image_id"]]
            showcase = {"img": {"src": image["url"], "srcMobile": image["url"],
                                 "width": image["width"], "height": image["height"],
                                 "widthMobile": image["width"], "heightMobile": image["height"]}}
            if title:
                showcase["title"] = title
            if text:
                showcase["text"] = {"content": text.splitlines() or [text], "theme": "default"}
            widget = {"widgetName": "raShowcase", "type": "billboard", "blocks": [showcase]}
        content.append(widget)
    payload = {"version": 0.2, "content": content}
    _validate_official(payload)
    return payload


@lru_cache(maxsize=1)
def _official_schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "contracts/ozon-rich-content.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_official(payload: Mapping[str, Any]) -> None:
    """Check emitted v0.2 widgets against the unmodified official snapshot.

    Only the JSON Schema keywords used on these emitted widget branches are
    needed. The general legacy validator has incompatible anyOf semantics, so
    it is intentionally not used to gate the current editor.
    """
    schema = _official_schema()
    types = {"object": dict, "array": list, "string": str, "number": (float, int),
             "integer": int, "boolean": bool, "null": type(None)}
    def matches(node, value):
        ref = node.get("$ref")
        if ref:
            if not ref.startswith("#/definitions/"):
                return False
            target = schema["definitions"].get(ref.split("/")[-1])
            return bool(target) and matches(target, value)
        if "allOf" in node and not all(matches(branch, value) for branch in node["allOf"]):
            return False
        if "anyOf" in node and not any(matches(branch, value) for branch in node["anyOf"]):
            return False
        if "enum" in node and value not in node["enum"]:
            return False
        if "type" in node:
            kind = node["type"]
            if kind not in types or not isinstance(value, types[kind]) or (kind in {"number", "integer"} and isinstance(value, bool)):
                return False
        if isinstance(value, dict):
            if any(key not in value for key in node.get("required") or []):
                return False
            for key, child in value.items():
                property_schema = (node.get("properties") or {}).get(key)
                if property_schema and not matches(property_schema, child):
                    return False
                if property_schema is None and node.get("additionalProperties") is False:
                    return False
        if isinstance(value, list):
            if len(value) < node.get("minItems", 0) or len(value) > node.get("maxItems", float("inf")):
                return False
            items = node.get("items")
            if isinstance(items, dict) and not all(matches(items, child) for child in value):
                return False
            if isinstance(items, list) and any(not matches(items[index], child) for index, child in enumerate(value) if index < len(items)):
                return False
        return True
    if not matches(schema["definitions"]["RichContentV02"], payload):
        raise ValueError("富内容不符合 Ozon 官方富内容模块格式，请重新编辑")


def publish_content(directory: Path | str, *, storage=None) -> dict[str, Any]:
    """Called only at an explicitly confirmed storage-publication boundary."""
    root = Path(directory).resolve()
    with product_edit_lock(root):
        _require_editable(root)
        saved = read_json(root / DRAFT_FILE)
        if not saved or not saved.get("blocks"):
            return {"ok": True, "images": [], "api_writes_performed": False}
        if saved.get("context_fingerprint") != context_fingerprint(root):
            raise ValueError("商品信息已变化，请重新核对并应用富内容")
        library = {row["id"]: row for row in media_library(root)}
        blocks = validate_blocks(saved["blocks"], list(library.values()))
        version = content_version(root)
        needed = set(row["image_id"] for row in blocks if row["image_id"])
        if storage is None and needed:
            from .oss_cos import _storage_from_env
            try:
                storage = _storage_from_env()
            except Exception:
                raise ValueError("富内容图片存储未配置，请检查 COS 配置和权限") from None
        if storage is not None and getattr(storage, "dry_run", False):
            raise ValueError("富内容公开地址不能使用模拟上传结果")
        results = {}
        for identity in needed:
            row = library[identity]
            try:
                result = storage.publish_slot(root, {"slot": "rich-" + row["sha256"], "output_path": row["relative_path"]})
            except Exception:
                raise ValueError("富内容图片存储发布失败，请检查 COS 配置和权限；本次未提交 Ozon") from None
            url = str(result.get("url") or "")
            parsed = urlsplit(url)
            if (result.get("status") not in {"uploaded", "unchanged"} or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                    or result.get("sha256") != row["sha256"]):
                raise ValueError("富内容图片未发布为与当前图片一致的 HTTPS 地址")
            results[identity] = {**row, "url": url, "object_key": result.get("key")}
        if content_version(root) != version:
            raise RichContentConflict("富内容上传期间变化，请重新公开")
        payload = {"version": 1, "product_id": root.name, "content_fingerprint": version,
                   "attribute_id": attribute_id(root), "images": results, "json": _rich_json(blocks, results),
                   "published_at": _now()}
        write_json(root / PUBLIC_FILE, payload)
        return {"ok": True, "images": list(results.values()), "json": payload["json"], "api_writes_performed": False}


def compile_attribute(directory: Path | str) -> dict[str, Any] | None:
    root = Path(directory).resolve()
    saved = read_json(root / DRAFT_FILE)
    if not saved:
        return None
    identity = attribute_id(root)
    if saved.get("blocks") and identity is None:
        raise ValueError("所选官方类目没有富内容字段，不能提交富内容")
    if not saved.get("blocks"):
        return {"attribute_id": identity, "remove": True}
    if saved.get("context_fingerprint") != context_fingerprint(root):
        raise ValueError("商品信息、规格或关键词已变化，请重新核对并应用富内容")
    publication = read_json(root / PUBLIC_FILE)
    if (publication.get("product_id") != root.name or publication.get("attribute_id") != identity
            or publication.get("content_fingerprint") != content_version(root)):
        raise ValueError("富内容尚未公开，或图片/文字已变化；请重新发布媒体地址")
    assets = {row["id"]: row for row in media_library(root)}
    images = publication.get("images") or {}
    blocks = validate_blocks(saved["blocks"], list(assets.values()))
    for block in blocks:
        key = block.get("image_id")
        if key and (key not in images or images[key].get("sha256") != assets[key]["sha256"]
                    or not str(images[key].get("url") or "").startswith("https://")):
            raise ValueError("富内容公开图片版本不一致，请重新发布媒体地址")
    rich_json = _rich_json(blocks, images)
    if publication.get("json") != rich_json:
        raise ValueError("富内容公开 JSON 与当前草稿不一致，请重新发布媒体地址")
    return {"attribute_id": identity, "attribute_name": "JSON rich content",
            "value": json.dumps(rich_json, ensure_ascii=False, separators=(",", ":")), "dictionary_value_id": None}


def publication_binding(directory: Path | str) -> dict[str, Any]:
    root = Path(directory)
    compiled = compile_attribute(root)
    if not compiled or compiled.get("remove"):
        return {"ok": True, "expected_content": {}, "urls": []}
    images = read_json(root / PUBLIC_FILE).get("images") or {}
    return {"ok": True, "urls": [row["url"] for row in images.values()],
            "expected_content": {row["url"]: {"sha256": row["sha256"], "size_bytes": row["size_bytes"]}
                                 for row in images.values()}}
