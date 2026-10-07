"""1688 采集入库（M1），对应原项目 ``POST /api/collector/products`` → ``ingest_capture()``。

规则（与原项目对齐）：

- ``source_url`` 必须是 1688 商品详情页（``1688.com/offer/<数字>``）；
- 默认已选 SKU 必须 1–10 个；``collection_mode=all_skus`` 保存全部原始规格，后台确认后才进入上架；
- **同一次采集重复入库**：同一 offer 命中已有商品时返回冲突（``DuplicateCaptureError``），
  列出 ``open_existing`` / ``create_new_version`` 两个选项；只有显式 ``allow_new_version=True`` 才建新版本；
- 落盘结构：``products/<P######>/input/{source.json, raw-snapshot.json, source-manifest.json,
  category-selection.json, main-images/, sku-images/, detail-images/}`` + ``status.json``（``COLLECTED``）；
- 采集绑定：``collection_id`` + ``source-manifest.json`` 的 sha256，供
  :func:`pipeline.status.source_snapshot_binding` 使用 —— 之后若有人改动采集输入，会被判定"已变化"。

**不做的事**：不调 Ozon、不生成任何 AI 内容、不改写已有商品的输入（重复采集只提示，不覆盖）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # 正常包内导入
    from parsing import parse_number
    from pipeline import status as product_status
    from pipeline.status import MAX_SELECTED_SKUS, OFFER_ID_PATTERN, PRODUCT_ID_PATTERN
except ModuleNotFoundError:  # 允许以脚本方式直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from parsing import parse_number
    from pipeline import status as product_status
    from pipeline.status import MAX_SELECTED_SKUS, OFFER_ID_PATTERN, PRODUCT_ID_PATTERN

SCHEMA_VERSION = "1.0.0"
SOURCE_KIND_WORKBENCH = "workbench_collection"
MAX_CAPTURE_SKUS = 2000  # 防止异常页面无限入库；超限报错，不截断规格。

IMAGE_DIRS: dict[str, str] = {
    "main": "input/main-images",
    "sku": "input/sku-images",
    "detail": "input/detail-images",
}

_PRICE_KEYS = ("purchase_price_cny", "cost_cny", "purchase_price", "price_cny")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif"}
# 1688 图片 CDN 校验 Referer；服务端直下时带浏览器 UA + detail 页 Referer 最稳。
_REMOTE_IMAGE_HEADERS = {
    "Referer": "https://detail.1688.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
_REMOTE_IMAGE_TIMEOUT = 20
_REMOTE_IMAGE_MAX_BYTES = 12 * 1024 * 1024
_REMOTE_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
)


def _detect_image_ext(data: bytes) -> str | None:
    for magic, suffix in _REMOTE_IMAGE_MAGIC:
        if data.startswith(magic):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                continue
            return suffix
    return None


def _download_remote_image(url: str) -> tuple[bytes, str] | None:
    """下载一张远程图片，返回 (bytes, 扩展名)；失败返回 None（不抛，采集不因单图失败中断）。"""
    try:
        req = urllib.request.Request(url, headers=_REMOTE_IMAGE_HEADERS)
        with urllib.request.urlopen(req, timeout=_REMOTE_IMAGE_TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not data or len(data) > _REMOTE_IMAGE_MAX_BYTES:
        return None
    ext = _detect_image_ext(data)
    if ext is None:
        return None
    return data, ext


class CaptureValidationError(ValueError):
    """采集载荷不合法（对应 HTTP 422）。"""


class DuplicateCaptureError(RuntimeError):
    """同一 offer 重复采集（对应 HTTP 409）。不覆盖已有商品。"""

    def __init__(
        self,
        existing_product_id: str,
        source_url: str,
        options: Sequence[str] = ("open_existing", "create_new_version"),
    ) -> None:
        super().__init__(f"该商品已采集过：{existing_product_id}")
        self.existing_product_id = existing_product_id
        self.source_url = source_url
        self.options = list(options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duplicate_of": self.existing_product_id,
            "source_url": self.source_url,
            "options": self.options,
        }


# --------------------------------------------------------------------- 基础工具


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_collection_id() -> str:
    return f"COL-{uuid.uuid4().hex[:12].upper()}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def offer_id_of(source_url: Any) -> str | None:
    match = OFFER_ID_PATTERN.search(str(source_url or ""))
    return match.group(1) if match else None


def _number(value: Any) -> float | None:
    return parse_number(value)


def _sanitize_name(name: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    stem = Path(text).stem or fallback
    suffix = Path(text).suffix.lower()
    stem = re.sub(r"[^0-9A-Za-z._\u4e00-\u9fff\u0400-\u04FF-]+", "_", stem)[:80] or fallback
    return f"{stem}{suffix}"


# --------------------------------------------------------------------- 产品编号与查重


def _existing_product_ids(products_root: Path) -> list[str]:
    if not products_root.is_dir():
        return []
    return sorted(
        path.name for path in products_root.iterdir() if path.is_dir() and PRODUCT_ID_PATTERN.match(path.name)
    )


def allocate_product_id(products_root: Path | str) -> str:
    """分配下一个 ``P######``：取当前最大编号 +1，不复用已删除过的号段。"""
    root = Path(products_root)
    used = [int(item[1:]) for item in _existing_product_ids(root)]
    return f"P{(max(used) + 1) if used else 1:06d}"


def find_existing_capture(products_root: Path | str, source_url: str) -> dict[str, Any] | None:
    """按 offer id（优先）或整串 URL 找已采集过的商品，返回最新那个。"""
    root = Path(products_root)
    wanted_offer = offer_id_of(source_url)
    wanted_url = str(source_url or "").strip()
    matches: list[dict[str, Any]] = []
    for product_id in _existing_product_ids(root):
        source = _read_json(root / product_id / "input" / "source.json")
        existing_url = str(source.get("source_url") or "")
        same = (
            bool(wanted_offer) and offer_id_of(existing_url) == wanted_offer
        ) or (not wanted_offer and existing_url == wanted_url and bool(wanted_url))
        if not same:
            continue
        matches.append(
            {
                "product_id": product_id,
                "source_url": existing_url,
                "collection_id": source.get("collection_id"),
                "captured_at": source.get("captured_at"),
                "version": int(source.get("version") or 1),
                "path": str(root / product_id),
            }
        )
    if not matches:
        return None
    matches.sort(key=lambda item: (item["version"], item["product_id"]))
    return matches[-1]


# --------------------------------------------------------------------- 载荷校验


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规范化采集载荷；不合法直接抛 :class:`CaptureValidationError`。"""
    if not isinstance(payload, Mapping):
        raise CaptureValidationError("载荷必须是对象")

    source_url = str(payload.get("source_url") or "").strip()
    if "1688.com/offer/" not in source_url:
        raise CaptureValidationError(f"source_url 不是 1688 商品详情页：{source_url or '空'}")
    if not offer_id_of(source_url):
        raise CaptureValidationError(f"无法从 source_url 解析 offer id：{source_url}")

    raw_skus = payload.get("skus")
    if not isinstance(raw_skus, list) or not raw_skus:
        raise CaptureValidationError("缺少 skus")
    collect_all = payload.get("collection_mode") == "all_skus"
    limit = MAX_CAPTURE_SKUS if collect_all else MAX_SELECTED_SKUS
    if len(raw_skus) > limit:
        raise CaptureValidationError(
            f"{'采集规格' if collect_all else '已选 SKU'}不能超过 {limit} 个，实际 {len(raw_skus)}（未截断）"
        )

    skus: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_skus):
        if not isinstance(item, Mapping):
            raise CaptureValidationError(f"skus[{index}] 必须是对象")
        sku_id = str(item.get("sku_id") or "").strip()
        if not sku_id and not collect_all:
            raise CaptureValidationError(f"skus[{index}] 缺少 sku_id")
        issues = []
        if collect_all and (not sku_id or sku_id == "unknown" or sku_id in seen_ids):
            issues.append("原始规格标识缺失或重复，须人工核对")
            sku_id = f"CAPTURE-ROW-{index + 1}"
            while sku_id in seen_ids:
                sku_id += "-ROW"
        seen_ids.add(sku_id)
        price = None
        price_key = None
        for key in _PRICE_KEYS:
            price = _number(item.get(key))
            if price is not None:
                price_key = key
                break
        if (price is None or price <= 0) and not collect_all:
            raise CaptureValidationError(f"skus[{index}]（{sku_id}）缺少有效采购价")
        if price is None or price <= 0:
            price = None
            issues.append("缺少有效采购价")
        normalized = dict(item)
        normalized["sku_id"] = sku_id
        normalized["position"] = index + 1
        normalized["purchase_price_cny"] = price
        normalized.setdefault("price_source_key", price_key)
        if issues:
            normalized["collection_issues"] = issues
            normalized["source_sku_id"] = item.get("sku_id")
        skus.append(normalized)

    category = payload.get("category") or payload.get("selected_category")
    # Edge 插件在 SKU 抽屉里选完 Ozon 类目后放在 ozon_category_selection
    ozon_cat = payload.get("ozon_category_selection")
    if category is None and isinstance(ozon_cat, Mapping) and ozon_cat:
        category = ozon_cat
    keyword_category = payload.get("keyword_category")
    if category is None and isinstance(keyword_category, Mapping) and keyword_category:
        category = keyword_category
    normalized_category = None
    if isinstance(category, Mapping) and category:
        normalized_category = {
            "category_id": str(category.get("category_id") or "").strip() or None,
            "type_id": str(category.get("type_id") or "").strip() or None,
            "category_path_zh": category.get("category_path_zh") or category.get("category_path"),
            "selected_at": category.get("selected_at") or now_iso(),
            "rules_snapshot_hash": category.get("rules_snapshot_hash"),
        }

    images: dict[str, list[dict[str, Any]]] = {"main": [], "sku": [], "detail": []}
    raw_images = payload.get("images")
    if isinstance(raw_images, Mapping):
        for role in images:
            entries = raw_images.get(role) or []
            if isinstance(entries, (str, Path)):
                entries = [entries]
            for entry in entries:
                if isinstance(entry, Mapping):
                    path = entry.get("path") or entry.get("source_path")
                    name = entry.get("name") or (Path(str(path)).name if path else "")
                else:
                    path, name = str(entry), Path(str(entry)).name
                images[role].append({"path": str(path) if path else None, "name": str(name or "")})
    elif "images" not in payload:
        # Edge 插件：main_images / detail_images 是 URL 数组；SKU 图在每个 sku.image_url
        for role, key in (("main", "main_images"), ("detail", "detail_images")):
            for entry in payload.get(key) or []:
                url = None
                if isinstance(entry, Mapping):
                    url = entry.get("url") or entry.get("src")
                elif isinstance(entry, str):
                    url = entry
                if url:
                    images[role].append({"url": str(url), "name": ""})
        for sku in skus:
            if not isinstance(sku, Mapping):
                continue
            url = sku.get("image_url") or sku.get("variant_image_url")
            if url and str(url) != "unknown":
                images["sku"].append({"url": str(url), "name": sku["sku_id"], "sku_id": sku["sku_id"]})

    return {
        "source_url": source_url,
        "offer_id": offer_id_of(source_url),
        "title_zh": payload.get("title_zh") or payload.get("title") or payload.get("title_cn"),
        "captured_at": payload.get("captured_at") or now_iso(),
        "skus": skus,
        "collection_mode": "all_skus" if collect_all else "selected_skus",
        "category": normalized_category,
        "images": images,
        "raw": payload.get("raw") if isinstance(payload.get("raw"), Mapping) else dict(payload),
        "extra": payload.get("extra") if isinstance(payload.get("extra"), Mapping) else {},
        "keywords": _normalize_keywords(payload),
        "keyword_category": dict(keyword_category) if isinstance(keyword_category, Mapping) else None,
        "keyword_source": payload.get("keyword_source"),
        # 1688 详情页的规格/属性：材质、包装数量、认证等（真模型曾因缺这些要求人工确认）
        "attributes_zh": _normalize_attributes(payload),
        # 详情页正文（洗涤说明/规格描述等）：真模型据此判断款式并写文案
        "description_zh": str(payload.get("description_zh") or payload.get("description") or "").strip() or None,
    }


#: 详情页属性表里我们关心的键（中文原文，用于属性填值与 facts 补全）
ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "material": ("材质", "材料", "面料", "成分", "材质成分", "材质说明"),
    "package_quantity": ("包装数量", "每包数量", "单包数量", "套装件数"),
    "certifications": ("认证", "证书", "检测报告", "资质", "认证证书"),
    "weight_g": ("克重", "重量", "单品重量", "毛重"),
    "brand": ("品牌", "商标"),
}


def _normalize_attributes(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把采集到的详情页属性规整成 ``{material, package_quantity, certifications, ...}``。

    兼容三种来源：① 浏览器脚本抓的 ``attributes_zh``（原样键值表）；
    ② 显式字段（``material_zh`` / ``package_quantity`` / ``certifications``）；
    ③ Edge 插件的 ``product_attributes`` 数组（``[{name, value}, ...]`` 或 ``[{key, value}]``）。
    """
    raw: dict[str, str] = {}
    source = payload.get("attributes_zh") or payload.get("attributes")
    if isinstance(source, Mapping):
        for key, value in source.items():
            text = str(value or "").strip()
            if text:
                raw[str(key).strip()] = text
    elif not source:
        # Edge 插件：product_attributes 是数组，转成键值表
        prod_attrs = payload.get("product_attributes")
        if isinstance(prod_attrs, list):
            for item in prod_attrs:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name_cn") or item.get("name") or item.get("key") or item.get("label") or "").strip()
                value = next((item.get(key) for key in ("value_cn", "value", "values")
                              if item.get(key) is not None and item.get(key) != ""), "")
                value = str(value).strip()
                if name and value:
                    raw[name] = value

    resolved: dict[str, Any] = {"raw": raw}
    for field, aliases in ATTRIBUTE_ALIASES.items():
        for alias in aliases:
            if alias in raw:
                resolved[field] = raw[alias]
                break
        if field in resolved:
            continue
        explicit = payload.get(f"{field}_zh") or payload.get({"weight_g": "weight_g"}.get(field, field))
        if explicit not in (None, "", []):
            resolved[field] = explicit

    quantity = resolved.get("package_quantity")
    if quantity is not None:
        try:
            resolved["package_quantity"] = int(float(str(quantity).replace(",", "").strip()))
        except (TypeError, ValueError):
            pass
    certifications = resolved.get("certifications")
    if isinstance(certifications, str):
        parts = [item.strip() for item in re.split(r"[、,，;；/|]+", certifications) if item.strip()]
        resolved["certifications"] = parts
    return resolved


def _normalize_keywords(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """采集载荷里可选的关键词（来自选品清单）：保留字符串与记录两种写法。"""
    raw = payload.get("keywords") or payload.get("source_keywords") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            text = str(item.get("keyword") or "").strip()
            if text:
                rows.append({**dict(item), "keyword": text})
        else:
            text = str(item or "").strip()
            if text:
                rows.append({"keyword": text})
    return rows


# --------------------------------------------------------------------- 落盘


def _copy_images(
    product_dir: Path, images: Mapping[str, Sequence[Mapping[str, Any]]], *, preserve_roles: bool = False
) -> tuple[dict[str, int], list[str], list[str]]:
    """把采集到的图片复制进商品目录；返回 (计数, 警告, 实际落盘相对路径)。

    图片条目支持两种：``{"path": 本地路径}``（原文件夹导入/控制台脚本）和
    ``{"url": 远程 URL}``（Edge 插件直接 POST，服务端带 Referer 下载）。
    """
    counts = {role: 0 for role in IMAGE_DIRS}
    warnings: list[str] = []
    stored: list[str] = []
    seen_hashes: dict[tuple[str, str], str] = {}
    downloaded_sources: dict[str, Path | None] = {}
    temp_files: list[Path] = []

    for role, entries in images.items():
        target_dir = product_dir / IMAGE_DIRS[role]
        target_dir.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(entries or [], start=1):
            raw_path = entry.get("path")
            url = entry.get("url")
            source: Path | None = None
            if raw_path:
                source = Path(str(raw_path))
                if not source.is_file():
                    warnings.append(f"{role} 图片不存在：{raw_path}")
                    continue
            elif url:
                url = str(url)
                if url not in downloaded_sources:
                    downloaded = _download_remote_image(url)
                    downloaded_sources[url] = None
                    if downloaded is not None:
                        data, ext = downloaded
                        fd, tmp_name = tempfile.mkstemp(prefix=f"ozon-img-{role}-", suffix=ext)
                        os.close(fd)
                        tmp = Path(tmp_name)
                        tmp.write_bytes(data)
                        temp_files.append(tmp)
                        downloaded_sources[url] = tmp
                source = downloaded_sources[url]
                if source is None:
                    warnings.append(f"{role} 第 {index} 张下载失败：{url}")
                    continue
            else:
                warnings.append(f"{role} 第 {index} 张没有本地路径也没有远程 URL，已跳过")
                continue
            if source.suffix.lower() not in _IMAGE_SUFFIXES:
                warnings.append(f"{role} 图片格式不支持：{source.name}")
                continue
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            identity = (role if preserve_roles else "all", digest)
            if identity in seen_hashes:
                if isinstance(entry, dict):
                    entry["stored_path"] = seen_hashes[identity]
                warnings.append(f"{role} 重复图片已跳过：{source.name}")
                continue
            name = _sanitize_name(entry.get("name") or source.name, f"{role}-{index:03d}")
            if Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
                name += source.suffix.lower()
            target = target_dir / f"{index:03d}-{name}"
            shutil.copy2(source, target)
            counts[role] += 1
            relative = str(target.relative_to(product_dir)).replace("\\", "/")
            stored.append(relative)
            seen_hashes[identity] = relative
            if isinstance(entry, dict):
                entry["stored_path"] = relative

    for tmp in temp_files:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return counts, warnings, stored


def _write_manifest(
    product_dir: Path,
    *,
    product_id: str,
    collection_id: str,
    source_url: str,
    source_kind: str,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    input_dir = product_dir / "input"
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        files.append(
            {
                "path": str(path.relative_to(product_dir)).replace("\\", "/"),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "collection_id": collection_id,
        "source_kind": source_kind,
        "source_url": source_url,
        "created_at": now_iso(),
        "file_count": len(files),
        "files": files,
    }
    _write_json(product_dir / "input" / "source-manifest.json", manifest)
    return manifest


def ingest_capture(
    products_root: Path | str,
    payload: Mapping[str, Any],
    *,
    allow_new_version: bool = False,
    source_kind: str = SOURCE_KIND_WORKBENCH,
) -> dict[str, Any]:
    """入库一次采集，返回与原项目形状一致的摘要。重复采集抛 :class:`DuplicateCaptureError`。"""
    root = Path(products_root)
    root.mkdir(parents=True, exist_ok=True)
    normalized = normalize_payload(payload)

    existing = find_existing_capture(root, normalized["source_url"])
    duplicate_of = None
    version = 1
    if existing is not None:
        if not allow_new_version:
            raise DuplicateCaptureError(existing["product_id"], normalized["source_url"])
        duplicate_of = existing["product_id"]
        version = int(existing.get("version") or 1) + 1

    product_id = allocate_product_id(root)
    product_dir = root / product_id
    (product_dir / "input").mkdir(parents=True, exist_ok=True)
    (product_dir / "output").mkdir(parents=True, exist_ok=True)

    collection_id = new_collection_id()
    warnings: list[str] = []

    collect_all = normalized["collection_mode"] == "all_skus"
    image_counts, image_warnings, stored_images = _copy_images(
        product_dir, normalized["images"], preserve_roles=collect_all
    )
    warnings.extend(image_warnings)
    sku_images = {entry.get("sku_id"): entry.get("stored_path") for entry in normalized["images"]["sku"]}
    for sku in normalized["skus"]:
        if sku_images.get(sku["sku_id"]):
            sku["image_path"] = sku_images[sku["sku_id"]]

    source_payload = {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "source_kind": source_kind,
        "collection_id": collection_id,
        "source_url": normalized["source_url"],
        "offer_id": normalized["offer_id"],
        "title_zh": normalized["title_zh"],
        "captured_at": normalized["captured_at"],
        "ingested_at": now_iso(),
        "version": version,
        "duplicate_of": duplicate_of,
        "skus": normalized["skus"],
        "collection_mode": normalized["collection_mode"],
        "sku_selection_required": collect_all,
        "selected_category": normalized["category"],
        "images": {role: count for role, count in image_counts.items()},
        "stored_images": stored_images,
        "extra": normalized["extra"],
    }
    attributes = normalized.get("attributes_zh") or {}
    if attributes.get("raw") or len(attributes) > 1:
        source_payload["attributes_zh"] = attributes
    if normalized.get("description_zh"):
        source_payload["description_zh"] = normalized["description_zh"]
    keywords = normalized.get("keywords") or []
    if keywords:
        source_payload["keywords"] = keywords
        source_payload["keyword_source"] = normalized.get("keyword_source") or "collection_plan"
        if isinstance(normalized.get("keyword_category"), Mapping):
            source_payload["keyword_category"] = normalized["keyword_category"]
    _write_json(product_dir / "input" / "source.json", source_payload)

    # 带上关键词的采集：直接把选词写进 input/selected-keywords.json，商品天然与关键词库对齐
    if keywords:
        try:
            from pipeline.selection import set_selected_keywords

            set_selected_keywords(product_dir, keywords, source=source_payload["keyword_source"])
        except Exception as error:  # noqa: BLE001 - 选词失败不该让采集入库失败
            warnings.append(f"关键词写入选词文件失败（可手工补）：{error}")
        else:
            warnings.append(f"已带上 {len(keywords)} 个关键词（来源：{source_payload['keyword_source']}），可直接跑文案")
    _write_json(
        product_dir / "input" / "raw-snapshot.json",
        {
            "schema_version": SCHEMA_VERSION,
            "product_id": product_id,
            "collection_id": collection_id,
            "captured_at": normalized["captured_at"],
            "raw": normalized["raw"],
        },
    )
    if normalized["category"]:
        _write_json(
            product_dir / "input" / "category-selection.json",
            {
                "schema_version": SCHEMA_VERSION,
                "product_id": product_id,
                **normalized["category"],
            },
        )
    else:
        warnings.append("尚未选择 Ozon 类目，请在工作台选择真实上架类目")
    if collect_all:
        warnings.append("全部规格已保存，尚未确认上架规格；请在工作台选择后再启动流程")

    manifest = _write_manifest(
        product_dir,
        product_id=product_id,
        collection_id=collection_id,
        source_url=normalized["source_url"],
        source_kind=source_kind,
    )

    status = product_status.new_status(
        product_id,
        collection_id=collection_id,
        source_url=normalized["source_url"],
        status="COLLECTED",
    )
    product_status.save_status(product_dir, status)

    return {
        "product_id": product_id,
        "status": "COLLECTED",
        "collection_id": collection_id,
        "version": version,
        "duplicate_of": duplicate_of,
        "source_path": f"products/{product_id}/input/source.json",
        "status_path": f"products/{product_id}/status.json",
        "warnings": warnings,
        "ozon_category": normalized["category"],
        "sku_selection_required": collect_all,
        "counts": {
            "skus": len(normalized["skus"]),
            "attributes": len(normalized["extra"].get("attributes") or []) if normalized["extra"] else 0,
            "main_images": image_counts["main"],
            "sku_images": image_counts["sku"],
            "detail_images": image_counts["detail"],
            "manifest_files": manifest["file_count"],
        },
    }


# --------------------------------------------------------------------- 文件夹导入


def build_payload_from_folder(
    folder: Path | str,
    *,
    source_url: str | None = None,
    category: Mapping[str, Any] | None = None,
    skus: Sequence[Mapping[str, Any]] | None = None,
    title_zh: str | None = None,
) -> dict[str, Any]:
    """把本地文件夹组织成采集载荷。

    文件夹约定（都可缺省）：``main-images/``、``sku-images/``、``detail-images/``，
    以及可选的 ``product.json``（提供 source_url / skus / category / title_zh）。
    命令行参数优先于 ``product.json``。
    """
    base = Path(folder)
    if not base.is_dir():
        raise CaptureValidationError(f"文件夹不存在：{base}")
    descriptor = _read_json(base / "product.json")

    resolved_url = source_url or descriptor.get("source_url")
    resolved_skus = skus or descriptor.get("skus")
    resolved_category = category or descriptor.get("category")
    resolved_title = title_zh or descriptor.get("title_zh")
    # 选品清单里的关键词可以写在 product.json 里（采集时"带上这个词"）
    resolved_keywords = descriptor.get("keywords") or descriptor.get("source_keywords")
    resolved_keyword_category = descriptor.get("keyword_category")

    images: dict[str, list[dict[str, Any]]] = {"main": [], "sku": [], "detail": []}
    for role, relative in IMAGE_DIRS.items():
        directory = base / Path(relative).name
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
                    images[role].append({"path": str(path), "name": path.name})
    flat = base / "images"
    if flat.is_dir():
        for path in sorted(flat.iterdir()):
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
                images["main"].append({"path": str(path), "name": path.name})

    payload: dict[str, Any] = {
        "source_url": resolved_url,
        "skus": list(resolved_skus or []),
        "category": resolved_category,
        "title_zh": resolved_title,
        "images": images,
        "captured_at": now_iso(),
        "raw": {"imported_from": str(base), "descriptor": descriptor or None},
    }
    if resolved_keywords:
        payload["keywords"] = resolved_keywords
        payload["keyword_source"] = descriptor.get("keyword_source") or "collection_plan"
    if isinstance(resolved_keyword_category, Mapping):
        payload["keyword_category"] = resolved_keyword_category
    return payload


def import_folder(
    products_root: Path | str,
    folder: Path | str,
    *,
    source_url: str | None = None,
    category: Mapping[str, Any] | None = None,
    skus: Sequence[Mapping[str, Any]] | None = None,
    title_zh: str | None = None,
    allow_new_version: bool = False,
    keywords: Sequence[Any] | None = None,
    keyword_source: str = "collection_plan",
) -> dict[str, Any]:
    payload = build_payload_from_folder(
        folder, source_url=source_url, category=category, skus=skus, title_zh=title_zh
    )
    if keywords:
        payload["keywords"] = [
            dict(item) if isinstance(item, Mapping) else str(item) for item in keywords
        ]
        payload["keyword_source"] = keyword_source
    return ingest_capture(products_root, payload, allow_new_version=allow_new_version)


# --------------------------------------------------------------------- CLI


def _parse_sku_flags(values: Sequence[str] | None) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for item in values or []:
        if ":" not in item:
            raise SystemExit(f"--sku 需要 <sku_id>:<采购价> 形式，收到：{item}")
        sku_id, _, price = item.partition(":")
        skus.append({"sku_id": sku_id.strip(), "purchase_price_cny": _number(price)})
    return skus


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把本地文件夹导入为采集商品（1688 上品工作台）")
    parser.add_argument("--folder", required=True, help="采集素材文件夹")
    parser.add_argument("--products-root", default="products")
    parser.add_argument("--source-url", default=None, help="覆盖 product.json 里的 1688 链接")
    parser.add_argument("--category-id", default=None)
    parser.add_argument("--type-id", default=None)
    parser.add_argument("--category-path-zh", default=None)
    parser.add_argument("--title-zh", default=None)
    parser.add_argument("--sku", action="append", dest="skus", help="<sku_id>:<采购价>，可重复")
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords",
        help="来自选品清单的关键词（可重复）：会写进 source.json 与 selected-keywords.json",
    )
    parser.add_argument("--keyword-source", default="collection_plan")
    parser.add_argument("--new-version", action="store_true", help="同一 offer 已存在时新建版本")
    args = parser.parse_args(argv)

    category = None
    if args.category_id or args.type_id:
        category = {
            "category_id": args.category_id,
            "type_id": args.type_id,
            "category_path_zh": args.category_path_zh,
        }

    try:
        summary = import_folder(
            args.products_root,
            args.folder,
            source_url=args.source_url,
            category=category,
            skus=_parse_sku_flags(args.skus) or None,
            title_zh=args.title_zh,
            allow_new_version=args.new_version,
            keywords=args.keywords or None,
            keyword_source=args.keyword_source,
        )
    except DuplicateCaptureError as error:
        print(json.dumps({"ok": False, **error.to_dict()}, ensure_ascii=False, indent=2))
        return 2
    except CaptureValidationError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
