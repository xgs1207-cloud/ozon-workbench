"""Populated desktop UI fixture. All records and wires are synthetic and isolated.

Start with ``python -m uvicorn tests.studio_browser_fixture:app --host 127.0.0.1 --port 8771``.
This module is not registered in the production app. Model jobs are visual fixtures;
publishing and public-media uploads are deliberately unavailable.
"""
from copy import deepcopy
from functools import lru_cache
from io import BytesIO
import importlib.util
from pathlib import Path
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from PIL import Image, ImageDraw, ImageFont


_spec = importlib.util.spec_from_file_location(
    "_studio_owned_operations_fixture", Path(__file__).with_name("operations_browser_fixture.py")
)
operations = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(operations)
app = FastAPI(title="离线设计工作台验收（无真实模型或商品写入）")
TEMP = operations.TEMP
RUNTIME = operations.RUNTIME
CALLS = []
PRODUCT = "P000007"
CATEGORY = {"shop_id": "qa-a", "category_id": 100, "type_id": 200,
            "confirmed_by_user": True, "source": "ozon_seller_api",
            "category_path": ["厨房用品", "炊具", "铸铁锅"],
            "category_path_zh": "厨房用品 / 炊具 / 铸铁锅"}
TITLE = "离线样本 · 南瓜造型珐琅铸铁锅 24cm / 3.5L"
COPY = {"title_ru": "Кастрюля чугунная с эмалевым покрытием, 24 см, 3,5 л, красная",
        "description_ru": "🎃 Выразительная форма тыквы\nКрасная кастрюля с белой внутренней эмалью.\n\n🍲 Для домашней кухни\nОбъём 3,5 л, диаметр 24 см.\n\n✨ Продуманные детали\nДве боковые ручки и крышка с серебристой ручкой.",
        "hashtags": ["#кастрюля", "#чугун", "#кухня"]}
SKUS = [{"sku_id": "red24", "sku_name": "红色/内白 24cm/3.5L", "listed": True,
         "purchase_price_cny": 110, "image_path": "images/sku/red.png", "collection_issues": [],
         "option_values": [{"name": "颜色", "value": "红色/内白"}, {"name": "规格", "value": "24cm/3.5L"}]},
        {"sku_id": "orange24", "sku_name": "南瓜橙/内白 24cm/3.5L", "listed": False,
         "purchase_price_cny": 110, "image_path": "images/sku/orange.png", "collection_issues": [],
         "option_values": [{"name": "颜色", "value": "南瓜橙/内白"}, {"name": "规格", "value": "24cm/3.5L"}]}]
FIELDS = [{"attribute_id": 9048, "attribute_name": "型号名称（针对合并为一张商品卡片）", "required": True, "control": "text"},
          {"attribute_id": 10096, "attribute_name": "商品颜色", "required": True, "control": "dictionary", "dictionary_id": 1, "is_aspect": True},
          {"attribute_id": 5014, "attribute_name": "材料", "required": True, "control": "dictionary", "dictionary_id": 2},
          {"attribute_id": 107, "attribute_name": "容量，升", "control": "decimal"},
          {"attribute_id": 108, "attribute_name": "直径，厘米", "control": "decimal"},
          {"attribute_id": 109, "attribute_name": "适用炉灶", "control": "text"},
          {"attribute_id": 110, "attribute_name": "护理说明", "control": "text"},
          {"attribute_id": 11254, "attribute_name": "JSON 富内容", "control": "text"}]
FORM = {"shop_id": "qa-a", "category_id": 100, "type_id": 200,
        "scope": "P000007:qa-a:100:200", "category_path": CATEGORY["category_path"],
        "fetched_at": "2026-10-08T09:00:00+08:00", "source": "ozon_seller_api", "fields": FIELDS}
DETAILS = {"material": "铸铁，内壁珐琅", "package_quantity": 1,
           "product_length_mm": 240, "product_width_mm": 240, "product_height_mm": 150,
           "product_weight_g": 5300, "package_length_mm": 340, "package_width_mm": 290,
           "package_height_mm": 220, "package_weight_g": 5800}
ATTRIBUTES = {"9048": [{"value": "WB-OFFLINE-PUMPKIN"}],
              "5014": [{"value": "Чугун", "dictionary_value_id": 502}],
              "107": [{"value": "3.5"}], "108": [{"value": "24"}]}
PER_SKU = {"red24": {"10096": [{"value": "Красный", "dictionary_value_id": 101}]},
           "orange24": {"10096": [{"value": "Оранжевый", "dictionary_value_id": 102}]}}
POINTS = ["南瓜造型与红色外观，适合突出厨房陈列氛围", "24cm / 3.5L 规格，清晰展示容量与大小", "铸铁主体与白色珐琅内壁", "双耳把手与银色提手，展示细节"]
ANALYSIS = {"status": "ready", "confirmed": True,
            "payload": {"facts": {"title_cn": TITLE, "material": "铸铁，内壁珐琅", "dimensions": "直径 24cm", "features": "南瓜造型、双耳把手", "package_quantity": 1},
                        "selling_points": [{"text_zh": text, "point_cn": text} for text in POINTS], "unknowns": []},
            "display_zh": {"status": "ready", "selling_points": [{"text": text} for text in POINTS]}}
SLOTS = [{"slot": "offline-main", "role": "variant_main", "source_sku_id": "red24", "workspace": "single",
          "purpose": "红色 / 内白独立主图", "prompt": "保留真实红色南瓜造型，简洁白色背景，以侧光展示珐琅表面与银色提手。",
          "guidance_cn": "突出南瓜造型，保持本规格红色外观与真实白色内壁。", "reference_image_ids": ["sku-red"],
          "output_path": "generated/main.png"},
         {"slot": "offline-detail", "role": "detail", "source_sku_id": "red24", "workspace": "single",
          "purpose": "珐琅内壁与把手细节", "prompt": "近景展示白色珐琅内壁与双耳把手，不添加未知参数。",
          "reference_image_ids": ["detail-interior"], "output_path": "generated/detail.png"},
         {"slot": "offline-failed", "role": "detail", "source_sku_id": "red24", "workspace": "single",
          "purpose": "失败恢复示例（无模型调用）", "prompt": "保留商品真实外观。", "reference_image_ids": ["sku-red"]},
         {"slot": "offline-set-main", "role": "variant_main", "source_sku_id": "red24", "workspace": "set", "set_id": "offline-set-1",
          "purpose": "套图主图", "prompt": "保持统一简洁背景，展示本规格红色南瓜锅。", "reference_image_ids": ["sku-red"],
          "output_path": "generated/set-main.png"}]


def initial_state():
    references = [{"id": "sku-red", "path": "images/sku/red.png", "role": "sku", "source_sku_id": "red24"},
                  {"id": "detail-interior", "path": "images/detail/interior.png", "role": "detail"},
                  {"id": "main-red", "path": "images/main/red.png", "role": "main"}]
    return {"skus": deepcopy(SKUS), "category": deepcopy(CATEGORY), "details": deepcopy(DETAILS),
            "attributes": deepcopy(ATTRIBUTES), "per_sku": deepcopy(PER_SKU), "copy": deepcopy(COPY),
            "analysis": deepcopy(ANALYSIS), "slots": deepcopy(SLOTS), "references": references,
            "selected_slots": ["offline-main", "offline-detail"], "offers": {"red24": "xzj.jp.10.8.1"},
            "prices": {"red24": {"price": 260, "currency": "CNY"}},
            "jobs": [{"id": "offline-job-1", "slot": "offline-main", "status": "completed", "stage": "complete"},
                     {"id": "offline-job-2", "slot": "offline-failed", "status": "failed", "error": "离线失败恢复样本：未向模型发起请求"}],
            "rich": {"attribute_id": 11254, "revision": 1, "context_fingerprint": "offline-fixture-context",
                     "blocks": [{"id": "offline-rich-1", "type": "image_text", "image_id": "rich-main",
                                 "title": "Форма тыквы", "text": "Кастрюля чугунная, 24 см, 3,5 л. Красный цвет и белая внутренняя эмаль."}],
                     "media": [{"id": "rich-main", "label": "已生成的红色锅主图", "kind": "generated",
                                "preview_url": f"/api/workbench/products/{PRODUCT}/media/generated/main.png", "origin": "generated"},
                               {"id": "rich-detail", "label": "已生成的内壁细节", "kind": "generated",
                                "preview_url": f"/api/workbench/products/{PRODUCT}/media/generated/detail.png", "origin": "generated"}]}}


STATE = initial_state()
LIBRARIES = {}
PROMPTS = [{"id": "offline-prompt", "name": "简洁白底主图", "prompt": "保持商品真实造型和颜色，用简洁白色背景与柔和侧光突出商品。"}]


def selected_skus():
    return [{"sku_id": row["sku_id"], "id": row["sku_id"], "name": row["sku_name"],
             "image_path": row["image_path"], "option_values": row["option_values"],
             "offer_id": STATE["offers"].get(row["sku_id"]),
             "manual_price": STATE["prices"].get(row["sku_id"])}
            for row in STATE["skus"] if row["listed"]]


def media():
    slots = STATE["slots"]
    generated = [row["output_path"] for row in slots if row.get("output_path")]
    return {"ok": True, "offline": True, "image_plan": {"main_images": [r for r in slots if r["role"] == "variant_main"],
            "detail_images": [r for r in slots if r["role"] != "variant_main"], "reference_images": STATE["references"],
            "selected_slots": STATE["selected_slots"]}, "generated_image_paths": generated,
            "captured_reference_images": STATE["references"], "image_generation": {"files": [{"slot": row["slot"], "path": row["output_path"], "generation_id": "offline"} for row in slots if row.get("output_path")]},
            "jobs": STATE["jobs"], "sets": [{"id": "offline-set-1", "name": "离线厨房套图", "completed": 1, "total": 1, "slots": ["offline-set-main"]}],
            "workspaces": {}}


def guided():
    m = media()
    return {"source": {"title_zh": TITLE, "source_url": "https://detail.1688.com/offer/1053070588316.html",
                       "offer_id": "1053070588316", "skus": STATE["skus"], "stored_images": [r["path"] for r in STATE["references"]]},
            "category_selection": STATE["category"], "manual_prices": {"prices": STATE["prices"]},
            "measurements": {"product": {}, "package": {}}, "human_confirmations": {"material": "铸铁，内壁珐琅", "package_quantity": 1},
            "analysis": {"recommendation": {"decision": "continue", "reason": "仅用于离线 UI 验收"}},
            "workflow": {"analysis": STATE["analysis"], "copy": {"status": "ready", "confirmed": True, "selected": True, "selected_id": "offline-copy-1",
                        "candidates": [{"id": "offline-copy-1", "mode": "search_first", **STATE["copy"]}]},
                        "preparation_blockers": [], "publication_blockers": [], "deferred_risks": []},
            "selected_keywords": {"keywords": [{"keyword": "кастрюля чугунная", "role": "core"}, {"keyword": "кастрюля тыква", "role": "secondary"}]},
            "copy": STATE["copy"], "grouping": {"upload_strategy": "separate_cards", "platform_card_count": len(selected_skus()), "reason": "离线规格分组样本"},
            "attributes": {}, "card_ready": True,
            "review": {"sections": {key: {"approved": True, "problems": []} for key in ["grouping", "copy", "image_plan", "images", "fields"]}, "blockers": [], "ready_to_preflight": True},
            "image_backend": {"name": "offline", "label": "离线验收（不调用模型）", "model": "fixture", "configured": True,
                              "produces_final_images": True, "aspect_ratio": "3:4", "image_size": "2k", "normalized_size": "900×1200"},
            "image_plan": m["image_plan"], "image_generation": m["image_generation"], "generated_image_paths": m["generated_image_paths"],
            "captured_reference_images": STATE["references"], "image_jobs": STATE["jobs"], "media_sets": m["sets"], "media_workspaces": {},
            "media_selection": {"videos": []}, "image_insights": {"status": "ready", "selling_points": [{"text_zh": p, "image_howto_zh": "近景与侧光呈现真实商品细节。"} for p in POINTS],
                                                                   "visible_observations": ["红色南瓜形锅身与白色内壁", "两侧把手、银色锅盖提手"], "features": POINTS},
            "image_qc": {"decision": "pass", "score": 100, "critical_failures": []},
            "qc": {"ok": True, "errors": [], "warnings": []}}


def document():
    return {"product_id": PRODUCT, "source_title": TITLE, "selected_skus": selected_skus(), "summary": STATE["analysis"],
            "copy": {**STATE["copy"], "confirmed": True}, "offer_ids": {"complete": True, "offers": STATE["offers"]},
            "card": {"category": STATE["category"], "field_display": {}, "form": FORM,
                     "attributes": STATE["attributes"], "per_sku_attributes": STATE["per_sku"]},
            "operational_fields": {"package": {key: STATE["details"].get("package_" + key)
                                               for key in ["weight_g", "length_mm", "width_mm", "height_mm"]}},
            "missing": {"validation_errors": []},
            "media": media(), "official_excluded_attribute_ids": [4191, 23171]}


def library_identity(request, response):
    identity = request.cookies.get("offline-studio-employee")
    if not identity or identity not in LIBRARIES:
        identity = str(uuid.uuid4())
        LIBRARIES[identity] = [{"id": "offline-term-1", "category": "厨房 / 铸铁锅", "text": "кастрюля чугунная", "note": "主关键词，完整短语前置"},
                               {"id": "offline-term-2", "category": "厨房 / 铸铁锅", "text": "кастрюля тыква", "note": "形状词，仅用于对应商品"},
                               {"id": "offline-term-3", "category": "玩具 / 解压", "text": "антистресс игрушка", "note": "离线分类样本"}]
        response.set_cookie("offline-studio-employee", identity, httponly=True, samesite="strict")
    return LIBRARIES[identity]


@app.get("/health")
def health():
    return {"ok": True, "offline_fixture": True, "provider_network_enabled": False}


@app.get("/api/collector/products")
def products():
    return {"items": [{"product_id": PRODUCT, "title_zh": TITLE, "sku_count": 2, "status": "collected",
                       "thumbnail_url": f"/api/workbench/products/{PRODUCT}/media/images/sku/red.png",
                       "source_url": "https://detail.1688.com/offer/1053070588316.html"},
                      {"product_id": "P000008", "title_zh": "离线样本 · 柔软解压小玩具，多色可选", "sku_count": 3, "status": "collected"}]}


@app.api_route("/api/workbench/keyword-library", methods=["GET", "POST"])
@app.api_route("/api/workbench/keyword-library/{item_id}", methods=["PUT", "DELETE"])
async def keywords(request: Request, response: Response, item_id: str = ""):
    response.headers["Cache-Control"] = "private, no-store"
    rows = library_identity(request, response)
    if request.method == "GET":
        category, query = request.query_params.get("category", ""), request.query_params.get("q", "").lower()
        found = [row for row in rows if (not category or row["category"] == category)
                 and (not query or query in " ".join((row["text"], row["note"])).lower())]
        offset, limit = int(request.query_params.get("offset", 0)), int(request.query_params.get("limit", 50))
        return {"ok": True, "items": found[offset:offset+limit], "total": len(found), "categories": sorted({r["category"] for r in rows})}
    if request.method == "DELETE":
        rows[:] = [row for row in rows if row["id"] != item_id]
        return {"ok": True, "offline": True}
    payload = await request.json()
    if not payload.get("category", "").strip() or not payload.get("text", "").strip():
        raise HTTPException(422, "类目和关键词必填")
    record = {key: payload.get(key, "") for key in ("category", "text", "note")}
    record["id"] = item_id or "offline-term-" + str(uuid.uuid4())
    if item_id:
        if not any(row["id"] == item_id for row in rows):
            raise HTTPException(404)
        rows[:] = [record if row["id"] == item_id else row for row in rows]
    else:
        rows.append(record)
    return {"ok": True, "item": record, "offline": True}


@app.get("/api/workbench/offer-prefix")
def prefix():
    return {"profile": {"prefix": "xzj.jp", "saved": True, "updated_at": "2026-10-08T09:00:00+08:00"}}


@app.get("/api/workbench/image-prompts")
def prompts():
    return {"items": PROMPTS}


@app.get("/api/workbench/warehouses")
@app.post("/api/workbench/warehouses/refresh")
async def warehouses(request: Request):
    payload = await request.json() if request.method == "POST" else {}
    shop = payload.get("shop") or request.query_params.get("shop") or "qa-a"
    if shop not in {row["id"] for row in operations.authorized_shops()}:
        raise HTTPException(404, "未知的离线店铺")
    return {"ok": True, "shop": shop, "complete": True,
            "items": [{"warehouse_id": "offline-warehouse", "name": "离线验收仓库", "eligible": True}]}


@app.get("/api/ozon/categories")
def categories():
    return {"items": [{"category_id": 100, "type_id": 200, "path": CATEGORY["category_path"]}], "source": "offline_fixture"}


@app.get("/api/ozon/category-values")
def dictionary(request: Request):
    values = [{"id": 101, "value": "Красный"}, {"id": 102, "value": "Оранжевый"}] if request.query_params.get("attribute_id") == "10096" else [{"id": 502, "value": "Чугун"}, {"id": 503, "value": "Эмаль"}]
    return {"items": values, "has_next": False, "last_value_id": values[-1]["id"]}


@app.get("/api/workbench/products/{product_id}/media/{media_path:path}")
def image(product_id: str, media_path: str):
    if product_id != PRODUCT or media_path not in {"images/sku/red.png", "images/sku/orange.png", "images/detail/interior.png", "images/main/red.png", "generated/main.png", "generated/detail.png", "generated/set-main.png"}:
        raise HTTPException(404)
    return Response(raster_fixture(media_path), media_type="image/png", headers={"Cache-Control": "no-store"})


@lru_cache(maxsize=7)
def raster_fixture(media_path: str):
    """Draw synthetic fixture pixels in memory; no image files or provider calls."""
    color = "#DB7946" if "orange" in media_path else "#C9474E"
    subtitle = "24cm / 3.5L" if "main" in media_path else "ENAMEL / CAST IRON"
    bitmap = Image.new("RGB", (900, 1200), "#F4F0EC")
    draw = ImageDraw.Draw(bitmap)
    draw.ellipse((185, 822, 715, 898), fill="#DDD3CB")
    draw.rounded_rectangle((205, 485, 695, 850), radius=145, fill=color)
    draw.arc((130, 550, 252, 665), start=70, end=285, fill=color, width=30)
    draw.arc((648, 550, 770, 665), start=255, end=470, fill=color, width=30)
    draw.ellipse((210, 422, 690, 588), fill="#F7EFE4", outline=color, width=20)
    draw.polygon([(260, 460), (285, 375), (365, 335), (450, 320), (535, 335), (615, 375), (640, 460)], fill=color)
    draw.ellipse((253, 435, 647, 485), fill=color)
    draw.arc((410, 275, 485, 355), start=170, end=375, fill="#ACB0AF", width=22)
    for x in (305, 405, 495, 595):
        draw.line((x, 600, x, 800), fill="#D9666D", width=8)
    try:
        title_font, note_font = ImageFont.truetype("Arial.ttf", 33), ImageFont.truetype("Arial.ttf", 18)
    except OSError:
        title_font, note_font = ImageFont.load_default(size=33), ImageFont.load_default(size=18)
    draw.text((450, 1030), subtitle, font=title_font, fill="#463D38", anchor="mm")
    draw.text((450, 1090), "OFFLINE UI FIXTURE", font=note_font, fill="#82766E", anchor="mm")
    buffer = BytesIO()
    bitmap.save(buffer, format="PNG")
    return buffer.getvalue()


@app.api_route("/api/workbench/products/{product_id}/{suffix:path}", methods=["GET", "POST", "PUT"])
async def product_route(product_id: str, suffix: str, request: Request):
    if product_id != PRODUCT:
        raise HTTPException(404, "这个离线商品没有完整编辑样本")
    CALLS.append((request.method, suffix))
    method = request.method
    body = await request.json() if method in {"POST", "PUT"} and request.headers.get("content-type", "").startswith("application/json") else {}
    if suffix in {"guided/submit", "guided/publish-media", "publications/continue"}:
        raise HTTPException(409, "离线验收禁止真实发布、媒体公开上传与库存写入")
    if suffix == "guided" and method == "GET":
        return guided()
    if suffix == "skus":
        if method == "POST":
            include = set(body.get("include", []))
            if not include or not include.issubset({row["sku_id"] for row in STATE["skus"]}):
                raise HTTPException(422, "请选择已有规格")
            for row in STATE["skus"]:
                row["listed"] = row["sku_id"] in include
        selected = [row["sku_id"] for row in STATE["skus"] if row["listed"]]
        return {"skus": STATE["skus"], "selected": selected, "has_selection": True, "active_count": len(selected), "pending_selection": False}
    if suffix == "listing-details":
        if method == "PUT":
            STATE["details"].update(body.get("details", {}))
        return {"details": STATE["details"], "provenance": {key: {"source": "manual", "evidence": "离线合成样本"} for key in STATE["details"]}}
    if suffix == "listing-form":
        if method == "PUT":
            STATE["attributes"] = body.get("attributes", {})
            STATE["per_sku"] = body.get("per_sku_attributes", {})
        return {"form": FORM, "attributes": STATE["attributes"], "per_sku_attributes": STATE["per_sku"], "selected_skus": selected_skus(), "validation_errors": [], "field_display": {}, "provenance": {"attributes": {}, "per_sku_attributes": {}}}
    if suffix == "listing-autofill":
        return {"scope": FORM["scope"], "basic_fields": {}, "attributes": {}, "per_sku_attributes": {}, "unresolved": [], "seller_offer_ids": STATE["offers"]}
    if suffix == "listing-document":
        return {"document": document()}
    if suffix == "publication-config":
        return {"ok": True, "config": {"saved": True, "shop": "qa-a", "source_url": "https://detail.1688.com/offer/1053070588316.html", "source_note": "红色/内白 24cm/3.5L", "warehouse_id": "offline-warehouse", "warehouse_name": "离线验收仓库", "stock": 100, "stock_by_sku": {}}}
    if suffix in {"listing-publications", "publications"}:
        return {"ok": True, "items": []}
    if suffix == "rich-content":
        if method == "PUT":
            STATE["rich"]["blocks"] = body.get("blocks", [])
            STATE["rich"]["revision"] += 1
        return {"ok": True, "offline": True, **STATE["rich"]}
    if suffix == "guided/media" and method == "GET":
        return media()
    if suffix == "guided/media/selection" and method == "PUT":
        allowed = {row["slot"] for row in STATE["slots"] if row.get("output_path")}
        if not set(body.get("selected_slots", [])).issubset(allowed):
            raise HTTPException(422, "只能选择已有离线结果")
        STATE["selected_slots"] = body["selected_slots"]
        return media()
    if suffix == "guided/media/confirm" and method == "POST":
        return {"ok": True, "offline": True}
    if suffix.startswith("guided/media/slots/") and method == "PUT":
        slot = suffix.split("/")[3]
        target = next((row for row in STATE["slots"] if row["slot"] == slot), None)
        if not target:
            raise HTTPException(404)
        target["prompt"] = body.get("prompt", target["prompt"])
        target["reference_image_ids"] = body.get("reference_ids", target["reference_image_ids"])
        return {"ok": True, "offline": True, "slot": target, **media()}
    if suffix in {"guided/analyze", "guided/candidates", "guided/plan", "guided/image-insights"} and method == "POST":
        # Prewritten synthetic content: no provider client, no network, no charge.
        return {"ok": True, "offline": True, "provider_called": False, "report": {"ok": True}}
    if suffix == "guided/prepare-card" and method == "POST":
        return {"ok": True, "offline": True, "report": {"ok": True, "blockers": []}}
    if suffix in {"prices", "keywords", "ozon-category", "guided/facts", "guided/copy", "guided/copy/save", "guided/candidates/choose", "guided/approve", "guided/analysis/confirm", "guided/copy/confirm"} and method != "GET":
        if suffix == "prices":
            STATE["prices"].update({row["sku_id"]: {"price": row["price"], "currency": row["currency"]} for row in body.get("prices", [])})
        if suffix in {"guided/copy", "guided/copy/save", "guided/candidates/choose"}:
            STATE["copy"].update({key: body[key] for key in COPY if key in body})
        return {"ok": True, "offline": True}
    if suffix == "preflight" and method == "POST":
        return {"ok": True, "report": {"ok": True, "offline": True, "message": "离线 UI 检查，不代表官方上架校验"}}
    raise HTTPException(404, "未实现的离线交互；不会转发到模型或真实 Seller API")


# Explicit new routes are checked first. Reuse owned temporary shop/operations
# wires, static assets and bootstrap routes, not the production application's API.
app.router.routes.extend(route for route in operations.app.router.routes
                         if getattr(route, "path", "") != "/api/collector/products")
