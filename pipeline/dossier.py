"""商品档案（人审用）：把散落的 JSON 汇总成一份**提交前能读懂**的报告。

为什么需要：上传前的最后一道关是**人**。可现在要看 status / source / copy-ru / image-plan /
image-qc-report / pricing / attributes / upload-feasibility / store-runs 十几个 JSON 才敢点提交。
``output/dossier.md`` 把这些拼成一页：选词依据 → SKU（含上架范围）→ 俄文文案（带规则检查）
→ 图片规划与卖点 → Ozon 属性 → 上传载荷预览 → 阻断项与下一步。

原则与其它模块一致：**有什么写什么，缺什么明说"缺"**，绝不用默认值假装数据已就绪。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

DOSSIER_FILE = "output/dossier.md"
SCHEMA_VERSION = "1.0.0"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in (value or []) if isinstance(item, Mapping)]


def _fmt(value: Any, *, empty: str = "—") -> str:
    if value is None or value == "":
        return empty
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def collect_dossier(product_dir: Path | str) -> dict[str, Any]:
    """读商品目录里已有的产物，汇总成结构化档案（不臆造任何字段）。"""
    directory = Path(product_dir)
    if not (directory / "status.json").is_file():
        raise FileNotFoundError(f"不是商品目录（缺 status.json）：{directory}")

    out = directory / "output"
    status = _read_json(directory / "status.json")
    source = _read_json(directory / "input" / "source.json")
    keywords = _read_json(directory / "input" / "selected-keywords.json")
    sku_selection = _read_json(directory / "input" / "selected-skus.json")
    analysis = _read_json(out / "product-analysis.json")
    positioning = _read_json(out / "product-positioning.json")
    design = _read_json(out / "ozon-ecommerce-design.json")
    copy = _read_json(out / "copy-ru.json")
    plan = _read_json(out / "image-plan.json")
    qc = _read_json(out / "image-qc-report.json")
    urls = _read_json(out / "image-public-urls.json")
    pricing = _read_json(out / "pricing-result.json")
    measurements = _read_json(out / "measurements.json")
    category = _read_json(out / "ozon-category.json")
    attributes = _read_json(out / "ozon-attributes-final.json")
    feasibility = _read_json(out / "upload-feasibility.json")
    publications = _read_json(out / "store-publications.json")

    selected = [str(item) for item in (sku_selection.get("selected") or [])]
    price_rows = {str(row.get("sku_id")): row for row in _rows(pricing.get("skus"))}
    sku_measurements = measurements.get("sku_measurements") or {}

    skus: list[dict[str, Any]] = []
    for index, sku in enumerate(_rows(source.get("skus")), start=1):
        sku_id = str(sku.get("sku_id") or f"S{index}")
        price = price_rows.get(sku_id) or {}
        dims = (sku_measurements or {}).get(sku_id) or {}
        skus.append(
            {
                "sku_id": sku_id,
                "color_ru": sku.get("color_ru"),
                "capacity": sku.get("capacity"),
                "purchase_price_cny": sku.get("purchase_price_cny"),
                "listed": (sku_id in set(selected)) if selected else True,
                "excluded_reason": next(
                    (item.get("reason") for item in _rows(sku_selection.get("excluded")) if str(item.get("sku_id")) == sku_id),
                    None,
                ),
                "selling_price_rub": price.get("selling_price_rub"),
                "status": price.get("status"),
                "package": dims.get("package"),
            }
        )

    receipts: dict[str, Any] = {}
    runs = out / "store-runs"
    if runs.is_dir():
        for store_dir in sorted(item for item in runs.iterdir() if item.is_dir()):
            result = _read_json(store_dir / "ozon-result.json")
            if result:
                receipts[store_dir.name] = {
                    "status": result.get("status"),
                    "task_id": result.get("task_id"),
                    "items": len(result.get("items") or []),
                    "errors": [str(item)[:120] for item in (result.get("errors") or [])][:3],
                }

    from rules.validate import official_copy_checks

    copy_report = official_copy_checks(copy) if copy else {"blocking": [], "advisory": []}
    required = attributes.get("required_summary") or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": directory.name,
        "source_url": source.get("source_url"),
        "title_zh": source.get("title_zh"),
        "status": status.get("status"),
        "progress": status.get("progress"),
        "completed_steps": status.get("completed_steps") or [],
        "target_store_ids": status.get("target_store_ids") or [],
        "batch_id": status.get("batch_id"),
        "api_write_count": status.get("api_write_count"),
        "collection_id": source.get("collection_id"),
        "keywords": [
            {
                "keyword": item.get("keyword"),
                "source": item.get("source"),
                "score": item.get("score"),
                "heat": item.get("heat_percentile"),
                "competition": item.get("competition_percentile"),
            }
            for item in _rows(keywords.get("keywords"))
        ],
        "category": {
            "category_id": category.get("category_id"),
            "type_id": category.get("type_id"),
            "name": category.get("category_name") or category.get("category_path_zh"),
            "match_status": category.get("match_status"),
            "source": category.get("metadata_source"),
        },
        "skus": skus,
        "analysis": {
            "summary": analysis.get("summary_ru") or analysis.get("summary_zh") or analysis.get("summary"),
            "facts": analysis.get("facts"),
            "risks": analysis.get("risks") or [],
            "decision": analysis.get("decision"),
        },
        "positioning": {
            "audience": positioning.get("target_audience"),
            "value_props": positioning.get("value_propositions") or positioning.get("selling_points") or [],
            "price_position": positioning.get("price_position"),
        },
        "copy": {
            "title_ru": copy.get("title_ru"),
            "title_length": len(str(copy.get("title_ru") or "")),
            "description_length": len(str(copy.get("description_ru") or "")),
            "hashtags": list(copy.get("hashtags") or []),
            "primary_keywords": list(copy.get("primary_keywords") or []),
            "sections": sorted((copy.get("description_sections") or {}).keys()),
            "rule_blocking": copy_report["blocking"],
            "rule_advisory": copy_report["advisory"],
        },
        "images": {
            "planned": len(_rows(plan.get("main_images"))) + len(_rows(plan.get("detail_images"))),
            "main": [
                {
                    "slot": item.get("slot"),
                    "purpose": item.get("purpose"),
                    "scene": item.get("scene"),
                    "russian_text": item.get("russian_text"),
                    "operation": item.get("operation"),
                    "status": item.get("status"),
                    "references": len(item.get("reference_paths") or []),
                    "output_path": item.get("output_path"),
                }
                for item in _rows(plan.get("main_images"))
            ],
            "detail_count": len(_rows(plan.get("detail_images"))),
            "qc": {
                "decision": qc.get("decision"),
                "score": qc.get("score"),
                "critical": qc.get("critical_failures") or [],
            },
            "published": len(urls.get("urls") or {}),
            "storage": urls.get("storage") or urls.get("base_url"),
            "sample_url": next(iter((urls.get("urls") or {}).values()), None),
            "design_provenance": design.get("generated_by") or design.get("provider"),
        },
        "attributes": {
            "total": required.get("total"),
            "missing": required.get("missing"),
            "missing_ids": required.get("missing_attribute_ids") or [],
        },
        "feasibility": {
            "upload_allowed": feasibility.get("upload_allowed"),
            "blockers": feasibility.get("production_blockers")
            or feasibility.get("blockers")
            or [],
        },
        "receipts": receipts,
        "publication_stores": sorted((publications.get("stores") or {}).keys()),
    }


def render_dossier(dossier: Mapping[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 商品档案 · {dossier.get('product_id')}")
    add("")
    add(
        f"- 1688 来源：{_fmt(dossier.get('source_url'))}\n"
        f"- 中文标题：{_fmt(dossier.get('title_zh'))}\n"
        f"- 状态：**{_fmt(dossier.get('status'))}**（进度 {_fmt(dossier.get('progress'))}%）"
        f"｜目标店铺：{_fmt(', '.join(dossier.get('target_store_ids') or []) or None)}"
        f"｜批次：{_fmt(dossier.get('batch_id'))}\n"
        f"- 已完成步骤 {len(dossier.get('completed_steps') or [])} 个｜累计 Ozon 写请求：{_fmt(dossier.get('api_write_count'))}"
    )
    add("")

    add("## 一、选词与类目")
    keywords = dossier.get("keywords") or []
    if keywords:
        add("| 关键词 | 来源 | 分数 | 热度分位 | 竞争分位 |")
        add("|---|---|---|---|---|")
        for item in keywords:
            add(
                f"| {_fmt(item.get('keyword'))} | {_fmt(item.get('source'))} | {_fmt(item.get('score'))} "
                f"| {_fmt(item.get('heat'))} | {_fmt(item.get('competition'))} |"
            )
    else:
        add("- ⚠️ 没有已选关键词（先跑关键词选择，文案会用不上词）")
    if keywords and all(item.get("score") is None for item in keywords):
        add("- ⚠️ 这些关键词没有分数：可能不在关键词库里，或还没打分（`POST /api/keywords/score`）")
    category = dossier.get("category") or {}
    add("")
    add(
        f"- Ozon 类目：`{_fmt(category.get('category_id'))}` / type `{_fmt(category.get('type_id'))}`"
        f"（{_fmt(category.get('name'))}）｜匹配状态：{_fmt(category.get('match_status'))}"
        f"｜来源：{_fmt(category.get('source'))}"
    )
    add("")

    add("## 二、SKU 与上架范围")
    skus = dossier.get("skus") or []
    if skus:
        add("| SKU | 颜色 | 规格 | 采购价 CNY | 上架 | 售价 RUB | 定价状态 |")
        add("|---|---|---|---|---|---|---|")
        for sku in skus:
            mark = "✅" if sku.get("listed") else f"❌（{_fmt(sku.get('excluded_reason'))}）"
            add(
                f"| {_fmt(sku.get('sku_id'))} | {_fmt(sku.get('color_ru'))} | {_fmt(sku.get('capacity'))} "
                f"| {_fmt(sku.get('purchase_price_cny'))} | {mark} | {_fmt(sku.get('selling_price_rub'))} "
                f"| {_fmt(sku.get('status'))} |"
            )
        listed = len([item for item in skus if item.get("listed")])
        add("")
        add(f"- 上架 **{listed}** / 采集 {len(skus)} 个 SKU")
    else:
        add("- ⚠️ 没有 SKU 数据")
    add("")

    analysis = dossier.get("analysis") or {}
    add("## 三、产品信息总结（AI）")
    add(f"- 总结：{_fmt(analysis.get('summary'))}")
    positioning = dossier.get("positioning") or {}
    values = positioning.get("value_props") or []
    if values:
        add(f"- 卖点/定位：受众 {_fmt(positioning.get('audience'))}｜价格带 {_fmt(positioning.get('price_position'))}")
        for item in values[:8]:
            add(f"  - {_fmt(item)}")
    risks = analysis.get("risks") or []
    if risks:
        add("- 风险（模型提出）：")
        for item in risks[:6]:
            add(f"  - {_fmt(item)}")
    add("")

    copy = dossier.get("copy") or {}
    add("## 四、俄文文案（已过规则）")
    add(f"- 标题（{_fmt(copy.get('title_length'))} 字符）：{_fmt(copy.get('title_ru'))}")
    add(f"- 简介长度：{_fmt(copy.get('description_length'))} 字符｜段落：{_fmt(', '.join(copy.get('sections') or []) or None)}")
    add(f"- 标签 {len(copy.get('hashtags') or [])} 个：{_fmt(' '.join(copy.get('hashtags') or []) or None)}")
    add(f"- 主关键词：{_fmt(', '.join(copy.get('primary_keywords') or []) or None)}")
    for item in copy.get("rule_blocking") or []:
        add(f"- ⛔ {item}")
    for item in copy.get("rule_advisory") or []:
        add(f"- ⚠️ {item}")
    add("")

    images = dossier.get("images") or {}
    add("## 五、图片规划与质检")
    add(
        f"- 计划 {_fmt(images.get('planned'))} 张（主图 {len(images.get('main') or [])} + 详情图 {_fmt(images.get('detail_count'))}）"
        f"｜质检：**{_fmt((images.get('qc') or {}).get('decision'))}**（分数 {_fmt((images.get('qc') or {}).get('score'))}）"
    )
    published = images.get("published") or 0
    if published:
        add(f"- 已发布公网地址 {published} 个（{_fmt(images.get('storage'))}）：{_fmt(images.get('sample_url'))}")
    else:
        add("- ⚠️ 还没有公网图片地址（上传门禁会拦住：Ozon 必须能抓到 https 图）")
    for item in images.get("main") or []:
        add(
            f"- `{_fmt(item.get('slot'))}`｜{_fmt(item.get('scene'))}｜{_fmt(item.get('operation'))}"
            f"｜参考图 {_fmt(item.get('references'))} 张｜状态 {_fmt(item.get('status'))}"
        )
        if item.get("purpose"):
            add(f"    - 用途：{item['purpose']}")
        if item.get("russian_text"):
            add(f"    - 图上俄文：{', '.join(str(text) for text in item['russian_text'])}")
    add("")

    attributes = dossier.get("attributes") or {}
    add("## 六、Ozon 类目属性")
    add(
        f"- 必填属性：共 {_fmt(attributes.get('total'))} 个，缺 **{_fmt(attributes.get('missing'))}** 个"
        f"{'：' + _fmt(attributes.get('missing_ids')) if attributes.get('missing_ids') else ''}"
    )
    add("")

    feasibility = dossier.get("feasibility") or {}
    add("## 七、上传载荷与阻断项")
    allowed = feasibility.get("upload_allowed")
    add(f"- 上传可行性：upload_allowed = {_fmt(allowed)}")
    blockers = feasibility.get("blockers") or []
    if blockers:
        for item in blockers[:10]:
            add(f"  - ⛔ {_fmt(item)}")
    elif allowed is None:
        add("  - ⚠️ 还没跑 upload_feasibility：**无法判断**能不能提交（别当成通过）")
    else:
        add("  - ✅ 没有阻断项")
    receipts = dossier.get("receipts") or {}
    if receipts:
        add("- 店铺回执：")
        for store, item in receipts.items():
            add(
                f"  - {store}：{_fmt(item.get('status'))}｜task_id {_fmt(item.get('task_id'))}"
                f"｜商品项 {_fmt(item.get('items'))}"
                + (f"｜错误 {item['errors']}" if item.get("errors") else "")
            )
    else:
        add("- 还没有店铺回执（未提交过）")
    add("")

    add("## 八、下一步")
    steps = dossier.get("completed_steps") or []
    if not copy.get("title_ru"):
        add("- 跑 `pipeline.runner`（或 `pipeline.launch`）把文案/图片补齐")
    if not published:
        add("- 发布图片：`pipeline.oss_cos --product-dir <目录>`（或 `pipeline.oss_local`）")
    if (attributes.get("missing") or 0) > 0:
        add("- 补必填属性：跑 `field_completion`，或按 `output/attribute-fill-input.json` 人工补值")
    if not feasibility.get("upload_allowed"):
        add("- 先跑 `pipeline.doctor --products-root products` 看每个商品的阻断项")
    add(f"- 当前已完成：{', '.join(steps) if steps else '（还没跑）'}")
    return "\n".join(lines) + "\n"


def write_dossier(product_dir: Path | str) -> dict[str, Any]:
    directory = Path(product_dir)
    dossier = collect_dossier(directory)
    path = directory / DOSSIER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dossier(dossier), encoding="utf-8")
    return {"ok": True, "product_id": directory.name, "file": DOSSIER_FILE, "dossier": dossier}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成商品档案（提交前的人审报告）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--print", action="store_true", dest="to_stdout", help="同时打印到终端")
    parser.add_argument("--json", action="store_true", help="打印结构化数据而不是 markdown")
    args = parser.parse_args(argv)

    try:
        result = write_dossier(args.product_dir)
    except FileNotFoundError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(result["dossier"], ensure_ascii=False, indent=2, default=str))
    else:
        text = render_dossier(result["dossier"])
        print(f"已写出 {Path(args.product_dir) / DOSSIER_FILE}")
        if args.to_stdout:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
