"""Explicit original-image adoption, with sealed capture and exact-byte receipts.

Originals are never relabelled as AI generations. This module performs no
network, model, Ozon or object-storage calls.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .listing_form import read_json

CAPTURED_GENERATOR = "captured-original"


def capture_proof(directory: Path | str, source_path: str, *, source_sku_id: str | None = None) -> dict[str, Any]:
    root = Path(directory).resolve()
    from .image_jobs import _reference_owners, _selected
    selected = set(_selected(root))
    pure = PurePosixPath(source_path)
    path = (root / source_path).resolve()
    if (pure.is_absolute() or ".." in pure.parts or "\\" in source_path or ":" in source_path
            or pure.parent.parent != PurePosixPath("input")
            or pure.parent.name not in {"main-images", "sku-images", "detail-images"}
            or not path.is_relative_to(root / "input") or not path.is_file()
            or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
        raise ValueError("原图必须来自此商品真实采集的图片目录")
    manifest_path = root / "input/source-manifest.json"
    manifest = read_json(manifest_path)
    def sealed(relative: str) -> tuple[str, int]:
        local = (root / relative).resolve()
        rows = [row for row in manifest.get("files") or [] if isinstance(row, Mapping) and row.get("path") == relative]
        if len(rows) != 1 or not local.is_relative_to(root) or not local.is_file():
            raise ValueError("原图或采集资料缺少唯一原始封存记录")
        body = local.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if rows[0].get("sha256") != digest or rows[0].get("bytes") != len(body):
            raise ValueError("原图或采集资料已变化，未通过原始封存校验；请重新采集")
        return digest, len(body)
    source_digest, _ = sealed("input/source.json")
    digest, size = sealed(source_path)
    source = read_json(root / "input/source.json")
    if source_path not in (source.get("stored_images") or []):
        raise ValueError("原图不在当前商品的已封存采集清单中")
    owners = _reference_owners(source, source_path)
    if owners and not owners.intersection(selected):
        raise ValueError("原图明确属于未选规格，请选择已确认的上架规格图片")
    from .reference_images import selected_reference_images
    if source_path not in {row["path"] for row in selected_reference_images(root)}:
        raise ValueError("原图缺少明确规格关联，请选择已确认规格的图片")
    if source_sku_id and (source_sku_id not in selected or (owners and source_sku_id not in owners)):
        raise ValueError("原图与此图位的上架规格不一致")
    if owners and source_sku_id is None:
        raise ValueError("此原图已明确绑定规格，请指定适用的上架规格，不作为全部规格共享图")
    from PIL import Image
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError
            if path.suffix.lower() not in {"PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}, "WEBP": {".webp"}}[image.format]:
                raise ValueError
            image.verify()
        # JPEG verify() only checks the container; decode locally to ensure the
        # sealed bytes are a real readable picture, not a header-only stub.
        with Image.open(path) as image:
            image.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ValueError("采集原图不是格式与扩展名一致的可读取 PNG/JPEG/WebP 图片") from None
    return {"version": 1, "origin": "captured", "source_path": source_path, "source_sha256": digest,
            "source_size": size, "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "source_file_sha256": source_digest, "collection_id": source.get("collection_id"),
            "source_product_id": source.get("product_id"), "workspace_product_id": root.name,
            "source_sku_ids": sorted(owners), "assigned_sku_id": source_sku_id}


def validate_captured_image(directory: Path | str, spec: Mapping[str, Any], record: Mapping[str, Any]) -> list[str]:
    root = Path(directory).resolve()
    proof = record.get("capture_receipt")
    if (record.get("generator") != CAPTURED_GENERATOR or record.get("origin") != "captured"
            or not isinstance(proof, Mapping) or spec.get("origin") != "captured"):
        return ["原图来源声明或封存凭据缺失；不能把采集图伪称 AI 生成"]
    try:
        current = capture_proof(root, str(proof.get("source_path") or ""), source_sku_id=spec.get("source_sku_id"))
        if dict(proof) != current or spec.get("capture_receipt") != current:
            raise ValueError("采集原图来源或规格归属发生变化，请重新采用并审核")
        relative = str(spec.get("output_path") or "")
        target = (root / relative).resolve()
        if not target.is_relative_to(root / "output/generated-images") or not target.is_file():
            raise ValueError("原图工作副本不在允许的商品图片区")
        body = target.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if (digest != current["source_sha256"] or len(body) != current["source_size"]
                or record.get("sha256") != digest or record.get("bytes") != len(body)):
            raise ValueError("原图工作副本与真实封存原图不一致，请重新采用并审核")
    except (ValueError, OSError) as error:
        return [str(error)]
    return []
