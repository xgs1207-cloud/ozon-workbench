"""Ordinary source-video COS publication; all probes/storage/network are offline."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

from pipeline import source_videos as videos
from pipeline.listing_form import read_json, write_json
from pipeline.oss_cos import CosError, CosObjectStorage, _anonymous_video_headers
from pipeline.video_publish import (
    PUBLICATIONS_FILE, SELECTION_FILE, publication_for, publish_source_videos,
    validate_cos_video_publication,
)
from tests.test_oss_cos import FakeCosClient
from tests.test_source_videos import MP4, MOV, SOURCE, SIGNED, Response, remote


class VideoCosClient(FakeCosClient):
    def __init__(self):
        super().__init__()
        self.types = {}
        self.fail_key = None

    def head_object(self, **kwargs):
        result = super().head_object(**kwargs)
        result["Content-Type"] = self.types.get(kwargs["Key"], "application/octet-stream")
        return result

    def put_object(self, **kwargs):
        if kwargs["Key"] == self.fail_key:
            raise RuntimeError("fixture-secret-key signed-private-source-url")
        self.types[kwargs["Key"]] = kwargs["ContentType"]
        return super().put_object(**kwargs)


class _VideoPublishFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "P000001"
        (self.directory / "input").mkdir(parents=True)
        write_json(self.directory / "input/source.json", {"source_url": SOURCE,
            "sku_selection_required": True, "skus": [{"sku_id": "S1"}, {"sku_id": "S2"}]})
        write_json(self.directory / "input/selected-skus.json", {"selected": ["S1"]})
        self.inspect = patch.object(videos, "_inspect_file", return_value={
            "media_verified": True, "duration_seconds": 24, "width": 720, "height": 1280})
        self.inspect.start()
        self.addCleanup(self.inspect.stop)
        self.client = VideoCosClient()
        self.storage = CosObjectStorage(self.client, bucket="fixture-1250000000", region="ap-hongkong",
                                        max_attempts=1, sleep=lambda seconds: None)
        self.public = patch("pipeline.oss_cos._anonymous_video_headers", side_effect=self.public_headers)
        self.public.start()
        self.addCleanup(self.public.stop)

    def public_headers(self, url):
        key = unquote(urlsplit(url).path).lstrip("/")
        return {str(name).lower(): str(value) for name, value in self.client.head_object(
            Bucket=self.storage.bucket, Key=key).items()}

    def uploaded(self, data=MP4):
        return videos.store_uploaded_video(self.directory, io.BytesIO(data), "source.mp4")["video_id"]

    def choice(self, video_id, **extra):
        return {"video_id": video_id, "title": "Видео товара", "source_sku_id": None, **extra}

    def publish(self, choices, rights=True):
        return publish_source_videos(self.directory, choices, rights, storage=self.storage)

    def puts(self):
        return [row for action, row in self.client.calls if action == "put_object"]


class VideoPublishTests(_VideoPublishFixture):
    def test_explicit_publication_saves_canonical_selection_and_owned_proof(self):
        video_id = self.uploaded()
        original_source = (self.directory / "input/source.json").read_bytes()
        result = self.publish([self.choice(video_id)])
        self.assertEqual(result["published"], 1)
        self.assertEqual(result["reused"], 0)
        self.assertFalse(result["api_writes_performed"])
        self.assertEqual(result["ozon_video_acceptance"], "not_live_verified")
        row = result["selection"]["videos"][0]
        self.assertTrue(row["url"].endswith(row["sha256"] + ".mp4"))
        self.assertTrue(row["publication"]["anonymous_verified"])
        self.assertEqual(read_json(self.directory / SELECTION_FILE), result["selection"])
        self.assertEqual(publication_for(self.directory, video_id, row["sha256"], row["url"]), row["publication"])
        self.assertEqual((self.directory / "input/source.json").read_bytes(), original_source)
        request = self.puts()[0]
        self.assertEqual(request["ContentType"], "video/mp4")
        self.assertTrue(request["EnableMD5"])
        self.assertEqual(request["Metadata"]["x-cos-meta-sha256"], hashlib.sha256(MP4).hexdigest())
        self.assertNotIn("fixture-secret-key", json.dumps(result))

    def test_same_bytes_are_reused_only_after_remote_and_public_head_checks(self):
        video_id = self.uploaded()
        first = self.publish([self.choice(video_id)])
        count = len(self.puts())
        second = self.publish([self.choice(video_id)])
        self.assertEqual(len(self.puts()), count)
        self.assertEqual(second["published"], 0)
        self.assertEqual(second["reused"], 1)
        self.assertEqual(first["videos"][0]["url"], second["videos"][0]["url"])

    def test_same_size_new_video_gets_another_versioned_key_without_deleting_old(self):
        old = self.uploaded(MP4)
        first = self.publish([self.choice(old)])
        new = self.uploaded(MP4[:-1] + b"!")
        second = self.publish([self.choice(new)])
        self.assertNotEqual(first["videos"][0]["url"], second["videos"][0]["url"])
        self.assertEqual(len(self.client.objects), 2)
        self.assertFalse(any(action == "delete_object" for action, _ in self.client.calls))

    def test_one_video_bound_to_two_selected_skus_is_uploaded_only_once(self):
        video_id = self.uploaded()
        write_json(self.directory / "input/selected-skus.json", {"selected": ["S1", "S2"]})
        result = self.publish([self.choice(video_id, source_sku_id="S1"), self.choice(video_id, source_sku_id="S2")])
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(len(result["videos"]), 2)
        self.assertEqual({row["source_sku_id"] for row in result["videos"]}, {"S1", "S2"})

    def test_missing_rights_unselected_sku_invalid_title_or_foreign_id_never_call_storage(self):
        video_id = self.uploaded()
        for choices, rights in (
            ([self.choice(video_id)], False), ([self.choice(video_id)], 1),
            ([self.choice(video_id, source_sku_id="S2")], True),
            ([self.choice(video_id, title="")], True), ([self.choice("foreign-video")], True),
            ([self.choice(video_id, url="https://external.example.com/video.mp4")], True),
        ):
            with self.subTest(choices=choices, rights=rights), self.assertRaises(ValueError):
                self.publish(choices, rights)
        self.assertEqual(self.client.calls, [])

    def test_all_choices_are_checked_before_any_public_storage_calls(self):
        video_id = self.uploaded()
        with self.assertRaises(ValueError):
            self.publish([self.choice(video_id), self.choice(video_id, source_sku_id="S2")])
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.directory / SELECTION_FILE).exists())

    def test_changed_file_hash_and_fresh_ffprobe_fail_before_storage(self):
        video_id = self.uploaded()
        with patch.object(videos, "_inspect_file", return_value={"media_verified": False}):
            with self.assertRaises(ValueError):
                self.publish([self.choice(video_id)])
        path, _ = videos.video_file(self.directory, video_id)
        path.write_bytes(MP4[:-1] + b"!")
        with self.assertRaises(ValueError):
            self.publish([self.choice(video_id)])
        self.assertEqual(self.client.calls, [])

    def test_fresh_probe_can_verify_video_saved_before_ffprobe_install(self):
        video_id = self.uploaded()
        manifest = videos._load_manifest(self.directory)
        manifest["videos"][0]["media_verified"] = False
        videos._write_manifest(self.directory, manifest)
        result = self.publish([self.choice(video_id)])
        self.assertEqual(result["published"], 1)
        self.assertTrue(videos._load_manifest(self.directory)["videos"][0]["media_verified"])

    def test_wrong_source_offer_and_bound_sku_are_blocked_before_storage(self):
        video_id = self.uploaded()
        original = videos._load_manifest(self.directory)
        for update in ({"offer_id": "foreign"}, {"sku_ids": ["S2"]}):
            data = json.loads(json.dumps(original))
            data["videos"][0].update(update)
            videos._write_manifest(self.directory, data)
            with self.assertRaises(ValueError):
                self.publish([self.choice(video_id)])
        self.assertEqual(self.client.calls, [])

    def test_more_than_five_videos_per_sku_are_blocked_before_storage(self):
        choices = [self.choice(self.uploaded(MP4 + str(index).encode())) for index in range(6)]
        with self.assertRaisesRegex(ValueError, "最多5"):
            self.publish(choices)
        self.assertEqual(self.client.calls, [])

    def test_mov_is_published_as_quicktime_and_keeps_actual_extension(self):
        result = self.publish([self.choice(self.uploaded(MOV))])
        row = result["videos"][0]
        self.assertEqual(row["format"], "mov")
        self.assertTrue(row["url"].endswith(".mov"))
        self.assertEqual(self.puts()[0]["ContentType"], "video/quicktime")

    def test_dry_run_or_non_cos_storage_cannot_save_official_video_selection(self):
        video_id = self.uploaded()
        self.storage.dry_run = True
        with self.assertRaises(ValueError):
            self.publish([self.choice(video_id)])
        self.storage.dry_run = False
        self.storage.name = "fake-placeholder"
        with self.assertRaises(ValueError):
            self.publish([self.choice(video_id)])
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.directory / SELECTION_FILE).exists())

    def test_public_head_failure_preserves_existing_selection_and_source_files(self):
        video_id = self.uploaded()
        prior = {"rights_confirmed": True, "videos": [{"legacy": "preserve"}]}
        write_json(self.directory / SELECTION_FILE, prior)
        path, _ = videos.video_file(self.directory, video_id)
        with patch("pipeline.oss_cos._anonymous_video_headers", side_effect=CosError("fixture-secret-key")):
            with self.assertRaises(ValueError) as error:
                self.publish([self.choice(video_id)])
        self.assertNotIn("fixture-secret-key", str(error.exception))
        self.assertEqual(read_json(self.directory / SELECTION_FILE), prior)
        self.assertEqual(path.read_bytes(), MP4)
        self.assertTrue(self.client.objects)  # Recoverable object, not automatically deleted.

    def test_partial_publication_tracks_success_without_overwriting_old_selection(self):
        first = self.uploaded(MP4)
        second = self.uploaded(MP4 + b"second")
        row = videos._row(videos._load_manifest(self.directory), second)
        self.client.fail_key = self.storage.video_key_for(self.directory.name, second, row["sha256"], ".mp4")
        prior = {"rights_confirmed": False, "videos": []}
        write_json(self.directory / SELECTION_FILE, prior)
        with self.assertRaises(ValueError) as error:
            self.publish([self.choice(first), self.choice(second)])
        self.assertNotIn("fixture-secret-key", str(error.exception))
        ledger = read_json(self.directory / PUBLICATIONS_FILE)
        self.assertEqual([entry["video_id"] for entry in ledger["entries"]], [first])
        self.assertEqual(read_json(self.directory / SELECTION_FILE), prior)
        self.client.fail_key = None
        result = self.publish([self.choice(first), self.choice(second)])
        self.assertEqual(result["reused"], 1)
        self.assertEqual(result["published"], 1)

    def test_canonical_cos_url_requires_exact_ledger_and_unchanged_local_bytes(self):
        video_id = self.uploaded()
        result = self.publish([self.choice(video_id)])
        row = result["videos"][0]
        with self.assertRaises(ValueError):
            videos.validate_listing_video_url(row["url"])
        self.assertEqual(videos.validate_listing_video_url(row["url"], publication=row["publication"]), row["url"])
        with self.assertRaises(ValueError):
            videos.validate_listing_video_url(row["url"], directory=self.directory, video_id="wrong", sha256=row["sha256"])
        path, _ = videos.video_file(self.directory, video_id)
        path.write_bytes(MP4[:-1] + b"!")
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, result["selection"])

    def test_verified_cos_video_compiles_to_ordinary_video_not_cover(self):
        from pipeline.ozon_write import _video_entry, OzonWriteError
        result = self.publish([self.choice(self.uploaded())])
        row = result["videos"][0]
        compiled = _video_entry(row)
        self.assertEqual([entry["id"] for entry in compiled["attributes"]], [21841, 21837])
        self.assertEqual({entry["complex_id"] for entry in compiled["attributes"]}, {100001})
        self.assertEqual(compiled["attributes"][0]["values"], [{"value": row["url"]}])
        with self.assertRaises(OzonWriteError):
            _video_entry({key: value for key, value in row.items() if key != "publication"})

    def test_forged_or_tampered_proofs_signed_urls_and_other_products_are_blocked(self):
        result = self.publish([self.choice(self.uploaded())])
        row = result["videos"][0]
        for update in ({"remote_verified": False}, {"anonymous_verified": False}, {"storage": "other"},
                       {"sha256": "0" * 64}, {"size_bytes": True}, {"product_id": "P999999"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_cos_video_publication(row["url"], {**row["publication"], **update})
        for url in (row["url"] + "?sign=temporary", row["url"] + "#fragment",
                    "https://127.0.0.1/" + row["publication"]["key"]):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_cos_video_publication(url, {**row["publication"], "url": url})
        other = self.directory.parent / "P999999"
        other.mkdir()
        write_json(other / PUBLICATIONS_FILE, read_json(self.directory / PUBLICATIONS_FILE))
        with self.assertRaises(ValueError):
            publication_for(other, row["video_id"], row["sha256"], row["url"], required=True)

    def test_unknown_cos_link_still_rejected_even_with_forged_input_proof(self):
        video_id = self.uploaded()
        row = self.choice(video_id, url="https://external.example.com/arbitrary.mp4", publication={"storage": "tencent-cos"})
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, {"rights_confirmed": True, "videos": [row]})

    def test_changed_remote_metadata_cannot_be_reused_on_size_alone(self):
        video_id = self.uploaded()
        first = self.publish([self.choice(video_id)])
        key = first["videos"][0]["publication"]["key"]
        self.client.metadata[key] = {"x-cos-meta-sha256": "0" * 64}
        before = len(self.puts())
        result = self.publish([self.choice(video_id)])
        self.assertEqual(len(self.puts()), before + 1)
        self.assertEqual(result["published"], 1)

    def test_successful_api_submission_freezes_video_publication_before_any_storage_call(self):
        video_id = self.uploaded()
        write_json(self.directory / "status.json", {"api_write_count": 1})
        with self.assertRaises(ValueError):
            self.publish([self.choice(video_id)])
        self.assertEqual(self.client.calls, [])

    def test_existing_corrupt_publication_ledger_is_not_overwritten(self):
        video_id = self.uploaded()
        ledger = self.directory / PUBLICATIONS_FILE
        ledger.write_text("corrupt-but-preserved", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "台账无法读取"):
            self.publish([self.choice(video_id)])
        self.assertEqual(ledger.read_text(encoding="utf-8"), "corrupt-but-preserved")
        self.assertEqual(self.client.calls, [])


class MemoryOnlyVideoPublishTests(_VideoPublishFixture):
    """The synthetic CDN snapshot is kept in RAM; storage and probes are mocked."""

    def captured(self, url=SIGNED, **extra):
        rows = videos.normalize_videos([{"source_url": url, **extra}], SOURCE, sku_ids={"S1", "S2"})["videos"]
        videos.initialize_video_manifest(self.directory, rows)
        return rows[0]["video_id"]

    def inspect_memory(self, **extra):
        return patch.object(videos, "_inspect_bytes", return_value={"media_verified": True,
            "duration_seconds": 24, "width": 720, "height": 1280, **extra})

    def test_captured_supplier_url_goes_to_cos_without_local_video_files(self):
        video_id = self.captured()
        with patch.object(videos, "_open_video", side_effect=remote(Response())) as fetch, self.inspect_memory():
            result = self.publish([self.choice(video_id)])
        self.assertEqual(result["uploaded"], 1)
        fetch.assert_called_once_with(SIGNED)
        self.assertFalse((self.directory / videos.VIDEO_DIRECTORY).exists())
        private = videos._load_manifest(self.directory)["videos"][0]
        self.assertNotIn("stored_path", private)
        self.assertIs(private["local_persistence"], False)
        self.assertEqual(private["status"], "published")
        self.assertEqual(private["source_url"], SIGNED)
        public = videos.list_source_videos(self.directory)["videos"][0]
        self.assertFalse(public["has_file"])
        self.assertEqual(public["published_url"], result["videos"][0]["url"])
        self.assertNotIn("private-test-value", json.dumps(result))
        self.assertNotIn("private-test-value", json.dumps(public))
        self.assertEqual(videos.validate_listing_videos(self.directory, result["selection"]), result["videos"])
        from pipeline.ozon_write import _video_entry
        compiled = _video_entry(result["videos"][0])
        self.assertEqual(compiled["attributes"][0]["values"], [{"value": result["videos"][0]["url"]}])
        self.assertEqual([item["id"] for item in compiled["attributes"]], [21841, 21837])

    def test_retry_of_published_remote_video_does_not_refetch_expiring_supplier_url(self):
        video_id = self.captured()
        with patch.object(videos, "_open_video", side_effect=remote(Response())), self.inspect_memory():
            first = self.publish([self.choice(video_id)])
        with patch.object(videos, "_open_video", side_effect=AssertionError("must not refetch")) as fetch:
            second = self.publish([self.choice(video_id)])
        fetch.assert_not_called()
        self.assertEqual(second["reused"], 1)
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(first["videos"][0]["url"], second["videos"][0]["url"])

    def test_remote_media_is_validated_before_storage_and_preserves_capture_on_failure(self):
        for extra in ({"duration_seconds": 2}, {"width": 640, "height": 720}, {"media_verified": False}):
            video_id = self.captured()
            with patch.object(videos, "_open_video", side_effect=remote(Response())), self.inspect_memory(**extra):
                with self.assertRaises(ValueError):
                    self.publish([self.choice(video_id)])
            self.assertEqual(self.client.calls, [])
            self.assertEqual(videos._load_manifest(self.directory)["videos"][0]["source_url"], SIGNED)
            self.assertFalse((self.directory / videos.VIDEO_DIRECTORY).exists())

    def test_remote_rights_binding_title_gates_precede_network(self):
        video_id = self.captured(sku_ids=["S1"])
        for choices, rights in (([self.choice(video_id)], False),
                                ([self.choice(video_id, source_sku_id="S2")], True),
                                ([self.choice(video_id, title="")], True)):
            with patch.object(videos, "_open_video") as fetch:
                with self.assertRaises(ValueError):
                    self.publish(choices, rights)
                fetch.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_remote_partial_failure_retries_only_unpublished_media_and_preserves_selection(self):
        first = self.captured()
        second_url = "https://cloud.video.taobao.com/play/second.mp4"
        second = self.captured(second_url)
        body = MP4 + b"second"
        self.client.fail_key = self.storage.video_key_for(self.directory.name, second, hashlib.sha256(body).hexdigest(), ".mp4")
        prior = {"rights_confirmed": False, "videos": []}
        write_json(self.directory / SELECTION_FILE, prior)
        with patch.object(videos, "_open_video", side_effect=lambda url: remote(Response(body if url == second_url else MP4))(url)), self.inspect_memory():
            with self.assertRaises(ValueError):
                self.publish([self.choice(first), self.choice(second)])
        self.assertEqual(read_json(self.directory / SELECTION_FILE), prior)
        self.assertEqual([row["video_id"] for row in read_json(self.directory / PUBLICATIONS_FILE)["entries"]], [first])
        self.assertFalse((self.directory / videos.VIDEO_DIRECTORY).exists())
        self.client.fail_key = None
        with patch.object(videos, "_open_video", side_effect=remote(Response(body))) as fetch, self.inspect_memory():
            result = self.publish([self.choice(first), self.choice(second)])
        fetch.assert_called_once_with(second_url)
        self.assertEqual(result["reused"], 1)
        self.assertEqual(result["uploaded"], 1)

    def test_remote_publication_rejects_manifest_metadata_tampering(self):
        video_id = self.captured()
        with patch.object(videos, "_open_video", side_effect=remote(Response())), self.inspect_memory():
            result = self.publish([self.choice(video_id)])
        manifest = videos._load_manifest(self.directory)
        manifest["videos"][0]["width"] = 1080
        videos._write_manifest(self.directory, manifest)
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, result["selection"])

    def test_remote_only_asset_cannot_replace_cos_receipt_with_unrelated_share_link(self):
        video_id = self.captured()
        with patch.object(videos, "_open_video", side_effect=remote(Response())), self.inspect_memory():
            result = self.publish([self.choice(video_id)])
        selection = {"rights_confirmed": True, "videos": [{**result["videos"][0], "url": "https://vk.com/video-123_456"}]}
        with self.assertRaises(ValueError):
            videos.validate_listing_videos(self.directory, selection)


class AnonymousVideoProbeTests(unittest.TestCase):
    def test_pins_public_ip_tls_hostname_and_does_not_send_credentials(self):
        response, connection, tls, raw = Mock(), Mock(), Mock(), Mock()
        response.status = 200
        response.getheaders.return_value = [("Content-Length", "42"), ("ETag", '"abc"')]
        connection.getresponse.return_value = response
        connection._context = tls
        with patch("pipeline.oss_cos.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]), \
                patch("pipeline.oss_cos.socket.create_connection", return_value=raw) as create, \
                patch("pipeline.oss_cos.http.client.HTTPSConnection", return_value=connection):
            headers = _anonymous_video_headers("https://cdn.example.com/immutable.mp4")
        self.assertEqual(headers["content-length"], "42")
        create.assert_called_once_with(("93.184.216.34", 443), 15)
        tls.wrap_socket.assert_called_once_with(raw, server_hostname="cdn.example.com")
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("HEAD", "/immutable.mp4"))
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("Cookie", kwargs["headers"])
        response.close.assert_called_once()
        connection.close.assert_called_once()

    def test_private_or_mixed_dns_and_signed_url_fail_without_connection(self):
        for addresses in (["127.0.0.1"], ["10.0.0.1"], ["224.0.0.1"], ["93.184.216.34", "::1"]):
            with self.subTest(addresses=addresses), patch("pipeline.oss_cos.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]), \
                patch("pipeline.oss_cos.socket.create_connection") as connect:
                with self.assertRaises(CosError):
                    _anonymous_video_headers("https://cdn.example.com/immutable.mp4")
                connect.assert_not_called()
        with patch("pipeline.oss_cos.socket.getaddrinfo") as dns, self.assertRaises(CosError):
            _anonymous_video_headers("https://cdn.example.com/immutable.mp4?signature=private")
        dns.assert_not_called()

    def test_redirect_and_provider_errors_are_redacted_not_followed(self):
        response, connection = Mock(), Mock()
        response.status = 302
        response.getheaders.return_value = [("Location", "https://secret.example.com")]
        connection.getresponse.return_value = response
        with patch("pipeline.oss_cos.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]), \
                patch("pipeline.oss_cos.socket.create_connection", return_value=Mock()), \
                patch("pipeline.oss_cos.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(CosError) as error:
                _anonymous_video_headers("https://cdn.example.com/immutable.mp4")
        self.assertNotIn("secret.example.com", str(error.exception))
        self.assertEqual(connection.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
