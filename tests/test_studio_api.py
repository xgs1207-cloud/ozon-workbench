import io
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from pipeline.listing_form import read_json
from pipeline.oss_cos import CosObjectStorage
from pipeline.source_videos import store_uploaded_video
from tests import test_listing_flow_api as flow_fixture
from tests.test_source_videos import MP4
from tests.test_video_publish import VideoCosClient


class StudioApiTests(unittest.TestCase):
    setUp = flow_fixture.ListingFlowApiTests.setUp
    tearDown = flow_fixture.ListingFlowApiTests.tearDown
    authorize = flow_fixture.ListingFlowApiTests.authorize
    collect = flow_fixture.ListingFlowApiTests.collect
    category_and_keywords = flow_fixture.ListingFlowApiTests.category_and_keywords
    assert_no_write = flow_fixture.ListingFlowApiTests.assert_no_write

    def test_captured_references_visible_before_any_paid_image_plan(self):
        self.collect()
        images = self.directory / "input/detail-images"
        images.mkdir(exist_ok=True)
        for number in range(45):
            (images / f"captured-{number:03d}.png").write_bytes(b"fixture")
        with patch("workbench_listing_api.web_provider") as provider:
            response = self.client.get(self.base + "/guided")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertFalse(result["image_plan"])
        details = [row for row in result["captured_reference_images"]
                   if row["role"] == "detail" and "/captured-" in row["path"]]
        self.assertEqual(len(details), 45)
        self.assertTrue(all(row["path"].startswith("input/detail-images/") for row in details))
        provider.assert_not_called()
        self.assert_no_write()

    def test_empty_keywords_allowed_only_after_official_category_confirmation(self):
        self.collect()
        self.assertEqual(self.client.put(self.base + "/keywords", json={"keywords": []}).status_code, 422)
        self.category_and_keywords()
        response = self.client.put(self.base + "/keywords", json={"keywords": []})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["selection"]["keywords"], [])
        self.assert_no_write()

    def test_video_publish_requires_explicit_confirm_and_rights(self):
        self.collect()
        body = {"confirm": "", "rights_confirmed": True, "videos": [{"video_id": "fake"}]}
        with patch("pipeline.oss_cos._storage_from_env") as factory:
            self.assertEqual(self.client.post(self.base + "/guided/publish-videos", json=body).status_code, 400)
            body.update(confirm="PUBLISH_VIDEOS", rights_confirmed=False)
            self.assertEqual(self.client.post(self.base + "/guided/publish-videos", json=body).status_code, 422)
            factory.assert_not_called()
        self.assert_no_write()

    def test_video_route_publishes_original_to_fake_storage_and_reuses_it(self):
        self.collect()
        client = VideoCosClient()
        storage = CosObjectStorage(client, bucket="fixture-1250000000", region="ap-hongkong", max_attempts=1)
        def public_headers(url):
            return {str(k).lower(): str(v) for k, v in client.head_object(
                Bucket=storage.bucket, Key=unquote(urlsplit(url).path).lstrip("/")).items()}
        with patch("pipeline.source_videos._inspect_file", return_value={
                "media_verified": True, "duration_seconds": 24, "width": 720, "height": 1280}), \
                patch("pipeline.oss_cos._storage_from_env", return_value=storage), \
                patch("pipeline.oss_cos._anonymous_video_headers", side_effect=public_headers):
            video = store_uploaded_video(self.directory, io.BytesIO(MP4), "supplier.mp4")
            body = {"confirm": "PUBLISH_VIDEOS", "rights_confirmed": True,
                    "videos": [{"video_id": video["video_id"], "title": "Видео товара", "source_sku_id": "S1"}]}
            response = self.client.post(self.base + "/guided/publish-videos", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["api_writes_performed"])
            self.assertEqual(response.json()["publication"]["published"], 1)
            selection = response.json()["selection"]
            self.assertEqual(read_json(self.directory / "input/listing-media.json"), selection)
            self.assertTrue(selection["videos"][0]["publication"]["anonymous_verified"])
            reused = self.client.post(self.base + "/guided/publish-videos", json=body)
            self.assertEqual(reused.json()["publication"]["reused"], 1)
            self.assertEqual(len([call for action, call in client.calls if action == "put_object"]), 1)
        self.assert_no_write()
