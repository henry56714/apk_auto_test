#!/usr/bin/env python3
"""Take README screenshots from the demo report using headless Chromium.

Run from perf_auto_test/scripts/:
    python -m pat.demo.screenshot
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

REPORT_HTML = Path(__file__).parent.parent.parent / "reports" / "demo" / "report.html"
OUT_DIR = Path(__file__).parent.parent.parent.parent.parent / "docs" / "screenshots"

SHOTS = [
    # (filename, description, selector_or_None, viewport_w, viewport_h, clip_or_None)
    # clip = {"x","y","w","h"} relative to page
    (
        "overview.png",
        "Header + verdict + KPIs + timeline rail",
        None,
        1440,
        900,
        {"x": 0, "y": 0, "width": 1440, "height": 820},
    ),
    (
        "incidents.png",
        "Incident list + detail panel (incident-001 open)",
        "#sec-incidents",
        1440,
        900,
        None,
    ),
    ("charts.png", "CPU + memory time-series charts", "#sec-charts", 1440, 900, None),
]


def take_screenshots() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    url = REPORT_HTML.as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url, wait_until="networkidle")

        # Switch to English so screenshots work for the international README
        page.click('button[data-lang-btn="en"]')
        page.wait_for_timeout(300)

        # ── overview: top of page ────────────────────────────────────────────
        fname, _, selector, vw, vh, clip = SHOTS[0]
        page.set_viewport_size({"width": vw, "height": vh})
        page.wait_for_timeout(200)
        page.screenshot(
            path=str(OUT_DIR / fname),
            clip=clip,
        )
        print(f"  ✓ {fname}")

        # ── incidents: expand section, open incident-001 ─────────────────────
        fname, _, selector, vw, vh, clip = SHOTS[1]
        inc_section = page.locator(".filter-bar")
        inc_section.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        # click first incident to open detail
        page.locator(".inc-item").first.click()
        page.wait_for_timeout(400)
        # scroll so filter bar + detail panel both visible
        page.locator(".filter-bar").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        # capture the master-detail section
        panel = page.locator(".filter-bar")
        box = panel.bounding_box()
        if box:
            # include some space above and the full md panel below
            md = page.locator(".md")
            md_box = md.bounding_box()
            top = box["y"] - 8
            bottom = (md_box["y"] + md_box["height"] + 8) if md_box else top + 700
            page.screenshot(
                path=str(OUT_DIR / fname),
                clip={
                    "x": 0,
                    "y": max(0, top),
                    "width": vw,
                    "height": min(900, bottom - top),
                },
            )
        else:
            page.screenshot(path=str(OUT_DIR / fname), full_page=False)
        print(f"  ✓ {fname}")

        # ── charts: scroll to charts section ────────────────────────────────
        fname, _, selector, vw, vh, clip = SHOTS[2]
        charts_sec = page.locator(".charts").first
        charts_sec.scroll_into_view_if_needed()
        page.wait_for_timeout(600)  # give Plotly time to finish rendering
        box = charts_sec.bounding_box()
        if box:
            page.screenshot(
                path=str(OUT_DIR / fname),
                clip={
                    "x": 0,
                    "y": max(0, box["y"] - 40),
                    "width": vw,
                    "height": min(900, box["height"] + 80),
                },
            )
        else:
            page.screenshot(path=str(OUT_DIR / fname), full_page=False)
        print(f"  ✓ {fname}")

        browser.close()

    print(f"\nScreenshots saved → {OUT_DIR}")


if __name__ == "__main__":
    take_screenshots()
