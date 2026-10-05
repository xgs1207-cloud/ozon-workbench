"""一键演示：采集入库 → 查重 → 建批次 → 干跑 → 选词 → 生成俄文文案。

    python examples/run_demo.py

全程在临时目录里做，不污染仓库，也**不碰 Ozon**（runner 强制校验 api_write_count == 0）。
M2 用的是确定性 fake 模型层：它的产物必须通过上游契约（contracts/original/*.schema.json）与
规则校验（rules/），所以真实 adapter 接上后可以拿它对照。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import DuplicateCaptureError, import_folder  # noqa: E402
from keyword_library import store as keyword_store  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.local_image import LocalPlaceholderGenerator  # noqa: E402
from pipeline import status as st  # noqa: E402
from pipeline.batch import create_batch  # noqa: E402
from pipeline.catalog import handle_field_completion, handle_variant_rules  # noqa: E402
from pipeline.context import StepContext  # noqa: E402
from pipeline.doctor import render_report, run_doctor  # noqa: E402
from pipeline.handlers import run_single_step  # noqa: E402
from pipeline.image_generation import handle_image_generation  # noqa: E402
from pipeline.image_qc import handle_image_qc  # noqa: E402
from pipeline.measurements import handle_measurements  # noqa: E402
from pipeline.publications import plan_publications, render_plan  # noqa: E402
from pipeline.publish_urls import write_url_map  # noqa: E402
from pipeline.runner import run_product  # noqa: E402
from pipeline.selection import select_from_library  # noqa: E402
from pipeline.upload import (  # noqa: E402
    DryRunUploader,
    SimulatedUploader,
    build_upload_payload,
    payload_problems,
    upload_product,
)

SOURCE_URL = "https://detail.1688.com/offer/123456789.html"


def build_capture_folder(root: pathlib.Path) -> pathlib.Path:
    """模拟一次真实采集的产物目录（插件或手工整理都长这样）。"""
    folder = root / "capture-123456789"
    (folder / "main-images").mkdir(parents=True)
    (folder / "detail-images").mkdir(parents=True)
    (folder / "main-images" / "01-red.png").write_bytes(b"\x89PNG\r\n\x1a\n main red")
    (folder / "main-images" / "02-blue.png").write_bytes(b"\x89PNG\r\n\x1a\n main blue")
    (folder / "detail-images" / "01-size.png").write_bytes(b"\x89PNG\r\n\x1a\n detail size")
    (folder / "product.json").write_text(
        json.dumps(
            {
                "source_url": SOURCE_URL,
                "title_zh": "316 不锈钢保温杯",
                "category": {
                    "category_id": "1001",
                    "type_id": "2001",
                    "category_path_zh": "家居/厨房（占位，需换成真实 Ozon 类目）",
                },
                "skus": [
                    {"sku_id": "S1", "offer_id": "SKU-RED-500", "purchase_price_cny": "18,50", "color_zh": "红色"},
                    {"sku_id": "S2", "offer_id": "SKU-BLUE-500", "purchase_price_cny": 19.0, "color_zh": "蓝色"},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return folder


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        products = root / "products"
        folder = build_capture_folder(root)

        print("== ① 采集入库（文件夹导入）==")
        summary = import_folder(products, folder)
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        print("\n== ② 重复采集查重（不覆盖已有商品）==")
        try:
            import_folder(products, folder)
        except DuplicateCaptureError as error:
            print(json.dumps({"ok": False, **error.to_dict()}, ensure_ascii=False, indent=2))

        print("\n== ③ 建批次并授权 ==")
        batch = create_batch(products, batches_root=root / "batches", target_store_ids=["demo-shop-a"])
        saved = st.load_status(products / summary["product_id"])
        print(
            json.dumps(
                {
                    "batch_id": batch["batch_id"],
                    "inventory_submission_enabled": batch["inventory_submission_enabled"],
                    "task_authorized": saved["task_authorized"],
                    "sku_run_snapshot": saved["sku_run_snapshot"],
                    "source_snapshot_binding": saved["source_snapshot_binding"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        print("\n== ④ 干跑（永不碰 Ozon）==")
        report = run_product(products / summary["product_id"], until="upload_feasibility")
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "dry_run",
                        "stop_reason",
                        "stopped_at",
                        "executed",
                        "completed_steps",
                        "pending_steps",
                        "product_status",
                        "api_write_count",
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        print("\n== ⑤ 关键词库 → 选词 ==")
        library = root / "keyword-library"
        keyword_store.upsert(
            library,
            [
                {"keyword": "термос 500 мл", "category_id": "1001", "type_id": "2001", "search_volume": 12400, "competitor_count": 830},
                {"keyword": "термос для чая", "category_id": "1001", "type_id": "2001", "search_volume": 3100, "competitor_count": 120},
                {"keyword": "термос подарочный", "category_id": "1001", "type_id": "2001", "search_volume": 1300, "competitor_count": 60},
                {"keyword": "термос стальной", "category_id": "1001", "type_id": "2001", "search_volume": 8800, "competitor_count": 1900},
            ],
        )
        qualified = keyword_store.query(
            library, category_id="1001", type_id="2001", status=keyword_store.STATUS_QUALIFIED
        )
        ranked = keyword_store.query(library, category_id="1001", type_id="2001", order="score", limit=5)
        print(f"  默认门槛下达标 {len(qualified)} 条（门槛偏严，靠人工确认入库补足）")
        # 真实流程：人在界面里挑词并入库；这里模拟确认分数最高的两条
        keyword_store.set_status(
            library,
            [item["key"] for item in ranked[:2]],
            keyword_store.STATUS_IN_LIBRARY,
            reason="演示：人工确认入库",
        )
        selection = select_from_library(products / summary["product_id"], library, limit=3)
        print(
            json.dumps(
                {
                    "source": selection["source"],
                    "keywords": [
                        {"keyword": item["keyword"], "score": item["score"], "status": item["status"]}
                        for item in selection["keywords"]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        print("\n== ⑥ 模型层生成（fake，产物过契约+规则校验）==")
        provider = FakeProvider()
        product_dir = products / summary["product_id"]
        for step in ("product_analysis", "product_positioning", "ecommerce_design"):
            result = run_single_step(product_dir, step, provider=provider)
            print(f"  {step}: " + json.dumps({k: v for k, v in result.items() if k != "artifacts"}, ensure_ascii=False))

        design = json.loads((product_dir / "output" / "ozon-ecommerce-design.json").read_text(encoding="utf-8"))
        print(
            "\n设计文档（ozon-ecommerce-design，16 个顶层键）："
            + json.dumps(
                {
                    "seo_title_ru": design["listing"]["seo_title_ru"],
                    "short_title_ru": design["listing"]["short_title_ru"],
                    "selling_points": len(design["listing"]["selling_points"]),
                    "primary_keywords": [item["text_ru"] for item in design["listing"]["keywords"]["primary"]],
                    "hashtags": design["listing"]["hashtags"],
                    "visual_style": design["visual_system"]["style_name"],
                    "main_images": len(design["main_images"]),
                    "detail_images": len(design["detail_images"]),
                    "sku_plan": [item["name_ru"] for item in design["sku_plan"]],
                    "attribute_decisions": design["attribute_decisions"]["coverage_summary"],
                    "decision_steps": len(design["decision_trace"]["steps"]),
                    "compliance_status": design["decision_trace"]["compliance_status"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        print("\n== ⑥b 文案投影（上游语义：russian_copy 不调模型，从设计文档投影）==")
        copy_result = run_single_step(product_dir, "russian_copy", provider=provider)
        print(f"  russian_copy: " + json.dumps({k: v for k, v in copy_result.items() if k != "artifacts"}, ensure_ascii=False))

        copy = json.loads((product_dir / "output" / "copy-ru.json").read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "title_ru": copy["title_ru"],
                    "short_title_ru": copy["short_title_ru"],
                    "hashtags": copy["hashtags"],
                    "description_sections": list(copy["description_sections"]),
                    "generated_by": copy["generated_by"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        print("\n== ⑦ 图片规划（N 张 SKU 主图 + 恰好 8 张共享详情图）==")
        plan_result = run_single_step(product_dir, "image_plan", provider=provider)
        plan = json.loads((product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "structure": plan["image_set_structure"],
                    "variant_strategy": plan["variant_image_strategy"],
                    "main_images": [
                        {"slot": item["slot"], "status": item["status"], "operation": item["operation"]}
                        for item in plan["main_images"]
                    ],
                    "detail_slots": [item["slot"] for item in plan["detail_images"]],
                    "risks": plan["risks"],
                    "needs_review": plan_result.get("needs_review"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        brief = (products / summary["product_id"] / "output" / "image-plan-brief.md").read_text(
            encoding="utf-8"
        )
        head = brief.split("## 共享详情图")[0]
        print("\n--- output/image-plan-brief.md 节选（主图部分）---")
        print(head.strip()[:1400])

        print("\n== ⑧ 类目属性编译 + 多店铺分发计划（全部本地，不碰 Ozon）==")
        product_dir = products / summary["product_id"]
        # 真实项目里这两个文件由 category_match 调 Ozon Seller API 拉取；演示用占位类目
        (product_dir / "output" / "ozon-category.json").write_text(
            json.dumps(
                {
                    "metadata_source": "ozon_seller_api",
                    "category_id": 1001,
                    "type_id": 2001,
                    "match_status": "api_confirmed",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (product_dir / "output" / "ozon-category-attributes.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "product_id": product_dir.name,
                    "fetched_at": "2026-10-05T12:00:00+08:00",
                    "api_endpoint": "/v1/description-category/attribute",
                    "category_id": 1001,
                    "category_name": "示例类目（占位）",
                    "type_id": 2001,
                    "attributes": [
                        {
                            "attribute_id": 85,
                            "attribute_name": "Бренд",
                            "required": True,
                            "type": "String",
                            "dictionary_id": 28732,
                            "complex_id": 0,
                            "is_collection": False,
                            "allowed_values": [{"id": 126745801, "value": "Нет бренда"}],
                            "values_truncated": False,
                        },
                        {
                            "attribute_id": 10097,
                            "attribute_name": "Название цвета",
                            "required": True,
                            "type": "String",
                            "dictionary_id": None,
                            "complex_id": 0,
                            "is_collection": False,
                            "allowed_values": [],
                            "values_truncated": False,
                        },
                    ],
                    "warnings": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        variant = handle_variant_rules(StepContext(product_dir, "variant_rules", True, "development", {}, None))
        completion = handle_field_completion(
            StepContext(product_dir, "field_completion", True, "development", {}, None)
        )
        attributes = json.loads(
            (product_dir / "output" / "ozon-attributes-final.json").read_text(encoding="utf-8")
        )
        print(
            json.dumps(
                {
                    "upload_strategy": variant["upload_strategy"],
                    "required_attributes": attributes["required_summary"],
                    "warnings": completion["warnings"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        plan_first = plan_publications(
            product_dir, ["demo-shop-a", "demo-shop-b"], sku_ids=["S1", "S2"], enabled_store_ids=["demo-shop-a"]
        )
        print("\n首次分发计划：")
        print(render_plan(plan_first))

        print("\n== ⑨ 生图（本地占位图）与图片质检（真跑）==")
        generation = handle_image_generation(
            StepContext(
                product_dir=product_dir,
                step="image_generation",
                image_generator=LocalPlaceholderGenerator(),
            )
        )
        print(
            "生图："
            + json.dumps(
                {key: generation[key] for key in ("generator", "generated_slots")},
                ensure_ascii=False,
            )
            + f"  警告：{generation['warnings'][0] if generation['warnings'] else '无'}"
        )
        try:
            qc = handle_image_qc(StepContext(product_dir=product_dir, step="image_qc"))
            qc_report = json.loads(
                (product_dir / "output" / "image-qc-report.json").read_text(encoding="utf-8")
            )
            print(
                "质检："
                + json.dumps(
                    {
                        "decision": qc_report["decision"],
                        "score": qc_report["score"],
                        "critical_failures": qc_report["critical_failures"],
                        "checked": len(qc_report["technical_checks"]),
                        "recommendation": qc_report["recommendation"],
                    },
                    ensure_ascii=False,
                )
            )
            print("质检建议：" + "；".join(qc["warnings"]))
        except Exception as error:  # noqa: BLE001 - 演示里把门禁失败如实打印
            print(f"质检未通过：{error}")

        print("\n== ⑩ 定价与尺寸重量（真实引擎）+ 上传载荷与干跑提交 ==")
        before = build_upload_payload(product_dir, shop_name="demo-shop-a")
        print("补齐数据前的阻断项：")
        print(json.dumps(before["production_blockers"], ensure_ascii=False, indent=2))

        # 真实流程：尺寸重量由人在 SKU 资料表里确认；定价引擎按 config/pricing.json 计算
        (product_dir / "input" / "workbench-sku-overrides.json").write_text(
            json.dumps(
                {
                    "product": {
                        "product_length_mm": 90,
                        "product_width_mm": 90,
                        "product_height_mm": 250,
                        "product_weight_g": 350,
                        "package_length_mm": 110,
                        "package_width_mm": 110,
                        "package_height_mm": 280,
                        "package_weight_g": 430,
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        measurements_result = handle_measurements(
            StepContext(product_dir=product_dir, step="measurements")
        )
        pricing = json.loads(
            (product_dir / "output" / "pricing-result.json").read_text(encoding="utf-8")
        )
        print(
            "\n定价引擎："
            + json.dumps(
                {
                    "recommendation": pricing["recommendation"],
                    "measurement_source": measurements_result["measurement_source"],
                    "skus": [
                        {
                            "sku_id": row["sku_id"],
                            "cost_cny": row["base_cost_cny"],
                            "price_rub": row["selling_price_rub"],
                            "margin": row["margin_rate"],
                            "status": row["status"],
                        }
                        for row in pricing["skus"]
                    ],
                },
                ensure_ascii=False,
            )
        )

        # 对象存储 adapter 的落地点：写出图位 → 公网 URL 映射
        write_url_map(product_dir, "https://cdn.example.com")

        payload = build_upload_payload(product_dir, shop_name="demo-shop-a")
        print(
            "\n补齐后的载荷："
            + json.dumps(
                {
                    "problems": payload_problems(payload),
                    "variant_mapping_status": payload["product_group"]["variant_mapping_status"],
                    "variants": len(payload["variants"]),
                    "images": len(payload["images"]),
                    "image_gate_passed": payload["image_upload_gate"]["passed"],
                    "inventory_fields_included": payload["api_request_template"]["inventory_fields_included"],
                },
                ensure_ascii=False,
            )
        )

        dry = upload_product(
            product_dir, ["demo-shop-a", "demo-shop-b"], DryRunUploader(), enabled_store_ids=["demo-shop-a"]
        )
        print(
            "干跑提交："
            + json.dumps(
                {key: dry[key] for key in ("uploader", "submitted", "skipped", "failed", "api_writes")},
                ensure_ascii=False,
            )
        )
        receipt = json.loads(
            (product_dir / "output" / "store-runs" / "demo-shop-a" / "ozon-result.json").read_text(encoding="utf-8")
        )
        print(
            "回执（output/store-runs/demo-shop-a/ozon-result.json）："
            + json.dumps(
                {"status": receipt["status"], "task_id": receipt["task_id"], "items": len(receipt["items"])},
                ensure_ascii=False,
            )
        )

        print("\n幂等（模拟 uploader 拿到 task_id 后不再重复创建）：")
        simulated = SimulatedUploader()
        first = upload_product(product_dir, ["demo-shop-c"], simulated)
        second = upload_product(product_dir, ["demo-shop-c"], simulated)
        print(
            json.dumps(
                {
                    "first": {key: first[key] for key in ("submitted", "skipped", "api_writes")},
                    "second": {key: second[key] for key in ("submitted", "skipped")},
                    "skip_reason": second["stores"]["demo-shop-c"].get("reason"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        try:
            upload_product(product_dir, ["demo-shop-a"], DryRunUploader(), upload_mode="production")
        except ValueError as error:
            print(f"\nproduction 模式保护：{error}")

        print("\n== ⑪ 上线前预检（doctor：还差什么才能真正提交）==")
        report = run_doctor(
            products,
            product_ids=[product_dir.name],
            registry_path=root / "config" / "shops.json",
            env={},
        )
        print(render_report(report))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
