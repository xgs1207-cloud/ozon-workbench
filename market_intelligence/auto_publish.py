"""Conservative per-session automatic card submission.

There is no Ozon sandbox. The session opt-in, server arm switch, artifact-bound
human approvals, read-only preflight, and one-shot claim all have to pass.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from pipeline.context import write_json
from pipeline.guided_review import status as review_status
from pipeline.preflight import preflight
from pipeline.stores import ensure_registry, list_shops
from pipeline.upload import (UPLOAD_MODE_PRODUCTION, build_upload_payload,
                             payload_problems, upload_product)

from .sessions import (claim_publish, finish_publish, get_session,
                       list_publish_attempts)


def process_ready(path: Path | str, products_root: Path | str, *, session_id: str,
                  preflight_fn: Callable[..., dict[str, Any]] = preflight,
                  uploader: Any | None = None, publisher: Any | None = None) -> dict[str, Any]:
    if os.environ.get("WORKBENCH_AUTO_PUBLISH_ARMED") != "1":
        raise ValueError("服务器尚未启用自动发布总开关；不会发送任何 Ozon 写请求")
    session = get_session(path, session_id)
    if not session["auto_publish_enabled"]:
        raise ValueError("这个选词批次没有开启自动发布")
    store_id = session["target_store_id"]
    shops = {str(shop.get("id")): shop for shop in list_shops(ensure_registry(None))}
    shop = shops.get(store_id)
    if not shop or not shop.get("enabled"):
        raise ValueError("目标店铺已停用")

    prior = {item["product_id"] for item in list_publish_attempts(path, session_id)}
    results = []
    for item in session["products"]:
        product_id = item["product_id"]
        directory = Path(products_root) / product_id
        if product_id in prior:
            results.append({"product_id": product_id, "status": "already_attempted"})
            continue
        review = review_status(directory)
        if not review["ready_to_preflight"]:
            results.append({"product_id": product_id, "status": "waiting_review", "blockers": review["blockers"]})
            continue
        try:
            # Generated images are not useful to Ozon until publicly reachable.
            # This is still before claim_publish, so an object-storage failure
            # never consumes the one permitted Ozon submission attempt.
            if publisher is None:
                from pipeline.oss_cos import _storage_from_env

                current_publisher = _storage_from_env()
            else:
                current_publisher = publisher
            publication = current_publisher.publish_product(directory)
            if publication.get("missing") or not publication.get("https_ok"):
                results.append({"product_id": product_id, "status": "blocked_images",
                                "blockers": ["有图片未发布成功，或公开地址不是 HTTPS"]})
                continue
            payload = build_upload_payload(
                directory, shop_name=store_id, upload_mode=UPLOAD_MODE_PRODUCTION,
                currency_code=str(shop.get("default_currency_code") or "RUB").upper(),
            )
            issues = payload_problems(payload, upload_mode=UPLOAD_MODE_PRODUCTION)
            if issues:
                results.append({"product_id": product_id, "status": "blocked_preflight", "blockers": issues[:8]})
                continue
            write_json(directory / "output" / "store-runs" / store_id / "payload.json", payload)
            report = preflight_fn(directory, shop=store_id)
            if not report.get("ok"):
                results.append({"product_id": product_id, "status": "blocked_preflight",
                                "blockers": report.get("production_blockers") or report.get("problems") or []})
                continue
        except Exception as error:  # noqa: BLE001 - preflight must never become a write
            results.append({"product_id": product_id, "status": "blocked_preflight", "blockers": [str(error)]})
            continue

        if not claim_publish(path, session_id=session_id, product_id=product_id, store_id=store_id):
            results.append({"product_id": product_id, "status": "already_attempted"})
            continue
        try:
            if uploader is None:
                from pipeline.ozon_write import OzonWriteUploader

                current_uploader = OzonWriteUploader()
            else:
                current_uploader = uploader
            report = upload_product(
                directory, [store_id], current_uploader, upload_mode=UPLOAD_MODE_PRODUCTION,
                enabled_store_ids=[store_id],
            )
            state = "submitted" if report.get("submitted") == 1 else "failed_manual_review"
            finish_publish(path, product_id=product_id, state=state, result=report)
            results.append({"product_id": product_id, "status": state, "report": report})
        except Exception as error:  # noqa: BLE001 - outcome may be ambiguous; never auto retry
            finish_publish(path, product_id=product_id, state="failed_manual_review", result={"error": str(error)})
            results.append({"product_id": product_id, "status": "failed_manual_review", "error": str(error)})

    return {"session_id": session_id, "store_id": store_id, "results": results,
            "submitted": sum(item["status"] == "submitted" for item in results)}
