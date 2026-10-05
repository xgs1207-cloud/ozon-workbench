"""流水线步骤注册表（与原项目 scripts/pipeline_runtime.py 的 PIPELINE_STEPS 对齐）。

这是**数据**，不是执行逻辑：M1–M4 的 runner 读它决定顺序、依赖产物与门禁。
每个步骤标注 ``kind``：

- ``inprocess``：纯本地函数，无外部依赖
- ``local``：调用本地脚本/CLI
- ``ai``：需要模型层的步骤（标题/简介、产品分析、生图等）—— 具体接哪家由 runner 注入

与原项目一致的关键点：15 步 = 阶段 A 7 步 + 阶段 B 8 步；单商品最多 10 个 SKU；
``offer_exists_check`` **不查 API**（写 ``deferred_store_specific``）；``app_mode != production`` 禁止真实上传。
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.0.0"

#: 单商品最多上架 SKU 数（原项目 MAX_SELECTED_SKUS_PER_PRODUCT）
MAX_SELECTED_SKUS = 10

PHASE_A = "A"
PHASE_B = "B"

#: 每步完成后的商品状态（原项目 STEP_STATUS）
STEP_STATUS: dict[str, str] = {
    "validate_source": "PROCESSING",
    "product_analysis": "PROCESSING",
    "product_positioning": "PROCESSING",
    "ecommerce_design": "PROCESSING",
    "russian_copy": "PROCESSING",
    "category_match": "CATEGORY_MATCHED",
    "variant_rules": "CATEGORY_MATCHED",
    "measurements": "PRICED",
    "offer_exists_check": "PRICED",
    "upload_feasibility": "PRICED",
    "image_plan": "CONTENT_GENERATED",
    "field_completion": "CONTENT_GENERATED",
    "image_generation": "IMAGES_GENERATED",
    "image_qc": "IMAGES_GENERATED",
    "ozon_upload": "UPLOADING",
}

#: 未提交任何 Ozon 写请求前，重跑必须保持的顺序
PIPELINE_STEPS: tuple[str, ...] = (
    # 阶段 A：采集与可行性
    "validate_source",
    "product_analysis",
    "category_match",
    "variant_rules",
    "measurements",
    "offer_exists_check",
    "upload_feasibility",
    # 阶段 B：内容与提交
    "product_positioning",
    "ecommerce_design",
    "russian_copy",
    "field_completion",
    "image_plan",
    "image_generation",
    "image_qc",
    "ozon_upload",
)

PHASE_A_STEPS: tuple[str, ...] = PIPELINE_STEPS[:7]
PHASE_B_STEPS: tuple[str, ...] = PIPELINE_STEPS[7:]

#: 按 SKU 冻结快照后，某些步骤可以整体重跑而不重做已完成图位
IMAGE_STEPS: tuple[str, ...] = ("image_plan", "image_generation", "image_qc")

STEP_LABELS_ZH: dict[str, str] = {
    "validate_source": "资料检查",
    "product_analysis": "商品分析",
    "category_match": "Ozon 类目",
    "variant_rules": "SKU 变体规则",
    "measurements": "重量尺寸与定价",
    "offer_exists_check": "重复检查",
    "upload_feasibility": "上架可行性",
    "product_positioning": "商品定位",
    "ecommerce_design": "电商设计",
    "russian_copy": "俄文 SEO 文案",
    "field_completion": "Ozon 属性填充",
    "image_plan": "图片方案",
    "image_generation": "图片生成",
    "image_qc": "图片检查",
    "ozon_upload": "Ozon 上传",
}

#: 步骤定义：inputs/outputs 用于"产物缺失就不该往下跑"的判定
STEP_DEFINITIONS: dict[str, dict[str, Any]] = {
    "validate_source": {
        "phase": PHASE_A,
        "kind": "inprocess",
        "inputs": ["input/source.json", "input/raw-snapshot.json", "input/category-selection.json"],
        # 原项目把 raw-snapshot / category-selection 当硬输入（由它的采集插件产出）。
        # 我们的采集侧还没接管，先降级为可选并在 handler 里告警；采集接入后应移出该列表。
        "optional_inputs": ["input/raw-snapshot.json", "input/category-selection.json"],
        "outputs": ["output/source-validation.json"],
        "gates": ["source_url 必须含 1688.com/offer/", "SKU 数 1–10", "每个 SKU 需 sku_id 与采购价"],
    },
    "product_analysis": {
        "phase": PHASE_A,
        "kind": "ai",
        "inputs": ["input/source.json"],
        "outputs": ["output/product-analysis.json"],
        "gates": [],
    },
    "category_match": {
        "phase": PHASE_A,
        "kind": "local",
        "inputs": ["output/product-analysis.json"],
        "outputs": ["output/ozon-category.json", "output/ozon-category-attributes.json"],
        "gates": [
            "metadata_source 必须是 ozon_seller_api",
            "category_id/type_id 必须是正整数",
            "match_status ∈ {api_confirmed, api_match_needs_review}",
        ],
    },
    "variant_rules": {
        "phase": PHASE_A,
        "kind": "local",
        "inputs": ["output/ozon-category-attributes.json"],
        "outputs": ["output/platform-grouping-result.json"],
        "gates": ["SKU 变体只能建在 is_aspect=true 的属性上"],
    },
    "measurements": {
        "phase": PHASE_A,
        "kind": "local",
        "inputs": ["input/source.json"],
        "outputs": ["output/pricing-result.json", "output/measurements.json", "output/profit-analysis.json"],
        "gates": [
            "每个 SKU 必须有采购价与卢布售价",
            "尺寸重量只接受确认值（input/workbench-sku-overrides.json），不估算",
            "包装尺寸/重量必须大于等于商品本体",
        ],
    },
    "offer_exists_check": {
        "phase": PHASE_A,
        "kind": "inprocess",
        "inputs": ["input/source.json"],
        "outputs": ["output/offer-id-precheck.json"],
        "gates": ["不查 Ozon API，写 deferred_store_specific"],
    },
    "upload_feasibility": {
        "phase": PHASE_A,
        "kind": "inprocess",
        "inputs": ["output/ozon-category.json"],
        # 阶段 A 跑在第 7 步，此时最终属性还没编译（第 11 步才由 field_completion 产出），
        # 所以上游语义是"早期预检回退读 ozon-attributes.json"；两个文件都可选，
        # 真正的"必填属性 missing == 0"硬门禁在提交前（upload payload / ozon_upload）。
        "optional_inputs": ["output/ozon-attributes-final.json", "output/ozon-attributes.json"],
        "outputs": ["output/upload-feasibility.json"],
        "gates": ["11 项检查全 PASS 才允许继续（类目、属性 schema、必填、SKU 结构、定价、包装>商品、图片、offer 冲突…）"],
    },
    "product_positioning": {
        "phase": PHASE_B,
        "kind": "ai",
        "inputs": ["output/product-analysis.json"],
        "outputs": ["output/product-positioning.json"],
        "gates": [],
    },
    "ecommerce_design": {
        "phase": PHASE_B,
        "kind": "ai",
        "inputs": [
            "input/source.json",
            "output/product-analysis.json",
            "output/product-positioning.json",
        ],
        # attribute-fill-input 是这一步自己的本地准备阶段产出的（原项目也是先跑
        # product_fact_merger / attribute_fill_input / image_source_preflight，再委托设计），
        # 所以它不能当成前置硬输入。
        "optional_inputs": ["output/attribute-fill-input.json"],
        "outputs": ["output/ozon-ecommerce-design.json"],
        "gates": ["设计合同校验（标题/简介/标签/图片方案齐备）"],
    },
    "russian_copy": {
        "phase": PHASE_B,
        "kind": "inprocess",
        "inputs": ["output/ozon-ecommerce-design.json"],
        "outputs": [
            "output/copy-ru.json",
            "output/title-ru.json",
            "output/description-ru.json",
            "output/keyword-research-ru.json",
        ],
        "gates": ["纯投影，不调模型；不得自造第二份文案"],
    },
    "field_completion": {
        "phase": PHASE_B,
        "kind": "local",
        "inputs": ["output/ozon-ecommerce-design.json", "output/ozon-category-attributes.json"],
        "outputs": [
            "output/ozon-attributes-final.json",
            "output/ozon-tags.json",
            "output/attribute-coverage-report.json",
        ],
        "gates": ["tags ≤30 且仅西里尔字母", "required_summary.missing == 0"],
    },
    "image_plan": {
        "phase": PHASE_B,
        "kind": "local",
        "inputs": ["output/ozon-ecommerce-design.json"],
        "outputs": ["output/image-plan.json"],
        "gates": ["N 张 SKU 主图 + 8 张共享详情图"],
    },
    "image_generation": {
        "phase": PHASE_B,
        "kind": "ai",
        "inputs": ["output/image-plan.json", "input/main-images", "input/sku-images", "input/detail-images"],
        "outputs": ["output/generated-images", "output/product-lock"],
        "gates": ["已通过的图位不重做", "图位重试上限（原项目默认 10）"],
    },
    "image_qc": {
        "phase": PHASE_B,
        "kind": "local",
        "inputs": ["output/generated-images"],
        "outputs": ["output/image-qc-report.json"],
        "gates": ["critical_failures > 0 必须回退重规划/重生成"],
    },
    "ozon_upload": {
        "phase": PHASE_B,
        "kind": "local",
        "inputs": ["output/ozon-attributes-final.json"],
        # 原项目在图片 QC 通过后由 field-completion 收尾产出这两个文件；它们依赖已确认的尺寸重量，
        # 我们不编造，所以先当可选输入（有就用，没有就按 uploader 的载荷构建器现算）。
        "optional_inputs": ["output/ozon-upload-config.json", "output/rich-content.json"],
        "outputs": ["output/ozon-result.json", "output/upload-summary.json"],
        "gates": [
            "app_mode 必须是 production（否则禁止真实上传）",
            "upload_feasibility 必须 PASS",
            "必填属性 missing == 0",
            "计划图必须齐全且有 https 公网地址",
            "buyer 可见文案不得含中日韩字符",
        ],
    },
}


def is_pipeline_step(step: Any) -> bool:
    return str(step) in PIPELINE_STEPS


def step_index(step: str) -> int:
    return PIPELINE_STEPS.index(step)


def next_step_after(step: str) -> str:
    """返回该步骤之后的下一步；已是最后一步则返回 ``complete``。"""
    if step not in PIPELINE_STEPS:
        return PIPELINE_STEPS[0]
    index = PIPELINE_STEPS.index(step)
    if index + 1 >= len(PIPELINE_STEPS):
        return "complete"
    return PIPELINE_STEPS[index + 1]


def step_definition(step: str) -> dict[str, Any]:
    if step not in STEP_DEFINITIONS:
        raise KeyError(f"未知步骤：{step}")
    return STEP_DEFINITIONS[step]


def ordered(completed: list[str] | tuple[str, ...] | None) -> list[str]:
    """把已完成步骤按流水线顺序重排（并丢掉非流水线步骤，保留 collect_source）。"""
    done = set(completed or ())
    result = ["collect_source"] if "collect_source" in done else []
    result.extend(step for step in PIPELINE_STEPS if step in done)
    return result


def contiguous_prefix(completed: list[str] | tuple[str, ...] | None) -> list[str]:
    """只保留从 ``collect_source`` 开始的连续前缀 —— 未提交前不许跳步。"""
    done = set(completed or ())
    prefix: list[str] = ["collect_source"] if "collect_source" in done else []
    for step in PIPELINE_STEPS:
        if step not in done:
            break
        prefix.append(step)
    return prefix
