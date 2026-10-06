"""Optional visual smoke check for the guided UI; never calls live Ozon writes.

Run after starting a local API server:
    python tests/browser_qa.py --url http://127.0.0.1:8767/
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8767/")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
        try:
            desktop = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            desktop.goto(args.url)
            desktop.get_by_role("heading", name="类目机会").wait_for()
            desktop.get_by_role("button", name="调整筛选规则").click()
            desktop.get_by_role("heading", name="筛选规则").wait_for()
            desktop.locator('[data-view="keywords"]').click()
            assert desktop.get_by_role("heading", name="关键词决策").is_visible()
            desktop.locator('[data-view="sessions"]').click()
            assert desktop.get_by_role("heading", name="1688 商品批次").is_visible()
            desktop.locator('[data-view="product"]').click()
            assert desktop.get_by_role("heading", name="上架审核").is_visible()
            desktop.locator('[data-view="categories"]').click()
            desktop.get_by_role("heading", name="类目机会").wait_for()
            desktop_box = desktop.evaluate("({viewport:innerWidth,scroll:document.documentElement.scrollWidth})")
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                desktop.screenshot(path=str(args.screenshots / "desktop.png"))

            mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
            mobile.goto(args.url)
            mobile.get_by_role("heading", name="类目机会").wait_for()
            mobile_box = mobile.evaluate("({viewport:innerWidth,scroll:document.documentElement.scrollWidth})")
            if args.screenshots:
                mobile.screenshot(path=str(args.screenshots / "mobile.png"))

            dense = browser.new_page(viewport={"width": 1440, "height": 900})
            metrics = {"demand": 120000, "stability": 87, "competition_density": .24,
                       "history_months": 4, "latest_period": "2026-09", "evidence_ids": [11, 12, 13, 14]}
            category = {"key": "床单 простыня", "category_key": "床单 простыня", "label": "床单",
                        "recommended": True, "score": 81.2, "confidence": "high", "warnings": [],
                        "reasons": ["需求稳定度 87/100", "竞争密度处于候选低位"], "metrics": metrics}
            words = [
                {"key": "простыня", "label": "простыня", "category_key": category["key"],
                 "recommended": True, "score": 83, "confidence": "high", "warnings": [],
                 "reasons": ["需求稳定度 90/100"], "metrics": metrics, "ozon_evidence": None},
                {"key": "простыня хлопковая", "label": "простыня хлопковая", "category_key": category["key"],
                 "recommended": True, "score": 77, "confidence": "high", "warnings": [],
                 "reasons": ["竞争较低"], "metrics": metrics, "ozon_evidence": None},
            ]
            dense.route("**/api/research/categories", lambda route: route.fulfill(
                json={"ok": True, "items": [category], "recommended_count": 1}))
            dense.route("**/api/research/keywords?*", lambda route: route.fulfill(
                json={"ok": True, "items": words, "recommended_count": 2}))
            fake_session = {"id": "R-EXAMPLE1234", "primary_keyword": "простыня",
                            "secondary_keywords": ["простыня хлопковая"], "category_key": category["key"],
                            "auto_publish_enabled": False, "target_store_id": "",
                            "products": [{"product_id": "P000001", "attached_at": "2026-10-06"}]}
            dense.route("**/api/research/sessions", lambda route: route.fulfill(
                json={"ok": True, "items": [fake_session]}))
            dense.route("**/api/collector/products", lambda route: route.fulfill(
                json={"items": [{"product_id": "P000001", "title_zh": "纯棉床单", "status": "COLLECTED"}]}))
            sections = {name: {"approved": False, "problems": ["尚未生成"]}
                        for name in ("grouping", "copy", "image_plan", "images", "fields")}
            fake_guided = {"ok": True, "source": {"title_zh": "纯棉床单"}, "analysis": {},
                           "review": {"sections": sections, "ready_to_preflight": False,
                                      "blockers": ["尚未确认 SKU", "尚未准备 Ozon 字段"]},
                           "copy": {}, "image_plan": {}, "image_qc": {}, "grouping": {},
                           "attributes": {}, "category_attributes": {}, "category_selection": {},
                           "manual_prices": {}, "human_confirmations": {}, "measurements": {}}
            dense.route("**/api/workbench/products/P000001/guided", lambda route: route.fulfill(json=fake_guided))
            dense.route("**/api/workbench/products/P000001/skus", lambda route: route.fulfill(
                json={"ok": True, "selected": ["S1"], "skus": [
                    {"sku_id": "S1", "color_ru": "белый", "purchase_price_cny": 42, "listed": True},
                    {"sku_id": "S2", "color_ru": "серый", "purchase_price_cny": 44, "listed": False}]}))
            dense.goto(args.url)
            dense.get_by_role("heading", name="类目机会").wait_for()
            assert dense.get_by_text("床单", exact=True).first.is_visible()
            if args.screenshots:
                dense.screenshot(path=str(args.screenshots / "categories-with-data.png"))
            dense.get_by_role("button", name="查看关键词").click()
            dense.get_by_role("heading", name="关键词决策").wait_for()
            dense.locator('input[name="primary"][value="простыня"]').check()
            dense.locator('input.secondary[value="простыня хлопковая"]').check()
            assert dense.get_by_role("button", name="以所选词建立批次").is_enabled()
            if args.screenshots:
                dense.screenshot(path=str(args.screenshots / "keywords-with-data.png"))
            dense.locator('[data-view="sessions"]').click()
            dense.get_by_role("button", name="R-EXAMPLE1234 · простыня").click()
            dense.get_by_role("button", name="进入审核").click()
            dense.get_by_role("heading", name="01 选择上架规格").wait_for()
            assert dense.get_by_role("heading", name="06 图片规划").is_visible()
            if args.screenshots:
                dense.screenshot(path=str(args.screenshots / "product-review.png"), full_page=True)
            print({"desktop": desktop_box, "mobile": mobile_box, "views": 4,
                   "settings": "ok", "mocked_selection": "ok", "mocked_product_review": "ok"})
            assert desktop_box["scroll"] <= desktop_box["viewport"]
            assert mobile_box["scroll"] <= mobile_box["viewport"]
        finally:
            browser.close()


if __name__ == "__main__":
    main()
