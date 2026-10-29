"""Optional localhost-only visual smoke check: pip install playwright first.

Uses a fresh headless Chrome profile. Never connects to cloud account APIs.
Run while `python -m http.server 8879 --bind 127.0.0.1 --directory site` is active.
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    data = json.loads((ROOT / "site/data.json").read_text())
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for sample in (False, True):
            for label, width, height in (("desktop", 1440, 1100), ("mobile", 390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                if sample:
                    sample_data = json.loads(json.dumps(data))
                    sample_data["mode"] = "SYNTHETIC UI TEST ONLY"
                    sample_data["breakdowns"] = {k: [{"label": v, "hours": 8}] for k,v in [("gpu", "L4"), ("project", "demo"), ("region", "US-MO-2")]}
                    sample_data["longest_runs"] = [{"provider":"runpod", "project":"demo", "gpu":"L4", "region":"US-MO-2", "hours":2}]
                    sample_data["kaggle"] = {"used_hours":4,"total_hours":30,"remaining_hours":26,"as_of":"2026-10-29","refresh_at":"2026-11-01","basis":"SYNTHETIC UI TEST ONLY"}
                    page.route("**/data.json", lambda route: route.fulfill(json=sample_data))
                page.goto("http://127.0.0.1:8879", wait_until="networkidle")
                assert not errors, errors
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert page.locator("svg rect").count() == 6
                if sample:
                    assert page.locator(".barfill").first.bounding_box()["width"] > 0
                    assert "26 h left" in page.locator("#quota").inner_text()
                    page.locator("#notice").evaluate("e => e.textContent = 'SYNTHETIC UI TEST ONLY — not account data'")
                name = f"{'synthetic-' if sample else ''}{label}.png"
                page.screenshot(path=str(artifacts / name), full_page=True)
                print(f"{name}: no JS errors; no horizontal overflow; all expected charts rendered")
                page.close()
        browser.close()


if __name__ == "__main__":
    main()
