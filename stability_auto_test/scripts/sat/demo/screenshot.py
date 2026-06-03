#!/usr/bin/env python3
"""Take README screenshots from the sat demo report using headless Chromium.

Run from stability_auto_test/scripts/:
    python -m sat.demo.screenshot
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

REPORT_HTML = Path(__file__).parent.parent.parent / "reports" / "demo" / "report.html"
OUT_DIR = Path(__file__).parent.parent.parent.parent.parent / "docs" / "screenshots"


def take_screenshots() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    url = REPORT_HTML.as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url, wait_until="networkidle")

        # Switch to English
        page.click('button[data-lang-btn="en"]')
        page.wait_for_timeout(500)

        # ── 1. overview: verdict-bar + event counters + timeline ─────────────
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(800)  # give Plotly time to render timeline
        page.screenshot(
            path=str(OUT_DIR / "sat_overview.png"),
            clip={"x": 0, "y": 0, "width": 1440, "height": 860},
        )
        print("  ✓ sat_overview.png")

        # ── 2. incidents: filter to java_crash, open first item (shows stack) ──
        page.locator("#type-chips .filter-chip[data-type='java_crash']").click()
        page.wait_for_timeout(300)
        page.locator(".inc-item:visible").first.click()
        page.wait_for_timeout(500)

        filter_bar = page.locator(".filter-bar")
        filter_bar.scroll_into_view_if_needed()
        page.wait_for_timeout(200)

        # Expand viewport so stack frames fit without scrolling
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.wait_for_timeout(200)
        filter_bar.scroll_into_view_if_needed()
        page.wait_for_timeout(200)

        fb_box = filter_bar.bounding_box()
        md = page.locator(".md")
        md_box = md.bounding_box()
        if fb_box and md_box:
            top = max(0, fb_box["y"] - 8)
            bottom = md_box["y"] + md_box["height"] + 12
            page.screenshot(
                path=str(OUT_DIR / "sat_incidents.png"),
                clip={"x": 0, "y": top, "width": 1440, "height": min(1100, bottom - top)},
            )
        else:
            page.screenshot(path=str(OUT_DIR / "sat_incidents.png"))
        print("  ✓ sat_incidents.png")

        # ── 3. process stability table ────────────────────────────────────────
        proc_section = page.locator(".sec-head").filter(has_text="Process stability")
        proc_section.scroll_into_view_if_needed()
        page.wait_for_timeout(300)

        table_wrap = page.locator(".table-wrap").first
        tw_box = table_wrap.bounding_box()
        if tw_box:
            sh_box = proc_section.bounding_box()
            top = max(0, (sh_box["y"] if sh_box else tw_box["y"]) - 8)
            bottom = tw_box["y"] + tw_box["height"] + 20
            page.screenshot(
                path=str(OUT_DIR / "sat_process_table.png"),
                clip={"x": 0, "y": top, "width": 1440, "height": min(900, bottom - top)},
            )
        else:
            page.screenshot(path=str(OUT_DIR / "sat_process_table.png"))
        print("  ✓ sat_process_table.png")

        browser.close()

    print(f"\nScreenshots saved → {OUT_DIR}")


if __name__ == "__main__":
    take_screenshots()
