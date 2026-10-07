"""Explicit, cached local AI stages. Generation never selects or publishes copy."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import validate_contract
from models import AnalysisRequest, CopyRequest, ImagePlanRequest
from rules.validate import validate_copy_bundle

from .listing_form import read_json, write_json, _require_editable
from .product_edit_lock import product_file_transaction, serialized_product_edit
from .selected_source import selected_source
from .selection import load_selected_keywords
from .analysis_gates import risk_gates
from .copy_evidence import (COPY_EVIDENCE_VERSION, candidate_evidence as _candidate_evidence,
                            safe_evidence_problems, verified_copy_facts as _verified_facts)

STATE_FILE = "input/guided-workflow.json"
CANDIDATES_FILE = "output/copy-candidates.json"
COPY_FILE = "output/copy-ru.json"
ANALYSIS_FILE = "output/product-analysis.json"
PLAN_FILE = "output/image-plan.json"
MODES = ("search_first", "conversion_first", "differentiation_first")
MODE_LABELS = {"search_first": "搜索匹配优先", "conversion_first": "买家理解优先", "differentiation_first": "真实差异优先"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _source(directory: Path) -> dict[str, Any]:
    source = selected_source(directory, read_json(directory / "input/source.json"), require_selection=True,
                             merge_category=False)
    source["fact_collection_only"] = True
    return source


def _signature(provider: Any) -> dict[str, Any]:
    transport = getattr(provider, "transport", None)
    config = getattr(transport, "config", None)
    return {"provider": str(getattr(provider, "name", provider.__class__.__name__)),
            "model": str(getattr(config, "model", getattr(transport, "model", "")))}


def _factual_analysis(directory: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    from models.fake import FakeProvider
    from models.http_provider import enrich_facts_from_inputs
    request = AnalysisRequest(product_id=directory.name, product_dir=directory, source=source)
    factual = FakeProvider().analyze_product(request)
    enrich_facts_from_inputs(factual, request)
    return factual


def analysis_fingerprint(directory: Path | str) -> str:
    directory = Path(directory)
    source = _source(directory)
    for key in ("captured_at", "ingested_at", "collection_id", "duplicate_of", "version", "selected_category", "category_source"):
        source.pop(key, None)
    # Hash the effective factual values, not editor metadata/provenance. Saving
    # an identical supplier material as a confirmed card field is not a new AI
    # input; changing/clearing the actual material is. Category dictionary IDs,
    # scope and confirmation timestamps belong to the card/copy stages only.
    factual = _factual_analysis(directory, source)

    def semantic(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: semantic(child) for key, child in value.items() if key not in {"source", "source_refs"}}
        if isinstance(value, list):
            return [semantic(child) for child in value]
        return value

    return _hash({"version": 2, "source": source, "effective_facts": semantic(factual.get("facts") or {})})


def copy_fingerprint(directory: Path | str) -> str:
    directory = Path(directory)
    keywords = load_selected_keywords(directory) or {}
    category = read_json(directory / "input/category-selection.json")
    schema = read_json(directory / "input/category-form.json")
    return _hash({"version": 2, "evidence_version": COPY_EVIDENCE_VERSION, "analysis_inputs": analysis_fingerprint(directory),
                  "analysis": read_json(directory / ANALYSIS_FILE), "keywords": keywords.get("keywords") or [],
                  "category": {key: category.get(key) for key in ("shop_id", "category_id", "type_id", "source", "confirmed_by_user")},
                  "schema_fields": schema.get("fields") or [],
                  "category_evidence": {key: schema.get(key) for key in
                                        ("source", "shop_id", "category_id", "type_id", "category_name", "category_path", "language")}})


def copy_generation_status(directory: Path | str) -> dict[str, Any]:
    """Read-only retry advice; never return the cached response or model keys."""
    directory = Path(directory)
    diagnostic = read_json(directory / "output/copy-generation-diagnostic.json")
    context = diagnostic.get("context") if isinstance(diagnostic.get("context"), Mapping) else {}
    try:
        current = bool(context.get("input_fingerprint") == copy_fingerprint(directory)
                       and context.get("evidence_version") == COPY_EVIDENCE_VERSION)
    except ValueError:
        current = False
    blocked = current and diagnostic.get("status") == "invalid"
    return {"status": "invalid_response" if blocked else "none" if not diagnostic else "stale" if not current else "none", "current": current,
            "retry_requires_confirmation": bool(blocked),
            "errors": safe_evidence_problems(diagnostic.get("problems") or []) if blocked else [],
            "model_calls": 0}


def _copy_source(directory: Path) -> dict[str, Any]:
    category = read_json(directory / "input/category-selection.json")
    valid_ids = all(not isinstance(category.get(key), bool) and str(category.get(key) or "").isdigit()
                    and int(category[key]) > 0 for key in ("category_id", "type_id"))
    if category.get("confirmed_by_user") is not True or category.get("source") != "ozon_seller_api" or not valid_ids:
        raise ValueError("请先人工确认 Ozon 官方真实类目和商品类型，再生成正式俄文候选")
    source = selected_source(directory, read_json(directory / "input/source.json"), require_selection=True)
    form = read_json(directory / "input/category-form.json")
    if (form.get("source") == "ozon_seller_api"
            and str(form.get("category_id")) == str(category["category_id"])
            and str(form.get("type_id")) == str(category["type_id"])
            and str(form.get("shop_id")) == str(category.get("shop_id"))):
        # The selection can contain only IDs and a translated breadcrumb. Use
        # the already fetched official leaf name; never guess a Russian type.
        category = {**category, "type_name": form.get("category_name"),
                    "type_name_language": form.get("language"), "category_path": form.get("category_path")}
        source["selected_category"] = category
    source["fact_collection_only"] = True
    return source


def plan_fingerprint(directory: Path | str) -> str:
    directory = Path(directory)
    return _hash({"version": 1, "copy_inputs": copy_fingerprint(directory),
                  "copy": read_json(directory / COPY_FILE),
                  "selected_source": _source(directory)})


def workflow_status(directory: Path | str) -> dict[str, Any]:
    directory = Path(directory)
    state = read_json(directory / STATE_FILE)
    try:
        current_analysis, current_copy, current_plan = (analysis_fingerprint(directory),
                                                       copy_fingerprint(directory), plan_fingerprint(directory))
        ids = _source(directory)["selected_sku_ids"]
    except ValueError as error:
        return {"selected_sku_ids": [], "analysis": {"status": "missing", "confirmed": False},
                "copy": {"status": "missing", "confirmed": False, "candidates": []},
                "plan": {"status": "missing"}, "blockers": [str(error)], "api_writes_performed": False}
    analysis, copy, plan = (dict(state.get(key) or {}) for key in ("analysis", "copy", "plan"))
    payload = read_json(directory / ANALYSIS_FILE)
    analysis_current = bool(payload and analysis.get("input_fingerprint") == current_analysis
                            and analysis.get("artifact_sha256") == _hash(payload))
    analysis_confirmed = analysis_current and analysis.get("confirmed_fingerprint") == current_analysis
    analysis.update(payload=payload, fingerprint=current_analysis, confirmed=analysis_confirmed,
                    status="confirmed" if analysis_confirmed else "ready" if analysis_current else "stale" if payload else "missing")
    candidates = read_json(directory / CANDIDATES_FILE)
    copies_current = bool(analysis_confirmed and copy.get("input_fingerprint") == current_copy
                          and candidates.get("input_fingerprint") == current_copy
                          and copy.get("candidate_sha256") == _hash(candidates))
    draft = read_json(directory / COPY_FILE)
    selected = bool(copies_current and copy.get("selected_id") and draft
                    and copy.get("selected_sha256") == _hash(draft))
    confirmed = selected and copy.get("confirmed_sha256") == _hash(draft)
    copy.update(fingerprint=current_copy, candidates=candidates.get("candidates") or [], payload=draft,
                selected=selected, confirmed=confirmed,
                status="confirmed" if confirmed else "selected" if selected else "candidates" if copies_current
                else "stale" if candidates or draft else "missing")
    plan_payload = read_json(directory / PLAN_FILE)
    plan_current = bool(confirmed and plan.get("input_fingerprint") == current_plan
                        and plan_payload and plan.get("artifact_sha256") == _hash(plan_payload))
    plan.update(fingerprint=current_plan, payload=plan_payload,
                status="ready" if plan_current else "stale" if plan_payload else "missing")
    gates = risk_gates(payload) if analysis_current else {"preparation": [], "publication": [], "deferred": []}
    return {"selected_sku_ids": ids, "analysis": analysis, "copy": copy, "plan": plan,
            "preparation_blockers": gates["preparation"], "publication_blockers": gates["publication"],
            "deferred_risks": gates["deferred"],
            "blockers": [], "api_writes_performed": False}


def _refs(directory: Path) -> list[str]:
    return [path for path in ("input/source.json", "input/selected-skus.json", "input/human-confirmations.json",
                             ANALYSIS_FILE, "input/selected-keywords.json") if (directory / path).is_file()]


def _assert_unchanged(directory: Path, fingerprint: str, calculator: Any) -> None:
    """Older callers may not honor the editor lock; never commit across inputs."""
    _require_editable(directory)
    if calculator(directory) != fingerprint:
        raise ValueError("生成期间规格、事实、类目或关键词已变更，本次旧产物未保存；请按最新输入继续")


@serialized_product_edit
def analyze_selected_product(directory: Path | str, provider: Any, *, force: bool = False) -> dict[str, Any]:
    directory = Path(directory)
    _require_editable(directory)
    fingerprint, signature = analysis_fingerprint(directory), _signature(provider)
    current = workflow_status(directory)
    state = read_json(directory / STATE_FILE)
    previous = state.get("analysis") or {}
    if not force and current["analysis"]["status"] in {"ready", "confirmed"} and previous.get("model") == signature:
        return {**current, "cache_hit": True, "input_fingerprint": fingerprint}
    captured_source = _source(directory)
    captured_facts = _factual_analysis(directory, captured_source)["facts"]
    _assert_unchanged(directory, fingerprint, analysis_fingerprint)
    payload = provider.analyze_product(AnalysisRequest(product_id=directory.name, product_dir=directory,
                                                       source=captured_source, source_refs=_refs(directory)))
    # Missing formal category is a final-card task, not a reason to suppress
    # factual supplier analysis. Keep it explicitly unresolved but non-blocking.
    payload = deepcopy(payload)
    expected_ids = set(captured_source["selected_sku_ids"])
    actual_ids = {str(row.get("sku_id")) for row in (payload.get("facts") or {}).get("skus") or []
                  if isinstance(row, Mapping)}
    if actual_ids != expected_ids or len((payload.get("facts") or {}).get("skus") or []) != len(expected_ids):
        raise ValueError("商品分析包含未选规格或缺少所选规格，不能保存")
    # Providers write narrative, not product specifications. Even a custom
    # adapter cannot bless its own invented material as a verified fact ID.
    payload["facts"] = captured_facts
    known = {"materials": bool(payload["facts"].get("materials")),
             "dimensions": payload["facts"].get("dimensions") not in (None, "unknown"),
             "weight": payload["facts"].get("weight") not in (None, "unknown"),
             "certifications": bool(payload["facts"].get("certifications"))}
    payload["unknowns"] = [row for row in payload.get("unknowns") or [] if not known.get(row.get("field"))]
    risks = payload.get("risks") or []
    category_risks = [row for row in risks if isinstance(row, Mapping) and row.get("area") == "category"
                      and not risk_gates({"risks": [row]})["preparation"]]
    for row in category_risks:
        row["blocking"] = False
    if category_risks and not any(row.get("blocking") for row in risks if isinstance(row, Mapping)):
        if (payload.get("recommendation") or {}).get("decision") == "needs_human_input":
            payload["recommendation"] = {"decision": "continue", "reason": "已总结所选规格；生成正式文案前需确认真实类目"}
    errors = validate_contract("product-analysis", payload)
    if errors:
        raise ValueError("商品分析未通过校验：" + "；".join(errors[:5]))
    _assert_unchanged(directory, fingerprint, analysis_fingerprint)
    state["analysis"] = {"input_fingerprint": fingerprint, "artifact_sha256": _hash(payload),
                         "model": signature, "generated_at": _now()}
    state.pop("copy", None)
    state.pop("plan", None)
    with product_file_transaction(directory, (ANALYSIS_FILE, STATE_FILE)):
        write_json(directory / ANALYSIS_FILE, payload)
        write_json(directory / STATE_FILE, state)
    return {**workflow_status(directory), "cache_hit": False, "input_fingerprint": fingerprint}


@serialized_product_edit
def confirm_analysis(directory: Path | str, input_fingerprint: str) -> dict[str, Any]:
    directory = Path(directory)
    _require_editable(directory)
    current = workflow_status(directory)
    analysis = current["analysis"]
    if analysis["status"] not in {"ready", "confirmed"} or input_fingerprint != analysis["fingerprint"]:
        raise ValueError("规格或事实已变更，请重新分析后确认")
    payload = analysis["payload"]
    if risk_gates(payload)["preparation"]:
        raise ValueError("商品分析仍有阻断性风险，不能确认继续：" + "；".join(risk_gates(payload)["preparation"][:3]))
    state = read_json(directory / STATE_FILE)
    state["analysis"].update(confirmed_fingerprint=input_fingerprint, confirmed_at=_now())
    write_json(directory / STATE_FILE, state)
    return workflow_status(directory)


def _keywords(directory: Path) -> list[dict[str, Any]]:
    rows = (load_selected_keywords(directory) or {}).get("keywords") or []
    selected = [dict(row) for row in rows if isinstance(row, Mapping)
                and row.get("role") not in {"ad", "reject", "exclude"} and row.get("relevance") != "conflict"]
    if not selected:
        # The confirmed real Ozon type and selected product facts suffice for
        # natural copy. Do not fabricate search demand or pull unrelated words.
        return []
    explicit_cores = [row for row in selected if row.get("role") == "core"]
    if len(explicit_cores) > 1:
        raise ValueError("请保留一个核心关键词，其余词设为辅助词或长尾词")
    # Existing selections predate roles. Preserve their ordering and give that
    # explicit user-picked first query the core role rather than invent a query.
    core = explicit_cores[0] if explicit_cores else selected[0]
    for row in selected:
        if row is core:
            row["role"] = "core"
        elif not row.get("role"):
            row["role"] = "secondary"
        if row.get("role") not in {"core", "secondary", "long_tail"}:
            raise ValueError("关键词角色无效，请使用核心词、辅助词或长尾词")
    selected.sort(key=lambda row: row is not core)
    return selected


def _validate_bundle(bundle: Mapping[str, Any]) -> None:
    errors = validate_copy_bundle(bundle)
    if errors:
        raise ValueError("俄文文案未通过校验：" + "；".join(errors[:6]))


@serialized_product_edit
def generate_copy_candidates(directory: Path | str, provider: Any, *, force: bool = False,
                             revalidate_only: bool = False) -> dict[str, Any]:
    directory = Path(directory)
    _require_editable(directory)
    if force and revalidate_only:
        raise ValueError("免费重校验与付费重新生成不能同时请求")
    current = workflow_status(directory)
    if not current["analysis"]["confirmed"]:
        raise ValueError("请先确认当前所选规格的商品分析")
    keywords = _keywords(directory)
    source = _copy_source(directory)
    fingerprint, signature = copy_fingerprint(directory), _signature(provider)
    state = read_json(directory / STATE_FILE)
    previous = state.get("copy") or {}
    if not force and current["copy"]["status"] in {"candidates", "selected", "confirmed"} and previous.get("model") == signature:
        return {**current, "cache_hit": True, "input_fingerprint": fingerprint}
    if revalidate_only and getattr(provider, "supports_copy_cache_revalidation", False) is not True:
        raise ValueError("当前模型不支持免费重校验；本次没有调用模型")
    batch_method = getattr(provider, "write_copy_candidates_ru", None)
    if not callable(batch_method):
        raise ValueError("当前模型提供方不支持一次生成三组候选，请配置火山方舟或升级适配器")
    verified_facts = _verified_facts(current["analysis"]["payload"], source)
    _assert_unchanged(directory, fingerprint, copy_fingerprint)
    batches = batch_method(CopyRequest(product_id=directory.name, product_dir=directory,
                                      source=source, analysis=current["analysis"]["payload"],
                                      source_refs=_refs(directory), selected_keywords=keywords,
                                      extra={"verified_facts": verified_facts, "allow_category_only_copy": not keywords,
                                             "force_new_model_call": force, "input_fingerprint": fingerprint,
                                             "revalidate_only": revalidate_only,
                                             "evidence_version": COPY_EVIDENCE_VERSION}))
    rows = batches.get("candidates") or []
    if len(rows) != 3 or {row.get("mode") for row in rows if isinstance(row, Mapping)} != set(MODES):
        raise ValueError("模型必须在同一批次返回三个不同侧重点的候选")
    generation_id = _hash({"input_fingerprint": fingerprint, "batch": batches})[:24]
    candidates = []
    for row in rows:
        mode, bundle = row["mode"], row["documents"]
        copy = dict(bundle.get("copy_bundle") or {})
        _validate_bundle(copy)
        for key, contract in (("title_ru", "title-ru"), ("description_ru", "description-ru"), ("keywords_ru", "keywords-ru")):
            errors = validate_contract(contract, bundle.get(key) or {})
            if errors:
                raise ValueError("候选子文档不符合契约：" + "；".join(errors[:3]))
        evidence = _candidate_evidence(copy, verified_facts, keywords)
        candidates.append({"id": f"{generation_id}-{mode}", "mode": mode, "label": MODE_LABELS[mode],
                           "title": copy["title_ru"], "description": copy["description_ru"],
                           "title_ru": copy["title_ru"], "description_ru": copy["description_ru"],
                           "hashtags": copy.get("hashtags") or [], "copy_bundle": copy, "documents": bundle,
                           **evidence, "status": "candidate"})
    payload = {"input_fingerprint": fingerprint, "generation_id": generation_id, "language": "ru",
               "status": "candidate", "product": {"facts": verified_facts, "ozon_attributes": []},
               "keyword_plan": [{"query": row["keyword"], "role": row["role"], "relevance": row.get("relevance")}
                                for row in keywords], "candidates": candidates, "generated_at": _now()}
    _assert_unchanged(directory, fingerprint, copy_fingerprint)
    state["copy"] = {"input_fingerprint": fingerprint, "candidate_sha256": _hash(payload),
                     "model": signature, "generated_at": _now()}
    state.pop("plan", None)
    with product_file_transaction(directory, (CANDIDATES_FILE, STATE_FILE)):
        write_json(directory / CANDIDATES_FILE, payload)
        write_json(directory / STATE_FILE, state)
    return {**workflow_status(directory), "cache_hit": False, "input_fingerprint": fingerprint}


@serialized_product_edit
def choose_copy_candidate(directory: Path | str, candidate_id: str, *, title_ru: str | None = None,
                          description_ru: str | None = None, hashtags: Sequence[str] | None = None) -> dict[str, Any]:
    directory = Path(directory)
    _require_editable(directory)
    current = workflow_status(directory)
    if current["copy"]["status"] not in {"candidates", "selected", "confirmed"}:
        raise ValueError("候选文案已过期，请重新生成后选择")
    candidate = next((row for row in current["copy"]["candidates"] if row.get("id") == candidate_id), None)
    if not candidate:
        raise ValueError("候选文案不存在，请从当前三组选项选择")
    bundle = deepcopy(candidate["copy_bundle"])
    if title_ru is not None:
        bundle["title_ru"] = title_ru.strip()
    if description_ru is not None:
        bundle["description_ru"] = description_ru.strip()
    if hashtags is not None:
        bundle["hashtags"] = list(hashtags)
    _validate_bundle(bundle)
    # An edited title/description cannot silently gain an unsupported number or
    # material behind the candidate's original evidence. Rebuild exact claims.
    bundle.pop("claim_evidence", None)
    candidate_snapshot = read_json(directory / CANDIDATES_FILE)
    audit = _candidate_evidence(bundle, (candidate_snapshot.get("product") or {}).get("facts") or [], _keywords(directory))
    bundle["claim_evidence"] = audit["claim_evidence"]
    payload = {"schema_version": "1.0.0", "product_id": directory.name, **bundle,
               "generated_by": "guided_candidate_selection", "generated_at": _now(),
               "candidate_id": candidate_id, "input_fingerprint": current["copy"]["fingerprint"]}
    state = read_json(directory / STATE_FILE)
    state["copy"].update(selected_id=candidate_id, selected_sha256=_hash(payload), selected_at=_now())
    state["copy"].pop("confirmed_sha256", None)
    state.pop("plan", None)
    documents = deepcopy(candidate["documents"])
    documents["title_ru"]["title_ru"] = bundle["title_ru"]
    documents["description_ru"]["description_ru"] = bundle["description_ru"]
    paths = (COPY_FILE, STATE_FILE, "output/title-ru.json", "output/description-ru.json", "output/keywords-ru.json")
    with product_file_transaction(directory, paths):
        write_json(directory / COPY_FILE, payload)
        for key in ("title_ru", "description_ru", "keywords_ru"):
            write_json(directory / f"output/{key.replace('_', '-')}.json", documents[key])
        write_json(directory / STATE_FILE, state)
    return {**workflow_status(directory), "copy_bundle": payload}


@serialized_product_edit
def confirm_selected_copy(directory: Path | str, input_fingerprint: str | None = None) -> dict[str, Any]:
    directory = Path(directory)
    _require_editable(directory)
    current = workflow_status(directory)
    copy = current["copy"]
    if not copy["selected"] or (input_fingerprint and input_fingerprint != copy["fingerprint"]):
        raise ValueError("请先选择并保存当前有效的文案候选")
    _validate_bundle(copy["payload"])
    state = read_json(directory / STATE_FILE)
    state["copy"].update(confirmed_sha256=_hash(copy["payload"]), confirmed_at=_now())
    write_json(directory / STATE_FILE, state)
    return workflow_status(directory)


@serialized_product_edit
def plan_selected_images(directory: Path | str, provider: Any, *, force: bool = False) -> dict[str, Any]:
    directory = Path(directory)
    _require_editable(directory)
    current = workflow_status(directory)
    if not current["analysis"]["confirmed"] or not current["copy"]["confirmed"]:
        raise ValueError("请先确认商品分析，并选择、保存和确认俄文文案，再规划图片")
    fingerprint, signature = plan_fingerprint(directory), _signature(provider)
    state = read_json(directory / STATE_FILE)
    previous = state.get("plan") or {}
    if not force and current["plan"]["status"] == "ready" and previous.get("model") == signature:
        return {**current, "cache_hit": True, "input_fingerprint": fingerprint}
    captured_source = _copy_source(directory)
    _assert_unchanged(directory, fingerprint, plan_fingerprint)
    plan = provider.plan_images(ImagePlanRequest(product_id=directory.name, product_dir=directory,
                                                 source=captured_source, source_refs=_refs(directory),
                                                 analysis=current["analysis"]["payload"], copy_bundle=current["copy"]["payload"]))
    errors = validate_contract("image-plan", plan)
    ids = current["selected_sku_ids"]
    main = plan.get("main_images") or []
    if len(main) != len(ids) or {str(row.get("source_sku_id") or row.get("sku_identity") or row.get("sku_id")) for row in main} != set(ids):
        errors.append("每个所选规格必须有且只有一张主图，SKU 标识必须一致")
    if errors:
        raise ValueError("图片规划未通过校验：" + "；".join(errors[:5]))
    _assert_unchanged(directory, fingerprint, plan_fingerprint)
    state["plan"] = {"input_fingerprint": fingerprint, "artifact_sha256": _hash(plan),
                     "model": signature, "generated_at": _now()}
    from models.image_plan import render_plan_brief
    with product_file_transaction(directory, (PLAN_FILE, STATE_FILE, "output/image-plan-brief.md")):
        write_json(directory / PLAN_FILE, plan)
        (directory / "output/image-plan-brief.md").write_text(render_plan_brief(plan), encoding="utf-8")
        write_json(directory / STATE_FILE, state)
    return {**workflow_status(directory), "cache_hit": False, "input_fingerprint": fingerprint}


@serialized_product_edit
def refresh_plan_metadata(directory: Path | str) -> dict[str, Any]:
    """Accept a validated local prompt/reference edit, never an input-scope change.

    The API calls this only after its constrained slot editor. Approval and
    generated image fingerprints still change naturally with the edited plan.
    """
    directory = Path(directory)
    _require_editable(directory)
    current = workflow_status(directory)
    state = read_json(directory / STATE_FILE)
    previous = state.get("plan") or {}
    fingerprint = plan_fingerprint(directory)
    if not current["analysis"]["confirmed"] or not current["copy"]["confirmed"] or previous.get("input_fingerprint") != fingerprint:
        raise ValueError("规格、关键词或文案已变更，不能把旧图片方案标记为有效")
    plan = read_json(directory / PLAN_FILE)
    errors = validate_contract("image-plan", plan)
    ids = current["selected_sku_ids"]
    main = plan.get("main_images") or []
    if len(main) != len(ids) or {str(row.get("source_sku_id") or row.get("sku_identity") or row.get("sku_id")) for row in main} != set(ids):
        errors.append("图片方案 SKU 与当前所选规格不一致")
    if errors:
        raise ValueError("编辑后的图片方案无效：" + "；".join(errors[:5]))
    state["plan"].update(artifact_sha256=_hash(plan), edited_at=_now())
    from models.image_plan import render_plan_brief
    with product_file_transaction(directory, (STATE_FILE, "output/image-plan-brief.md")):
        (directory / "output/image-plan-brief.md").write_text(render_plan_brief(plan), encoding="utf-8")
        write_json(directory / STATE_FILE, state)
    return workflow_status(directory)


def pipeline_artifact_current(directory: Path | str, artifact: str) -> bool:
    """Modern stages may only reuse artifacts tied to the current input snapshot."""
    directory = Path(directory)
    if not read_json(directory / STATE_FILE):
        return True  # retain existing legacy/CLI behavior until modern flow starts
    current = workflow_status(directory)
    if artifact == "analysis":
        return current["analysis"]["status"] in {"ready", "confirmed"}
    if artifact == "copy":
        return current["copy"]["selected"]
    if artifact == "plan":
        return current["plan"]["status"] == "ready"
    return False
