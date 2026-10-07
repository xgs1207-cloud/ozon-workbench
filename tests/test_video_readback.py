"""Video lifecycle verification: real services, fixture transports, zero network."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from pipeline.listing_form import read_json, write_json
from pipeline.ozon_http import OzonHttpError
from pipeline.ozon_status import apply_confirmation
from pipeline.ozon_verify import (PATH_PRODUCT_ATTRIBUTES, PATH_PRODUCT_INFO, READBACK_FILE,
                                  _video_attributes, cached_readback, readback_submitted, video_readback)
from pipeline.ozon_write import build_import_request
from pipeline import source_videos
from pipeline.video_publish import publish_source_videos
from pipeline.oss_cos import CosObjectStorage


OFFER = "TEST-VIDEO-S1"
SKU = "S1"
URL = "https://fixture-1250000000.cos.ap-hongkong.myqcloud.com/video.mp4"
TITLE = "Видео товара"


def payload(video_url=URL):
    return {"product_id": "P_VIDEO", "title": "Игрушка", "description": "Игрушка",
            "category": {"category_id": 17028973, "type_id": 92811}, "attributes": [],
            "images": [{"role": "main", "url": "https://media.example/main.png", "source_sku_id": SKU}],
            "product_group": {"must_merge": False},
            "variants": [{"source_sku_id": SKU, "offer_id": OFFER, "price": 35, "currency_code": "CNY",
                          "package_length_mm": 50, "package_width_mm": 50, "package_height_mm": 70,
                          "weight_g": 120}],
            "videos": [{"video_id": "video-1", "url": video_url, "title": TITLE,
                        "source_sku_id": SKU, "duration_seconds": 24}]}


def attrs(url=URL, title=TITLE):
    return [{"id": 21841, "complex_id": 100001, "values": [{"value": url}]},
            {"id": 21837, "complex_id": 100001, "values": [{"value": title}]}]


def item(complex_attributes=None, offer=OFFER):
    return {"offer_id": offer, "id": 701, "sku": 801, "attributes": [],
            "primary_image": "https://ir.ozone.ru/s3/multimedia-1/main.jpg",
            "complex_attributes": attrs() if complex_attributes is None else complex_attributes}


def info(offer=OFFER, status="processed", errors=None):
    return {"offer_id": offer, "id": 701, "sku": 801,
            "statuses": {"status": status}, "errors": errors or []}


class ReadTransport:
    def __init__(self, attributes=None, product_info=None):
        self.attributes = attributes if attributes is not None else {"result": [item()]}
        self.product_info = product_info if product_info is not None else {"items": [info()]}
        self.calls = []

    def post(self, endpoint, body):
        self.calls.append((endpoint, copy.deepcopy(body)))
        if endpoint not in {PATH_PRODUCT_ATTRIBUTES, PATH_PRODUCT_INFO}:
            raise AssertionError("Unexpected write or unrelated read")
        result = self.attributes if endpoint == PATH_PRODUCT_ATTRIBUTES else self.product_info
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class VideoReadbackParsingTests(unittest.TestCase):
    def media(self, complex_attributes=None, **kwargs):
        return video_readback(payload(), {OFFER: item(complex_attributes)}, {OFFER: info(**kwargs)}, [OFFER])

    def test_official_flat_attributes_and_nested_import_groups(self):
        expected = [{"url": URL, "title": TITLE}]
        self.assertEqual(_video_attributes(item()), expected)
        self.assertEqual(_video_attributes(item([{"attributes": attrs()}])), expected)

    def test_array_position_pairs_multiple_videos_not_cross_group_titles(self):
        flat = attrs()
        flat[0]["values"].append({"value": URL + "2"})
        flat[1]["values"].append({"value": "Второе видео"})
        self.assertEqual(_video_attributes(item(flat))[1], {"url": URL + "2", "title": "Второе видео"})
        groups = [{"attributes": [attrs()[0]]}, {"attributes": [attrs()[1]]}]
        self.assertEqual(_video_attributes(item(groups))[0]["title"], "")

    def test_short_cover_is_not_an_ordinary_video_and_malformed_values_are_ignored(self):
        raw = [{"id": 21845, "complex_id": 100002, "values": [{"value": URL}]},
               {"id": 21841, "complex_id": 100001, "values": "bad-shape"}, None]
        self.assertEqual(_video_attributes(item(raw)), [])

    def test_exact_url_readable_never_claims_bytes_or_buyer_playback(self):
        result = self.media()
        self.assertEqual(result["status"], "readable_in_api")
        self.assertFalse(result["buyer_playback_verified"])
        self.assertFalse(result["video_processing_api_available"])
        row = result["items"][0]["videos"][0]
        self.assertEqual(row["url_evidence"], "exact_source_url")
        self.assertFalse(row["source_byte_identity_verified"])
        self.assertFalse(row["storefront_verified"])

    def test_rehosted_candidate_not_source_identity_proof(self):
        result = self.media(attrs("https://ir.ozone.ru/media/video.mp4"))
        self.assertEqual(result["status"], "rehosted_in_api_unverified_identity")
        self.assertFalse(result["items"][0]["videos"][0]["url_exact_match"])
        self.assertFalse(result["buyer_playback_verified"])

    def test_video_not_readable_after_import_is_pending_not_success(self):
        result = self.media([], status="processed")
        self.assertEqual(result["status"], "processing_or_not_yet_readable")
        self.assertTrue(result["items"][0]["pending"])
        self.assertFalse(result["items"][0]["failed"])

    def test_title_or_foreign_url_mismatch_is_not_success(self):
        for observed in (attrs(title="Другое"), attrs("https://foreign.example/video.mp4"),
                         attrs("https://[bad/video.mp4")):
            with self.subTest(observed=observed):
                self.assertEqual(self.media(observed)["status"], "failed")

    def test_video_error_overrides_readable_attributes(self):
        result = self.media(errors=[{"code": "VIDEO_FAILED", "message": "Видео не загружено"}])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["items"][0]["failed"])
        self.assertIn("错误", result["items"][0]["message"])

    def test_signed_readback_query_is_not_exposed(self):
        result = self.media(attrs(URL + "?auth_key=private-signature"))
        self.assertNotIn("private-signature", json.dumps(result))
        self.assertEqual(result["items"][0]["videos"][0]["observed_url"], URL)

    def test_other_sku_video_and_explicit_empty_override_do_not_leak(self):
        document = payload()
        document["videos"][0]["source_sku_id"] = "FOREIGN"
        self.assertEqual(video_readback(document, {}, {}, [OFFER])["video_expected"], 0)
        document = payload()
        document["variants"][0]["videos"] = []
        self.assertEqual(video_readback(document, {}, {}, [OFFER])["status"], "not_selected")

    def test_capture_duplicate_cannot_remove_protected_player_state(self):
        source = "https://detail.1688.com/offer/123456789.html"
        protected = {"provider_video_id": "V1", "source_url": "https://tbm-auth.alicdn.com/V1.mp4", "drm": True}
        direct = {"provider_video_id": "V1", "source_url": "https://tbm-auth.alicdn.com/V1.mp4?sign=new"}
        for rows in ([protected, direct], [direct, protected]):
            normalized = source_videos.normalize_videos(rows, source)["videos"]
            self.assertEqual(len(normalized), 1)
            self.assertEqual(normalized[0]["status"], "protected_media")

    def test_network_provenance_is_whitelisted_and_private_source_not_exposed(self):
        source = "https://detail.1688.com/offer/123456789.html"
        rows = [{"source_url": "https://tbm-auth.alicdn.com/V1.mp4?sign=private", "network_source": "arbitrary-secret"},
                {"source_url": "https://tbm-auth.alicdn.com/V2.mp4?sign=private",
                 "network_source": "loaded_resource_timing_bound_to_product_player"}]
        normalized = source_videos.normalize_videos(rows, source)["videos"]
        self.assertIsNone(normalized[0]["network_source"])
        self.assertEqual(normalized[1]["network_source"], "loaded_resource_timing_bound_to_product_player")
        with tempfile.TemporaryDirectory() as temporary:
            source_videos.initialize_video_manifest(Path(temporary), normalized)
            public = source_videos.list_source_videos(Path(temporary))
            self.assertNotIn("sign=private", json.dumps(public))
            self.assertNotIn("arbitrary-secret", json.dumps(public))

    def test_same_endpoint_drm_propagates_to_missing_or_different_provider_alias_only(self):
        source = "https://detail.1688.com/offer/123456789.html"
        rows = [{"source_url": "https://tbm-auth.alicdn.com/same.mp4?sign=dom"},
                {"provider_video_id": "V1", "source_url": "https://tbm-auth.alicdn.com/same.mp4?sign=json", "drm": True},
                {"provider_video_id": "V2", "source_url": "https://tbm-auth.alicdn.com/same.mp4?sign=other"},
                {"source_url": "https://tbm-auth.alicdn.com/different.mp4"}]
        for ordered in (rows, list(reversed(rows))):
            normalized = source_videos.normalize_videos(ordered, source)["videos"]
            for row in normalized:
                self.assertEqual(row["status"], "metadata_only" if "different.mp4" in row["source_url"] else "protected_media")


class SubmittedReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.product = Path(self.temp.name) / "P_VIDEO"
        self.run = self.product / "output/store-runs/default"
        self.run.mkdir(parents=True)
        write_json(self.product / "output/store-publications.json", {
            "product_id": "P_VIDEO", "stores": {"default": {"status": "created", "task_id": "1001",
            "sku_publications": [{"sku_id": SKU, "offer_id": OFFER, "status": "imported", "task_id": "1001"}]}}})
        write_json(self.run / "payload.json", payload())

    def test_explicit_read_is_exactly_two_allowlisted_calls_then_cache_has_no_network(self):
        transport = ReadTransport()
        before = (self.run / "payload.json").read_bytes()
        ledger = (self.product / "output/store-publications.json").read_bytes()
        report = readback_submitted(self.product, store_id="default", transport=transport)
        self.assertTrue(report["ok"], report)
        self.assertEqual([call[0] for call in transport.calls], [PATH_PRODUCT_ATTRIBUTES, PATH_PRODUCT_INFO])
        self.assertEqual(transport.calls[0][1]["filter"]["offer_id"], [OFFER])
        self.assertEqual(transport.calls[1][1], {"offer_id": [OFFER]})
        self.assertFalse(report["api_writes_performed"])
        self.assertFalse(report["automatic_resubmit"])
        self.assertEqual(cached_readback(self.product, store_id="default"), report)
        self.assertEqual(before, (self.run / "payload.json").read_bytes())
        self.assertEqual(ledger, (self.product / "output/store-publications.json").read_bytes())

    def test_published_editor_lock_does_not_block_readonly_evidence(self):
        write_json(self.product / "output/ozon-result.json", {"api_writes_performed": True, "status": "created"})
        report = readback_submitted(self.product, store_id="default", transport=ReadTransport())
        self.assertTrue(report["ok"])
        self.assertEqual(read_json(self.product / "output/ozon-result.json")["status"], "created")

    def test_not_submitted_does_not_request_or_create(self):
        write_json(self.product / "output/store-publications.json", {})
        transport = ReadTransport()
        report = readback_submitted(self.product, store_id="default", transport=transport)
        self.assertEqual(report["status"], "not_submitted")
        self.assertEqual(transport.calls, [])

    def test_missing_snapshot_does_not_mean_no_video_selected_or_closed_loop_success(self):
        (self.run / "payload.json").unlink()
        report = readback_submitted(self.product, store_id="default", transport=ReadTransport())
        self.assertFalse(report["ok"])
        self.assertEqual(report["media_readback"]["status"], "expectation_unknown")
        self.assertFalse(report["media_readback"]["expectation_available"])

    def test_unreadable_video_after_import_is_incomplete_and_not_replayed(self):
        transport = ReadTransport(attributes={"result": [item([])]})
        report = readback_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["media_readback"]["status"], "processing_or_not_yet_readable")
        self.assertEqual(len(transport.calls), 2)

    def test_rehosted_candidate_is_explicit_unverified_not_passed(self):
        report = readback_submitted(self.product, store_id="default",
            transport=ReadTransport(attributes={"result": [item(attrs("https://ir.ozone.ru/video.mp4"))]}))
        self.assertFalse(report["ok"])
        self.assertEqual(report["media_readback"]["status"], "rehosted_in_api_unverified_identity")

    def test_foreign_duplicate_or_missing_product_info_cannot_pass(self):
        for rows in ([info("FOREIGN")], [info(), info()], []):
            with self.subTest(rows=rows):
                report = readback_submitted(self.product, store_id="default", transport=ReadTransport(product_info={"items": rows}))
                self.assertFalse(report["ok"])
                self.assertIn("product_info_scope_valid", report["failed"])
        report = readback_submitted(self.product, store_id="default",
            transport=ReadTransport(attributes={"result": [item(), item(offer="FOREIGN")]}))
        self.assertIn("response_scope_valid", report["failed"])

    def test_timeout_stops_at_first_read_and_does_not_echo_error_secrets(self):
        for error in (TimeoutError("secret"), OzonHttpError("secret", status=429, body="secret")):
            transport = ReadTransport(attributes=error)
            report = readback_submitted(self.product, store_id="default", transport=transport)
            self.assertEqual(report["status"], "readback_failed")
            self.assertEqual(len(transport.calls), 1)
            self.assertNotIn("secret", json.dumps(report))
            self.assertFalse((self.run / READBACK_FILE).exists())

    def test_second_read_failure_is_not_import_retry(self):
        transport = ReadTransport(product_info=OzonHttpError("upstream error", status=503))
        report = readback_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse(report["ok"])
        self.assertEqual(report["api_read_count"], 2)
        self.assertEqual(len(transport.calls), 2)

    def test_cached_report_binds_shop_product_and_submitted_snapshot(self):
        readback_submitted(self.product, store_id="default", transport=ReadTransport())
        document = payload()
        document["videos"][0]["title"] = "Изменено"
        write_json(self.run / "payload.json", document)
        self.assertEqual(cached_readback(self.product, store_id="default")["status"], "stale")
        write_json(self.run / READBACK_FILE, {"ok": True, "product_id": "FOREIGN", "store": "default"})
        self.assertFalse(cached_readback(self.product, store_id="default")["ok"])

    def test_snapshot_change_during_read_discards_report(self):
        transport = ReadTransport()
        original = transport.post
        def changed(endpoint, body):
            result = original(endpoint, body)
            if endpoint == PATH_PRODUCT_INFO:
                document = payload()
                document["description"] = "Changed during IO"
                write_json(self.run / "payload.json", document)
            return result
        transport.post = changed
        with self.assertRaisesRegex(ValueError, "快照发生变化"):
            readback_submitted(self.product, store_id="default", transport=transport)
        self.assertFalse((self.run / READBACK_FILE).exists())

    def test_store_and_foreign_ledger_guard_before_any_request(self):
        transport = ReadTransport()
        with self.assertRaises(ValueError):
            readback_submitted(self.product, store_id="../other", transport=transport)
        write_json(self.product / "output/store-publications.json", {"product_id": "FOREIGN"})
        with self.assertRaises(ValueError):
            readback_submitted(self.product, store_id="default", transport=transport)
        self.assertEqual(transport.calls, [])

    def test_import_success_keeps_separate_video_awaiting_readback(self):
        confirmation = {"terminal": True, "checked_at": "2026-10-08T00:00:00Z",
                        "counts": {"imported": 1, "failed": 0}, "items": [{"offer_id": OFFER,
                        "product_id": 701, "status": "imported", "errors": []}], "errors": []}
        result = apply_confirmation(self.product, store_id="default", confirmation=confirmation, task_id="1001")
        self.assertTrue(result["video_readback_required"])
        self.assertFalse(result["import_success_is_video_success"])
        self.assertEqual(read_json(self.run / "video-import-status.json")["video_readback_status"], "awaiting_readback")
        self.assertFalse((self.run / READBACK_FILE).exists())


class VideoFixtureClosedLoopTests(unittest.TestCase):
    def test_private_capture_file_cos_compiler_and_explicit_api_readback(self):
        """A real code path with fake COS/Ozon; never represents a live test."""
        from tests.test_video_publish import VideoCosClient, MP4
        from urllib.parse import unquote, urlsplit
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary) / "P_VIDEO"
            source = {"source_url": "https://detail.1688.com/offer/123456789.html", "skus": [{"sku_id": SKU}]}
            write_json(product / "input/source.json", source)
            write_json(product / "input/selected-skus.json", {"selected": [SKU]})
            source_bytes = (product / "input/source.json").read_bytes()
            client = VideoCosClient()
            storage = CosObjectStorage(client, bucket="fixture-1250000000", region="ap-hongkong",
                                       max_attempts=1, sleep=lambda _: None)
            def public_head(url):
                key = unquote(urlsplit(url).path).lstrip("/")
                return {str(k).lower(): str(v) for k, v in client.head_object(Bucket=storage.bucket, Key=key).items()}
            with patch.object(source_videos, "_inspect_file", return_value={"media_verified": True,
                       "duration_seconds": 24, "width": 720, "height": 1280}), \
                 patch("pipeline.oss_cos._anonymous_video_headers", side_effect=public_head):
                video = source_videos.store_uploaded_video(product, io.BytesIO(MP4), "supplier.mp4")
                published = publish_source_videos(product, [{"video_id": video["video_id"], "title": TITLE,
                                                "source_sku_id": SKU}], True, storage=storage)
            self.assertEqual(published["video_stage"], "storage_verified_not_submitted")
            self.assertFalse(published["buyer_playback_verified"])
            document = payload(published["videos"][0]["url"])
            document["videos"] = published["selection"]["videos"]
            request = build_import_request(document)
            group = request["items"][0]["complex_attributes"]
            self.assertEqual(group[0]["attributes"][0]["id"], 21841)
            self.assertNotIn("inventory", json.dumps(request))
            run = product / "output/store-runs/default"
            write_json(run / "payload.json", document)
            write_json(product / "output/store-publications.json", {"product_id": product.name,
                "stores": {"default": {"sku_publications": [{"offer_id": OFFER, "sku_id": SKU,
                                                              "status": "imported", "task_id": "1"}]}}})
            transport = ReadTransport(attributes={"result": [item(group)]})
            report = readback_submitted(product, store_id="default", transport=transport)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["media_readback"]["status"], "readable_in_api")
            self.assertEqual((product / "input/source.json").read_bytes(), source_bytes)
            self.assertEqual(len([row for action, row in client.calls if action == "put_object"]), 1)
            self.assertEqual(len(transport.calls), 2)


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("fastapi", "httpx")), "needs fastapi/httpx")
class VideoReadbackRouteTests(unittest.TestCase):
    def setUp(self):
        SubmittedReadbackTests.setUp(self)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import workbench_listing_api as listing
        self.transport = ReadTransport()
        isolated_api = types.ModuleType("api")
        isolated_api._store_for = lambda directory, store: "default"
        self.api_stub = patch.dict(sys.modules, {"api": isolated_api})
        self.api_stub.start()
        self.addCleanup(self.api_stub.stop)
        self.directory = patch.object(listing, "directory_for", return_value=self.product)
        self.directory_mock = self.directory.start()
        self.addCleanup(self.directory.stop)
        self.transport_patch = patch("pipeline.ozon_verify._transport_for_store", return_value=self.transport)
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        app = FastAPI()
        app.include_router(listing.router)
        self.client = TestClient(app)
        self.url = "/api/workbench/products/P_VIDEO/guided/readback"

    def test_post_uses_readonly_directory_even_when_card_is_published(self):
        response = self.client.post(self.url, json={"store": "default"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(response.json()["api_writes_performed"])
        self.directory_mock.assert_called_once_with("P_VIDEO", edit=False)
        self.assertEqual(len(self.transport.calls), 2)

    def test_get_never_instantiates_or_calls_external_transport(self):
        response = self.client.get(self.url + "?store=default")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["report"]["status"], "not_checked")
        self.assertEqual(self.transport.calls, [])
        self.directory_mock.assert_called_once_with("P_VIDEO", edit=False)

    def test_failed_read_still_returns_structured_no_write_report(self):
        self.transport.attributes = OzonHttpError("private-token", status=401)
        response = self.client.post(self.url, json={"store": "default"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["report"]["status"], "readback_failed")
        self.assertNotIn("private-token", response.text)
        self.assertEqual(len(self.transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
