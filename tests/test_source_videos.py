"""All network/media inspection is mocked; never downloads a production video."""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from collector.ingest import ingest_capture, normalize_payload
from pipeline import source_videos as videos


SOURCE = "https://detail.1688.com/offer/1072823232979.html"
SIGNED = "https://cloud.video.taobao.com/play/test.mp4?auth_key=private-test-value"
# Synthetic container, not a playable video. No real remote media is touched.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"synthetic-payload"
MOV = b"\x00\x00\x00\x10ftypqt  \x00\x00\x00\x00" + b"synthetic-payload"


class Response(io.BytesIO):
    def __init__(self, body=MP4, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Type": "video/mp4"}
    def getheader(self, key):
        return self.headers.get(key)


def remote(response):
    @contextmanager
    def opened(_url):
        yield response
    return opened


class _VideoFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "P000001"
        self.directory.mkdir()
        self.inspect = patch.object(videos, "_inspect_file", return_value={"media_verified": False})
        self.inspect.start()
    def tearDown(self):
        self.inspect.stop()
        self.temp.cleanup()
    def capture(self, **extra):
        return videos.normalize_videos([{"source_url": SIGNED, "offer_id": "1072823232979",
             "poster_url": "https://cbu01.alicdn.com/img/ibank/POSTER.jpg", **extra}], SOURCE, sku_ids={"SKU1"})["videos"]
    def initialize(self, **extra):
        rows = self.capture(**extra)
        videos.initialize_video_manifest(self.directory, rows)
        return rows[0]["video_id"]


class SourceVideoTests(_VideoFixture):
    def test_source_metadata_bound_to_offer_skus_and_not_ad_or_live(self):
        rows = [
            {"source_url": SIGNED, "offer_id": "other"},
            {"source_url": SIGNED, "role": "live"},
            {"source_url": SIGNED, "sku_ids": ["SKU1", "FOREIGN"]},
        ]
        result = videos.normalize_videos(rows, SOURCE, sku_ids={"SKU1"})
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(result["videos"][0]["sku_ids"], ["SKU1"])
        self.assertEqual(len(result["warnings"]), 3)
        self.assertFalse(result["videos"][0]["poster_is_ozon_video_cover"])
    def test_blob_hls_empty_protected_and_insecure_have_explicit_states(self):
        result = videos.normalize_videos([
            {"source_url": "blob:https://detail.1688.com/a"},
            {"source_url": "https://tbm-auth.alicdn.com/a.m3u8"},
            {"provider_video_id": "not-loaded"},
            {"source_url": "https://tbm-auth.alicdn.com/protected.mp4", "drm": True},
            {"source_url": "http://tbm-auth.alicdn.com/insecure.mp4"},
        ], SOURCE)
        self.assertEqual([row["status"] for row in result["videos"]],
            ["unsupported_blob", "unsupported_stream", "not_loaded", "protected_media", "unsupported_url"])
    def test_provider_dedup_prefers_real_url_over_blob_and_signed_query_is_preserved(self):
        rows = [{"provider_video_id": "V1", "source_url": "blob:https://detail.1688.com/a"},
                {"provider_video_id": "V1", "source_url": SIGNED}]
        result = videos.normalize_videos(rows, SOURCE)["videos"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_url"], SIGNED)
        self.assertEqual(result[0]["status"], "metadata_only")
    def test_safe_public_list_has_no_signed_url_credentials_or_local_path(self):
        self.initialize(poster_url="https://cbu01.alicdn.com/POSTER.jpg?sign=private-poster")
        result = videos.list_source_videos(self.directory)
        serialized = json.dumps(result)
        for secret in ("private-test-value", "private-poster", "stored_path", "source_url", "auth_key"):
            self.assertNotIn(secret, serialized)
        self.assertIsNone(result["videos"][0]["poster_url"])
        self.assertEqual(result["videos"][0]["source_host"], "cloud.video.taobao.com")
        self.assertFalse(result["api_writes_performed"])
    def test_upload_streaming_content_hash_dedup_and_controlled_file_preview(self):
        first = self.initialize()
        one = videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "../../escaped.mp4", video_id=first)
        two = videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "another.mp4")
        self.assertEqual(one["sha256"], hashlib.sha256(MP4).hexdigest())
        self.assertEqual(one["sha256"], two["sha256"])
        self.assertEqual(one["transfer_source"], "manual_upload")
        self.assertFalse(one["media_verified"])
        files = list((self.directory / videos.VIDEO_DIRECTORY).glob("*.mp4"))
        self.assertEqual(len(files), 1)
        path, mime = videos.video_file(self.directory, first, verify_hash=True)
        self.assertEqual(path.read_bytes(), MP4)
        self.assertEqual(mime, "video/mp4")
        with self.assertRaises(ValueError):
            videos.video_file(self.directory, "../../source.json")
    def test_invalid_or_oversized_upload_does_not_replace_existing_asset(self):
        video_id = self.initialize()
        videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "valid.mp4", video_id=video_id)
        before = (self.directory / videos.MANIFEST_PATH).read_bytes()
        for body, size in [(b"<!doctype html>login", 1000), (MP4, 10), (b"not-video", 1000)]:
            with self.assertRaises(ValueError):
                videos.store_uploaded_video(self.directory, io.BytesIO(body), "bad.mp4", video_id=video_id, max_bytes=size)
            self.assertEqual((self.directory / videos.MANIFEST_PATH).read_bytes(), before)
        self.assertEqual(len(list((self.directory / videos.VIDEO_DIRECTORY).iterdir())), 1)
    def test_per_product_quota_does_not_charge_a_deduplicated_file_twice(self):
        with patch.object(videos, "MAX_PRODUCT_BYTES", len(MP4)):
            first = videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "one.mp4")
            second = videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "two.mp4")
            self.assertEqual(first["sha256"], second["sha256"])
            with self.assertRaises(ValueError):
                videos.store_uploaded_video(self.directory, io.BytesIO(MP4 + b"new"), "three.mp4")
        self.assertEqual(len(list((self.directory / videos.VIDEO_DIRECTORY).glob("*.mp4"))), 1)
    def test_pending_upload_prevents_download_and_pending_download_prevents_upload(self):
        video_id = self.initialize()
        data = videos._load_manifest(self.directory)
        data["videos"][0].update({"status": "uploading", "transfer_started_at": videos.time.time()})
        videos._write_manifest(self.directory, data)
        with patch.object(videos, "_open_video") as fetch:
            with self.assertRaises(ValueError):
                videos.download_source_video(self.directory, video_id)
            fetch.assert_not_called()
        data["videos"][0]["status"] = "downloading"
        videos._write_manifest(self.directory, data)
        with self.assertRaises(ValueError):
            videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "one.mp4", video_id=video_id)
    def test_mid_upload_row_is_claimed_and_failure_restores_original_metadata(self):
        video_id = self.initialize()
        before = (self.directory / videos.MANIFEST_PATH).read_bytes()
        def fail(_directory, _stream, _max_bytes):
            current = videos._load_manifest(self.directory)["videos"][0]
            self.assertEqual(current["status"], "uploading")
            self.assertTrue(current["transfer_job_id"])
            raise OSError("private-path-do-not-leak")
        with patch.object(videos, "_save_stream", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "原视频已保留"):
                videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "file.mp4", video_id=video_id)
        self.assertEqual((self.directory / videos.MANIFEST_PATH).read_bytes(), before)
    def test_mov_upload_uses_container_not_filename_and_stream_size_limit(self):
        result = videos.store_uploaded_video(self.directory, io.BytesIO(MOV), "misleading.jpg")
        path, mime = videos.video_file(self.directory, result["video_id"])
        self.assertEqual(path.suffix, ".mov")
        self.assertEqual(mime, "video/quicktime")
    def test_source_verification_is_explicit_memory_only_and_reuses_metadata(self):
        video_id = self.initialize()
        with patch.object(videos, "_open_video", side_effect=remote(Response())) as fetch, \
             patch.object(videos, "_inspect_bytes", return_value={"media_verified": True,
                         "duration_seconds": 24, "width": 720, "height": 1280}):
            result = videos.download_source_video(self.directory, video_id)
            self.assertEqual(result["status"], "source_ready")
            self.assertEqual(result["transfer_source"], "source_memory_probe")
            self.assertFalse(result["has_file"])
            self.assertFalse((self.directory / videos.VIDEO_DIRECTORY).exists())
            videos.download_source_video(self.directory, video_id)
            fetch.assert_called_once_with(SIGNED)
    def test_failed_downloads_keep_product_and_original_video_evidence(self):
        for response, state in [
            (Response(b"<html>login</html>"), "invalid_media"),
            (Response(headers={"Content-Type": "text/html"}), "invalid_media"),
            (Response(headers={"Content-Type": "video/mp4", "Content-Length": "999999999"}), "too_large"),
        ]:
            video_id = self.initialize()
            with patch.object(videos, "_open_video", side_effect=remote(response)):
                result = videos.download_source_video(self.directory, video_id)
            self.assertEqual(result["status"], state)
            private = json.loads((self.directory / videos.MANIFEST_PATH).read_text(encoding="utf-8"))
            self.assertEqual(private["videos"][0]["source_url"], SIGNED)
            self.assertFalse(result["has_file"])

    def test_memory_snapshot_is_bounded_and_failure_releases_transfer_capacity(self):
        video_id = self.initialize()
        with patch.object(videos, "_open_video", side_effect=remote(Response())):
            with self.assertRaises(ValueError):
                with videos.source_video_snapshot(self.directory, video_id, max_bytes=10):
                    self.fail("oversize snapshot must not be yielded")
        with patch.object(videos, "_open_video", side_effect=remote(Response())), \
             patch.object(videos, "_inspect_bytes", return_value={"media_verified": True,
                         "duration_seconds": 24, "width": 720, "height": 1280}):
            with videos.source_video_snapshot(self.directory, video_id) as snapshot:
                self.assertEqual(snapshot["body"], MP4)
                self.assertEqual(snapshot["size_bytes"], len(MP4))
        self.assertFalse((self.directory / videos.VIDEO_DIRECTORY).exists())

    def test_source_refresh_failure_is_not_advertised_as_listing_ready(self):
        video_id = self.initialize()
        with patch.object(videos, "_open_video", side_effect=videos._TransferError("needs_refresh")):
            result = videos.download_source_video(self.directory, video_id)
        self.assertFalse(result["can_publish"])
        self.assertTrue(result["can_prepare"])
        self.assertTrue(result["requires_source_refresh"])
    def test_login_or_expired_signature_is_not_retried_with_browser_credentials(self):
        video_id = self.initialize()
        with patch.object(videos, "_open_video", side_effect=videos._TransferError("needs_refresh")) as fetch:
            result = videos.download_source_video(self.directory, video_id)
        self.assertEqual(result["status"], "needs_refresh")
        fetch.assert_called_once_with(SIGNED)
        self.assertNotIn("auth_key", json.dumps(result))
    def test_unsupported_media_never_fetches(self):
        for url in ("blob:https://detail.1688.com/b", "https://tbm-auth.alicdn.com/b.m3u8"):
            rows = videos.normalize_videos([{"source_url": url}], SOURCE)["videos"]
            videos.initialize_video_manifest(self.directory, rows)
            with patch.object(videos, "_open_video") as fetch:
                videos.download_source_video(self.directory, rows[0]["video_id"])
                fetch.assert_not_called()
    def test_validation_rejects_private_ips_schemes_ports_credentials_and_foreign_hosts(self):
        invalid = ["https://localhost/x.mp4", "https://127.0.0.1/x.mp4", "https://evil.test/x.mp4",
                   "http://tbm-auth.alicdn.com/x.mp4", "https://tbm-auth.alicdn.com:8443/x.mp4",
                   "https://user:pass@tbm-auth.alicdn.com/x.mp4", "file:///C:/private.mp4"]
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.1.2.3", 443))]) as dns:
            for url in invalid:
                with self.assertRaises(videos._TransferError):
                    videos._validated_target(url)
            dns.assert_not_called()
            with self.assertRaises(videos._TransferError):
                videos._validated_target(SIGNED)
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 443, 0, 0))]):
            with self.assertRaises(videos._TransferError):
                videos._validated_target(SIGNED)
    def test_validated_connection_pins_public_ip_and_keeps_hostname_for_tls(self):
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]):
            host, path, addresses = videos._validated_target(SIGNED)
        self.assertEqual(addresses, ["8.8.8.8"])
        self.assertIn("auth_key=private-test-value", path)
        with patch.object(socket, "create_connection") as connect, patch.object(videos.ssl, "create_default_context") as ssl:
            connection = videos._PinnedHTTPSConnection(host, addresses[0])
            connection.connect()
            connect.assert_called_once_with(("8.8.8.8", 443), 20)
            ssl.return_value.wrap_socket.assert_called_once_with(connect.return_value, server_hostname=host)
    def test_redirect_is_revalidated_before_network_and_sends_no_browser_cookies(self):
        redirect = Response(status=302, headers={"Location": "https://localhost/private.mp4"})
        with patch.object(videos, "_validated_target", side_effect=[("cloud.video.taobao.com", "/x.mp4", ["8.8.8.8"]), videos._TransferError("unsupported_url")]) as validate, \
             patch.object(videos, "_PinnedHTTPSConnection") as connection:
            connection.return_value.getresponse.return_value = redirect
            with self.assertRaises(videos._TransferError):
                with videos._open_video(SIGNED):
                    self.fail("private redirect must not be opened")
            self.assertEqual(validate.call_count, 2)
            connection.assert_called_once()
            headers = connection.return_value.request.call_args.kwargs["headers"]
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Authorization", headers)
    def test_preview_rejects_changed_bytes_even_when_size_unchanged(self):
        result = videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "test.mp4")
        path, _ = videos.video_file(self.directory, result["video_id"])
        path.write_bytes(MP4[:-1] + b"!")
        with self.assertRaises(ValueError):
            videos.video_file(self.directory, result["video_id"], verify_hash=True)
    def test_product_ingest_captures_metadata_without_video_download_and_preserves_seal(self):
        payload = {"source_url": SOURCE, "title_cn": "商品", "collection_mode": "all_skus",
                   "skus": [{"sku_id": "SKU1", "purchase_price": 3}], "videos": [{"source_url": SIGNED}]}
        with patch.object(videos, "_open_video") as fetch:
            saved = ingest_capture(self.root / "products", payload)
            fetch.assert_not_called()
        directory = self.root / "products" / saved["product_id"]
        self.assertEqual(saved["counts"]["videos"], 1)
        source = json.loads((directory / "input/source.json").read_text(encoding="utf-8"))
        original = {path: path.read_bytes() for path in (directory / "input").glob("*.json")}
        videos.store_uploaded_video(directory, io.BytesIO(MP4), "test.mp4", video_id=source["videos"][0]["video_id"])
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertTrue((directory / videos.MANIFEST_PATH).exists())
    def test_bad_video_metadata_does_not_reject_whole_capture(self):
        payload = {"source_url": SOURCE, "title_cn": "商品", "skus": [{"sku_id": "SKU1", "purchase_price": 3}], "videos": "bad"}
        normalized = normalize_payload(payload)
        self.assertEqual(normalized["videos"], [])
        self.assertTrue(normalized["video_warnings"])


class ListingVideoValidationTests(_VideoFixture):
    def setUp(self):
        super().setUp()
        self.inspect.stop()
        self.inspect = patch.object(videos, "_inspect_file", return_value={"media_verified": True,
                                    "duration_seconds": 24, "width": 720, "height": 1280})
        self.inspect.start()
        (self.directory / "input").mkdir()
        (self.directory / "input/source.json").write_text(json.dumps({"source_url": SOURCE,
            "skus": [{"sku_id": "SKU1"}, {"sku_id": "SKU2"}], "sku_selection_required": True}), encoding="utf-8")
        (self.directory / "input/selected-skus.json").write_text(json.dumps({"selected": ["SKU1"]}), encoding="utf-8")
    def saved_video(self, **extra):
        video_id = self.initialize(**extra)
        videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "source.mp4", video_id=video_id)
        return video_id
    def selection(self, _video_id, **extra):
        return {"rights_confirmed": True, "videos": [{"video_id": _video_id,
            "url": "https://vkvideo.ru/video-123_456", "title": "Видео товара", "source_sku_id": None, **extra}], "video_cover": None}
    def test_supported_stable_sharing_hosts_reject_signed_raw_or_unsupported_urls_without_network(self):
        accepted = ["https://vk.com/video-123_456", "https://vkvideo.ru/video123_456",
                    "https://disk.yandex.ru/i/Share01", "https://disk.yandex.com/d/Share02",
                    "https://yadi.sk/d/Share03", "https://rutube.ru/video/" + "a" * 32 + "/"]
        rejected = [SIGNED, "https://cdn.example.com/source.mp4", "https://cdn1.ozone.ru/unverified.mp4",
                    "https://evilvk.com/video-123_456", "http://vk.com/video-123_456",
                    "https://vk.com:8080/video-123_456", "https://user:password@vk.com/video-123_456",
                    "https://vk.com/video-123_456?sign=private", "https://disk.yandex.ru/i/share#token",
                    "https://vk.com/login", "https://rutube.ru/channel/" + "a" * 32]
        with patch.object(socket, "getaddrinfo") as dns, patch.object(videos, "_open_video") as network:
            for url in accepted:
                self.assertEqual(videos.validate_listing_video_url(url), url)
            for url in rejected:
                with self.assertRaises(ValueError):
                    videos.validate_listing_video_url(url)
            dns.assert_not_called()
            network.assert_not_called()
    def test_validator_returns_real_file_metadata_not_user_assertions_and_performs_no_writes(self):
        video_id = self.saved_video()
        before = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        selection = self.selection(video_id, duration_seconds=99999, width=1, height=1, size_bytes=99999, format="jpeg")
        with patch.object(videos, "_open_video") as network:
            result = videos.validate_listing_videos(self.directory, selection)
            network.assert_not_called()
        self.assertEqual(result[0]["format"], "mp4")
        self.assertEqual(result[0]["duration_seconds"], 24)
        self.assertEqual(result[0]["width"], 720)
        self.assertEqual(result[0]["height"], 1280)
        self.assertEqual(result[0]["size_bytes"], len(MP4))
        self.assertNotIn("private-test-value", json.dumps(result))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_eleven_and_hundred_selected_skus_can_share_one_verified_source_video_without_network(self):
        video_id = self.saved_video()
        for count in (11, 100):
            with self.subTest(count=count):
                ids = [f"SKU{index}" for index in range(1, count + 1)]
                (self.directory / "input/source.json").write_text(json.dumps({"source_url": SOURCE,
                    "skus": [{"sku_id": sku_id} for sku_id in ids], "sku_selection_required": True}), encoding="utf-8")
                (self.directory / "input/selected-skus.json").write_text(json.dumps({"selected": ids}), encoding="utf-8")
                with patch.object(videos, "_open_video") as network:
                    result = videos.validate_listing_videos(self.directory, self.selection(video_id))
                    network.assert_not_called()
                self.assertEqual(len(result), 1)
                self.assertIsNone(result[0]["source_sku_id"])
    def test_missing_rights_unknown_video_sku_or_unselected_sku_are_rejected(self):
        video_id = self.saved_video()
        for rights in (False, None, "true", 1):
            request = self.selection(video_id)
            request["rights_confirmed"] = rights
            with self.assertRaises(ValueError):
                videos.validate_listing_videos(self.directory, request)
        for extra in ({"video_id": "unknown"}, {"source_sku_id": "FOREIGN"}, {"source_sku_id": "SKU2"}):
            with self.assertRaises(ValueError):
                videos.validate_listing_videos(self.directory, self.selection(video_id, **extra))
    def test_clear_videos_needs_no_rights_and_every_cover_alias_is_explicitly_rejected(self):
        self.assertEqual(videos.validate_listing_videos(self.directory, {"rights_confirmed": False, "videos": []}), [])
        for key in ("video_cover", "video_cover_id", "cover_video_id"):
            with self.assertRaises(ValueError):
                videos.validate_listing_videos(self.directory, {"videos": [], key: {"poster_url": "https://cbu01.alicdn.com/poster.jpg"}})
    def test_unstored_unverified_wrong_duration_resolution_or_changed_sha_is_rejected(self):
        video_id = self.initialize()
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, self.selection(video_id))
        videos.store_uploaded_video(self.directory, io.BytesIO(MP4), "source.mp4", video_id=video_id)
        original = videos._load_manifest(self.directory)
        for update in ({"media_verified": False}, {"duration_seconds": 7.9}, {"duration_seconds": 300.1},
                       {"height": 1079}, {"height": 1921}, {"width": None}, {"duration_seconds": True}):
            data = json.loads(json.dumps(original))
            data["videos"][0].update(update)
            videos._write_manifest(self.directory, data)
            with self.assertRaises(ValueError):
                videos.validate_listing_videos(self.directory, self.selection(video_id))
        videos._write_manifest(self.directory, original)
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, self.selection(video_id, sha256="0" * 64))
        path, _ = videos.video_file(self.directory, video_id)
        path.write_bytes(MP4[:-1] + b"!")
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, self.selection(video_id))
    def test_source_sku_bound_video_cannot_be_shared_to_other_selected_skus(self):
        video_id = self.saved_video(sku_ids=["SKU1"])
        (self.directory / "input/selected-skus.json").write_text(json.dumps({"selected": ["SKU1", "SKU2"]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, self.selection(video_id))
        result = videos.validate_listing_videos(self.directory, self.selection(video_id, source_sku_id="SKU1"))
        self.assertEqual(result[0]["source_sku_id"], "SKU1")
    def test_unknown_capture_sku_binding_requires_explicit_human_scope(self):
        video_id = self.saved_video(sku_ids=["FOREIGN"])
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, self.selection(video_id))
        videos.validate_listing_videos(self.directory, self.selection(video_id, source_sku_id="SKU1"))
    def test_duplicated_reference_url_or_more_than_five_effective_per_sku_are_rejected(self):
        video_id = self.saved_video()
        request = self.selection(video_id)
        request["videos"] += request["videos"]
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, request)
        request = {"rights_confirmed": True, "videos": []}
        for index in range(6):
            result = videos.store_uploaded_video(self.directory, io.BytesIO(MP4 + str(index).encode()), str(index) + ".mp4")
            request["videos"].append({"video_id": result["video_id"], "url": f"https://vk.com/video-123_{index + 100}",
                                       "title": "Видео", "source_sku_id": None})
        with self.assertRaisesRegex(ValueError, "最多5"):
            videos.validate_listing_videos(self.directory, request)
        request["videos"] = request["videos"][:2]
        request["videos"][1]["url"] = request["videos"][0]["url"]
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, request)


if __name__ == "__main__":
    unittest.main()
