"""本地工作台 HTTP 接口（关键词库 + 采集入库）。

启动（与既有工作台的 8765 分开，避免抢端口）：

    uvicorn api:app --app-dir ozon-workbench --host 127.0.0.1 --port 8766

环境变量：

- ``KEYWORD_LIBRARY_ROOT``：关键词库目录（默认 ``ozon-workbench/keyword-library``）
- ``WORKBENCH_PRODUCTS_ROOT``：商品目录（默认 ``ozon-workbench/products``）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from collector.ingest import (
    CaptureValidationError,
    DuplicateCaptureError,
    import_folder,
    ingest_capture,
)
from keyword_library import store
from keyword_library.scoring import ScoreConfig

LIBRARY_ROOT = Path(
    os.environ.get("KEYWORD_LIBRARY_ROOT")
    or (Path(__file__).resolve().parent / "keyword-library")
)

PRODUCTS_ROOT = Path(
    os.environ.get("WORKBENCH_PRODUCTS_ROOT")
    or (Path(__file__).resolve().parent / "products")
)

app = FastAPI(title="ozon-workbench · local workbench", version="0.2.0")

_SCORE_FIELDS = (
    "lam",
    "min_heat_percentile",
    "max_competition_percentile",
    "min_search_volume",
    "max_competitor_count",
)


class CategoryRef(BaseModel):
    category_id: str
    type_id: str
    category_path_zh: str | None = None


class KeywordIn(BaseModel):
    keyword: str
    search_volume: float | None = None
    competitor_count: float | None = None
    ads_count: float | None = None
    cpc: float | None = None
    trend: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    category_id: str | None = None
    type_id: str | None = None
    category_path_zh: str | None = None


class IngestRequest(BaseModel):
    source: str = "seerfar"
    category: CategoryRef | None = None
    keywords: list[KeywordIn]


class ScoreRequest(BaseModel):
    lam: float | None = None
    min_heat_percentile: float | None = None
    max_competition_percentile: float | None = None
    min_search_volume: float | None = None
    max_competitor_count: float | None = None
    category_id: str | None = None
    type_id: str | None = None


class StatusRequest(BaseModel):
    keys: list[str]
    status: str
    reason: str | None = None
    product_id: str | None = None


def _config(payload: ScoreRequest | None) -> ScoreConfig:
    base = ScoreConfig()
    if payload is None:
        return base
    values = base.to_dict()
    for field in _SCORE_FIELDS:
        incoming = getattr(payload, field)
        if incoming is not None:
            values[field] = incoming
    return ScoreConfig(**values)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "library_root": str(LIBRARY_ROOT),
        "score_config": ScoreConfig().to_dict(),
    }


@app.post("/api/keywords/ingest")
def ingest(payload: IngestRequest) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in payload.keywords:
        category_id = item.category_id or (payload.category.category_id if payload.category else None)
        type_id = item.type_id or (payload.category.type_id if payload.category else None)
        if not category_id or not type_id:
            raise HTTPException(status_code=422, detail=f"关键词缺少类目：{item.keyword}")
        row = item.model_dump()
        row["category_id"] = str(category_id)
        row["type_id"] = str(type_id)
        if not row.get("category_path_zh") and payload.category:
            row["category_path_zh"] = payload.category.category_path_zh
        rows.append(row)
    if not rows:
        raise HTTPException(status_code=422, detail="keywords 为空")
    summary = store.upsert(LIBRARY_ROOT, rows, source=payload.source)
    return {"ok": True, **summary}


@app.get("/api/keywords")
def list_keywords(
    category_id: str | None = None,
    type_id: str | None = None,
    status: str | None = None,
    min_score: float | None = None,
    q: str | None = None,
    only_qualified: bool = False,
    order: str = Query("score", pattern="^(score|heat|competition|keyword)$"),
    limit: int = Query(100, ge=1, le=2000),
) -> dict[str, Any]:
    items = store.query(
        LIBRARY_ROOT,
        category_id=category_id,
        type_id=type_id,
        status=status,
        min_score=min_score,
        text=q,
        only_qualified=only_qualified,
        order=order,
        limit=limit,
    )
    return {"count": len(items), "items": items}


@app.get("/api/keywords/export")
def export_for_copy(
    category_id: str,
    type_id: str,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """给标题/简介生成用：优先已入库，其次达标，按分数降序。"""
    def collect(status: str | None, only_qualified: bool) -> list[dict[str, Any]]:
        return store.query(
            LIBRARY_ROOT,
            category_id=category_id,
            type_id=type_id,
            status=status,
            only_qualified=only_qualified,
            order="score",
            limit=limit,
        )

    chosen = collect(store.STATUS_IN_LIBRARY, False)
    if len(chosen) < limit:
        seen = {item["key"] for item in chosen}
        for item in collect(None, True):
            if item["key"] not in seen:
                chosen.append(item)
                seen.add(item["key"])
            if len(chosen) >= limit:
                break
    return {
        "category_id": category_id,
        "type_id": type_id,
        "count": len(chosen),
        "keywords": [
            {
                "keyword": item["keyword"],
                "score": item.get("score"),
                "status": item.get("status"),
                "search_volume": item.get("search_volume"),
                "competitor_count": item.get("competitor_count"),
            }
            for item in chosen
        ],
    }


@app.post("/api/keywords/score")
def rescore(payload: ScoreRequest | None = None) -> dict[str, Any]:
    config = _config(payload)
    category_id = payload.category_id if payload else None
    type_id = payload.type_id if payload else None
    if (category_id is None) != (type_id is None):
        raise HTTPException(status_code=422, detail="category_id 与 type_id 必须同时给出")
    summary = store.rescore(LIBRARY_ROOT, config, category_id=category_id, type_id=type_id)
    return {"ok": True, "score_config": config.to_dict(), **summary}


@app.post("/api/keywords/status")
def set_status(payload: StatusRequest) -> dict[str, Any]:
    try:
        summary = store.set_status(
            LIBRARY_ROOT,
            payload.keys,
            payload.status,
            reason=payload.reason,
            product_id=payload.product_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **summary}


@app.get("/api/keywords/categories")
def categories() -> dict[str, Any]:
    payload = store.index(LIBRARY_ROOT)
    return {
        "category_count": payload["category_count"],
        "total": payload["total"],
        "categories": payload["categories"],
    }


# ----------------------------------------------------------------- 采集入库（M1）


class FolderImportRequest(BaseModel):
    folder: str
    source_url: str | None = None
    category: CategoryRef | None = None
    skus: list[dict[str, Any]] | None = None
    title_zh: str | None = None
    allow_new_version: bool = False
    keywords: list[str] = Field(
        default_factory=list,
        description="来自选品清单的关键词：会写进 source.json 与 selected-keywords.json",
    )
    keyword_source: str = "collection_plan"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _product_summary(product_id: str) -> dict[str, Any]:
    directory = PRODUCTS_ROOT / product_id
    status = _read_json_file(directory / "status.json")
    source = _read_json_file(directory / "input" / "source.json")
    return {
        "product_id": product_id,
        "status": status.get("status"),
        "current_step": status.get("current_step"),
        "next_action": status.get("next_action"),
        "progress": status.get("progress"),
        "batch_id": status.get("batch_id"),
        "attention_required": status.get("attention_required"),
        "error_message": status.get("error_message"),
        "source_url": source.get("source_url"),
        "title_zh": source.get("title_zh"),
        "collection_id": source.get("collection_id"),
        "captured_at": source.get("captured_at"),
        "sku_count": len(source.get("skus") or []),
        "image_counts": source.get("images") or {},
    }


def _list_product_ids() -> list[str]:
    if not PRODUCTS_ROOT.is_dir():
        return []
    return sorted(
        path.name
        for path in PRODUCTS_ROOT.iterdir()
        if path.is_dir() and (path / "status.json").is_file()
    )


@app.post("/api/collector/products")
def collector_ingest(payload: dict[str, Any], allow_new_version: bool = False) -> dict[str, Any]:
    """采集入库：同一 offer 重复采集返回 409（不覆盖已有商品）。"""
    try:
        summary = ingest_capture(PRODUCTS_ROOT, payload, allow_new_version=allow_new_version)
    except DuplicateCaptureError as error:
        raise HTTPException(status_code=409, detail=error.to_dict()) from error
    except CaptureValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **summary}


@app.post("/api/collector/products/import-folder")
def collector_import_folder(request: FolderImportRequest) -> dict[str, Any]:
    """把本地素材文件夹导入为采集商品（不依赖浏览器插件）。"""
    category = request.category.model_dump() if request.category else None
    try:
        summary = import_folder(
            PRODUCTS_ROOT,
            request.folder,
            source_url=request.source_url,
            category=category,
            skus=request.skus,
            title_zh=request.title_zh,
            allow_new_version=request.allow_new_version,
            keywords=request.keywords or None,
            keyword_source=request.keyword_source,
        )
    except DuplicateCaptureError as error:
        raise HTTPException(status_code=409, detail=error.to_dict()) from error
    except CaptureValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **summary}


@app.get("/api/collector/products")
def collector_list(status: str | None = None) -> dict[str, Any]:
    items = [_product_summary(product_id) for product_id in _list_product_ids()]
    if status:
        items = [item for item in items if str(item.get("status") or "") == status]
    return {"count": len(items), "items": items}


@app.get("/api/collector/products/{product_id}")
def collector_detail(product_id: str) -> dict[str, Any]:
    directory = PRODUCTS_ROOT / product_id
    if not (directory / "status.json").is_file():
        raise HTTPException(status_code=404, detail=f"商品不存在：{product_id}")
    return {
        "summary": _product_summary(product_id),
        "status": _read_json_file(directory / "status.json"),
        "source": _read_json_file(directory / "input" / "source.json"),
        "manifest": _read_json_file(directory / "input" / "source-manifest.json"),
    }


# ------------------------------------------------------- 选词与文案生成（M2）


class KeywordSelectionRequest(BaseModel):
    keywords: list[Any] | None = None
    from_library: bool = False
    limit: int = Field(10, ge=1, le=50)
    category_id: str | None = None
    type_id: str | None = None
    library_root: str | None = None


class CopyRequestModel(BaseModel):
    provider: str | None = None
    steps: list[str] = Field(default_factory=lambda: ["product_analysis", "russian_copy"])


def _require_product(product_id: str) -> Path:
    directory = PRODUCTS_ROOT / product_id
    if not (directory / "status.json").is_file():
        raise HTTPException(status_code=404, detail=f"商品不存在：{product_id}")
    return directory


@app.get("/api/workbench/products/{product_id}/keywords")
def get_selected_keywords(product_id: str) -> dict[str, Any]:
    from pipeline.selection import load_selected_keywords

    directory = _require_product(product_id)
    payload = load_selected_keywords(directory)
    return {"product_id": product_id, "selection": payload}


@app.put("/api/workbench/products/{product_id}/keywords")
def put_selected_keywords(product_id: str, request: KeywordSelectionRequest) -> dict[str, Any]:
    """选词：直接给词，或从关键词库按分数取（``from_library=true``）。"""
    from pipeline.selection import select_from_library, set_selected_keywords

    directory = _require_product(product_id)
    try:
        if request.from_library:
            payload = select_from_library(
                directory,
                request.library_root or LIBRARY_ROOT,
                category_id=request.category_id,
                type_id=request.type_id,
                limit=request.limit,
            )
        else:
            if not request.keywords:
                raise HTTPException(status_code=422, detail="keywords 为空；或设 from_library=true")
            payload = set_selected_keywords(directory, request.keywords, source="manual")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "selection": payload}


@app.post("/api/workbench/products/{product_id}/copy")
def generate_copy(product_id: str, request: CopyRequestModel) -> dict[str, Any]:
    """跑模型步骤生成产品信息总结与俄文标题/简介（fake 也可，用于自检）。"""
    from models import ModelError, load_provider
    from pipeline.context import PipelineGateError
    from pipeline.handlers import MODEL_HANDLERS, run_single_step

    directory = _require_product(product_id)
    unknown = [step for step in request.steps if step not in MODEL_HANDLERS]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"不是模型步骤：{', '.join(unknown)}（可选：{', '.join(sorted(MODEL_HANDLERS))}）"
        )
    try:
        provider = load_provider(request.provider)
    except ModelError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    results: list[dict[str, Any]] = []
    for step in request.steps:
        try:
            result = run_single_step(directory, step, provider=provider)
        except PipelineGateError as error:
            raise HTTPException(
                status_code=409,
                detail={"step": error.step, "reason": error.reason, "details": error.details, "done": results},
            ) from error
        results.append({"step": step, **result})
    return {"ok": True, "provider": getattr(provider, "name", "unknown"), "results": results}


# ------------------------------------------------- 产物 / 台账 / 预检 / 运行（M3–M4）


class RunPipelineRequest(BaseModel):
    provider: str | None = Field(None, description="模型层：fake / none（默认 fake）")
    image_generator: str | None = Field(None, description="生图后端：placeholder / none")
    uploader: str | None = Field(None, description="上传器：dry-run / simulated / none")
    until: str | None = None
    step_budget: int = Field(8, ge=1, le=25)
    execute_upload: bool = Field(False, description="只有显式打开且服务侧 app_mode=production 才会真提交")


ARTIFACT_FILES: tuple[tuple[str, str], ...] = (
    ("采集输入", "input/source.json"),
    ("已选关键词", "input/selected-keywords.json"),
    ("商品分析", "output/product-analysis.json"),
    ("俄文文案", "output/copy-ru.json"),
    ("图片计划", "output/image-plan.json"),
    ("生图报告", "output/image-generation-report.json"),
    ("图片质检", "output/image-qc-report.json"),
    ("最终属性", "output/ozon-attributes-final.json"),
    ("上传载荷", "output/store-runs"),
    ("上传汇总", "output/upload-summary.json"),
    ("发布台账", "output/store-publications.json"),
    ("运行报告", "output/run-report.json"),
)


@app.get("/api/workbench/products/{product_id}/artifacts")
def product_artifacts(product_id: str) -> dict[str, Any]:
    """列出该商品的产物与关键摘要（界面用它显示"做到哪一步了"）。"""
    directory = _require_product(product_id)
    files: list[dict[str, Any]] = []
    for label, relative in ARTIFACT_FILES:
        path = directory / relative
        if path.is_dir():
            stored = sorted(item for item in path.rglob("*") if item.is_file())
            files.append(
                {
                    "label": label,
                    "path": relative,
                    "kind": "dir",
                    "exists": True,
                    "file_count": len(stored),
                }
            )
        else:
            files.append(
                {
                    "label": label,
                    "path": relative,
                    "kind": "file",
                    "exists": path.is_file(),
                    "bytes": path.stat().st_size if path.is_file() else 0,
                }
            )

    qc = _read_json_file(directory / "output" / "image-qc-report.json")
    attributes = _read_json_file(directory / "output" / "ozon-attributes-final.json")
    plan = _read_json_file(directory / "output" / "image-plan.json")
    return {
        "product_id": product_id,
        "summary": _product_summary(product_id),
        "files": files,
        "image_qc": {
            "decision": qc.get("decision"),
            "score": qc.get("score"),
            "critical_failures": qc.get("critical_failures"),
        },
        "attributes": attributes.get("required_summary"),
        "planned_images": {
            "main": len(plan.get("main_images") or []),
            "detail": len(plan.get("detail_images") or []),
        },
    }


@app.get("/api/workbench/products/{product_id}/publications")
def product_publications(product_id: str) -> dict[str, Any]:
    """发布台账 + 分发计划（幂等状态一目了然）。"""
    from pipeline.publications import load_publications, plan_publications
    from pipeline.stores import enabled_shop_ids, load_registry

    directory = _require_product(product_id)
    publications = load_publications(directory)
    store_ids = [str(item) for item in (publications.get("stores") or {}).keys()]
    if not store_ids:
        status = _read_json_file(directory / "status.json")
        store_ids = [str(item) for item in (status.get("target_store_ids") or [])]
    plan = plan_publications(directory, store_ids, enabled_store_ids=enabled_shop_ids(load_registry())) if store_ids else []
    return {"product_id": product_id, "publications": publications, "plan": plan}


@app.get("/api/workbench/doctor")
def workbench_doctor(product_id: str | None = None) -> dict[str, Any]:
    """上线前预检：还差什么才能真正提交（不发起任何 Ozon 调用）。"""
    from pipeline.doctor import run_doctor

    return run_doctor(PRODUCTS_ROOT, product_ids=[product_id] if product_id else None)


@app.post("/api/workbench/products/{product_id}/run")
def run_pipeline(product_id: str, request: RunPipelineRequest) -> dict[str, Any]:
    """跑流水线（默认干跑 + fake 模型层 + 占位生图，绝不会真提交）。"""
    from models import ModelError, load_provider
    from models.local_image import LocalPlaceholderGenerator
    from pipeline.runner import run_product
    from pipeline.upload import DryRunUploader, SimulatedUploader

    directory = _require_product(product_id)

    provider = None
    if (request.provider or "fake").lower() not in {"none", "off"}:
        try:
            provider = load_provider(request.provider)
        except ModelError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    image_generator = None
    if request.image_generator and request.image_generator.lower() not in {"none", "off"}:
        image_generator = LocalPlaceholderGenerator()

    uploader = None
    if request.uploader and request.uploader.lower() not in {"none", "off"}:
        uploader = SimulatedUploader() if request.uploader.lower().startswith("sim") else DryRunUploader()

    try:
        report = run_product(
            directory,
            until=request.until,
            dry_run=not request.execute_upload,
            step_budget=request.step_budget,
            provider=provider,
            uploader=uploader,
            image_generator=image_generator,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True, "report": report}


# ------------------------------------------- 选品清单 / 采集清单（M1 ↔ M2 的桥）


class SourcingPlanRequest(BaseModel):
    category_id: str | None = None
    type_id: str | None = None
    top: int = Field(20, ge=1, le=500)
    statuses: list[str] = Field(default_factory=lambda: ["qualified", "in_library"])
    min_score: float | None = None
    translate: bool = Field(False, description="用模型给中文找货词（否则用 Seerfar 中文类目名）")
    provider: str | None = None


class CollectionPlanRequest(BaseModel):
    top: int | None = Field(None, ge=1, le=500)
    only: list[str] = Field(default_factory=list)
    from_library: bool = Field(False, description="True 时直接从关键词库重建，忽略现有选品清单")
    category_id: str | None = None
    type_id: str | None = None


class MarkCollectedRequest(BaseModel):
    product_id: str
    keyword: str = Field(..., min_length=2)


def _lab_root() -> Path:
    """清单类产物的落点：默认与关键词库同级（这样 output/ 被 .gitignore 覆盖）。"""
    return Path(LIBRARY_ROOT).parent if str(LIBRARY_ROOT) not in {"", "."} else Path(".")


@app.post("/api/workbench/sourcing-plan")
def workbench_sourcing_plan(request: SourcingPlanRequest) -> dict[str, Any]:
    """生成选品清单（Ozon 复核 + 1688 找货链接），并写到 output/。"""
    from collector.sourcing import build_sourcing_plan, write_plan
    from models import ModelError, load_provider

    provider = None
    if request.translate:
        try:
            provider = load_provider(request.provider)
        except ModelError as error:
            raise HTTPException(status_code=422, detail=f"模型层不可用：{error}") from error

    plan = build_sourcing_plan(
        LIBRARY_ROOT,
        category_id=request.category_id,
        type_id=request.type_id,
        top_n=request.top,
        statuses=tuple(request.statuses),
        min_score=request.min_score,
        provider=provider,
        translate=request.translate,
    )
    written = write_plan(_lab_root(), plan, limit=50)
    return {"ok": True, "plan": plan, "written": written}


@app.get("/api/workbench/sourcing-plan")
def workbench_sourcing_plan_read() -> dict[str, Any]:
    """读回已生成的选品清单（界面刷新用）。"""
    path = _lab_root() / "output" / "sourcing-plan.json"
    payload = _read_json_file(path)
    if not payload:
        raise HTTPException(status_code=404, detail=f"还没有选品清单：先 POST /api/workbench/sourcing-plan（{path}）")
    return {"ok": True, "plan": payload, "path": str(path)}


@app.post("/api/workbench/collection-plan")
def workbench_collection_plan(request: CollectionPlanRequest) -> dict[str, Any]:
    """选品清单 → 采集清单（带"哪些词已采集"的状态跟踪），并写到 output/。"""
    from collector.collection_plan import build_collection_plan, write_collection_plan

    rows: list[dict[str, Any]] = []
    if request.from_library:
        from collector.sourcing import build_sourcing_plan

        rows = list(
            build_sourcing_plan(
                LIBRARY_ROOT,
                category_id=request.category_id,
                type_id=request.type_id,
                top_n=0,
            ).get("rows")
            or []
        )
    else:
        payload = _read_json_file(_lab_root() / "output" / "sourcing-plan.json")
        rows = list(payload.get("rows") or [])
        if not rows:
            raise HTTPException(
                status_code=404,
                detail="还没有选品清单：先 POST /api/workbench/sourcing-plan，或用 from_library=true 从库重建",
            )

    plan = build_collection_plan(
        rows, products_root=PRODUCTS_ROOT, top=request.top, only=request.only
    )
    written = write_collection_plan(_lab_root(), plan)
    return {"ok": True, "plan": plan, "written": written}


@app.get("/api/workbench/collection-plan")
def workbench_collection_plan_read() -> dict[str, Any]:
    path = _lab_root() / "output" / "collection-plan.json"
    payload = _read_json_file(path)
    if not payload:
        raise HTTPException(status_code=404, detail=f"还没有采集清单：先 POST /api/workbench/collection-plan（{path}）")
    return {"ok": True, "plan": payload, "path": str(path)}


@app.post("/api/workbench/collection-plan/mark")
def workbench_mark_collected(request: MarkCollectedRequest) -> dict[str, Any]:
    """把关键词补记到已有商品（等价于 CLI 的 --mark）。"""
    from collector.collection_plan import mark_keyword

    directory = _require_product(request.product_id)
    try:
        result = mark_keyword(directory, request.keyword)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **result}


@app.get("/api/workbench/keyword-products")
def workbench_keyword_products() -> dict[str, Any]:
    """关键词 → 商品 的汇总（回答"这个词下有几个商品、走到哪一步"）。"""
    from pipeline.doctor import product_keywords
    from pipeline.status import load_status, normalize

    index: dict[str, list[dict[str, Any]]] = {}
    for product_id in _list_product_ids():
        directory = PRODUCTS_ROOT / product_id
        status = normalize(load_status(directory))
        for text in product_keywords(directory):
            index.setdefault(text, []).append(
                {
                    "product_id": product_id,
                    "status": status.get("status"),
                    "current_step": status.get("current_step"),
                }
            )
    return {"ok": True, "count": len(index), "keywords": index}
