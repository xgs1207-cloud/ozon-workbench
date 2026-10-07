"""Full HTTP draft workflow with isolated shops and offline provider/transports."""
from __future__ import annotations

import json
from unittest.mock import patch
import unittest

import api
import workbench_listing_api
from pipeline.listing_form import read_json, write_json
from tests import test_authorized_forms_api as authorized_forms
from tests.test_guided_workflow import CountingProvider


class ListingFlowApiTests(unittest.TestCase):
    setUp = authorized_forms.AuthorizedFormsApiTests.setUp
    tearDown = authorized_forms.AuthorizedFormsApiTests.tearDown
    authorize = authorized_forms.AuthorizedFormsApiTests.authorize
    dictionary = authorized_forms.AuthorizedFormsApiTests.dictionary

    def collect(self, *, videos=None, measurements=True, prices=True, selected_ids=None):
        self.authorize()
        body = {"title_zh": "测试抗压玩具", "collection_mode": "all_skus",
                "source_url": "https://detail.1688.com/offer/123456789.html",
                "attributes_zh": {"材质": "硅胶", "包装数量": "2"},
                "skus": [{"sku_id": "S1", "name": "红色", "color_ru": "красный", "purchase_price_cny": 1.5},
                         {"sku_id": "S2", "name": "蓝色", "color_ru": "синий", "purchase_price_cny": 1.8}]}
        if videos is not None:
            body["videos"] = videos
        response = self.client.post("/api/collector/products", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.product_id = response.json()["product_id"]
        self.selected_ids = selected_ids or ["S1"]
        self.base = f"/api/workbench/products/{self.product_id}"
        self.directory = self.root / "products" / self.product_id
        for name in ("main-images", "sku-images", "detail-images"):
            target = self.directory / "input" / name
            target.mkdir(exist_ok=True)
            (target / "01.png").write_bytes(b"fixture-image-not-for-publishing")
        selected = self.client.post(self.base + "/skus", json={"include": self.selected_ids})
        self.assertEqual(selected.status_code, 200, selected.text)
        if measurements:
            response = self.client.put(self.base + "/measurements", json={
                "product": {"length_mm": 40, "width_mm": 40, "height_mm": 60, "weight_g": 100},
                "package": {"length_mm": 50, "width_mm": 50, "height_mm": 70, "weight_g": 120}})
            self.assertEqual(response.status_code, 200, response.text)
        if prices:
            response = self.client.put(self.base + "/prices", json={"prices": [
                {"sku_id": identity, "price": 99, "currency": "CNY"} for identity in self.selected_ids]})
            self.assertEqual(response.status_code, 200, response.text)
        self.provider = CountingProvider()
        self.model_patch = patch.object(workbench_listing_api, "web_provider", lambda: self.provider)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def assert_no_write(self):
        self.assertEqual(read_json(self.directory / "status.json").get("api_write_count", 0), 0)
        forbidden = {"/v3/product/import", "/v2/product/import", "/v1/product/import",
                     "/v1/product/import/prices", "/v2/products/stocks"}
        paths = {call["path"] for transport in self.transports for call in transport.calls}
        self.assertFalse(paths & forbidden, paths)
        self.assertFalse((self.directory / "runtime/listing-submit-attempt.json").exists())

    def analyze(self):
        response = self.client.post(self.base + "/guided/analyze", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.analysis_fp = response.json()["input_fingerprint"]
        confirmed = self.client.post(self.base + "/guided/analysis/confirm", json={"input_fingerprint": self.analysis_fp})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(confirmed.json()["workflow"]["analysis"]["confirmed"])
        return response.json()

    def category_and_keywords(self):
        category = self.client.put(self.base + "/ozon-category", json={
            "shop": "qa-store", "category_id": 2000001, "type_id": 93080})
        self.assertEqual(category.status_code, 200, category.text)
        keywords = self.client.put(self.base + "/keywords", json={"keywords": [
            {"keyword": "антистресс игрушка", "role": "core"},
            {"keyword": "мягкая игрушка", "role": "secondary"}]})
        self.assertEqual(keywords.status_code, 200, keywords.text)

    def copy_and_plan(self):
        self.analyze()
        self.category_and_keywords()
        response = self.client.post(self.base + "/guided/candidates", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["copy"]["candidates"]), 3)
        self.candidate_id = response.json()["copy"]["candidates"][0]["id"]
        self.assertFalse((self.directory / "output/copy-ru.json").exists())
        chosen = self.client.put(self.base + "/guided/candidates/choose", json={
            "candidate_id": self.candidate_id, "title_ru": "Антистресс игрушка красная",
            "hashtags": ["#антистрессигрушка"]})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertFalse(chosen.json()["copy"]["confirmed"])
        self.copy_fp = chosen.json()["copy"]["fingerprint"]
        confirmed = self.client.post(self.base + "/guided/copy/confirm", json={"input_fingerprint": self.copy_fp})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        planned = self.client.post(self.base + "/guided/plan", json={})
        self.assertEqual(planned.status_code, 200, planned.text)
        self.assertEqual(len(planned.json()["plan"]["payload"]["main_images"]), len(self.selected_ids))
        reserved = self.client.post(self.base + "/offer-ids/reserve", json={"profile_id": "qa-employee", "shop": "qa-store"})
        self.assertEqual(reserved.status_code, 200, reserved.text)
        self.assertTrue(reserved.json()["offer_ids"]["complete"])
        return planned.json()

    def save_form(self):
        for identity in (85, 102, 103):
            self.dictionary(identity)
        response = self.client.put(self.base + "/listing-form", json={
            "shop": "qa-store", "category_id": 2000001, "type_id": 93080,
            "attributes": {"85": [{"value": "Нет бренда", "dictionary_value_id": 501}],
                           "101": [{"value": 2}], "102": [{"value": "硅胶", "dictionary_value_id": 51}]},
            "per_sku_attributes": {identity: {"103": [{"value": "Красный" if identity == "S1" else "Синий",
                                                      "dictionary_value_id": 61 if identity == "S1" else 62}]}
                                   for identity in self.selected_ids}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["missing_required"])

    def test_confirm_specs_analysis_copy_plan_card_no_model_repeat_and_no_ozon_write(self):
        self.collect()
        self.copy_and_plan()
        before = (self.directory / "output/copy-ru.json").read_bytes()
        self.save_form()
        workflow = self.client.get(self.base + "/guided").json()["workflow"]
        self.assertTrue(workflow["analysis"]["confirmed"])
        self.assertTrue(workflow["copy"]["confirmed"])
        cached = self.client.post(self.base + "/guided/analyze", json={})
        self.assertTrue(cached.json()["cache_hit"])
        self.assertEqual(len(self.provider.analysis_requests), 1)
        prepared = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        self.assertTrue(prepared.json()["report"]["ok"], prepared.text)
        self.assertEqual(prepared.json()["report"]["model_calls"], 0)
        self.assertEqual((self.directory / "output/copy-ru.json").read_bytes(), before)
        self.assertEqual(len(self.provider.copy_requests), 3)  # local fake batch, not paid calls
        self.assertEqual(len(self.provider.plan_requests), 1)
        self.assert_no_write()

    def test_cannot_skip_analysis_category_choice_or_copy_confirmation(self):
        self.collect()
        for route in ("/guided/candidates", "/guided/plan", "/guided/prepare-card"):
            blocked = self.client.post(self.base + route, json={"store": "qa-store"} if route.endswith("card") else {})
            self.assertEqual(blocked.status_code, 422, blocked.text)
        analyzed = self.analyze()
        self.assertEqual(self.client.post(self.base + "/guided/candidates", json={}).status_code, 422)
        self.category_and_keywords()
        generated = self.client.post(self.base + "/guided/candidates", json={}).json()
        choice = self.client.put(self.base + "/guided/candidates/choose", json={
            "candidate_id": generated["copy"]["candidates"][0]["id"]})
        self.assertEqual(choice.status_code, 200, choice.text)
        self.assertEqual(self.client.post(self.base + "/guided/plan", json={}).status_code, 422)
        self.assert_no_write()

    def test_unknown_fields_price_and_dimensions_block_card_readiness(self):
        self.collect(measurements=False, prices=False)
        self.copy_and_plan()
        prepared = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        self.assertFalse(prepared.json()["report"]["ok"], prepared.text)
        self.assertTrue(prepared.json()["report"]["blockers"])
        self.assertFalse(self.client.get(self.base + "/guided").json()["review"]["ready_to_preflight"])
        self.assert_no_write()

    def test_package_only_dimensions_prepare_card_without_inventing_product_measurements(self):
        self.collect(measurements=False)
        saved = self.client.put(self.base + "/listing-details", json={"details": {
            "package_length_mm": 50, "package_width_mm": 50,
            "package_height_mm": 70, "package_weight_g": 120}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.copy_and_plan()
        self.save_form()
        prepared = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        self.assertTrue(prepared.json()["report"]["ok"], prepared.text)
        measurements = read_json(self.directory / "output/measurements.json")
        self.assertIsNone(measurements["product"])
        self.assertEqual(measurements["package"], {
            "length_mm": 50, "width_mm": 50, "height_mm": 70, "weight_g": 120})
        document = self.client.get(self.base + "/listing-document?shop=qa-store").json()["document"]
        self.assertTrue(document["operational_fields"]["product_optional"])
        self.assertEqual(set(document["operational_fields"]["product"].values()), {None})
        self.assert_no_write()

    def test_missing_confirmation_and_unreviewed_modern_submit_do_not_write_or_legacy_bypass(self):
        self.collect()
        self.copy_and_plan()
        self.save_form()
        self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        for suffix in ("/guided/submit", "/submit"):
            self.assertEqual(self.client.post(self.base + suffix, json={"store": "qa-store"}).status_code, 400)
            blocked = self.client.post(self.base + suffix, json={"store": "qa-store", "confirm": "SUBMIT"})
            self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assert_no_write()

    def test_selection_change_invalidates_prior_analysis_copy_and_plan_over_http(self):
        self.collect()
        self.copy_and_plan()
        changed = self.client.post(self.base + "/skus", json={"include": ["S2"]})
        self.assertEqual(changed.status_code, 200, changed.text)
        result = self.client.get(self.base + "/guided").json()["workflow"]
        self.assertEqual(result["analysis"]["status"], "stale")
        self.assertEqual(result["copy"]["status"], "stale")
        self.assertEqual(result["plan"]["status"], "stale")
        old_choice = self.client.put(self.base + "/guided/candidates/choose", json={"candidate_id": self.candidate_id})
        self.assertEqual(old_choice.status_code, 422)
        self.assert_no_write()

    def test_private_video_source_does_not_leak_and_unverified_selection_is_rejected(self):
        token = "qa-private-video-signature-never-send"
        self.collect(videos=[{"source_url": f"https://cloud.video.taobao.com/demo.mp4?sign={token}",
                              "role": "product", "title": "商品视频", "offer_id": "123456789"}])
        for suffix in ("/videos", "/guided", "/summary"):
            response = self.client.get(self.base + suffix)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(token, response.text)
        videos = self.client.get(self.base + "/videos").json()["videos"]
        self.assertTrue(videos)
        response = self.client.put(self.base + "/videos/selection", json={
            "rights_confirmed": True, "videos": [{"video_id": videos[0]["video_id"],
                "url": "https://example.com/video.mp4", "source_sku_id": "S1"}]})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse((self.directory / "input/listing-media.json").exists())
        self.assert_no_write()

    def test_invalid_xlsx_template_is_422_and_not_persisted(self):
        self.collect()
        response = self.client.post(self.base + "/listing-template", content=b"not an xlsx zip archive",
                                    headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse((self.directory / "runtime/listing-template.xlsx").exists())
        self.assert_no_write()

    def test_modern_legacy_prepare_is_card_only_and_cannot_restart_model_pipeline(self):
        self.collect()
        self.copy_and_plan()
        self.save_form()
        before = (self.directory / "output/copy-ru.json").read_bytes()
        with patch.object(api, "workbench_launch", side_effect=AssertionError("modern prepare must not launch legacy models")):
            response = self.client.post(self.base + "/guided/prepare", json={"store": "qa-store"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["report"]["ok"], response.text)
        self.assertEqual(response.json()["report"]["model_calls"], 0)
        self.assertEqual((self.directory / "output/copy-ru.json").read_bytes(), before)
        self.assert_no_write()

    def test_unknown_or_crashed_submit_attempt_blocks_raw_and_guided_edits(self):
        self.collect()
        before = (self.directory / "input/selected-skus.json").read_bytes()
        for state in ("started", "unknown_requires_readback", "finished"):
            write_json(self.directory / "runtime/listing-submit-attempt.json", {
                "shop": "qa-store", "state": state, "no_automatic_retry": True})
            requests = [
                ("post", "/skus", {"include": ["S2"]}),
                ("put", "/keywords", {"keywords": ["другая игрушка"]}),
                ("put", "/prices", {"prices": [{"sku_id": "S1", "price": 199, "currency": "CNY"}]}),
                ("post", "/guided/analyze", {}),
            ]
            for method, suffix, body in requests:
                with self.subTest(state=state, route=suffix):
                    response = getattr(self.client, method)(self.base + suffix, json=body)
                    self.assertIn(response.status_code, (409, 422), response.text)
            self.assertEqual((self.directory / "input/selected-skus.json").read_bytes(), before)
        self.assertEqual(read_json(self.directory / "status.json").get("api_write_count", 0), 0)
        self.assertEqual(len(self.provider.analysis_requests), 0)

    def test_modern_rub_quote_is_not_silently_converted_for_cny_contract(self):
        self.collect()
        self.copy_and_plan()
        self.save_form()
        changed = self.client.put(self.base + "/prices", json={"prices": [
            {"sku_id": "S1", "price": 499, "currency": "RUB"}]})
        self.assertEqual(changed.status_code, 200, changed.text)
        prepared = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        from pipeline.upload import build_upload_payload
        payload = build_upload_payload(self.directory, shop_name="qa-store", upload_mode="production", currency_code="CNY")
        self.assertTrue(any("币种" in row and "CNY" in row for row in payload["production_blockers"]), payload["production_blockers"])
        self.assertEqual(read_json(self.directory / "input/manual-prices.json")["prices"]["S1"]["currency"], "RUB")
        with patch("pipeline.guided_review.status", return_value={"ready_to_preflight": True, "blockers": []}), patch(
                "pipeline.upload.upload_product", side_effect=AssertionError("must not submit wrong currency")) as upload:
            blocked = self.client.post(self.base + "/guided/submit", json={"store": "qa-store", "confirm": "SUBMIT"})
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertIn("币种", blocked.text)
            upload.assert_not_called()
        self.assert_no_write()

    def test_manual_separate_cards_survives_card_recompilation(self):
        self.collect(selected_ids=["S1", "S2"])
        self.copy_and_plan()
        self.save_form()
        first = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        self.assertTrue(first.json()["report"]["ok"], first.text)
        chosen = self.client.put(self.base + "/guided/grouping", json={"strategy": "separate_cards"})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertEqual(chosen.json()["grouping"]["upload_strategy"], "separate_cards")
        again = self.client.post(self.base + "/guided/prepare-card", json={"store": "qa-store"})
        self.assertEqual(again.status_code, 200, again.text)
        grouped = read_json(self.directory / "output/platform-grouping-result.json")
        self.assertEqual(grouped["upload_strategy"], "separate_cards")
        self.assertFalse(grouped["platform_can_merge"])
        self.assertEqual(grouped["platform_card_count"], 2)
        self.assert_no_write()


if __name__ == "__main__":
    unittest.main()
