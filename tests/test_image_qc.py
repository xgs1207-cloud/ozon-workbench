"""图片探测、占位生图与技术质检测试（全部本地、零外部依赖）。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collector.ingest import ingest_capture  # noqa: E402
from contracts import available_contracts, validate_contract  # noqa: E402
from models.fake import FakeProvider  # noqa: E402
from models.local_image import LocalPlaceholderGenerator  # noqa: E402
from pipeline.context import PipelineGateError, StepContext  # noqa: E402
from pipeline.handlers import run_single_step  # noqa: E402
from pipeline.image_generation import handle_image_generation  # noqa: E402
from pipeline.image_probe import aspect_ratio_text, probe_image, write_solid_png  # noqa: E402
from pipeline.image_qc import handle_image_qc, run_image_qc  # noqa: E402
from pipeline.selection import set_selected_keywords  # noqa: E402
from pipeline.upload import build_upload_payload  # noqa: E402

HAS_CONTRACTS = len(available_contracts()) > 0
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"
MINIMAL_GIF = b"GIF89a" + (10).to_bytes(2, "little") + (10).to_bytes(2, "little") + b"\x00\x00\x00"


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_probe_png_roundtrip(self):
        path = write_solid_png(self.root / "a.png", 900, 1200)
        probe = probe_image(path)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["format"], "png")
        self.assertEqual((probe["width"], probe["height"]), (900, 1200))
        self.assertEqual(aspect_ratio_text(900, 1200), "3:4")

    def test_probe_detects_gif(self):
        path = self.root / "a.gif"
        path.write_bytes(MINIMAL_GIF)
        probe = probe_image(path)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["format"], "gif")
        self.assertEqual((probe["width"], probe["height"]), (10, 10))

    def test_probe_rejects_garbage_and_missing(self):
        garbage = self.root / "garbage.png"
        garbage.write_bytes(b"not an image at all")
        self.assertFalse(probe_image(garbage)["ok"])
        self.assertFalse(probe_image(self.root / "missing.png")["ok"])
        empty = self.root / "empty.png"
        empty.write_bytes(b"")
        self.assertFalse(probe_image(empty)["ok"])

    def test_no_pillow_dependency(self):
        source = pathlib.Path(__file__).resolve().parents[1] / "pipeline" / "image_probe.py"
        self.assertNotIn("PIL", source.read_text(encoding="utf-8"))


class ImageQCFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.products = self.root / "products"
        self.provider = FakeProvider()
        self.summary = ingest_capture(
            self.products,
            {
                "source_url": "https://detail.1688.com/offer/121212121.html",
                "title_zh": "316 不锈钢保温杯",
                "category": {"category_id": "1001", "type_id": "2001"},
                "skus": [
                    {"sku_id": "S1", "color_ru": "красный", "capacity": "500 мл", "purchase_price_cny": 18.0},
                    {"sku_id": "S2", "color_ru": "синий", "capacity": "500 мл", "purchase_price_cny": 19.0},
                ],
            },
        )
        self.product_dir = self.products / self.summary["product_id"]
        for relative, count in (("main-images", 2), ("sku-images", 2), ("detail-images", 3)):
            directory = self.product_dir / "input" / relative
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (directory / f"{index:02d}.png").write_bytes(IMAGE_BYTES + relative.encode() + bytes([index]))
        set_selected_keywords(self.product_dir, ["термос 500 мл", "термос для чая"])
        run_single_step(self.product_dir, "product_analysis", provider=self.provider)
        run_single_step(self.product_dir, "russian_copy", provider=self.provider)
        run_single_step(self.product_dir, "image_plan", provider=self.provider)

    def tearDown(self):
        self.tmp.cleanup()

    def generate(self, generator=None):
        return handle_image_generation(
            StepContext(
                product_dir=self.product_dir,
                step="image_generation",
                image_generator=generator or LocalPlaceholderGenerator(),
            )
        )

    def qc(self):
        return handle_image_qc(StepContext(product_dir=self.product_dir, step="image_qc"))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class GenerationTests(ImageQCFixture):
    def test_placeholder_generation_writes_all_slots(self):
        result = self.generate()
        self.assertEqual(result["generated_slots"], 10)  # 2 主图 + 8 详情图
        report = json.loads(
            (self.product_dir / "output" / "image-generation-report.json").read_text(encoding="utf-8")
        )
        self.assertFalse(report["final_images"])
        self.assertIn("非最终图片", result["warnings"][0])
        self.assertIn("local-placeholder", result["warnings"][0])
        for item in report["files"]:
            probe = probe_image(self.product_dir / item["path"])
            self.assertTrue(probe["ok"], item)
            self.assertEqual((probe["width"], probe["height"]), (900, 1200))

    def test_generation_without_plan_is_rejected(self):
        (self.product_dir / "output" / "image-plan.json").unlink()
        with self.assertRaises(PipelineGateError):
            self.generate()

    def test_bad_generator_output_is_caught(self):
        class BrokenGenerator(LocalPlaceholderGenerator):
            name = "broken"

            def generate(self, request):
                result = super().generate(request)
                for item in result["generated"]:
                    (request.product_dir / item["path"]).write_bytes(b"garbage")
                return result

        with self.assertRaises(PipelineGateError) as ctx:
            self.generate(BrokenGenerator())
        self.assertIn("不可读", str(ctx.exception))


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class QCTests(ImageQCFixture):
    def test_qc_passes_technically_and_reports_semantic_gap(self):
        self.generate()
        result = self.qc()
        report = json.loads(
            (self.product_dir / "output" / "image-qc-report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validate_contract("image-qc-report", report), [])
        self.assertEqual(report["critical_failures"], [])
        self.assertEqual(report["decision"], "revise")  # 语义维度未评分 → 不谎报 pass
        self.assertFalse(report["regenerate_needed"])
        self.assertEqual(result["decision"], "revise")
        self.assertTrue(any("占位图" in item for item in report["suggestions"]))
        self.assertEqual(report["dimensions"]["visual_quality"]["status"], "pass")
        self.assertEqual(report["dimensions"]["product_consistency"]["score"], 0)

    def test_wrong_aspect_ratio_is_critical(self):
        self.generate()
        # 把一张主图改成正方形
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        target = self.product_dir / plan["main_images"][0]["output_path"]
        write_solid_png(target, 1000, 1000)
        with self.assertRaises(PipelineGateError) as ctx:
            self.qc()
        report = json.loads(
            (self.product_dir / "output" / "image-qc-report.json").read_text(encoding="utf-8")
        )
        self.assertIn("aspect_ratio_mismatch", report["critical_failures"])
        self.assertTrue(report["regenerate_needed"])
        self.assertTrue((self.product_dir / "output" / "image-regeneration-request.json").is_file())
        self.assertIn("aspect_ratio_mismatch", str(ctx.exception))

    def test_non_png_format_is_rejected(self):
        self.generate()
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        target = self.product_dir / plan["detail_images"][0]["output_path"]
        target.write_bytes(MINIMAL_GIF)
        with self.assertRaises(PipelineGateError):
            self.qc()
        report = json.loads(
            (self.product_dir / "output" / "image-qc-report.json").read_text(encoding="utf-8")
        )
        self.assertIn("unsupported_format", report["critical_failures"])

    def test_low_resolution_is_revise_not_reject(self):
        self.generate()
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        write_solid_png(self.product_dir / plan["main_images"][0]["output_path"], 600, 800)
        report = run_image_qc(self.product_dir)
        self.assertEqual(validate_contract("image-qc-report", report), [])
        self.assertEqual(report["critical_failures"], [])
        self.assertEqual(report["decision"], "revise")
        self.assertTrue(report["regenerate_needed"])
        statuses = {item["slot"]: item["status"] for item in report["technical_checks"]}
        self.assertEqual(statuses["main-S1"], "revise")

    def test_missing_reference_is_flagged(self):
        self.generate()
        plan_path = self.product_dir / "output" / "image-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for item in plan["main_images"] + plan["detail_images"]:
            item["reference_image_ids"] = []
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        report = run_image_qc(self.product_dir)
        self.assertTrue(any(issue["code"] == "missing_reference" for issue in report["issues"]))
        self.assertEqual(report["images_checked"][0]["reference_images"], ["unknown"])


@unittest.skipUnless(HAS_CONTRACTS, "contracts/original 尚未拉取")
class UploadGateIntegrationTests(ImageQCFixture):
    def test_upload_payload_blocks_without_qc_report(self):
        self.generate()
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(any("图片质检报告" in item for item in payload["production_blockers"]))

    def test_upload_payload_blocks_on_qc_critical(self):
        self.generate()
        plan = json.loads((self.product_dir / "output" / "image-plan.json").read_text(encoding="utf-8"))
        write_solid_png(self.product_dir / plan["main_images"][0]["output_path"], 1000, 1000)
        # 直接跑报告并落盘（handler 会把 critical 转成人工处理，这里只验证上传门禁读取报告）
        report = run_image_qc(self.product_dir)
        (self.product_dir / "output" / "image-qc-report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertTrue(any("图片质检" in item for item in payload["production_blockers"]))
        self.assertIn("aspect_ratio_mismatch", payload["image_upload_gate"]["qc_critical_failures"])

    def test_upload_gate_reports_qc_decision_when_clean(self):
        self.generate()
        self.qc()
        payload = build_upload_payload(self.product_dir, shop_name="shop-a")
        self.assertEqual(payload["image_upload_gate"]["qc_critical_failures"], [])
        self.assertEqual(payload["image_upload_gate"]["qc_decision"], "revise")
        self.assertEqual(payload["image_upload_gate"]["semantic_qc"], "not_configured")


if __name__ == "__main__":
    unittest.main()
