"""Offline rich editing, media ownership, AI review and official JSON compile."""
from copy import deepcopy
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from pipeline import rich_content as rich
from pipeline.context import StepContext, PipelineGateError
from pipeline.listing_form import read_json, write_json
import workbench_rich_content_api as rich_api


def pixels(color="pink", size=(64, 96)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStorage:
    dry_run = False
    def __init__(self):
        self.calls = []
    def publish_slot(self, root, row):
        self.calls.append(deepcopy(row))
        body = (root / row["output_path"]).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        return {"status": "uploaded", "key": row["slot"], "url": "https://media.example.invalid/" + digest + ".png",
                "sha256": digest, "bytes": len(body)}


class FakeTransport:
    def __init__(self, result, during=None):
        self.result, self.during, self.calls = result, during, []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.during:
            self.during()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result if isinstance(self.result, str) else json.dumps(self.result, ensure_ascii=False)


class RichContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "P000001"
        write_json(self.root / "input/source.json", {"title_zh": "粉色硅胶抗压玩具", "source_url": "https://detail.1688.com/offer/123456.html",
            "attributes_zh": {"material": "硅胶"},
            "skus": [{"sku_id": "S1", "name_zh": "粉色", "color_ru": "розовый"}]})
        write_json(self.root / "input/selected-skus.json", {"selected": ["S1"]})
        self.selection = {"source": "ozon_seller_api", "shop_id": "qa", "category_id": 12, "type_id": 34,
                          "confirmed_by_user": True, "category_name": "Антистресс"}
        write_json(self.root / "input/category-selection.json", self.selection)
        write_json(self.root / "input/category-form.json", {**self.selection,
            "fields": [{"attribute_id": 11254, "name": "JSON rich content", "type": "string", "complex_id": None}]})
        write_json(self.root / "input/selected-keywords.json", {"keywords": [
            {"keyword": "антистресс игрушка", "role": "core"},
            {"keyword": "чужой бренд", "role": "reject"}, {"keyword": "advertising phrase", "role": "ad"}]})
        write_json(self.root / "output/product-analysis.json", {"facts": {"material": "силикон"}, "selling_points": ["真实粉色外观"]})
        write_json(self.root / "output/copy-ru.json", {"title_ru": {"title": "Антистресс игрушка розовая"}})
        self.source_image = self.root / "input/main-images/1.png"
        self.source_image.parent.mkdir(parents=True)
        self.source_image.write_bytes(pixels())
        self.source_bytes = self.source_image.read_bytes()
        self.image = rich.media_library(self.root)[0]

    def draft(self, blocks=None, revision=0):
        if blocks is None:
            blocks = [{"id": "b1", "type": "image_text", "image_id": self.image["id"],
                       "title": "Антистресс игрушка", "text": "Розовая игрушка из силикона."}]
        return rich.save_content(self.root, revision=revision, context_fingerprint=rich.context_fingerprint(self.root), blocks=blocks)

    def request(self, **kwargs):
        return {"revision": 0, "context_fingerprint": rich.context_fingerprint(self.root), "prompt": "根据卖点写富内容，包含选定关键词",
                "blocks": [], **kwargs}

    def test_get_does_not_write_files_or_call_models(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = rich.read_content(self.root)
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["blocks"], [])
        self.assertEqual(result["attribute_id"], 11254)
        self.assertIn("/rich-content/images/img-", result["media"][0]["preview_url"])
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_import_verifies_pixels_uses_server_hash_and_does_not_change_revision(self):
        result = rich.import_image(self.root, filename="../../sensitive.html", data_base64=base64.b64encode(pixels("blue")).decode())
        self.assertEqual(result["image"]["kind"], "imported")
        self.assertTrue(result["image"]["relative_path"].startswith(rich.IMAGE_DIRECTORY + "/"))
        self.assertTrue(result["image"]["relative_path"].endswith(".png"))
        self.assertEqual(rich.read_content(self.root)["revision"], 0)
        self.assertFalse((self.root / "sensitive.html").exists())
        with self.assertRaises(ValueError):
            rich.import_image(self.root, filename="bad.png", data_base64="not base64!")
        with self.assertRaises(ValueError):
            rich.import_image(self.root, filename="bad.png", data_base64=base64.b64encode(b"<svg onload=evil>").decode())

    def test_large_pixel_upload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "1600"):
            rich.import_image(self.root, filename="large.png", data_base64=base64.b64encode(pixels(size=(4001, 4000))).decode())

    def test_save_freezes_source_and_generated_images_independently(self):
        generated = self.root / "output/generated-images/other.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(pixels("blue"))
        write_json(self.root / "output/image-generation-report.json", {"files": [
            {"path": "output/generated-images/other.png", "generator": "rightapi", "slot": "single-2"}]})
        other = next(row for row in rich.media_library(self.root) if row["kind"] == "generated")
        self.draft([{ "id": "b1", "type": "image", "image_id": self.image["id"]},
                    {"id": "b2", "type": "image_text", "image_id": other["id"], "text": "Игрушка"}])
        before = rich.content_version(self.root)
        self.source_image.write_bytes(pixels("yellow"))
        generated.write_bytes(pixels("green"))
        self.assertEqual(rich.content_version(self.root), before)
        self.assertEqual(rich.media_path(self.root, self.image["id"]).read_bytes(), self.source_bytes)

    def test_save_cannot_inject_remote_url_traversal_or_markup(self):
        for block in (
            {"type": "image", "image_id": "https://127.0.0.1/private"},
            {"type": "image", "image_id": "../../file.png"},
            {"type": "text", "text": "<script>alert(1)</script>"},
            {"type": "text", "text": "https://example.com"},
            {"type": "text", "text": "mail@example.com"},
            {"type": "text", "text": "shop.example.com"},
            {"type": "text", "text": "ftp://example.com/catalog"},
            {"type": "text", "text": "+7 999 123-45-67"},
            {"type": "text", "text": "Телефон 8 (999) 123-45-67"}):
            with self.assertRaises(ValueError):
                self.draft([block])
        self.assertFalse((self.root / rich.DRAFT_FILE).exists())
        self.draft([{ "type": "text", "text": "Размер 100 × 90 × 80 мм; модель WB-12345678901."}])

    def test_duplicate_ids_and_empty_saved_block_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            self.draft([{ "id": "a", "type": "text", "text": "A"}, {"id": "a", "type": "text", "text": "B"}])
        with self.assertRaisesRegex(ValueError, "为空"):
            self.draft([{ "id": "a", "type": "text", "text": ""}])

    def test_stale_revision_and_fact_changes_never_overwrite(self):
        saved = self.draft()
        original = (self.root / rich.DRAFT_FILE).read_bytes()
        with self.assertRaises(rich.RichContentConflict):
            rich.save_content(self.root, revision=0, context_fingerprint=saved["context_fingerprint"], blocks=[])
        write_json(self.root / "input/selected-keywords.json", {"keywords": [{"keyword": "new query"}]})
        with self.assertRaises(rich.RichContentConflict):
            rich.save_content(self.root, revision=1, context_fingerprint=saved["context_fingerprint"], blocks=[])
        self.assertEqual(original, (self.root / rich.DRAFT_FILE).read_bytes())

    def test_audit_refresh_does_not_invalidate_context_but_dictionary_change_does(self):
        write_json(self.root / "input/human-confirmations.json", {"attributes": {"102": [{"value": "силикон", "dictionary_value_id": 2}]},
            "category_form_scope": "old", "category_form_confirmed_at": "old", "provenance": {"foo": "old"}})
        initial = rich.context_fingerprint(self.root)
        doc = read_json(self.root / "input/human-confirmations.json")
        doc.update(category_form_scope="new", category_form_confirmed_at="new", provenance={"foo": "new"})
        write_json(self.root / "input/human-confirmations.json", doc)
        form = read_json(self.root / "input/category-form.json")
        form.update(fetched_at="2099-01-01", cache_hit=True)
        write_json(self.root / "input/category-form.json", form)
        self.assertEqual(initial, rich.context_fingerprint(self.root))
        doc["attributes"]["102"][0]["dictionary_value_id"] = 3
        write_json(self.root / "input/human-confirmations.json", doc)
        self.assertNotEqual(initial, rich.context_fingerprint(self.root))

    def test_attribute_requires_official_form_and_matching_shop_category(self):
        form = read_json(self.root / "input/category-form.json")
        for changes in ({"source": "manual"}, {"shop_id": "other"}, {"type_id": 88},
                        {"fields": [{"attribute_id": 123, "name": "JSON unrelated"}]}):
            write_json(self.root / "input/category-form.json", {**form, **changes})
            self.assertIsNone(rich.attribute_id(self.root))
            with self.assertRaisesRegex(ValueError, "官方字段"):
                self.draft()

    def test_ai_only_one_call_includes_selected_facts_keywords_and_returns_review_candidate(self):
        result = {"message_zh": "已自然加入关键词，请核对。", "blocks": [{"id": "t", "type": "text",
                  "title": "Антистресс игрушка", "text": "Розовая игрушка из силикона."}]}
        transport = FakeTransport(result)
        response = rich.generate_content(self.root, provider=SimpleNamespace(transport=transport), **self.request())
        self.assertEqual(response["candidate"]["status"], "candidate")
        self.assertEqual(len(transport.calls), 1)
        body = json.loads(transport.calls[0]["user"])
        self.assertEqual(body["product"]["selected_keywords"], [{"keyword": "антистресс игрушка", "role": "core"}])
        self.assertIn("硅胶", json.dumps(body["product"]["verified_facts"], ensure_ascii=False))
        self.assertEqual([r["sku_id"] for r in body["product"]["selected_skus"]], ["S1"])
        self.assertFalse((self.root / rich.DRAFT_FILE).exists())
        applied = rich.apply_candidate(self.root, revision=0, context_fingerprint=response["candidate"]["context_fingerprint"],
                                      candidate_id=response["candidate"]["id"])
        self.assertEqual(applied["revision"], 1)
        self.assertEqual(applied["blocks"], response["candidate"]["blocks"])

    def test_ai_can_fill_incomplete_editor_but_bad_result_does_not_modify_draft(self):
        transport = FakeTransport({"blocks": [{"id": "t", "type": "text", "text": "Игрушка"}]})
        response = rich.generate_content(self.root, provider=SimpleNamespace(transport=transport),
            **self.request(blocks=[{"id": "placeholder", "type": "text", "text": ""}]))
        self.assertTrue(response["ok"])
        transport = FakeTransport({"blocks": [{"type": "image", "image_id": "http://localhost"}]})
        with self.assertRaises(ValueError):
            rich.generate_content(self.root, provider=SimpleNamespace(transport=transport), **self.request())
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse((self.root / rich.DRAFT_FILE).exists())

    def test_ai_timeout_or_invalid_json_never_auto_retries_or_changes_saved_draft(self):
        saved = self.draft()
        before = (self.root / rich.DRAFT_FILE).read_bytes()
        for result in (TimeoutError("private backend detail"), "not json"):
            transport = FakeTransport(result)
            with self.assertRaises(Exception):
                rich.generate_content(self.root, provider=SimpleNamespace(transport=transport),
                    **self.request(revision=1, blocks=saved["blocks"]))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(before, (self.root / rich.DRAFT_FILE).read_bytes())

    def test_ai_stale_async_result_is_retained_not_applied(self):
        transport = FakeTransport({"blocks": [{"type": "text", "text": "Игрушка"}]}, during=lambda:
            write_json(self.root / "input/selected-keywords.json", {"keywords": [{"keyword": "changed"}]}))
        with self.assertRaises(rich.RichContentConflict):
            rich.generate_content(self.root, provider=SimpleNamespace(transport=transport), **self.request())
        self.assertEqual(read_json(self.root / rich.CANDIDATES_FILE)["candidates"][0]["status"], "stale")
        self.assertFalse((self.root / rich.DRAFT_FILE).exists())

    def test_changed_sku_ai_excludes_historical_summary_copy_and_other_variant_facts(self):
        source = read_json(self.root / "input/source.json")
        source["skus"].append({"sku_id": "S2", "name_zh": "蓝色", "color_ru": "синий"})
        write_json(self.root / "input/source.json", source)
        write_json(self.root / "output/product-analysis.json", {"facts": {"material": "forbidden_old_material",
            "skus": [{"sku_id": "S1", "name_cn": "old_pink"}]}, "selling_points": ["old_pink_claim"]})
        write_json(self.root / "output/copy-ru.json", {"title_ru": "old_pink_title", "description_ru": "old_pink_description"})
        write_json(self.root / "input/guided-workflow.json", {"analysis": {"input_fingerprint": "obsolete"},
                                                            "copy": {"input_fingerprint": "obsolete"}})
        write_json(self.root / "input/selected-skus.json", {"selected": ["S2"]})
        transport = FakeTransport({"blocks": [{"type": "text", "text": "Синяя игрушка"}]})
        response = rich.generate_content(self.root, provider=SimpleNamespace(transport=transport), **self.request())
        self.assertTrue(response["ok"])
        body = json.loads(transport.calls[0]["user"])
        self.assertEqual([row["sku_id"] for row in body["product"]["selected_skus"]], ["S2"])
        self.assertEqual(body["product"]["approved_copy"], {})
        serialized = json.dumps(body["product"], ensure_ascii=False)
        self.assertNotIn("old_pink", serialized)
        self.assertNotIn("forbidden_old_material", serialized)
        self.assertFalse(body["product"]["summary_confirmed"])

    def test_publication_compiles_exact_official_json_and_bound_image_bytes(self):
        self.draft()
        with self.assertRaisesRegex(ValueError, "尚未公开"):
            rich.compile_attribute(self.root)
        storage = FakeStorage()
        result = rich.publish_content(self.root, storage=storage)
        payload = json.loads(rich.compile_attribute(self.root)["value"])
        self.assertEqual(payload["version"], 0.2)
        self.assertEqual(payload["content"][0]["widgetName"], "raShowcase")
        showcase = payload["content"][0]["blocks"][0]
        self.assertEqual(showcase["img"]["width"], 64)
        self.assertEqual(showcase["img"]["srcMobile"], showcase["img"]["src"])
        self.assertEqual(showcase["text"], {"content": ["Розовая игрушка из силикона."], "theme": "default"})
        self.assertEqual(payload, result["json"])
        self.assertEqual(len(storage.calls), 1)
        binding = rich.publication_binding(self.root)
        self.assertEqual(binding["expected_content"][showcase["img"]["src"]]["sha256"], self.image["sha256"])
        fake = deepcopy(payload)
        fake["content"][0]["blocks"][0]["text"] = "Wrong flat string"
        with self.assertRaisesRegex(ValueError, "官方"):
            rich._validate_official(fake)

    def test_text_only_publishes_without_cos_and_edit_invalidates_only_fields(self):
        self.draft([{ "id": "t", "type": "text", "title": "Игрушка", "text": "Силикон.\nРозовый цвет."}])
        with patch("pipeline.oss_cos._storage_from_env", side_effect=AssertionError("No storage needed")):
            result = rich.publish_content(self.root)
        self.assertEqual(result["images"], [])
        content = json.loads(rich.compile_attribute(self.root)["value"])["content"][0]
        self.assertEqual(content["widgetName"], "raTextBlock")
        self.assertEqual(content["text"]["content"], ["Силикон.", "Розовый цвет."])
        from pipeline.guided_review import DEPENDENCIES, digest
        for filename in DEPENDENCIES["fields"]:
            if not (self.root / filename).is_file():
                write_json(self.root / filename, {})
        # The context may change due to writing factual inputs; explicitly
        # reapply using the updated context before inspecting field approvals.
        self.draft([{ "id": "t", "type": "text", "text": "Игрушка"}], revision=1)
        previous = digest(self.root, "fields")
        from pipeline.listing_draft import card_fingerprint
        card = card_fingerprint(self.root)
        self.draft([{ "id": "t", "type": "text", "text": "Розовая игрушка"}], revision=2)
        self.assertNotEqual(previous, digest(self.root, "fields"))
        self.assertNotEqual(card, card_fingerprint(self.root))
        with self.assertRaisesRegex(ValueError, "尚未公开"):
            rich.compile_attribute(self.root)

    def test_legacy_json_preserved_until_explicit_replace_and_clear(self):
        raw = json.dumps({"version": 0.2, "content": [{"widgetName": "raTextBlock", "text": {"content": ["Legacy"]}}]})
        write_json(self.root / "input/human-confirmations.json", {"attributes": {"11254": [{"value": raw}]}})
        report = rich.read_content(self.root)
        self.assertEqual(report["legacy_json"], raw)
        self.assertTrue(report["warnings"])
        self.draft([])
        self.assertTrue(rich.compile_attribute(self.root)["remove"])
        self.assertEqual(read_json(self.root / "input/human-confirmations.json")["attributes"]["11254"][0]["value"], raw)

    def test_wrong_publication_json_or_media_binding_never_compiles(self):
        self.draft()
        rich.publish_content(self.root, storage=FakeStorage())
        manifest = read_json(self.root / rich.PUBLIC_FILE)
        original = deepcopy(manifest)
        manifest["json"]["content"][0]["blocks"][0]["title"] = "Tampered"
        write_json(self.root / rich.PUBLIC_FILE, manifest)
        with self.assertRaisesRegex(ValueError, "JSON"):
            rich.compile_attribute(self.root)
        write_json(self.root / rich.PUBLIC_FILE, original)
        frozen = rich.media_path(self.root, self.image["id"])
        frozen.write_bytes(pixels("yellow"))
        with self.assertRaises(ValueError):
            rich.compile_attribute(self.root)

    def test_preview_never_reads_arbitrary_files(self):
        for identity in ("../../config/secrets", "img-foo", "file:///tmp/a"):
            with self.assertRaises(ValueError):
                rich.media_path(self.root, identity)

    def test_current_source_validator_no_legacy_cost_requirement(self):
        from pipeline.listing_draft import _validate_current_source
        result = _validate_current_source(StepContext(self.root, "validate_source"))
        self.assertEqual(result["checks"]["sku_count"], 1)
        self.assertTrue(result["warnings"])
        source = read_json(self.root / "input/source.json")
        source["skus"].append(deepcopy(source["skus"][0]))
        write_json(self.root / "input/source.json", source)
        with self.assertRaisesRegex(PipelineGateError, "重复"):
            _validate_current_source(StepContext(self.root, "validate_source"))

    def test_http_revision_import_preview_ai_candidate_and_confirm_boundary(self):
        app = FastAPI()
        app.include_router(rich_api.router)
        base = "/api/workbench/products/P000001/rich-content"
        provider = SimpleNamespace(transport=FakeTransport({"blocks": [{"id": "t", "type": "text", "text": "Игрушка"}]}))
        with patch.object(rich_api, "directory_for", return_value=self.root), patch.object(rich_api, "web_provider", return_value=provider), TestClient(app) as client:
            report = client.get(base).json()
            self.assertEqual(client.get(report["media"][0]["preview_url"]).content, self.source_bytes)
            self.assertEqual(client.post(base + "/publish", json={"confirm": "not confirmed"}).status_code, 400)
            request = {"revision": report["revision"], "context_fingerprint": report["context_fingerprint"], "blocks": []}
            candidate = client.post(base + "/generate", json={**request, "prompt": "生成简介"})
            self.assertEqual(candidate.status_code, 200, candidate.text)
            saved = client.put(base, json={**request, "blocks": candidate.json()["candidate"]["blocks"]})
            self.assertEqual(saved.status_code, 200, saved.text)
            stale = client.put(base, json=request)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(client.get(base).json()["revision"], 1)
            self.assertEqual(client.post(base + "/images", json={"filename": "x.png", "data_base64": "bad!"}).status_code, 422)


class RichContentListingIntegrationTests(unittest.TestCase):
    """Actual app routers and card compiler; all providers/transports isolated."""
    from tests.test_listing_flow_api import ListingFlowApiTests as _Flow
    setUp = _Flow.setUp
    tearDown = _Flow.tearDown
    authorize = _Flow.authorize
    dictionary = _Flow.dictionary
    collect = _Flow.collect
    analyze = _Flow.analyze
    category_and_keywords = _Flow.category_and_keywords
    copy_and_plan = _Flow.copy_and_plan
    save_form = _Flow.save_form
    assert_no_write = _Flow.assert_no_write

    def test_rich_save_prepare_refresh_publication_and_same_final_api_attribute(self):
        from tests import test_authorized_forms_api as forms
        from pipeline.upload import build_upload_payload
        from pipeline.ozon_write import build_import_request
        extra = deepcopy(forms.FAKE_ATTRIBUTES)
        extra["result"].append({"id": 11254, "name": "JSON rich content", "type": "string", "is_required": False})
        with patch.object(forms, "FAKE_ATTRIBUTES", extra):
            self.collect()
            self.copy_and_plan()
            self.save_form()
            (self.directory / "input/main-images/01.png").write_bytes(pixels())
            base = self.base + "/rich-content"
            initial = self.client.get(base)
            self.assertEqual(initial.status_code, 200, initial.text)
            self.assertEqual(initial.json()["attribute_id"], 11254)
            saved = self.client.put(base, json={"revision": 0, "context_fingerprint": initial.json()["context_fingerprint"],
                "blocks": [{"id": "t", "type": "text", "title": "Антистресс игрушка", "text": "Мягкая игрушка."}]})
            self.assertEqual(saved.status_code, 200, saved.text)
            fingerprint = saved.json()["context_fingerprint"]
            prepared = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertTrue(prepared.json()["report"]["ok"], prepared.text)
            self.assertEqual(rich.context_fingerprint(self.directory), fingerprint)
            with patch("pipeline.oss_cos._storage_from_env", side_effect=AssertionError("Text-only no COS")):
                publication = self.client.post(base + "/publish", json={"confirm": "PUBLISH_MEDIA"})
            self.assertEqual(publication.status_code, 200, publication.text)
            payload = build_upload_payload(self.directory, shop_name="qa-store", currency_code="CNY")
            attribute = next(row for row in payload["attributes"] if row["attribute_id"] == 11254)
            self.assertEqual(json.loads(attribute["value"]), publication.json()["json"])
            # The API compiler is checked on a minimal complete isolated item,
            # never sent to Ozon. This proves the same JSON string is serialized.
            request = build_import_request({"category": {"category_id": 12, "type_id": 34}, "description": "Игрушка",
                "attributes": [attribute], "variants": [{"source_sku_id": "S1", "offer_id": "qa.rich.1", "price": "99.00",
                    "currency_code": "CNY", "display_name_ru": "Игрушка", "color_image": "https://images.example.com/main.png"}],
                "images": [], "sku_measurements": {"package_dimensions": {"length_mm": 100, "width_mm": 90, "height_mm": 80, "weight_g": 180}}})
            actual = next(row for row in request["items"][0]["attributes"] if row["id"] == 11254)
            self.assertEqual(actual["values"][0]["value"], attribute["value"])
            self.assert_no_write()


if __name__ == "__main__":
    unittest.main()
