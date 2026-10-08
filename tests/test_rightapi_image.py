"""Offline RightAPI contract, charging, reference-integrity and download tests."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from models.base import ImageRequest, ModelError
from models.rightapi_image import (
    DEFAULT_BASE_URL, DEFAULT_MODEL, MAX_IMAGE_BYTES, MAX_JSON_BYTES,
    RightApiImageGenerator, RightApiImageTransport, _PinnedHTTPSConnection,
)


def png_bytes(width=30, height=40):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def read(self, size=-1):
        return self.body.read(size)

    read1 = read

    def getheader(self, name):
        return self.headers.get(name)

    def close(self):
        self.closed = True


class Connections:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = 0

    def __call__(self, host, address, timeout):
        outer = self

        class Connection:
            def request(self, method, path, body=None, headers=None):
                outer.calls.append({"host": host, "address": address, "timeout": timeout,
                    "method": method, "path": path, "body": body, "headers": headers})

            def getresponse(self):
                result = outer.responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            def close(self):
                outer.closed += 1

        return Connection()


def json_response(data=None, **kwargs):
    return Response(json.dumps(data if data is not None else {
        "data": [{"url": "https://cdn.example.com/image.png?signature=private-signature"}]}).encode(), **kwargs)


class TransportTests(unittest.TestCase):
    def transport(self, responses, **kwargs):
        connections = Connections(responses)
        return RightApiImageTransport(api_key="test-private-key", resolver=resolver,
            connection_factory=connections, **kwargs), connections

    def test_exact_user_contract_and_download_without_authentication(self):
        image = png_bytes()
        transport, connections = self.transport([json_response(), Response(image, headers={"Content-Type": "image/png"})])
        result = transport.generate(prompt="图片提示词", images=["https://cbu01.alicdn.com/source.png"], size="2k", aspect_ratio="3:4")
        self.assertEqual(result, [image])
        request, download = connections.calls
        self.assertEqual(request["host"], "www.rightapi.ai")
        self.assertEqual(request["path"], "/draw/v1/images/generations")
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-private-key")
        self.assertEqual(json.loads(request["body"]), {"model": "gpt-image-2.5", "prompt": "图片提示词",
            "images": ["https://cbu01.alicdn.com/source.png"], "aspect_ratio": "3:4", "image_size": "2k", "response_format": "url"})
        self.assertEqual(download["method"], "GET")
        self.assertNotIn("Authorization", download["headers"])
        self.assertNotIn("Cookie", download["headers"])
        self.assertEqual(download["address"], "93.184.216.34")
        self.assertEqual(connections.closed, 2)

    def test_http_errors_are_not_retried_or_echoed(self):
        for status in (400, 401, 429, 500, 502):
            with self.subTest(status=status):
                transport, connections = self.transport([Response(b"test-private-key https://secret/?signature=signed", status)])
                with self.assertRaises(ModelError) as ctx:
                    transport.generate(prompt="p")
                self.assertIn(f"HTTP {status}", str(ctx.exception))
                self.assertNotIn("test-private-key", str(ctx.exception))
                self.assertNotIn("signature", str(ctx.exception))
                self.assertEqual(len(connections.calls), 1)

    def test_post_redirect_is_not_followed(self):
        transport, connections = self.transport([Response(status=307, headers={"Location": "https://evil.example.com"})])
        with self.assertRaises(ModelError):
            transport.generate(prompt="p")
        self.assertEqual(len(connections.calls), 1)

    def test_network_failure_does_not_retry_or_echo_exception(self):
        transport, connections = self.transport([OSError("test-private-key https://cdn/?signature=secret")])
        with self.assertRaises(ModelError) as ctx:
            transport.generate(prompt="p")
        self.assertEqual(len(connections.calls), 1)
        self.assertNotIn("test-private-key", str(ctx.exception))
        self.assertNotIn("signature", str(ctx.exception))

    def test_untrusted_json_error_bodies_are_not_echoed(self):
        for result in ({"error": {"message": "test-private-key signature=private"}}, ["test-private-key"], None):
            body = json.dumps(result).encode()
            transport, _ = self.transport([Response(body)])
            with self.assertRaises(ModelError) as ctx:
                transport.generate(prompt="p")
            self.assertNotIn("test-private-key", str(ctx.exception))
            self.assertNotIn("signature", str(ctx.exception))

    def test_non_json_does_not_echo_body(self):
        transport, _ = self.transport([Response(b"test-private-key invalid")])
        with self.assertRaises(ModelError) as ctx:
            transport.generate(prompt="p")
        self.assertNotIn("test-private-key", str(ctx.exception))

    def test_json_content_length_and_stream_limits(self):
        for response in (Response(b"{}", headers={"Content-Length": str(MAX_JSON_BYTES + 1)}),
                         Response(b" " * (MAX_JSON_BYTES + 1))):
            transport, connections = self.transport([response])
            with self.assertRaises(ModelError):
                transport.generate(prompt="p")
            self.assertEqual(len(connections.calls), 1)

    def test_result_content_length_limit(self):
        transport, connections = self.transport([json_response(), Response(b"", headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)})])
        with self.assertRaises(ModelError):
            transport.generate(prompt="p")
        self.assertEqual(len(connections.calls), 2)

    def test_result_stream_limit(self):
        transport, _ = self.transport([json_response(), Response(b"x" * (MAX_IMAGE_BYTES + 1))])
        with self.assertRaises(ModelError):
            transport.generate(prompt="p")

    def test_result_mime_must_be_an_image(self):
        transport, _ = self.transport([json_response(), Response(b"<html>", headers={"Content-Type": "text/html"})])
        with self.assertRaises(ModelError):
            transport.generate(prompt="p")

    def test_download_redirect_revalidates_public_address(self):
        transport, connections = self.transport([json_response(), Response(status=302, headers={"Location": "https://127.0.0.1/result.png"})])
        def dynamic_resolver(host, port, **kwargs):
            if host == "127.0.0.1":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
            return resolver(host, port, **kwargs)
        transport._resolver = dynamic_resolver
        with self.assertRaises(ModelError):
            transport.generate(prompt="p")
        self.assertEqual(len(connections.calls), 2)

    def test_public_download_redirect_keeps_credentials_absent(self):
        image = png_bytes()
        transport, connections = self.transport([json_response(), Response(status=302, headers={"Location": "https://second.example.com/output.png"}), Response(image)])
        self.assertEqual(transport.generate(prompt="p"), [image])
        self.assertEqual(len(connections.calls), 3)
        for call in connections.calls[1:]:
            self.assertNotIn("Authorization", call["headers"])

    def test_private_and_mixed_dns_are_rejected_before_post(self):
        for addresses in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"],
                          ["93.184.216.34", "192.168.1.1"], ["224.0.0.1"], ["ff0e::1"], ["240.0.0.1"]):
            transport, connections = self.transport([])
            transport._resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]
            with self.assertRaises(ModelError):
                transport.generate(prompt="p")
            self.assertEqual(connections.calls, [])

    def test_unsafe_reference_urls_are_rejected_before_post(self):
        for url in ("http://public.example.com/x", "https://user:secret@public.example.com/x",
                    "https://public.example.com:8080/x", "data:image/png;base64,AA", "https://public.example.com/x#fragment"):
            transport, connections = self.transport([])
            with self.assertRaises(ModelError):
                transport.generate(prompt="p", images=[url])
            self.assertEqual(connections.calls, [])

    def test_more_than_three_references_are_rejected_not_truncated(self):
        transport, connections = self.transport([])
        with self.assertRaises(ModelError):
            transport.generate(prompt="p", images=["https://cdn.example.com/a.png"] * 4)
        self.assertEqual(connections.calls, [])

    def test_unique_synchronous_result_is_required(self):
        for data in ([], [{"b64_json": "AAAA"}], [{"url": "https://cdn.example.com/a"}, {"url": "https://cdn.example.com/b"}]):
            transport, connections = self.transport([json_response({"data": data})])
            with self.assertRaises(ModelError):
                transport.generate(prompt="p")
            self.assertEqual(len(connections.calls), 1)

    def test_env_defaults_and_override(self):
        generator = RightApiImageGenerator.from_env({"RIGHTAPI_API_KEY": "dummy"}, slot_filter=["main-S1"])
        self.assertEqual(generator.transport.model, DEFAULT_MODEL)
        self.assertEqual(generator.transport.base_url, DEFAULT_BASE_URL)
        self.assertEqual(generator.size, "2k")
        self.assertEqual(generator.aspect_ratio, "3:4")
        self.assertEqual(generator.slot_filter, {"main-S1"})
        configured = RightApiImageGenerator.from_env({"RIGHTAPI_API_KEY": "dummy", "RIGHTAPI_IMAGE_SIZE": "4k",
            "RIGHTAPI_IMAGE_MODEL": "custom", "RIGHTAPI_ASPECT_RATIO": "1:1"}, normalize=False)
        self.assertEqual(configured.size, "4k")
        self.assertFalse(configured.normalize)
        with self.assertRaises(ModelError):
            RightApiImageGenerator.from_env({})

    def test_base_url_has_no_auth_query_or_http(self):
        for base in ("http://www.rightapi.ai/draw/v1", "https://u:p@www.rightapi.ai/draw/v1", "https://www.rightapi.ai/draw/v1?token=secret"):
            with self.assertRaises(ModelError):
                RightApiImageTransport(api_key="dummy", base_url=base)

    def test_connection_pins_ip_and_validates_tls_hostname(self):
        raw, wrapped = unittest.mock.Mock(), unittest.mock.Mock()
        connection = _PinnedHTTPSConnection("cdn.example.com", "93.184.216.34", 10)
        with patch("models.rightapi_image.socket.create_connection", return_value=raw) as create:
            connection._context = unittest.mock.Mock()
            connection._context.wrap_socket.return_value = wrapped
            connection.connect()
        create.assert_called_once_with(("93.184.216.34", 443), 10)
        connection._context.wrap_socket.assert_called_once_with(raw, server_hostname="cdn.example.com")
        self.assertIs(connection.sock, wrapped)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.main = "input/main-images/001-main.png"
        self.sku = "input/sku-images/001-S1.png"
        self.other = "input/sku-images/002-S2.png"
        for relative in (self.main, self.sku, self.other):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png_bytes())
        self.source = {"product_id": "P000001", "sku_selection_required": True,
            "stored_images": [self.main, self.sku, self.other],
            "skus": [{"sku_id": "S1", "image_path": self.sku, "image_url": "https://cbu01.alicdn.com/S1.png"},
                     {"sku_id": "S2", "image_path": self.other, "image_url": "https://cbu01.alicdn.com/S2.png"}],
            "image_sources": [{"path": self.main, "url": "https://cbu01.alicdn.com/main.png", "role": "main", "source_sku_id": None}]}
        self.write("input/source.json", self.source)
        self.write("input/selected-skus.json", {"selected": ["S1"]})
        self.write("input/raw-snapshot.json", {"raw": {"main_images": ["https://cbu01.alicdn.com/main.png"]}})
        self.plan = {"main_images": [{"slot": "main-S1", "source_sku_id": "S1", "prompt": "商品图片",
            "reference_product_images": [self.main, self.sku], "output_path": "output/generated-images/variant-main/main-S1.png"}],
            "detail_images": [{"slot": "detail-01", "prompt": "商品细节", "reference_product_images": [self.main],
            "output_path": "output/generated-images/detail/detail-01.png"}]}
        self.write("output/image-plan.json", self.plan)
        self.seal()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def seal(self):
        rows = []
        for path in (self.root / "input").rglob("*"):
            if path.is_file() and path.name != "source-manifest.json":
                rows.append({"path": path.relative_to(self.root).as_posix(), "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.write("input/source-manifest.json", {"files": rows})

    def setup_generator(self, responses=None, **kwargs):
        connections = Connections(responses if responses is not None else [json_response(), Response(png_bytes())] * 2)
        transport = RightApiImageTransport(api_key="dummy-secret", resolver=resolver, connection_factory=connections)
        return RightApiImageGenerator(transport, **kwargs), connections

    def generate(self, generator):
        return generator.generate(ImageRequest(product_id="P000001", product_dir=self.root, source={}))

    def test_single_slot_precise_url_refs_no_extra_calls(self):
        generator, connections = self.setup_generator(slot_filter=["main-S1"])
        result = self.generate(generator)
        self.assertEqual(result["generator"], "rightapi")
        self.assertEqual(len(result["generated"]), 1)
        self.assertEqual(len(connections.calls), 2)
        body = json.loads(connections.calls[0]["body"])
        self.assertEqual(body["images"], ["https://cbu01.alicdn.com/main.png", "https://cbu01.alicdn.com/S1.png"])
        from PIL import Image
        with Image.open(self.root / result["generated"][0]["path"]) as image:
            self.assertEqual(image.size, (900, 1200))
            self.assertEqual(image.format, "PNG")
        self.assertNotIn("https://", json.dumps(result))
        self.assertNotIn("dummy-secret", json.dumps(result))

    def test_eleven_and_hundred_selected_skus_allow_single_slot_with_offline_transport(self):
        for count in (11, 100):
            with self.subTest(count=count):
                self.source["skus"] = self.source["skus"][:2] + [{"sku_id": f"S{index}"} for index in range(3, count + 1)]
                self.write("input/source.json", self.source)
                self.write("input/selected-skus.json", {"selected": [f"S{index}" for index in range(1, count + 1)]})
                self.seal()
                generator, connections = self.setup_generator(slot_filter=["main-S1"])
                result = self.generate(generator)
                self.assertEqual(len(result["generated"]), 1)
                self.assertEqual(len(connections.calls), 2, "only one mock generation and one mock download")
                self.assertEqual(len(json.loads(connections.calls[0]["body"])["images"]), 2)

    def test_no_reference_is_explicit_only(self):
        generator, connections = self.setup_generator(slot_filter=["main-S1"], use_reference=False, normalize=False)
        self.generate(generator)
        self.assertEqual(json.loads(connections.calls[0]["body"])["images"], [])

    def test_legacy_exact_original_index_mapping(self):
        self.source.pop("image_sources")
        self.write("input/source.json", self.source)
        self.seal()
        generator, connections = self.setup_generator(slot_filter=["main-S1"])
        self.generate(generator)
        self.assertEqual(json.loads(connections.calls[0]["body"])["images"][0], "https://cbu01.alicdn.com/main.png")

    def test_legacy_index_is_not_zipped_against_successful_downloads(self):
        self.source.pop("image_sources")
        new = "input/main-images/002-main.png"
        (self.root / self.main).rename(self.root / new)
        self.source["stored_images"][0] = new
        self.write("input/source.json", self.source)
        self.write("input/raw-snapshot.json", {"raw": {"main_images": ["https://cbu01.alicdn.com/failed.png", "https://cbu01.alicdn.com/right.png"]}})
        for row in self.plan["main_images"] + self.plan["detail_images"]:
            row["reference_product_images"] = [new if ref == self.main else ref for ref in row["reference_product_images"]]
        self.write("output/image-plan.json", self.plan)
        self.seal()
        generator, connections = self.setup_generator(slot_filter=["main-S1"])
        self.generate(generator)
        self.assertEqual(json.loads(connections.calls[0]["body"])["images"][0], "https://cbu01.alicdn.com/right.png")

    def test_legacy_index_mirrors_collector_filtering_empty_and_non_url_entries(self):
        self.source.pop("image_sources")
        new = "input/main-images/002-main.png"
        (self.root / self.main).rename(self.root / new)
        self.source["stored_images"][0] = new
        self.write("input/source.json", self.source)
        self.write("input/raw-snapshot.json", {"raw": {"main_images": [None, "", {}, {"name": "non-url"},
            123, {"url": "https://cbu01.alicdn.com/A.png"}, {"src": "https://cbu01.alicdn.com/B.png"}]}})
        for row in self.plan["main_images"] + self.plan["detail_images"]:
            row["reference_product_images"] = [new if ref == self.main else ref for ref in row["reference_product_images"]]
        self.write("output/image-plan.json", self.plan)
        self.seal()
        generator, connections = self.setup_generator(slot_filter=["main-S1"])
        self.generate(generator)
        self.assertEqual(json.loads(connections.calls[0]["body"])["images"][0], "https://cbu01.alicdn.com/B.png")

    def test_legacy_local_import_with_raw_url_array_is_not_guessed(self):
        self.source.pop("image_sources")
        self.write("input/source.json", self.source)
        self.write("input/raw-snapshot.json", {"raw": {"images": {"main": [{"path": "local.png"}]},
            "main_images": ["https://cbu01.alicdn.com/unrelated.png"]}})
        self.seal()
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_unmapped_local_reference_is_rejected_before_charge(self):
        self.source.pop("image_sources")
        self.write("input/source.json", self.source)
        self.write("input/raw-snapshot.json", {"raw": {"main_images": []}})
        self.seal()
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_unselected_sku_reference_rejected_before_charge(self):
        self.plan["main_images"][0]["reference_product_images"] = [self.other]
        self.write("output/image-plan.json", self.plan)
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_unselected_slot_rejected_before_charge(self):
        self.plan["main_images"][0]["source_sku_id"] = "S2"
        self.write("output/image-plan.json", self.plan)
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_missing_sku_confirmation_rejected_before_charge(self):
        (self.root / "input/selected-skus.json").unlink()
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_missing_pillow_rejected_before_charge(self):
        generator, connections = self.setup_generator()
        with patch.dict("sys.modules", {"PIL": None}):
            with self.assertRaises(ModelError) as ctx:
                self.generate(generator)
        self.assertIn("尚未调用付费接口", str(ctx.exception))
        self.assertEqual(connections.calls, [])

    def test_non_capture_file_cannot_be_used_as_reference(self):
        self.source["image_sources"].append({"path": "input/raw-snapshot.json", "url": "https://cbu01.alicdn.com/source.png"})
        self.write("input/source.json", self.source)
        self.seal()
        self.plan["main_images"][0]["reference_product_images"] = ["input/raw-snapshot.json"]
        self.write("output/image-plan.json", self.plan)
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_changed_source_and_reference_fail_seal_before_charge(self):
        for relative in ("input/source.json", self.main):
            with self.subTest(relative=relative):
                original = (self.root / relative).read_bytes()
                (self.root / relative).write_bytes(original + b" ")
                generator, connections = self.setup_generator()
                with self.assertRaises(ModelError):
                    self.generate(generator)
                self.assertEqual(connections.calls, [])
                (self.root / relative).write_bytes(original)

    def test_missing_output_path_fails_entire_preflight_before_charge(self):
        self.plan["detail_images"][0]["output_path"] = "../../user-file.png"
        self.write("output/image-plan.json", self.plan)
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_path_traversal_and_external_output_rejected(self):
        for relative in ("output/generated-images/../../../escape.png", "/escape.png", "C:/escape.png",
                         "input/source.json", "output/generated-images/fake.jpg"):
            self.plan["main_images"][0]["output_path"] = relative
            self.write("output/image-plan.json", self.plan)
            generator, connections = self.setup_generator(slot_filter=["main-S1"])
            with self.assertRaises(ModelError):
                self.generate(generator)
            self.assertEqual(connections.calls, [])

    def test_invalid_image_does_not_replace_original_or_continue_charging(self):
        target = self.root / self.plan["main_images"][0]["output_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old-image")
        generator, connections = self.setup_generator([json_response(), Response(b"not-an-image")])
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(target.read_bytes(), b"old-image")
        self.assertEqual(len(connections.calls), 2)

    def test_second_post_failure_stops_and_preserves_first_image(self):
        generator, connections = self.setup_generator([json_response(), Response(png_bytes()), Response(status=429)])
        result = self.generate(generator)
        self.assertEqual(len(result["generated"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(connections.calls), 3)

    def test_ambiguous_reference_url_is_rejected_before_charge(self):
        self.source["image_sources"].append({"path": self.main, "url": "https://cbu01.alicdn.com/other.png"})
        self.write("input/source.json", self.source)
        self.seal()
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_ref_path_outside_product_rejected_before_charge(self):
        self.plan["main_images"][0]["reference_product_images"] = ["../outside.png"]
        self.write("output/image-plan.json", self.plan)
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])

    def test_unknown_or_duplicate_slots_rejected_before_charge(self):
        generator, connections = self.setup_generator(slot_filter=["unknown"])
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])
        self.plan["detail_images"][0]["slot"] = "main-S1"
        self.write("output/image-plan.json", self.plan)
        generator, connections = self.setup_generator()
        with self.assertRaises(ModelError):
            self.generate(generator)
        self.assertEqual(connections.calls, [])


if __name__ == "__main__":
    unittest.main()
