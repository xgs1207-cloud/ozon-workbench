"""Desktop integration assets and package-only route boundary, no remote calls."""
from pathlib import Path
import unittest

from tests import test_authorized_forms_api as authorized_forms


class ListingBenchAssetsTests(unittest.TestCase):
    setUp = authorized_forms.AuthorizedFormsApiTests.setUp
    tearDown = authorized_forms.AuthorizedFormsApiTests.tearDown
    authorize = authorized_forms.AuthorizedFormsApiTests.authorize
    product = authorized_forms.AuthorizedFormsApiTests.product

    def test_new_desktop_assets_are_served_and_loaded_after_flow(self):
        for name, content_type in (("listing-card.js", "javascript"), ("listing-media.js", "javascript"),
                                   ("listing-publication.js", "javascript"),
                                   ("listing-rich-content.js", "javascript"),
                                   ("listing-rich-content.css", "text/css"),
                                   ("image-preview.js", "javascript"),
                                   ("listing-bench.css", "text/css")):
            response = self.client.get("/assets/" + name)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(content_type, response.headers["content-type"])
        html = (Path(__file__).resolve().parents[1] / "web/research-workbench.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/assets/listing-flow.js'), html.index('src="/assets/listing-card.js'))
        self.assertLess(html.index('src="/assets/listing-card.js'), html.index('src="/assets/listing-media.js'))
        self.assertLess(html.index('src="/assets/listing-media.js'), html.index('src="/assets/listing-publication.js'))
        self.assertLess(html.index('src="/assets/listing-publication.js'), html.index('src="/assets/image-preview.js'))
        self.assertLess(html.index('src="/assets/listing-publication.js'), html.index('src="/assets/listing-rich-content.js'))
        self.assertLess(html.index('src="/assets/listing-rich-content.js'), html.index('src="/assets/image-preview.js'))

    def test_package_only_measurements_keep_unknown_body_unknown(self):
        self.authorize()
        _, base = self.product()
        package = {"length_mm": 80, "width_mm": 70, "height_mm": 90, "weight_g": 120}
        response = self.client.put(base + "/measurements", json={"package": package})
        self.assertEqual(response.status_code, 200, response.text)
        values = response.json()["overrides"]["product"]
        self.assertEqual(values, {"package_" + key: value for key, value in package.items()})

    def test_package_only_update_preserves_known_body_and_checks_hierarchy(self):
        self.authorize()
        _, base = self.product()
        body = {"length_mm": 40, "width_mm": 40, "height_mm": 60, "weight_g": 100}
        package = {"length_mm": 80, "width_mm": 70, "height_mm": 90, "weight_g": 120}
        self.assertEqual(self.client.put(base + "/measurements", json={"product": body, "package": package}).status_code, 200)
        response = self.client.put(base + "/measurements", json={"package": {**package, "weight_g": 140}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["overrides"]["product"]["product_weight_g"], 100)
        invalid = self.client.put(base + "/measurements", json={"package": {**package, "weight_g": 90}})
        self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
