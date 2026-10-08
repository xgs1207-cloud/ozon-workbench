"""Independent browser-private library: fixture-only, no Seerfar or Ozon calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline import keyword_library as service
from workbench_keyword_library_api import COOKIE_NAME, PREFIX, register_keyword_library_routes


class EmployeeKeywordLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = FastAPI()
        register_keyword_library_routes(self.app, runtime_root=lambda: self.root)
        self.a = TestClient(self.app)
        self.b = TestClient(self.app)

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def add(self, client=None, **kwargs):
        client = client or self.a
        if not client.cookies.get(COOKIE_NAME):
            client.get(PREFIX)
        return client.post(PREFIX, json={"category": "厨具", "text": "кастрюля чугунная", "note": "主关键词", **kwargs})

    def test_identity_created_lazy_cookie_not_client_profile(self):
        self.assertEqual(list(self.root.iterdir()), [])
        result = self.a.get(PREFIX)
        self.assertEqual(result.status_code, 200)
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertIn("SameSite=strict", result.headers["set-cookie"])
        self.assertIn(f"Path={PREFIX}", result.headers["set-cookie"])
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.json()["identity"]["mode"], "signed_browser_profile")
        self.assertNotIn("owner", result.json())
        forged = self.a.post(PREFIX, json={"category": "厨具", "text": "锅", "owner_id": "someone"})
        self.assertEqual(forged.status_code, 422)

    def test_crud_category_search_notes_and_literal_wildcards(self):
        created = self.add().json()["item"]
        self.assertEqual(created["note"], "主关键词")
        self.assertEqual(self.a.get(f"{PREFIX}/{created['id']}").json()["item"], created)
        result = self.a.get(PREFIX, params={"q": "ЧУГУННАЯ", "category": "厨具"}).json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["categories"], ["厨具"])
        self.assertEqual(self.a.get(PREFIX, params={"q": "主关键词"}).json()["total"], 1)
        self.assertEqual(self.a.get(PREFIX, params={"q": "%"}).json()["total"], 0)
        updated = self.a.put(f"{PREFIX}/{created['id']}", json={"category": "锅具", "text": "кастрюля", "note": "锅体材质已确认"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.a.get(PREFIX, params={"category": "厨具"}).json()["total"], 0)
        self.assertEqual(self.a.get(f"{PREFIX}/categories").json()["items"], [{"category": "锅具", "count": 1}])
        self.assertEqual(self.a.delete(f"{PREFIX}/{created['id']}").status_code, 200)
        self.assertEqual(self.a.get(PREFIX).json()["total"], 0)
        self.assertEqual(self.a.get(f"{PREFIX}/{created['id']}").status_code, 404)

    def test_cross_owner_guessed_item_cannot_be_read_edited_or_deleted(self):
        created = self.add().json()["item"]
        self.assertEqual(self.b.get(PREFIX).json()["total"], 0)
        self.assertEqual(self.b.get(f"{PREFIX}/{created['id']}").status_code, 404)
        self.assertEqual(self.b.put(f"{PREFIX}/{created['id']}", json={"category": "a", "text": "b"}).status_code, 404)
        self.assertEqual(self.b.delete(f"{PREFIX}/{created['id']}").status_code, 404)
        self.assertEqual(self.a.get(PREFIX).json()["total"], 1)
        self.assertEqual(self.add(self.b).status_code, 200)  # identical text in a separate independent library

    def test_tampered_cookie_and_owner_query_do_not_change_identity(self):
        self.add()
        self.assertEqual(self.a.get(PREFIX, params={"profile_id": "other", "owner_id": "other"}).json()["total"], 1)
        token = self.a.cookies.get(COOKIE_NAME)
        forged = token[:-1] + ("a" if token[-1] != "a" else "b")
        with TestClient(self.app) as client:
            result = client.get(PREFIX, headers={"cookie": f"{COOKIE_NAME}={forged}"})
        self.assertEqual(result.status_code, 401)

    def test_restart_preserves_cookie_identity(self):
        created = self.add().json()["item"]
        token = self.a.cookies.get(COOKIE_NAME)
        fresh_app = FastAPI()
        register_keyword_library_routes(fresh_app, runtime_root=self.root)
        with TestClient(fresh_app) as client:
            result = client.get(PREFIX, headers={"cookie": f"{COOKIE_NAME}={token}"})
        self.assertEqual(result.json()["items"][0]["id"], created["id"])

    def test_csrf_and_secure_cookie(self):
        self.assertEqual(self.add().status_code, 200)
        rejected = self.a.post(PREFIX, headers={"Origin": "https://evil.example"}, json={"category": "a", "text": "b"})
        self.assertEqual(rejected.status_code, 403)
        rejected = self.a.delete(f"{PREFIX}/missing", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(rejected.status_code, 403)
        with TestClient(self.app, base_url="https://workbench.example") as client:
            response = client.get(PREFIX)
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_duplicate_normalized_and_validation(self):
        self.add(text="  КАСТРЮЛЯ   чугунная ")
        self.assertEqual(self.add().status_code, 409)
        self.assertEqual(self.add(category="  ").status_code, 422)
        self.assertEqual(self.add(text="\u0000bad").status_code, 422)
        self.assertEqual(self.a.get(PREFIX, params={"limit": 201}).status_code, 422)

    def test_identity_signature_expiry_and_concurrent_key_initialization(self):
        db = self.root / "new.sqlite3"
        with ThreadPoolExecutor(max_workers=4) as executor:
            tokens = list(executor.map(lambda _: service.browser_identity(db, None, now=100), range(8)))
        for owner, token in tokens:
            self.assertEqual(service.browser_identity(db, token, now=101), (owner, None))
            with self.assertRaises(service.InvalidIdentity):
                service.browser_identity(db, token, now=100 + service.IDENTITY_TTL + 1)

    def test_mutation_without_bootstrapped_identity_does_not_create_an_orphan_library(self):
        response = self.a.post(PREFIX, json={"category": "厨具", "text": "кастрюля"})
        self.assertEqual(response.status_code, 428)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.add().status_code, 200)


if __name__ == "__main__":
    unittest.main()
