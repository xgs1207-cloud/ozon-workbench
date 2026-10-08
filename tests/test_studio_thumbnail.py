"""Exercise the presentation-only cover lookup without booting provider clients."""
import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class StudioThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tree = ast.parse((Path(__file__).parents[1] / "api.py").read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_product_thumbnail")
        scope = {"Path": Path, "PRODUCTS_ROOT": self.root}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "thumbnail", "exec"), scope)
        self.lookup = scope["_product_thumbnail"]

    def test_missing_media_remains_empty(self):
        self.assertIsNone(self.lookup(self.root / "P000007", "P000007"))

    def test_cover_uses_local_read_only_media_route(self):
        product = self.root / "P000007"
        folder = product / "input/main-images"
        folder.mkdir(parents=True)
        (folder / "商品 01.png").touch()
        self.assertEqual(self.lookup(product, "P000007"),
                         "/api/workbench/products/P000007/media/input/main-images/%E5%95%86%E5%93%81%2001.png")

    def test_main_cover_precedes_sku_cover_and_excludes_other_files(self):
        product = self.root / "P000007"
        for folder in ("main-images", "sku-images"):
            (product / "input" / folder).mkdir(parents=True)
        (product / "input/main-images/status.json").touch()
        (product / "input/sku-images/a.jpg").touch()
        self.assertIn("sku-images/a.jpg", self.lookup(product, "P000007"))
        (product / "input/main-images/b.webp").touch()
        self.assertIn("main-images/b.webp", self.lookup(product, "P000007"))

    def test_directory_outside_product_root_is_rejected(self):
        with TemporaryDirectory() as external:
            self.assertIsNone(self.lookup(Path(external), "P000007"))


if __name__ == "__main__":
    unittest.main()
