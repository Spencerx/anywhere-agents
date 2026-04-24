"""One-shot headless render of the session-start banner HTML to PNG.

Run:
    python docs/_render_banner.py

Reads docs/banner.html (the committed source) and writes docs/session-banner.png
with device_scale_factor=2 for retina sharpness. Rerun whenever docs/banner.html
changes (color palette, layout, content).
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SRC = HERE / "banner.html"
OUT = HERE / "session-banner.png"


def main() -> None:
    url = SRC.as_uri()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1180, "height": 720},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(300)
        sheet = page.locator(".sheet")
        sheet.screenshot(path=str(OUT), omit_background=False)
        browser.close()
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
