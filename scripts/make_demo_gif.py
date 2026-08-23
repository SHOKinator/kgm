"""
Generates docs/demo.gif for the README by driving the local Gradio app with
Playwright and stitching screenshots together with Pillow. Runs entirely
against the local database (no network calls to zakup.sk.kz) — safe to
re-run any time the UI changes.

Usage:
    python scripts/make_demo_gif.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_QUERY = "Аккумулятор, для ИБП, напряжение 12 В, емкость 20 А/ч"
OUT_PATH = ROOT / "docs" / "demo.gif"
VIEWPORT = {"width": 1100, "height": 760}


def main() -> None:
    import app as appmod

    demo = appmod.build_app()
    demo.launch(prevent_thread_lock=True, quiet=True, show_error=True, inbrowser=False)
    time.sleep(1.5)

    frames: list[Image.Image] = []
    durations: list[int] = []

    def snap(hold_ms: int) -> None:
        png = page.screenshot()
        frames.append(Image.open(io.BytesIO(png)).convert("RGB"))
        durations.append(hold_ms)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(demo.local_url, wait_until="networkidle")
            page.wait_for_timeout(800)

            # 1) empty state
            snap(1300)

            # 2) typing animation — click the textbox, type char by char
            box = page.locator("textarea, input[type=text]").first
            box.click()
            typed = ""
            for ch in DEMO_QUERY:
                typed += ch
                box.type(ch, delay=0)
                if len(typed) % 3 == 0:
                    snap(45)
            snap(500)

            # 3) click search, wait for results to actually render, hold
            page.get_by_role("button", name="Search").click()
            page.wait_for_selector("text=Found in local database", timeout=10000)
            page.wait_for_timeout(300)
            snap(2800)

        gif_frames = [f.resize((VIEWPORT["width"], VIEWPORT["height"])) for f in frames]
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        gif_frames[0].save(
            OUT_PATH, save_all=True, append_images=gif_frames[1:],
            duration=durations, loop=0, optimize=True,
        )
        print(f"Wrote {OUT_PATH} ({len(gif_frames)} frames)")
    finally:
        demo.close()


if __name__ == "__main__":
    main()
