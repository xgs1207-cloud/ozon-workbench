from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from pipeline.prompt_library import delete_prompt, list_prompts, save_prompt


class PromptLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "market.sqlite3"
    def tearDown(self):
        self.temp.cleanup()

    def test_save_select_update_and_delete_persist_without_model(self):
        self.assertEqual(list_prompts(self.database), [])
        saved = save_prompt(self.database, "主图", "保持商品外观，纯色背景")
        self.assertEqual(list_prompts(self.database)[0]["prompt"], saved["prompt"])
        updated = save_prompt(self.database, "主图", "保持颜色和配件一致")
        self.assertEqual(updated["id"], saved["id"])
        self.assertEqual(updated["created_at"], saved["created_at"])
        self.assertEqual(len(list_prompts(self.database)), 1)
        self.assertTrue(delete_prompt(self.database, saved["id"]))
        self.assertFalse(delete_prompt(self.database, saved["id"]))
        self.assertEqual(list_prompts(self.database), [])

    def test_validation_and_sql_values(self):
        for name, prompt in [(" ", "x"), ("x", " "), ("x" * 81, "x"), ("x", "y" * 4001), ("x\ny", "x")]:
            with self.assertRaises(ValueError):
                save_prompt(self.database, name, prompt)
        saved = save_prompt(self.database, "A'; DROP TABLE image_prompts; --", "<script>literal prompt</script>")
        self.assertEqual(list_prompts(self.database)[0]["id"], saved["id"])

    def test_http_controls_and_no_model_calls(self):
        import api
        with patch.object(api, "MARKET_DB_PATH", self.database), TestClient(api.app) as client:
            response = client.post("/api/workbench/image-prompts", json={"name": "常用主图", "prompt": "保持真实商品形状"})
            self.assertEqual(response.status_code, 200, response.text)
            identity = response.json()["id"]
            self.assertEqual(response.json()["model_calls"], 0)
            self.assertEqual(client.get("/api/workbench/image-prompts").json()["items"][0]["id"], identity)
            self.assertEqual(client.post("/api/workbench/image-prompts", json={"name": " ", "prompt": "test"}).status_code, 422)
            self.assertEqual(client.delete(f"/api/workbench/image-prompts/{identity}").status_code, 200)
            self.assertEqual(client.delete(f"/api/workbench/image-prompts/{identity}").status_code, 404)
