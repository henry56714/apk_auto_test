"""HTML report renderer — full v2 design (Claude Design handoff).

Single self-contained `report.html` that mirrors `report.json`. Layout:

    Header   — package + device + run window + verdict pill
    Hero     — verdict bar + 4 event-count cards + derived strip
    01       — Plotly event timeline (7 lanes: 4 events + 3 lifecycle)
    02       — process stability table
    03       — Incidents (filter bar + master/detail)
    04       — Appendix: bookmarks · effective config · data files
    Footer

All rendering is data-driven: Python embeds the result dict as JSON; the
bundled JS reads the JSON, renders all sections, and wires the
interactions (filters, search, language toggle, copy-package, in-tab file
viewers).
"""

from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Dict

HTML_FILENAME = "report.html"


def _safe_json(obj) -> str:
    """JSON-encode for embedding in a `<script type="application/json">` block.

    Escapes the two characters that could terminate the script tag or break
    JS string parsing if they leak from user-controlled data:
      - ``</`` → ``<\\/``   (avoid a literal ``</script>`` closing the tag)
      - U+2028 / U+2029 → escaped form (defensive)
    """
    return (
        json.dumps(obj, ensure_ascii=False, default=str)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


_CSS = r"""
  :root {
    --ink:           #0b1220;
    --ink-2:         #1f2937;
    --ink-3:         #374151;
    --muted:         #4b5563;
    --faint:         #6b7280;
    --rule:          #d1d5db;
    --rule-2:        #e5e7eb;
    --bg:            #ffffff;
    --bg-soft:       #f7f8fa;
    --bg-tint:       #eef1f4;
    --accent:        #1d4ed8;
    --accent-2:      #c2410c;
    --green:         #15803d;
    --red:           #b91c1c;
    --red-deep:      #7f1d1d;
    --gray:          #4b5563;

    --fs-xs:   13px;
    --fs-sm:   14px;
    --fs-md:   15px;
    --fs-base: 16px;
    --fs-mid:  17px;
    --fs-lg:   20px;
    --fs-xl:   24px;
    --fs-2xl:  32px;
    --fs-hero: 44px;
  }

  .zh, .en { display: none; }
  html[data-lang="zh"] .zh,
  html:not([data-lang]) .zh { display: inline; }
  html[data-lang="en"] .en { display: inline; }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
                 "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans SC", sans-serif;
    font-size: var(--fs-base);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  .mono {
    font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.94em;
    font-variant-numeric: tabular-nums;
  }

  .page { max-width: 1280px; margin: 0 auto; padding: 48px 56px 96px; }

  /* ===== HEADER ===== */
  .doc-head {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: 32px;
    padding-bottom: 28px;
    border-bottom: 1px solid var(--rule);
  }
  .doc-head .eyebrow {
    font-size: var(--fs-xs); letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--muted); font-weight: 600;
    margin-bottom: 10px;
  }
  .doc-head h1 {
    margin: 0;
    font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
    font-size: var(--fs-2xl); font-weight: 600;
    letter-spacing: -0.005em;
    color: var(--ink);
    line-height: 1.2;
    display: inline-flex; align-items: center; gap: 8px;
  }
  .copy-btn {
    width: 22px; height: 22px;
    border: 1px solid var(--rule); border-radius: 4px;
    background: var(--bg); color: var(--muted);
    cursor: pointer; padding: 0;
    display: inline-flex; align-items: center; justify-content: center;
    transition: all 0.12s ease;
  }
  .copy-btn:hover { color: var(--ink); border-color: var(--muted); background: var(--bg-soft); }
  .copy-btn.ok { color: var(--green); border-color: var(--green); background: rgba(21,128,61,0.08); }
  .copy-btn svg { width: 12px; height: 12px; display: block; }
  .doc-head .meta {
    margin-top: 14px;
    font-size: var(--fs-md);
    color: var(--ink-3);
  }
  .doc-head .meta span + span::before { content: "·"; margin: 0 10px; color: var(--faint); }

  .verdict { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; min-width: 220px; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 7px 16px;
    border: 1px solid var(--green);
    color: var(--green);
    background: rgba(21,128,61,0.08);
    border-radius: 999px;
    font-size: var(--fs-md); font-weight: 600;
  }
  .pill::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
  .pill.red    { color: var(--red);     border-color: var(--red);     background: rgba(185,28,28,0.08); }
  .pill.orange { color: var(--accent-2); border-color: var(--accent-2); background: rgba(194,65,12,0.08); }
  .verdict .micro { font-size: var(--fs-sm); color: var(--muted); }

  /* ===== SECTION TITLE ===== */
  section { margin-top: 60px; }
  .sec-head {
    display: flex; align-items: baseline; gap: 16px;
    margin-bottom: 22px;
    flex-wrap: wrap;
  }
  .sec-head h2 {
    margin: 0;
    font-size: var(--fs-lg); font-weight: 700;
    color: var(--ink);
    letter-spacing: -0.005em;
  }
  .sec-head .num-tag {
    font-family: "SF Mono", "JetBrains Mono", Menlo, monospace;
    font-size: var(--fs-sm); color: var(--faint);
    font-weight: 500;
  }
  .sec-head .desc {
    margin-left: auto;
    font-size: var(--fs-md);
    color: var(--muted);
  }

  /* ===== HERO ===== */
  .hero {
    margin-top: 32px;
    border: 1px solid var(--rule);
    border-radius: 6px;
    overflow: hidden;
    background: var(--bg);
  }
  .verdict-bar {
    display: flex; align-items: center; gap: 18px;
    padding: 22px 28px;
    border-bottom: 1px solid var(--rule);
    background: rgba(21,128,61,0.06);
    border-left: 4px solid var(--green);
  }
  .verdict-bar.red    { background: rgba(185,28,28,0.06);  border-left-color: var(--red); }
  .verdict-bar.orange { background: rgba(194,65,12,0.06);  border-left-color: var(--accent-2); }
  .verdict-bar .vicon {
    width: 32px; height: 32px; flex-shrink: 0;
    color: var(--green);
  }
  .verdict-bar.red    .vicon { color: var(--red); }
  .verdict-bar.orange .vicon { color: var(--accent-2); }
  .verdict-bar .vtxt { flex: 1; min-width: 0; }
  .verdict-bar .vtitle {
    font-size: var(--fs-lg); font-weight: 700;
    color: var(--green);
    margin-bottom: 4px;
    letter-spacing: -0.005em;
  }
  .verdict-bar.red    .vtitle { color: var(--red); }
  .verdict-bar.orange .vtitle { color: var(--accent-2); }
  .verdict-bar .vsub {
    font-size: var(--fs-sm); color: var(--muted);
  }

  .ev-cards {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
  }
  .ev-card {
    padding: 24px 26px 20px;
    border-right: 1px solid var(--rule);
    cursor: pointer;
    background: var(--bg);
    transition: background 0.12s ease;
    position: relative;
  }
  .ev-card:last-child { border-right: 0; }
  .ev-card:hover { background: var(--bg-soft); }
  .ev-card .e-head {
    display: flex; align-items: center; gap: 8px;
    font-size: var(--fs-xs); font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 16px;
  }
  .ev-card .e-head .dot {
    width: 8px; height: 8px; border-radius: 999px;
    background: var(--rule);
  }
  .ev-card.java   .e-head { color: var(--red); }
  .ev-card.java   .e-head .dot { background: var(--red); }
  .ev-card.native .e-head { color: var(--red-deep); }
  .ev-card.native .e-head .dot { background: var(--red-deep); }
  .ev-card.anr    .e-head { color: var(--accent-2); }
  .ev-card.anr    .e-head .dot { background: var(--accent-2); }
  .ev-card.death  .e-head { color: var(--gray); }
  .ev-card.death  .e-head .dot { background: var(--gray); }
  .ev-card .e-count {
    font-family: "SF Mono", "JetBrains Mono", Menlo, monospace;
    font-size: var(--fs-hero); font-weight: 700;
    line-height: 1; letter-spacing: -0.02em;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    margin-bottom: 12px;
  }
  .ev-card.zero .e-count { color: #d1d5db; }
  .ev-card.java:not(.zero)   .e-count { color: var(--red); }
  .ev-card.native:not(.zero) .e-count { color: var(--red-deep); }
  .ev-card.anr:not(.zero)    .e-count { color: var(--accent-2); }
  .ev-card.death:not(.zero)  .e-count { color: var(--gray); }
  .ev-card .e-sub {
    font-size: var(--fs-sm); color: var(--ink-3);
    margin-bottom: 4px; min-height: 1.6em;
  }
  .ev-card.zero .e-sub { color: var(--faint); }

  .derived {
    display: flex; flex-wrap: wrap; gap: 32px;
    padding: 16px 28px;
    border-top: 1px solid var(--rule);
    background: var(--bg-soft);
    font-size: var(--fs-sm); color: var(--ink-3);
  }
  .derived .d {
    display: inline-flex; align-items: baseline; gap: 8px;
  }
  .derived .d .lbl {
    color: var(--muted);
    font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em;
    font-weight: 600;
  }
  .derived .d .val {
    font-family: "SF Mono", Menlo, monospace;
    font-variant-numeric: tabular-nums;
    color: var(--ink); font-weight: 600;
  }
  .derived .d .val.warn { color: var(--accent-2); }

  /* ===== TIMELINE ===== */
  .rail {
    border: 1px solid var(--rule);
    border-radius: 6px;
    padding: 20px 24px 16px;
    background: var(--bg);
  }
  .rail-head {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 12px;
    flex-wrap: wrap; gap: 16px;
  }
  .rail-head .span {
    font-family: "SF Mono", "JetBrains Mono", Menlo, monospace;
    font-size: var(--fs-md); color: var(--ink-2); font-weight: 500;
  }
  .rail-head .legend { display: flex; gap: 18px; font-size: var(--fs-sm); color: var(--ink-3); flex-wrap: wrap; }
  .rail-head .legend .item { display: inline-flex; align-items: center; gap: 7px; }
  .sw { width: 11px; height: 11px; display: inline-block; border-radius: 999px; }
  .sw.x-red   { background: var(--red);      width: 9px; height: 9px; transform: rotate(45deg); border-radius: 1px; }
  .sw.x-rd    { background: var(--red-deep); width: 9px; height: 9px; transform: rotate(45deg); border-radius: 1px; }
  .sw.x-or    { background: var(--accent-2); width: 9px; height: 9px; transform: rotate(45deg); border-radius: 1px; }
  .sw.x-gr    { background: var(--gray);     width: 9px; height: 9px; transform: rotate(45deg); border-radius: 1px; }
  .sw.l-b     { background: var(--accent);   width: 2px; height: 13px; border-radius: 0; }
  .sw.d-g     { background: var(--green);    width: 10px; height: 10px; border-radius: 999px; }
  .sw.d-or    { background: var(--accent-2); width: 10px; height: 10px; border-radius: 999px; }
  .sw.d-gr    { background: var(--gray);     width: 10px; height: 10px; border-radius: 999px; }

  #timeline-plot { width: 100%; height: 460px; }

  /* ===== TABLE ===== */
  .sub-title {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 14px;
    gap: 12px; flex-wrap: wrap;
  }
  .sub-title .t { font-size: var(--fs-md); font-weight: 700; color: var(--ink); }
  .sub-title .c { font-size: var(--fs-sm); color: var(--muted); }

  .table-wrap { border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; background: var(--bg); }
  .table-scroll { overflow-x: auto; }
  table.tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-md); }
  table.tbl th, table.tbl td {
    text-align: left;
    padding: 14px 18px;
    border-bottom: 1px solid var(--rule-2);
    white-space: nowrap;
    color: var(--ink-2);
  }
  table.tbl thead th {
    background: var(--bg-soft);
    font-size: var(--fs-xs); font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--ink-3);
    border-bottom: 1px solid var(--rule);
  }
  table.tbl tbody tr:last-child td { border-bottom: 0; }
  table.tbl tbody tr:hover td { background: var(--bg-soft); }
  table.tbl td.r, table.tbl th.r { text-align: right; font-variant-numeric: tabular-nums; }
  table.tbl td.mono {
    font-family: "SF Mono", "JetBrains Mono", Menlo, monospace;
    font-size: calc(var(--fs-md) - 0.5px);
    color: var(--ink);
  }
  .uptime-cell {
    display: inline-flex; align-items: center; gap: 10px;
    width: 130px; justify-content: flex-end;
  }
  .uptime-bar {
    flex: 1; height: 6px; max-width: 80px; min-width: 60px;
    background: var(--rule-2); border-radius: 999px; overflow: hidden;
  }
  .uptime-bar .fill { display: block; height: 100%; background: var(--green); border-radius: 999px; }
  .uptime-bar .fill.warn { background: var(--accent-2); }
  .uptime-val { font-variant-numeric: tabular-nums; min-width: 48px; text-align: right; }
  .uptime-val.warn { color: var(--accent-2); font-weight: 600; }

  .chip {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 3px 10px;
    border-radius: 4px;
    background: var(--bg-tint);
    color: var(--ink-2);
    font-size: var(--fs-sm); font-weight: 600;
    border: 1px solid var(--rule);
    font-variant-numeric: tabular-nums;
  }
  .chip::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: var(--faint); }
  .chip.red       { color: var(--red);      background: rgba(185,28,28,0.08); border-color: rgba(185,28,28,0.3); }
  .chip.red::before       { background: var(--red); }
  .chip.red-deep  { color: var(--red-deep); background: rgba(127,29,29,0.08); border-color: rgba(127,29,29,0.3); }
  .chip.red-deep::before  { background: var(--red-deep); }
  .chip.orange    { color: var(--accent-2); background: rgba(194,65,12,0.08); border-color: rgba(194,65,12,0.3); }
  .chip.orange::before    { background: var(--accent-2); }
  .chip.green     { color: var(--green);    background: rgba(21,128,61,0.08); border-color: rgba(21,128,61,0.3); }
  .chip.green::before     { background: var(--green); }
  .chip.gray      { color: var(--muted);    background: var(--bg-soft); }
  .chip.gray::before      { background: var(--faint); }
  .chip.blue      { color: var(--accent);   background: rgba(29,78,216,0.08); border-color: rgba(29,78,216,0.3); }
  .chip.blue::before      { background: var(--accent); }
  .chip.fatal     { color: var(--red);      background: rgba(185,28,28,0.10); border-color: rgba(185,28,28,0.35); }
  .chip.fatal::before     { background: var(--red); }
  .chip.error     { color: var(--accent-2); background: rgba(194,65,12,0.10); border-color: rgba(194,65,12,0.35); }
  .chip.error::before     { background: var(--accent-2); }
  .chip.warning   { color: var(--muted); }

  .tbl td .cnt-link { cursor: pointer; user-select: none; }
  .tbl td .cnt-link:hover { filter: brightness(0.92); }

  .v-red    { color: var(--red); font-weight: 600; }
  .v-orange { color: var(--accent-2); font-weight: 600; }
  .v-muted  { color: var(--faint); }

  /* ===== INCIDENTS FILTER ===== */
  .filter-bar {
    border: 1px solid var(--rule); border-bottom: 0;
    border-radius: 6px 6px 0 0;
    background: var(--bg);
    padding: 16px 22px;
    display: flex; flex-direction: column; gap: 14px;
  }
  .chip-row { display: flex; flex-wrap: wrap; gap: 8px; }
  .filter-chip {
    appearance: none; border: 1px solid var(--rule);
    padding: 6px 14px;
    border-radius: 999px;
    background: var(--bg);
    color: var(--ink-3);
    font-family: inherit; font-size: var(--fs-sm); font-weight: 600;
    cursor: pointer;
    transition: all 0.12s ease;
    font-variant-numeric: tabular-nums;
    display: inline-flex; align-items: center; gap: 7px;
  }
  .filter-chip::before {
    content: ""; width: 6px; height: 6px; border-radius: 999px;
    background: var(--faint);
  }
  .filter-chip:hover { border-color: var(--muted); color: var(--ink); }
  .filter-chip[disabled] { opacity: 0.45; cursor: not-allowed; }
  .filter-chip[data-active="true"] { color: #fff; }
  .filter-chip[data-active="true"]::before { background: rgba(255,255,255,0.9); }
  .filter-chip[data-type="all"][data-active="true"]            { background: var(--ink); border-color: var(--ink); }
  .filter-chip[data-type="java_crash"][data-active="true"]     { background: var(--red); border-color: var(--red); }
  .filter-chip[data-type="native_crash"][data-active="true"]   { background: var(--red-deep); border-color: var(--red-deep); }
  .filter-chip[data-type="anr"][data-active="true"]            { background: var(--accent-2); border-color: var(--accent-2); }
  .filter-chip[data-type="process_death"][data-active="true"]  { background: var(--gray); border-color: var(--gray); }

  .filter-chip[data-type="java_crash"]::before    { background: var(--red); }
  .filter-chip[data-type="native_crash"]::before  { background: var(--red-deep); }
  .filter-chip[data-type="anr"]::before           { background: var(--accent-2); }
  .filter-chip[data-type="process_death"]::before { background: var(--gray); }
  .filter-chip[data-type="all"]::before           { background: var(--ink); }

  .filter-row {
    display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
  }
  .field {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: var(--fs-sm); color: var(--muted);
  }
  .field label {
    font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em;
    font-weight: 600;
  }
  .field select,
  .field input[type="search"] {
    appearance: none;
    border: 1px solid var(--rule); border-radius: 4px;
    background: var(--bg);
    color: var(--ink);
    padding: 6px 10px;
    font-family: inherit; font-size: var(--fs-sm);
    min-width: 160px;
  }
  .field select { padding-right: 28px; cursor: pointer; }
  .field input[type="search"] { min-width: 240px; }
  .field input[type="search"]::placeholder { color: var(--faint); }
  .field-spacer { flex: 1; }

  /* ===== INCIDENTS MASTER-DETAIL ===== */
  .md {
    display: grid;
    grid-template-columns: 380px 1fr;
    border: 1px solid var(--rule);
    border-radius: 0 0 6px 6px;
    background: var(--bg);
    overflow: hidden;
  }
  .md .list { border-right: 1px solid var(--rule); background: var(--bg-soft); }
  .md .list-head {
    padding: 14px 22px;
    border-bottom: 1px solid var(--rule);
    display: flex; justify-content: space-between; align-items: baseline;
  }
  .md .list-head .t { font-size: var(--fs-sm); font-weight: 700; color: var(--ink); text-transform: uppercase; letter-spacing: 0.06em; }
  .md .list-head .c { font-size: var(--fs-sm); color: var(--muted); font-variant-numeric: tabular-nums; }
  .md .list-scroll { max-height: 640px; overflow-y: auto; }
  .md .list-scroll::-webkit-scrollbar { width: 8px; }
  .md .list-scroll::-webkit-scrollbar-thumb { background: var(--rule); border-radius: 4px; }
  .md .list-scroll::-webkit-scrollbar-thumb:hover { background: var(--faint); }
  .md .list-empty {
    padding: 40px 22px;
    text-align: center;
    font-size: var(--fs-sm); color: var(--faint);
  }

  .inc-item {
    padding: 16px 22px 14px;
    border-bottom: 1px solid var(--rule-2);
    cursor: pointer;
    background: transparent;
    transition: background 0.12s ease;
    position: relative;
    border-left: 3px solid transparent;
  }
  .inc-item.type-java_crash    { border-left-color: var(--red); }
  .inc-item.type-native_crash  { border-left-color: var(--red-deep); }
  .inc-item.type-anr           { border-left-color: var(--accent-2); }
  .inc-item.type-process_death { border-left-color: var(--gray); }
  .inc-item:hover { background: rgba(29,78,216,0.04); }
  .inc-item.active { background: var(--bg); }
  .inc-item.flash {
    animation: flash 1.4s ease;
  }
  @keyframes flash {
    0%   { background: rgba(245,158,11,0.35); }
    100% { background: var(--bg); }
  }
  .inc-item .row1 { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
  .inc-item .row1 .id {
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-sm); font-weight: 600; color: var(--ink-3);
  }
  .inc-item .row2 {
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-xs); color: var(--muted);
    margin-bottom: 8px;
  }
  .inc-item .row3 {
    font-size: var(--fs-sm); color: var(--ink-2);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .inc-item .row3 .mono { font-family: "SF Mono", Menlo, monospace; }

  .detail { padding: 30px 34px; min-height: 520px; }
  .detail[hidden] { display: none; }
  .detail .hd {
    display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--rule);
    margin-bottom: 26px;
  }
  .detail .hd h3 {
    margin: 0;
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-lg); font-weight: 700;
    color: var(--ink);
  }
  .detail .hd .meta {
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-sm); color: var(--muted);
  }
  .detail .hd .spacer { flex: 1; }

  .stat-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 24px;
    margin-bottom: 30px;
  }
  .stat .k {
    font-size: var(--fs-xs); color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.06em;
    margin-bottom: 8px; font-weight: 600;
  }
  .stat .v {
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-mid); font-weight: 700;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    word-break: break-all; line-height: 1.3;
  }
  .stat .v.red { color: var(--red); }
  .stat .v.orange { color: var(--accent-2); }

  .summary-box {
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-sm);
    background: var(--bg-soft);
    border: 1px solid var(--rule-2);
    border-left: 3px solid var(--red);
    border-radius: 4px;
    padding: 14px 16px;
    color: var(--ink);
    margin-bottom: 24px;
    line-height: 1.6;
    word-break: break-word;
  }
  .summary-box.native_crash  { border-left-color: var(--red-deep); }
  .summary-box.anr           { border-left-color: var(--accent-2); }
  .summary-box.process_death { border-left-color: var(--gray); }

  .frames {
    background: #0f172a;
    color: #e2e8f0;
    border-radius: 4px;
    padding: 14px 18px;
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-sm);
    line-height: 1.65;
    overflow-x: auto;
    margin-bottom: 18px;
  }
  .frames .f-line {
    display: flex; gap: 12px;
    white-space: nowrap;
    cursor: pointer;
    padding: 1px 0;
    border-radius: 2px;
    transition: background 0.1s ease;
  }
  .frames .f-line:hover { background: rgba(255,255,255,0.06); }
  .frames .f-line.copied { background: rgba(21,128,61,0.25); }
  .frames .f-line .idx { color: #64748b; min-width: 28px; }
  .frames .f-line.biz { background: rgba(245,158,11,0.14); color: #fde68a; }
  .frames .f-line.biz:hover { background: rgba(245,158,11,0.22); }
  .frames .f-empty { color: #94a3b8; font-style: italic; }

  .file-line {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px;
    background: var(--bg-soft);
    border: 1px solid var(--rule-2);
    border-radius: 4px;
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-sm);
    color: var(--ink);
    margin-bottom: 8px;
    word-break: break-all;
  }
  .file-line .k {
    color: var(--muted);
    font-size: var(--fs-xs);
    text-transform: uppercase; letter-spacing: 0.06em;
    font-weight: 700;
    flex-shrink: 0;
  }
  .file-line a { color: var(--accent); text-decoration: none; }
  .file-line a:hover { text-decoration: underline; }
  .file-line.warn {
    border-left: 3px solid var(--accent-2);
    background: rgba(194,65,12,0.06);
  }
  .file-line.warn .k { color: var(--accent-2); }

  /* ===== ACCORDIONS ===== */
  details.acc {
    border: 1px solid var(--rule);
    border-radius: 6px;
    margin-bottom: 14px;
    background: var(--bg);
    overflow: hidden;
  }
  details.acc > summary {
    list-style: none; cursor: pointer;
    padding: 18px 26px;
    display: flex; justify-content: space-between; align-items: center;
    font-size: var(--fs-md); font-weight: 700; color: var(--ink);
  }
  details.acc > summary::-webkit-details-marker { display: none; }
  details.acc > summary .s-right {
    display: flex; align-items: center; gap: 14px;
    font-size: var(--fs-sm); color: var(--muted); font-weight: 500;
  }
  details.acc > summary::after {
    content: ""; width: 9px; height: 9px;
    border-right: 1.5px solid var(--muted);
    border-bottom: 1.5px solid var(--muted);
    transform: rotate(45deg);
    transition: transform 0.15s ease;
    margin-left: 14px;
  }
  details.acc[open] > summary::after { transform: rotate(-135deg); }
  details.acc > .body { padding: 10px 26px 26px; border-top: 1px solid var(--rule); }
  .cfg-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 38px; padding-top: 18px; }
  .cfg-group .g-title {
    font-size: var(--fs-xs); color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em;
    margin-bottom: 14px; padding-bottom: 10px;
    border-bottom: 1px solid var(--rule);
    font-weight: 700;
  }
  .cfg-group .kv {
    display: flex; justify-content: space-between;
    padding: 8px 0;
    font-size: var(--fs-sm);
    gap: 10px;
  }
  .cfg-group .kv .k { color: var(--ink-3); }
  .cfg-group .kv .v {
    font-family: "SF Mono", Menlo, monospace;
    color: var(--ink); font-weight: 500;
    text-align: right; word-break: break-all;
  }
  .cfg-group .kv .v.bool-y { color: var(--green); }
  .cfg-group .kv .v.bool-n { color: var(--muted); }

  .files-tree {
    font-family: "SF Mono", Menlo, monospace;
    font-size: var(--fs-sm);
    line-height: 2;
    padding-top: 12px;
  }
  .files-tree .fr {
    display: grid; grid-template-columns: 280px 1fr 40px;
    gap: 24px;
    padding: 6px 10px;
    margin: 0 -10px;
    border-bottom: 1px dashed var(--rule-2);
    align-items: center;
    cursor: pointer;
    border-radius: 4px;
    transition: background 0.12s ease;
  }
  .files-tree .fr:last-child { border-bottom: 0; }
  .files-tree .fr:hover { background: var(--bg-soft); }
  .files-tree .fr:hover .open-icon { color: var(--accent); }
  .files-tree .fr.disabled { cursor: default; opacity: 0.55; }
  .files-tree .fr.disabled:hover { background: transparent; }
  .files-tree .fr.disabled:hover .open-icon { color: var(--faint); }
  .files-tree .fr .name { color: var(--ink); }
  .files-tree .fr .name.dir { color: var(--accent); }
  .files-tree .fr .desc { color: var(--muted); font-family: inherit; }
  .files-tree .fr .open-icon {
    font-family: -apple-system, sans-serif;
    color: var(--faint);
    font-size: 14px;
    text-align: right;
    transition: color 0.12s ease;
  }

  /* ===== language toggle ===== */
  .lang-toggle {
    position: fixed;
    top: 20px;
    right: 24px;
    z-index: 50;
    display: inline-flex;
    border: 1px solid var(--rule);
    border-radius: 999px;
    background: var(--bg);
    padding: 3px;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04);
  }
  .lang-toggle button {
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--muted);
    font-family: inherit;
    font-size: var(--fs-sm);
    font-weight: 600;
    padding: 5px 14px;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.12s ease, color 0.12s ease;
  }
  .lang-toggle button:hover { color: var(--ink); }
  .lang-toggle button.active {
    background: var(--ink);
    color: #fff;
  }

  .foot {
    margin-top: 80px; padding-top: 26px;
    border-top: 1px solid var(--rule);
    font-size: var(--fs-sm); color: var(--muted);
    display: flex; justify-content: space-between; gap: 16px;
    flex-wrap: wrap;
  }

  .empty-state {
    padding: 80px 40px;
    text-align: center;
    color: var(--muted);
    font-size: var(--fs-md);
  }
  .empty-state .emoji { font-size: 40px; display: block; margin-bottom: 14px; }

  kbd {
    font-family: "SF Mono", Menlo, monospace;
    font-size: 0.85em;
    background: var(--bg-soft);
    border: 1px solid var(--rule);
    border-bottom-width: 2px;
    border-radius: 3px;
    padding: 1px 6px;
    color: var(--ink-3);
  }
"""


_BODY_SKELETON = r"""
<nav class="lang-toggle" role="tablist" aria-label="Language">
  <button type="button" data-lang-btn="zh">中文</button>
  <button type="button" data-lang-btn="en">EN</button>
</nav>

<main class="page">

  <!-- HEADER -->
  <header class="doc-head">
    <div class="title-block">
      <div class="eyebrow">
        <span class="zh">稳定性自动化测试报告</span>
        <span class="en">Stability report</span>
      </div>
      <h1>
        <span id="pkg-name">—</span>
        <button class="copy-btn" id="copy-pkg" type="button" aria-label="copy package name" title="copy package name">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="4" y="4" width="8" height="9" rx="1.5"/>
            <path d="M2.5 11V3.5A1.5 1.5 0 0 1 4 2h6"/>
          </svg>
        </button>
      </h1>
      <div class="meta" id="run-meta"></div>
    </div>
    <div class="verdict" id="verdict-pill"></div>
  </header>

  <!-- HERO -->
  <section style="margin-top: 24px;">
    <div class="hero">
      <div class="verdict-bar" id="verdict-bar">
        <svg class="vicon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="7" x2="12" y2="13"/>
          <circle cx="12" cy="16.5" r="1" fill="currentColor"/>
        </svg>
        <div class="vtxt">
          <div class="vtitle" id="verdict-title"></div>
          <div class="vsub" id="verdict-sub"></div>
        </div>
      </div>

      <div class="ev-cards">
        <div class="ev-card java" data-card-type="java_crash">
          <div class="e-head"><span class="dot"></span><span>Java crash</span></div>
          <div class="e-count" id="cnt-java_crash">0</div>
          <div class="e-sub" id="sub-java_crash"></div>
        </div>
        <div class="ev-card native" data-card-type="native_crash">
          <div class="e-head"><span class="dot"></span><span>Native crash</span></div>
          <div class="e-count" id="cnt-native_crash">0</div>
          <div class="e-sub" id="sub-native_crash"></div>
        </div>
        <div class="ev-card anr" data-card-type="anr">
          <div class="e-head"><span class="dot"></span><span>ANR</span></div>
          <div class="e-count" id="cnt-anr">0</div>
          <div class="e-sub" id="sub-anr"></div>
        </div>
        <div class="ev-card death" data-card-type="process_death">
          <div class="e-head"><span class="dot"></span><span>Process death</span></div>
          <div class="e-count" id="cnt-process_death">0</div>
          <div class="e-sub" id="sub-process_death"></div>
        </div>
      </div>

      <div class="derived" id="derived-strip"></div>
    </div>
  </section>

  <!-- TIMELINE -->
  <section>
    <div class="sec-head">
      <h2><span class="zh">事件时间轴</span><span class="en">Event timeline</span></h2>
      <span class="num-tag">01</span>
      <span class="desc">
        <span class="zh">上半部 4 行为事件、下半部 3 行为生命周期 · 点击标记跳转</span>
        <span class="en">top 4 lanes = events, bottom 3 = lifecycle · click any marker to jump</span>
      </span>
    </div>
    <div class="rail">
      <div class="rail-head">
        <span class="span" id="rail-span">—</span>
        <div class="legend">
          <span class="item"><span class="sw x-red"></span>Java crash</span>
          <span class="item"><span class="sw x-rd"></span>Native crash</span>
          <span class="item"><span class="sw x-or"></span>ANR</span>
          <span class="item"><span class="sw x-gr"></span>Process death</span>
          <span class="item"><span class="sw d-g"></span><span class="zh">进程新增</span><span class="en">new</span></span>
          <span class="item"><span class="sw d-or"></span><span class="zh">进程重启</span><span class="en">restart</span></span>
          <span class="item"><span class="sw d-gr"></span><span class="zh">进程消失</span><span class="en">gone</span></span>
          <span class="item"><span class="sw l-b"></span><span class="zh">书签</span><span class="en">bookmark</span></span>
        </div>
      </div>
      <div id="timeline-plot"></div>
    </div>
  </section>

  <!-- PROCESS TABLE -->
  <section>
    <div class="sec-head">
      <h2><span class="zh">进程稳定性总表</span><span class="en">Process stability</span></h2>
      <span class="num-tag">02</span>
      <span class="desc">
        <span class="zh">点击事件计数可在下方筛选该进程的该类事件</span>
        <span class="en">click a count to filter the incidents list below</span>
      </span>
    </div>
    <div class="table-wrap">
      <div class="table-scroll">
        <table class="tbl">
          <thead>
            <tr>
              <th><span class="zh">进程</span><span class="en">Process</span></th>
              <th><span class="zh">首次发现</span><span class="en">First seen</span></th>
              <th><span class="zh">最后发现</span><span class="en">Last seen</span></th>
              <th class="r"><span class="zh">在线率</span><span class="en">Uptime</span></th>
              <th class="r"><span class="zh">重启</span><span class="en">Restart</span></th>
              <th class="r">Java</th>
              <th class="r">Native</th>
              <th class="r">ANR</th>
              <th class="r"><span class="zh">退出</span><span class="en">P.death</span></th>
              <th class="r"><span class="zh">取样失败</span><span class="en">Sample fail</span></th>
            </tr>
          </thead>
          <tbody id="proc-tbody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- INCIDENTS -->
  <section>
    <div class="sec-head">
      <h2><span class="zh">Incidents — 稳定性事件</span><span class="en">Incidents</span></h2>
      <span class="num-tag">03</span>
      <span class="desc">
        <span id="inc-desc-count"></span>
        <span class="zh"> · 选中条目查看堆栈与现场快照 · </span>
        <span class="en"> · select a row to view stack &amp; evidence · </span>
        <kbd>/</kbd>
        <span class="zh"> 聚焦搜索</span>
        <span class="en"> to search</span>
      </span>
    </div>

    <div class="filter-bar">
      <div class="chip-row" id="type-chips">
        <button class="filter-chip" type="button" data-type="all" data-active="true">
          <span class="zh">全部</span><span class="en">All</span>&nbsp;<span class="cnt" data-cnt-for="all">(0)</span>
        </button>
        <button class="filter-chip" type="button" data-type="java_crash">
          Java crash&nbsp;<span class="cnt" data-cnt-for="java_crash">(0)</span>
        </button>
        <button class="filter-chip" type="button" data-type="native_crash">
          Native crash&nbsp;<span class="cnt" data-cnt-for="native_crash">(0)</span>
        </button>
        <button class="filter-chip" type="button" data-type="anr">
          ANR&nbsp;<span class="cnt" data-cnt-for="anr">(0)</span>
        </button>
        <button class="filter-chip" type="button" data-type="process_death">
          Process death&nbsp;<span class="cnt" data-cnt-for="process_death">(0)</span>
        </button>
      </div>
      <div class="filter-row">
        <div class="field">
          <label><span class="zh">进程</span><span class="en">Process</span></label>
          <select id="proc-filter"></select>
        </div>
        <div class="field">
          <label><span class="zh">严重度</span><span class="en">Severity</span></label>
          <select id="sev-filter">
            <option value="all">All</option>
            <option value="fatal">fatal</option>
            <option value="error">error</option>
            <option value="warning">warning</option>
          </select>
        </div>
        <div class="field">
          <input id="search-inp" type="search" placeholder="🔍 异常类 / 摘要 / 进程名 / Search exception, summary, process…"/>
        </div>
        <div class="field-spacer"></div>
        <div class="field">
          <label><span class="zh">排序</span><span class="en">Sort</span></label>
          <select id="sort-sel">
            <option value="time_desc"><span class="zh">时间倒序</span><span class="en">time desc</span></option>
            <option value="time_asc"><span class="zh">时间正序</span><span class="en">time asc</span></option>
            <option value="sev"><span class="zh">严重度</span><span class="en">severity</span></option>
          </select>
        </div>
      </div>
    </div>

    <div class="md">
      <aside class="list">
        <div class="list-head">
          <span class="t"><span class="zh">事件列表</span><span class="en">All incidents</span></span>
          <span class="c" id="list-count">0 / 0</span>
        </div>
        <div class="list-scroll" id="inc-list"></div>
      </aside>
      <div class="detail" id="inc-detail"></div>
    </div>
  </section>

  <!-- APPENDIX -->
  <section>
    <div class="sec-head">
      <h2><span class="zh">附加信息</span><span class="en">Appendix</span></h2>
      <span class="num-tag">04</span>
      <span class="desc">
        <span class="zh">书签 · 配置 · 数据文件</span>
        <span class="en">bookmarks · effective config · data files</span>
      </span>
    </div>

    <details class="acc">
      <summary>
        <span>🔖 <span class="zh">书签 Bookmarks</span><span class="en">Bookmarks</span></span>
        <span class="s-right" id="bookmark-count"></span>
      </summary>
      <div class="body" id="bookmark-body"></div>
    </details>

    <details class="acc">
      <summary>
        <span>⚙ <span class="zh">跑测配置 Effective Config</span><span class="en">Effective Config</span></span>
        <span class="s-right" id="cfg-count"></span>
      </summary>
      <div class="body">
        <div class="cfg-grid" id="cfg-grid"></div>
      </div>
    </details>

    <details class="acc">
      <summary>
        <span>📁 <span class="zh">数据文件索引 Files</span><span class="en">Data files</span></span>
        <span class="s-right" id="files-count"></span>
      </summary>
      <div class="body">
        <div class="files-tree" id="files-tree"></div>
      </div>
    </details>
  </section>

  <footer class="foot">
    <div>
      <span class="zh">报告由 stability_auto_test v1.0 生成 · schema v1.0</span>
      <span class="en">Generated by stability_auto_test v1.0 · schema v1.0</span>
    </div>
    <div class="mono" id="foot-time"></div>
  </footer>
</main>
"""


_JS = r"""
(function () {
  'use strict';

  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    var txt = el.textContent || '';
    if (!txt.trim()) return null;
    try { return JSON.parse(txt); } catch (e) { return null; }
  }

  var report    = readJson('report-data')    || { run: {}, processes: [], bookmarks: [] };
  var incidents = readJson('incidents-data') || [];
  var lifecycle = readJson('lifecycle-data') || [];
  var dataFiles = readJson('files-data')     || { events: [], lifecycle: [], logcat: [] };
  var configEff = readJson('config-data')    || {};

  var run       = report.run || {};
  var processes = report.processes || [];
  var bookmarks = report.bookmarks || [];

  var TYPE_LABELS = {
    java_crash:    { zh: 'Java crash',    en: 'Java crash',    color: '#b91c1c' },
    native_crash:  { zh: 'Native crash',  en: 'Native crash',  color: '#7f1d1d' },
    anr:           { zh: 'ANR',           en: 'ANR',           color: '#c2410c' },
    process_death: { zh: 'Process death', en: 'Process death', color: '#4b5563' },
  };
  var TYPE_CLS = {
    java_crash: 'red', native_crash: 'red-deep', anr: 'orange', process_death: 'gray',
  };
  var SEV_LABELS = {
    fatal:   { zh: 'fatal',   en: 'fatal',   cls: 'fatal' },
    error:   { zh: 'error',   en: 'error',   cls: 'error' },
    warning: { zh: 'warning', en: 'warning', cls: 'warning' },
  };

  function getLang() {
    return document.documentElement.getAttribute('data-lang') || 'zh';
  }
  function tr(zh, en) { return getLang() === 'zh' ? zh : en; }
  function fmtTime(ts) {
    if (!ts) return '—';
    var m = String(ts).match(/(\d\d:\d\d:\d\d(?:\.\d+)?)/);
    return m ? m[1] : ts;
  }
  function fmtTimeShort(ts) {
    if (!ts) return '—';
    var m = String(ts).match(/(\d\d:\d\d:\d\d)/);
    return m ? m[1] : ts;
  }
  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
  }
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // counts
  var counts = { java_crash: 0, native_crash: 0, anr: 0, process_death: 0 };
  incidents.forEach(function (i) {
    if (counts[i.type] != null) counts[i.type] += 1;
  });
  var totalIncidents = incidents.length;
  var totalCrashes   = counts.java_crash + counts.native_crash;
  var totalAnr       = counts.anr;
  var totalDeath     = counts.process_death;

  // ============ HEADER + VERDICT ============
  function renderHeader() {
    document.getElementById('pkg-name').textContent = run.package || '—';
    document.title = 'Stability report — ' + (run.package || '');

    var device = run.device || {};
    var dev = device.serial ? ('📱 ' + device.serial) : '';
    var androidVersion = device.android_version ? ('Android ' + device.android_version + (device.sdk_int ? (' (SDK ' + device.sdk_int + ')') : '')) : '';
    var cores = device.cpu_cores != null
      ? ('<span class="zh">' + device.cpu_cores + ' 核</span><span class="en">' + device.cpu_cores + ' cores</span>')
      : '';
    var started = run.started_at || '';
    var ended   = run.ended_at   || '';
    var dur     = fmtDuration(run.duration_sec);
    var timeRange = (started && ended) ? (started + ' → ' + fmtTimeShort(ended)) : (started || ended || '—');

    var bits = [];
    if (dev)            bits.push('<span>' + escapeHtml(dev) + '</span>');
    if (androidVersion) bits.push('<span>' + escapeHtml(androidVersion) + '</span>');
    if (cores)          bits.push('<span>' + cores + '</span>');
    if (timeRange)      bits.push('<span>' + escapeHtml(timeRange) + '</span>');
    if (dur)            bits.push('<span>' + escapeHtml(dur) + '</span>');
    document.getElementById('run-meta').innerHTML = bits.join('');

    // verdict pill (top-right)
    var exitOk    = (run.exit_code === 0);
    var pillCls   = exitOk ? '' : 'red';
    var pillIcon  = exitOk ? '✓' : '⚠';
    var reason    = run.exit_reason || '—';
    var pillText  = exitOk
      ? '<span class="zh">正常结束 · ' + escapeHtml(reason) + '</span><span class="en">' + escapeHtml(reason) + '</span>'
      : '<span class="zh">异常退出 · ' + escapeHtml(reason) + '</span><span class="en">' + escapeHtml(reason) + '</span>';
    var micro = exitOk && totalIncidents === 0
      ? '<span class="zh">exit_code = 0 · 未检测到稳定性事件</span><span class="en">exit_code = 0 · no stability events</span>'
      : '<span class="zh">exit_code = ' + run.exit_code + ' · 检测到 ' + totalIncidents + ' 个稳定性事件</span><span class="en">exit_code = ' + run.exit_code + ' · ' + totalIncidents + ' stability events detected</span>';
    document.getElementById('verdict-pill').innerHTML =
      '<span class="pill ' + pillCls + '">' + pillIcon + ' ' + pillText + '</span>' +
      '<span class="micro">' + micro + '</span>';

    // foot time
    var ft = document.getElementById('foot-time');
    if (ft) ft.textContent = run.ended_at || run.started_at || '';
  }

  // ============ HERO VERDICT BAR ============
  function renderVerdictBar() {
    var bar      = document.getElementById('verdict-bar');
    var titleEl  = document.getElementById('verdict-title');
    var subEl    = document.getElementById('verdict-sub');
    var icon     = bar.querySelector('.vicon');

    var cls, titleZh, titleEn, subZh, subEn, iconSvg;
    if (totalIncidents === 0) {
      cls = 'green';
      titleZh = '未检测到稳定性事件 · 整体健康';
      titleEn = 'No stability events detected · healthy run';
      subZh = '跑测窗口内无 Java/Native crash、ANR 或异常退出';
      subEn = 'No Java/Native crash, ANR or unexpected death within the window';
      iconSvg = '<circle cx="12" cy="12" r="10"/><polyline points="8,12.5 11,15.5 16,9.5"/>';
    } else if (totalCrashes > 0) {
      cls = 'red';
      var parts = [];
      if (totalCrashes > 0) parts.push(tr(totalCrashes + ' 次崩溃', totalCrashes + ' crashes'));
      if (totalAnr > 0)     parts.push(tr(totalAnr + ' 次 ANR', totalAnr + ' ANRs'));
      titleZh = '测试期间发生 ' + parts.join('、').replace(/次/g, '次') + (parts.length === 0 ? '' : '');
      titleZh = (totalCrashes > 0 ? totalCrashes + ' 次崩溃' : '') +
                (totalCrashes > 0 && totalAnr > 0 ? '，同时踩中 ' : '') +
                (totalAnr > 0 ? totalAnr + ' 次 ANR' : '');
      titleZh = '测试期间发生 ' + titleZh;
      titleEn = (totalCrashes > 0 ? totalCrashes + ' crashes' : '') +
                (totalCrashes > 0 && totalAnr > 0 ? ' and ' : '') +
                (totalAnr > 0 ? totalAnr + ' ANRs' : '') + ' detected during the run';
      subZh = '⚠ "exit_code = 0" 仅表示跑测正常结束 · 测试结果并不健康，查看下方 Incidents 区定位首条事件';
      subEn = '⚠ "exit_code = 0" only means the run finished — the result is unhealthy. See Incidents below to locate the first event.';
      iconSvg = '<circle cx="12" cy="12" r="10"/><line x1="12" y1="7" x2="12" y2="13"/><circle cx="12" cy="16.5" r="1" fill="currentColor"/>';
    } else {
      cls = 'orange';
      titleZh = '检测到 ' + totalIncidents + ' 个事件（未含崩溃）';
      titleEn = totalIncidents + ' events detected (no crashes)';
      subZh  = '⚠ 包含 ANR / 进程异常退出，查看下方 Incidents 列表';
      subEn  = '⚠ Includes ANRs / process deaths — see Incidents list below';
      iconSvg = '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><circle cx="12" cy="17" r="1" fill="currentColor"/>';
    }

    bar.classList.remove('red', 'orange', 'green');
    bar.classList.add(cls);
    titleEl.innerHTML = '<span class="zh">' + escapeHtml(titleZh) + '</span><span class="en">' + escapeHtml(titleEn) + '</span>';
    subEl.innerHTML   = '<span class="zh">' + subZh + '</span><span class="en">' + subEn + '</span>';
    icon.innerHTML    = iconSvg;
  }

  // ============ HERO CARDS ============
  function renderHeroCards() {
    ['java_crash', 'native_crash', 'anr', 'process_death'].forEach(function (type) {
      var n = counts[type];
      var cntEl = document.getElementById('cnt-' + type);
      var subEl = document.getElementById('sub-' + type);
      cntEl.textContent = n;
      var card = cntEl.closest('.ev-card');
      if (card) card.classList.toggle('zero', n === 0);

      var procSet = {};
      incidents.forEach(function (i) { if (i.type === type) procSet[i.process] = true; });
      var procCount = Object.keys(procSet).length;

      var sub = '';
      if (n === 0) {
        sub = '<span class="zh">未捕获</span><span class="en">none captured</span>';
      } else if (type === 'java_crash') {
        var classes = {};
        incidents.forEach(function (i) {
          if (i.type === type && i.evidence && i.evidence.exception_class) classes[i.evidence.exception_class] = true;
        });
        var ks = Object.keys(classes);
        if (ks.length > 0) {
          var firstShort = ks[0].split('.').pop();
          sub = '<span class="zh">' + escapeHtml(firstShort) + (ks.length > 1 ? ' 等 ' + ks.length + ' 类' : '') + '</span>' +
                '<span class="en">' + escapeHtml(firstShort) + (ks.length > 1 ? ' +' + (ks.length - 1) : '') + '</span>';
        } else {
          sub = '<span class="zh">涉及 ' + procCount + ' 个进程</span><span class="en">affects ' + procCount + ' process' + (procCount === 1 ? '' : 'es') + '</span>';
        }
      } else if (type === 'native_crash') {
        var sigs = {};
        incidents.forEach(function (i) {
          if (i.type === type && i.evidence && i.evidence.signal) sigs[i.evidence.signal] = true;
        });
        var sigList = Object.keys(sigs);
        sub = sigList.length > 0
          ? '<span class="zh">' + escapeHtml(sigList.join(' · ')) + '</span><span class="en">' + escapeHtml(sigList.join(' · ')) + '</span>'
          : '<span class="zh">涉及 ' + procCount + ' 个进程</span><span class="en">affects ' + procCount + ' process' + (procCount === 1 ? '' : 'es') + '</span>';
      } else if (type === 'anr') {
        var reasons = {};
        incidents.forEach(function (i) {
          if (i.type === type && i.evidence && i.evidence.reason) {
            var head = String(i.evidence.reason).split(/[—:·]/)[0].trim().slice(0, 18);
            if (head) reasons[head] = true;
          }
        });
        var rk = Object.keys(reasons);
        sub = rk.length > 0
          ? '<span class="zh">' + escapeHtml(rk.slice(0, 3).join(' · ')) + '</span><span class="en">' + escapeHtml(rk.slice(0, 3).join(' · ')) + '</span>'
          : '<span class="zh">涉及 ' + procCount + ' 个进程</span><span class="en">affects ' + procCount + ' process' + (procCount === 1 ? '' : 'es') + '</span>';
      } else {
        sub = '<span class="zh">涉及 ' + procCount + ' 个进程</span><span class="en">across ' + procCount + ' process' + (procCount === 1 ? '' : 'es') + '</span>';
      }
      subEl.innerHTML = sub;
    });
  }

  // ============ DERIVED STRIP ============
  function renderDerivedStrip() {
    var strip = document.getElementById('derived-strip');
    var nProc = processes.length;
    var avgUp = nProc > 0
      ? (processes.reduce(function (s, p) { return s + (p.uptime_ratio || 0); }, 0) / nProc) * 100
      : 0;
    var totalLogFail = 0, totalDropFail = 0;
    processes.forEach(function (p) {
      var sf = p.sample_failures || {};
      totalLogFail  += sf.logcat  || 0;
      totalDropFail += sf.dropbox || 0;
    });
    var buffers = (configEff.logcat_buffers || ['main', 'system', 'events', 'crash']).join(' · ');
    var totalFail = totalLogFail + totalDropFail;
    var failHtml = totalFail > 0
      ? '<span class="d"><span class="lbl"><span class="zh">取样失败</span><span class="en">sample failures</span></span>' +
        '<span class="val warn">logcat ' + totalLogFail + ' · dropbox ' + totalDropFail + '</span></span>'
      : '';
    strip.innerHTML =
      '<span class="d"><span class="lbl"><span class="zh">平均在线率</span><span class="en">avg uptime</span></span>' +
      '<span class="val">' + (nProc > 0 ? avgUp.toFixed(1) + ' %' : '—') + '</span></span>' +
      '<span class="d"><span class="lbl">logcat buffers</span><span class="val">' + escapeHtml(buffers) + '</span></span>' +
      failHtml;
  }

  // ============ TYPE CHIP COUNTS ============
  function renderTypeChipCounts() {
    document.querySelector('[data-cnt-for="all"]').textContent = '(' + totalIncidents + ')';
    ['java_crash', 'native_crash', 'anr', 'process_death'].forEach(function (t) {
      var el = document.querySelector('[data-cnt-for="' + t + '"]');
      if (el) el.textContent = '(' + counts[t] + ')';
    });
    document.getElementById('inc-desc-count').innerHTML =
      '<span class="zh">' + totalIncidents + ' 条事件</span>' +
      '<span class="en">' + totalIncidents + ' incident' + (totalIncidents === 1 ? '' : 's') + '</span>';
    document.getElementById('list-count').textContent = totalIncidents + ' / ' + totalIncidents;
  }

  // ============ PROCESS DROPDOWN ============
  function renderProcessSelect() {
    var sel = document.getElementById('proc-filter');
    var opts = ['<option value="all">' + tr('全部进程', 'All processes') + '</option>'];
    var seen = {};
    processes.forEach(function (p) { if (!seen[p.name]) { seen[p.name] = true; opts.push('<option value="' + escapeAttr(p.name) + '">' + escapeHtml(p.name) + '</option>'); } });
    // also include any incident.process not in processes
    incidents.forEach(function (i) { if (i.process && !seen[i.process]) { seen[i.process] = true; opts.push('<option value="' + escapeAttr(i.process) + '">' + escapeHtml(i.process) + '</option>'); } });
    sel.innerHTML = opts.join('');
  }

  // ============ PROCESS TABLE ============
  function renderProcessTable() {
    var tbody = document.getElementById('proc-tbody');
    if (processes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" class="v-muted" style="text-align:center;padding:24px;">' +
        tr('未观察到任何进程', 'No processes observed') + '</td></tr>';
      return;
    }
    var rows = processes.map(function (p) {
      var up = p.uptime_ratio || 0;
      var upPct = (up * 100).toFixed(1);
      var upWarn = up < 0.90;
      var ev = p.events || {};
      var sf = p.sample_failures || {};
      var sampleFail = (sf.logcat || 0) + (sf.dropbox || 0);
      function cnt(type, n, cls) {
        if (n === 0) return '<span class="v-muted">0</span>';
        return '<span class="chip ' + cls + ' cnt-link" data-proc="' + escapeAttr(p.name) + '" data-type="' + type + '">' + n + '</span>';
      }
      return '<tr>' +
        '<td class="mono">' + escapeHtml(p.name) + '</td>' +
        '<td class="mono">' + fmtTimeShort(p.first_seen_at) + '</td>' +
        '<td class="mono">' + fmtTimeShort(p.last_seen_at)  + '</td>' +
        '<td class="r"><span class="uptime-cell">' +
          '<span class="uptime-bar"><span class="fill' + (upWarn ? ' warn' : '') + '" style="width:' + upPct + '%"></span></span>' +
          '<span class="uptime-val' + (upWarn ? ' warn' : '') + '">' + upPct + ' %</span>' +
        '</span></td>' +
        '<td class="r">' + (p.restart_count === 0 ? '<span class="v-muted">—</span>' : '<span class="chip orange">' + p.restart_count + '</span>') + '</td>' +
        '<td class="r">' + cnt('java_crash',    ev.java_crash    || 0, 'red')      + '</td>' +
        '<td class="r">' + cnt('native_crash',  ev.native_crash  || 0, 'red-deep') + '</td>' +
        '<td class="r">' + cnt('anr',           ev.anr           || 0, 'orange')   + '</td>' +
        '<td class="r">' + cnt('process_death', ev.process_death || 0, 'gray')     + '</td>' +
        '<td class="r" title="logcat=' + (sf.logcat || 0) + ' · dropbox=' + (sf.dropbox || 0) + '">' +
          (sampleFail === 0 ? '<span class="v-muted">—</span>' : '<span class="chip orange">' + sampleFail + '</span>') +
        '</td>' +
      '</tr>';
    }).join('');
    tbody.innerHTML = rows;

    tbody.querySelectorAll('.cnt-link').forEach(function (chip) {
      chip.addEventListener('click', function () {
        setTypeFilter(chip.getAttribute('data-type'));
        document.getElementById('proc-filter').value = chip.getAttribute('data-proc');
        document.getElementById('sev-filter').value = 'all';
        document.getElementById('search-inp').value = '';
        filterState.process = chip.getAttribute('data-proc');
        filterState.severity = 'all';
        filterState.search = '';
        applyFilters();
        document.querySelector('#type-chips').scrollIntoView({ block: 'start', behavior: 'smooth' });
      });
    });
  }

  // ============ TIMELINE (Plotly) ============
  function renderTimeline() {
    var span = document.getElementById('rail-span');
    if (run.started_at && run.ended_at) {
      span.textContent = fmtTimeShort(run.started_at) + '  —  ' + fmtDuration(run.duration_sec) + '  —  ' + fmtTimeShort(run.ended_at);
    } else {
      span.textContent = '—';
    }

    if (!window.Plotly) return;

    var yLabels = {
      java_crash:    'Java crash',
      native_crash:  'Native crash',
      anr:           'ANR',
      process_death: 'Process death',
      lc_new:        tr('进程新增 · new',     'new'),
      lc_restart:    tr('进程重启 · restart', 'restart'),
      lc_gone:       tr('进程消失 · gone',    'gone'),
    };
    var YORDER_BOTTOM_TO_TOP = ['lc_gone', 'lc_restart', 'lc_new', 'process_death', 'anr', 'native_crash', 'java_crash'];
    var LC_COLORS = { new: '#15803d', restart: '#c2410c', gone: '#4b5563' };

    function toIso(ts) { return String(ts).replace(' ', 'T'); }

    var t0 = run.started_at ? toIso(run.started_at) : null;
    var t1 = run.ended_at   ? toIso(run.ended_at)   : null;

    // ── x-range padding: extend left/right so edge markers aren't clipped ──
    // Append 'Z' to force UTC parsing — without it, browsers in non-UTC timezones
    // interpret the bare ISO string as local time, shifting the computed axis range
    // by the UTC offset and pushing all markers outside the visible range.
    // (Plotly always treats bare timestamp strings as UTC.)
    var t0ms = t0 ? new Date(t0 + 'Z').getTime() : 0;
    var t1ms = t1 ? new Date(t1 + 'Z').getTime() : 0;
    var durMs = t1ms > t0ms ? t1ms - t0ms : 3600000;
    var estPlotWidthPx = 994; // page 1280 – page-padding 112 – plot-margins 174
    var msPerPx = durMs / estPlotWidthPx;
    var padMs = Math.max(30000, durMs * 0.01);
    var t0Range = t0 ? new Date(t0ms - padMs).toISOString() : null;
    var t1Range = t1 ? new Date(t1ms + padMs).toISOString() : null;

    // ── Numeric y-axis: each lane is an integer, lanes 0-2 = lifecycle, 3-6 = events ──
    var LANE_Y = {
      lc_gone: 0, lc_restart: 1, lc_new: 2,
      process_death: 3, anr: 4, native_crash: 5, java_crash: 6,
    };

    // jitterY: spread points that would visually overlap on the same lane
    // by distributing them evenly within ±maxJitter of the lane centre.
    // Cluster = consecutive points (sorted by time) within thresholdMs of each other.
    function jitterY(xs, baseY, thresholdMs, maxJitter) {
      var n = xs.length;
      var yvals = [];
      for (var ii = 0; ii < n; ii++) yvals.push(baseY);
      if (n < 2) return yvals;
      var order = xs.map(function (x, idx) { return { t: new Date(x).getTime(), idx: idx }; });
      order.sort(function (a, b) { return a.t - b.t; });
      var i = 0;
      while (i < order.length) {
        var j = i + 1;
        while (j < order.length && order[j].t - order[i].t < thresholdMs) j++;
        var count = j - i;
        if (count > 1) {
          for (var k = 0; k < count; k++) {
            yvals[order[i + k].idx] = baseY + (-maxJitter + k * (2 * maxJitter / (count - 1)));
          }
        }
        i = j;
      }
      return yvals;
    }

    // Jitter threshold = one marker diameter worth of time.
    var evtClusterMs = msPerPx * 14;
    var lcClusterMs  = msPerPx * 10;

    var shapes = [];
    var annotations = [];

    var traces = [];
    ['java_crash', 'native_crash', 'anr', 'process_death'].forEach(function (type) {
      var xs = [], ids = [], text = [];
      incidents.filter(function (i) { return i.type === type; }).forEach(function (inc) {
        xs.push(toIso(inc.triggered_at));
        ids.push(inc.id);
        var proc = inc.process + (inc.pid ? (' (pid=' + inc.pid + ')') : '');
        var summary = String(inc.summary || '');
        text.push('<b>' + inc.id + '</b><br>' + yLabels[type] + ' · ' + proc + '<br>' + inc.triggered_at +
                  '<br>' + escapeHtml(summary).slice(0, 120) + (summary.length > 120 ? '…' : ''));
      });
      var ys = jitterY(xs, LANE_Y[type], evtClusterMs, 0.30);
      traces.push({
        x: xs, y: ys, ids: ids,
        mode: 'markers', type: 'scatter',
        marker: { symbol: 'x', size: 14, color: TYPE_LABELS[type].color, line: { width: 2.5, color: TYPE_LABELS[type].color } },
        text: text, hovertemplate: '%{text}<extra></extra>',
        name: yLabels[type], showlegend: false,
        cliponaxis: false,
      });
    });

    ['new', 'restart', 'gone'].forEach(function (ev) {
      var xs = [], text = [];
      lifecycle.filter(function (lc) { return lc.event === ev; }).forEach(function (lc) {
        xs.push(toIso(lc.timestamp));
        var info;
        if (ev === 'restart') info = 'pid ' + lc.old_pid + ' → ' + lc.new_pid + ' · gap ' + (lc.gap_sec || 0).toFixed(1) + 's';
        else if (ev === 'new') info = 'pid=' + lc.new_pid;
        else info = 'pid=' + lc.old_pid;
        text.push('<b>' + ev + '</b><br>' + lc.process + '<br>' + lc.timestamp + '<br>' + info);
      });
      var ys = jitterY(xs, LANE_Y['lc_' + ev], lcClusterMs, 0.25);
      traces.push({
        x: xs, y: ys,
        mode: 'markers', type: 'scatter',
        marker: { symbol: 'circle', size: 10, color: LC_COLORS[ev], line: { width: 1.5, color: '#ffffff' } },
        text: text, hovertemplate: '%{text}<extra></extra>',
        name: yLabels['lc_' + ev], showlegend: false,
        cliponaxis: false,
      });
    });

    // Dotted separator between lifecycle lanes (0-2) and event lanes (3-6)
    shapes.push({
      type: 'line', xref: 'paper', yref: 'y',
      x0: 0, x1: 1, y0: 2.5, y1: 2.5,
      line: { color: '#d1d5db', width: 1, dash: 'dot' },
      layer: 'below',
    });

    bookmarks.forEach(function (b) {
      var ts = toIso(b.timestamp);
      shapes.push({
        type: 'line', xref: 'x', yref: 'paper',
        x0: ts, x1: ts, y0: 0, y1: 1,
        line: { color: '#1d4ed8', width: 1.5, dash: 'dash' },
        layer: 'below',
      });
      annotations.push({
        x: ts, xref: 'x', y: 1.04, yref: 'paper',
        text: '🔖 ' + b.label,
        showarrow: false,
        font: { size: 11, color: '#1d4ed8', family: 'SF Mono, Menlo, monospace' },
        xanchor: 'left',
        bgcolor: 'rgba(255,255,255,0.95)',
        bordercolor: '#1d4ed8',
        borderwidth: 1,
        borderpad: 3,
      });
    });

    var layout = {
      margin: { l: 150, r: 24, t: 32, b: 44 },
      height: 460,
      paper_bgcolor: '#ffffff',
      plot_bgcolor: '#ffffff',
      xaxis: {
        type: 'date',
        range: t0Range && t1Range ? [t0Range, t1Range] : undefined,
        gridcolor: '#e5e7eb',
        linecolor: '#9ca3af',
        tickfont: { family: 'SF Mono, Menlo, monospace', size: 11, color: '#4b5563' },
        tickformat: '%H:%M',
        showgrid: true,
      },
      yaxis: {
        type: 'linear',
        tickmode: 'array',
        tickvals: [0, 1, 2, 3, 4, 5, 6],
        ticktext: YORDER_BOTTOM_TO_TOP.map(function (k) { return yLabels[k]; }),
        range: [-0.6, 6.6],
        gridcolor: '#f3f4f6',
        linecolor: '#9ca3af',
        tickfont: { family: '-apple-system, sans-serif', size: 13, color: '#1f2937' },
        showgrid: true,
        zeroline: false,
      },
      hovermode: 'closest',
      shapes: shapes,
      annotations: annotations,
      showlegend: false,
    };

    var config = {
      displaylogo: false,
      responsive: true,
      modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
      toImageButtonOptions: { format: 'png', filename: 'stability_timeline' },
    };

    var plotEl = document.getElementById('timeline-plot');
    Plotly.newPlot(plotEl, traces, layout, config).then(function () {
      plotEl.on('plotly_click', function (ev) {
        if (!ev.points || ev.points.length === 0) return;
        var id = ev.points[0].id;
        if (id) jumpToIncident(id);
      });
    });
  }

  // ============ INCIDENT FILTER / LIST / DETAIL ============
  var filterState = {
    type: 'all', process: 'all', severity: 'all',
    search: '', sort: 'time_desc', selectedId: null,
  };

  function setTypeFilter(type) {
    filterState.type = type;
    document.querySelectorAll('#type-chips .filter-chip').forEach(function (c) {
      c.setAttribute('data-active', c.getAttribute('data-type') === type ? 'true' : 'false');
    });
  }

  function applyFilters() {
    var q = filterState.search.trim().toLowerCase();
    var filtered = incidents.filter(function (inc) {
      if (filterState.type !== 'all' && inc.type !== filterState.type) return false;
      if (filterState.process !== 'all' && inc.process !== filterState.process) return false;
      if (filterState.severity !== 'all' && inc.severity !== filterState.severity) return false;
      if (q) {
        var ev = inc.evidence || {};
        var hay = (inc.summary + ' ' + (ev.exception_class || '') + ' ' +
                   inc.process + ' ' + (ev.top_frames || []).join(' ')).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    var sevRank = { fatal: 0, error: 1, warning: 2 };
    if (filterState.sort === 'time_desc')      filtered.sort(function (a, b) { return String(b.triggered_at).localeCompare(a.triggered_at); });
    else if (filterState.sort === 'time_asc')  filtered.sort(function (a, b) { return String(a.triggered_at).localeCompare(b.triggered_at); });
    else if (filterState.sort === 'sev')       filtered.sort(function (a, b) { return (sevRank[a.severity] || 99) - (sevRank[b.severity] || 99); });

    renderList(filtered);
    document.getElementById('list-count').textContent = filtered.length + ' / ' + totalIncidents;

    if (!filtered.find(function (i) { return i.id === filterState.selectedId; })) {
      filterState.selectedId = filtered.length > 0 ? filtered[0].id : null;
    }
    renderDetail(filterState.selectedId);
  }

  function renderList(items) {
    var wrap = document.getElementById('inc-list');
    if (items.length === 0) {
      wrap.innerHTML = '<div class="list-empty">' + tr('当前筛选条件下没有事件。', 'No incidents match the current filter.') + '</div>';
      return;
    }
    wrap.innerHTML = items.map(function (inc) {
      var t = TYPE_LABELS[inc.type] || { zh: inc.type, en: inc.type };
      var sev = SEV_LABELS[inc.severity] || { zh: inc.severity, en: inc.severity, cls: 'gray' };
      var chipCls = TYPE_CLS[inc.type] || 'gray';
      var summary = String(inc.summary || '');
      // Decode numeric process_death reason codes embedded in the summary string.
      if (inc.type === 'process_death') {
        var ev0 = inc.evidence || {};
        if (ev0.reason != null) {
          var raw0 = String(ev0.reason);
          var decoded0 = decodeReason(raw0);
          if (decoded0 !== raw0) summary = summary.split(raw0).join(decoded0);
        }
      }
      if (summary.length > 110) summary = summary.slice(0, 110) + '…';
      return '<div class="inc-item type-' + inc.type + (inc.id === filterState.selectedId ? ' active' : '') + '" data-id="' + escapeAttr(inc.id) + '">' +
        '<div class="row1">' +
          '<span class="chip ' + chipCls + '">' + escapeHtml(t.zh) + '</span>' +
          '<span class="chip ' + sev.cls + '">' + escapeHtml(sev.zh) + '</span>' +
          '<span class="id">' + escapeHtml(inc.id) + '</span>' +
        '</div>' +
        '<div class="row2">' + fmtTime(inc.triggered_at) + ' · ' + escapeHtml(inc.process) + (inc.pid ? ' · pid=' + inc.pid : '') + '</div>' +
        '<div class="row3">' + escapeHtml(summary) + '</div>' +
      '</div>';
    }).join('');
    wrap.querySelectorAll('.inc-item').forEach(function (el) {
      el.addEventListener('click', function () {
        filterState.selectedId = el.getAttribute('data-id');
        document.querySelectorAll('.inc-item').forEach(function (x) { x.classList.toggle('active', x === el); });
        renderDetail(filterState.selectedId);
      });
    });
  }

  // ── Process-state decoder & logcat-file locator ────────────────────────────
  // am_proc_died: parts[4] = PROCESS_STATE_* code at time of death.
  // am_kill:      parts[4] = string reason (e.g. "remove task") — left as-is.
  var PROC_STATE_LABELS = {
    0:'persistent', 1:'persistent-ui', 2:'top', 3:'bound-top',
    4:'foreground-service', 5:'bound-foreground-service',
    6:'important-foreground', 7:'important-background',
    8:'transient-background', 9:'backup', 10:'service',
    11:'receiver', 12:'top-sleeping', 13:'heavy-weight',
    14:'home', 15:'last-activity', 16:'cached-activity',
    17:'cached-activity-client', 18:'cached-recent',
    19:'cached-empty', 20:'nonexistent'
  };
  function decodeReason(raw) {
    if (raw == null || raw === '') return '—';
    var n = parseInt(raw, 10);
    if (!isNaN(n) && String(n) === String(raw).trim()) {
      var label = PROC_STATE_LABELS[n];
      return label ? label + ' (' + n + ')' : String(raw);
    }
    return String(raw);
  }
  // Given a UTC triggered_at string, find the matching logcat_*.log file.
  function logcatFileForTs(ts, logcatFiles) {
    if (!ts || !logcatFiles || !logcatFiles.length) return null;
    var m = /^(\d{4}-\d{2}-\d{2})\s+(\d{2})/.exec(ts);
    if (!m) return null;
    var expected = 'logcat_' + m[1] + '_' + m[2] + '.log';
    return logcatFiles.indexOf(expected) >= 0 ? expected : null;
  }

  function renderDetail(id) {
    var root = document.getElementById('inc-detail');
    if (!id) {
      root.innerHTML = '<div class="empty-state"><span class="emoji">' + (totalIncidents === 0 ? '✅' : '🔍') + '</span>' +
        '<div>' + (totalIncidents === 0
          ? tr('未检测到稳定性事件。', 'No stability events detected.')
          : tr('当前筛选下没有事件。', 'No incidents match the current filter.')) + '</div></div>';
      return;
    }
    var inc = incidents.find(function (x) { return x.id === id; });
    if (!inc) return;

    var t = TYPE_LABELS[inc.type] || { zh: inc.type, en: inc.type };
    var sev = SEV_LABELS[inc.severity] || { zh: inc.severity, en: inc.severity, cls: 'gray' };
    var chipCls = TYPE_CLS[inc.type] || 'gray';
    var ev = inc.evidence || {};
    var top = ev.top_frames || [];

    var pkgPrefix = run.package || '';
    var emptyFrameMsg;
    if (inc.type === 'java_crash' || inc.type === 'native_crash') {
      if (ev.source === 'dropbox') {
        emptyFrameMsg = tr(
          'dropbox 事件未提取到调用栈（Chromium/WebView 等非标准崩溃格式），可查阅现场文件 (.txt)。',
          'No stack frames from dropbox event (non-standard crash format e.g. Chromium/WebView) — see the .txt evidence file.'
        );
      } else {
        emptyFrameMsg = tr('未捕获到调用栈帧。', 'No stack frames captured.');
      }
    } else if (inc.type === 'anr') {
      emptyFrameMsg = tr(
        'ANR 调用栈记录在 trace 文件中（如有）。',
        'ANR stack is in the trace file (if available).'
      );
    } else {
      emptyFrameMsg = tr('进程退出事件无调用栈。', 'Process death events have no stack frames.');
    }
    var framesHtml = top.length === 0
      ? '<div class="f-empty">' + emptyFrameMsg + '</div>'
      : top.map(function (line, i) {
          var isBiz = pkgPrefix && String(line).indexOf(pkgPrefix) >= 0;
          return '<div class="f-line' + (isBiz ? ' biz' : '') + '" data-line="' + escapeAttr(line) + '">' +
            '<span class="idx">#' + String(i).padStart(2, '0') + '</span><span>' + escapeHtml(line) + '</span></div>';
        }).join('');

    function statBox(k_zh, k_en, v, cls) {
      cls = cls || '';
      return '<div class="stat"><div class="k"><span class="zh">' + k_zh + '</span><span class="en">' + k_en + '</span></div>' +
             '<div class="v ' + cls + '">' + (v != null && v !== '' ? escapeHtml(String(v)) : '—') + '</div></div>';
    }
    // SOURCE_LABELS: logcat = 实时流，dropbox = 兜底轮询，watcher = 进程监控
    var SRC_LABEL = { logcat: 'logcat（实时）', dropbox: 'dropbox（兜底）', watcher: 'watcher（进程监控）' };
    var SRC_LABEL_EN = { logcat: 'logcat (realtime)', dropbox: 'dropbox (fallback poll)', watcher: 'watcher (process monitor)' };
    function sourceBox(src) {
      var zh = SRC_LABEL[src] || src || '—';
      var en = SRC_LABEL_EN[src] || src || '—';
      return '<div class="stat"><div class="k"><span class="zh">来源</span><span class="en">Source</span></div>' +
             '<div class="v"><span class="zh">' + escapeHtml(zh) + '</span><span class="en">' + escapeHtml(en) + '</span></div></div>';
    }
    var typeFields = '';
    if (inc.type === 'java_crash') {
      typeFields = statBox('异常类', 'Exception class', ev.exception_class || '—', 'red') +
                   sourceBox(ev.source) +
                   statBox('设备时间戳', 'Device ts', ev.device_ts || '—') +
                   (ev.fallback_reason ? statBox('降级原因', 'Fallback reason', ev.fallback_reason) : '');
    } else if (inc.type === 'native_crash') {
      typeFields = statBox('信号', 'Signal', ev.signal || '—', 'red') +
                   statBox('fault addr', 'Fault addr', ev.fault_addr || '—') +
                   sourceBox(ev.source) +
                   statBox('设备时间戳', 'Device ts', ev.device_ts || '—') +
                   (ev.fallback_reason ? statBox('降级原因', 'Fallback reason', ev.fallback_reason) : '');
    } else if (inc.type === 'anr') {
      typeFields =
        '<div class="stat" style="grid-column: span 2;"><div class="k"><span class="zh">原因</span><span class="en">Reason</span></div>' +
        '<div class="v orange" style="font-size: var(--fs-sm); font-weight: 600;">' + escapeHtml(ev.reason || '—') + '</div></div>' +
        sourceBox(ev.source) +
        statBox('设备时间戳', 'Device ts', ev.device_ts || '—') +
        (ev.fallback_reason ? statBox('降级原因', 'Fallback reason', ev.fallback_reason) : '');
    } else {
      typeFields = statBox('原因', 'Reason', decodeReason(ev.reason)) +
                   sourceBox(ev.source) +
                   statBox('设备时间戳', 'Device ts', ev.device_ts || '—');
    }

    var filesParts = [];
    if (ev.logcat_slice_file) {
      filesParts.push('<div class="file-line"><span class="k">' + tr('logcat 现场', 'logcat slice') + '</span>' +
        '<a href="incidents/' + escapeAttr(ev.logcat_slice_file) + '" target="_blank">incidents/' + escapeHtml(ev.logcat_slice_file) + '</a></div>');
    }
    if (inc.type === 'native_crash') {
      if (ev.trace_file) {
        filesParts.push('<div class="file-line"><span class="k">tombstone</span>' +
          '<a href="incidents/' + escapeAttr(ev.trace_file) + '" target="_blank">incidents/' + escapeHtml(ev.trace_file) + '</a></div>');
      } else if (ev.fallback_reason) {
        filesParts.push('<div class="file-line warn"><span class="k">tombstone</span><span>⚠ ' + escapeHtml(ev.fallback_reason) + '</span></div>');
      }
    }
    if (inc.type === 'anr') {
      if (ev.trace_file) {
        filesParts.push('<div class="file-line"><span class="k">ANR trace</span>' +
          '<a href="incidents/' + escapeAttr(ev.trace_file) + '" target="_blank">incidents/' + escapeHtml(ev.trace_file) + '</a></div>');
      } else if (ev.fallback_reason) {
        filesParts.push('<div class="file-line warn"><span class="k">ANR trace</span><span>⚠ ' + escapeHtml(ev.fallback_reason) + '</span></div>');
      }
    }
    // Always show the logcat log file covering this event's timestamp.
    var lcFile = logcatFileForTs(inc.triggered_at, dataFiles.logcat);
    if (lcFile) {
      filesParts.push('<div class="file-line"><span class="k">' + tr('logcat 日志', 'logcat log') + '</span>' +
        '<a href="' + escapeAttr(lcFile) + '" target="_blank">' + escapeHtml(lcFile) + '</a></div>');
    }

    root.innerHTML =
      '<div class="hd">' +
        '<h3>' + escapeHtml(inc.id) + '</h3>' +
        '<span class="chip ' + chipCls + '">' + escapeHtml(t.zh) + '</span>' +
        '<span class="chip ' + sev.cls + '">' + escapeHtml(sev.zh) + '</span>' +
        '<span class="meta">' + escapeHtml(inc.process) + (inc.pid ? ' · pid=' + inc.pid : '') + ' · ' + escapeHtml(inc.triggered_at || '') + '</span>' +
        '<span class="spacer"></span>' +
      '</div>' +
      '<div class="stat-row">' + typeFields + '</div>' +
      '<div class="sub-title"><span class="t"><span class="zh">摘要 Summary</span><span class="en">Summary</span></span></div>' +
      '<div class="summary-box ' + inc.type + '">' + escapeHtml(inc.summary || '') + '</div>' +
      '<div class="sub-title"><span class="t"><span class="zh">Top 栈帧</span><span class="en">Top stack frames</span></span>' +
        '<span class="c">' + tr('点击行复制 · 业务包名高亮', 'click line to copy · business package highlighted') + '</span>' +
      '</div>' +
      '<div class="frames">' + framesHtml + '</div>' +
      '<div class="sub-title" style="margin-top: 24px;">' +
        '<span class="t"><span class="zh">现场文件 Evidence files</span><span class="en">Evidence files</span></span>' +
      '</div>' +
      (filesParts.join('') || '<div class="file-line"><span class="k">—</span><span class="v-muted">' + tr('无附加文件', 'no extra files') + '</span></div>');

    root.querySelectorAll('.f-line[data-line]').forEach(function (line) {
      line.addEventListener('click', async function () {
        try { await navigator.clipboard.writeText(line.getAttribute('data-line')); } catch (e) {}
        line.classList.add('copied');
        setTimeout(function () { line.classList.remove('copied'); }, 700);
      });
    });
  }

  function jumpToIncident(id) {
    setTypeFilter('all');
    filterState.type = 'all';
    filterState.selectedId = id;
    document.getElementById('proc-filter').value = 'all';
    document.getElementById('sev-filter').value = 'all';
    document.getElementById('search-inp').value = '';
    filterState.process = 'all';
    filterState.severity = 'all';
    filterState.search = '';
    applyFilters();
    document.querySelector('#type-chips').scrollIntoView({ block: 'start', behavior: 'smooth' });
    setTimeout(function () {
      var el = document.querySelector('.inc-item[data-id="' + id + '"]');
      if (el) {
        el.classList.add('flash');
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setTimeout(function () { el.classList.remove('flash'); }, 1500);
      }
    }, 360);
  }

  // ============ HERO CARDS — click to filter ============
  function initHeroCards() {
    document.querySelectorAll('.ev-card').forEach(function (card) {
      card.addEventListener('click', function () {
        var type = card.getAttribute('data-card-type');
        if (counts[type] === 0) return;
        setTypeFilter(type);
        filterState.type = type;
        document.getElementById('proc-filter').value = 'all';
        document.getElementById('sev-filter').value = 'all';
        document.getElementById('search-inp').value = '';
        filterState.process = 'all';
        filterState.severity = 'all';
        filterState.search = '';
        applyFilters();
        document.querySelector('#type-chips').scrollIntoView({ block: 'start', behavior: 'smooth' });
      });
    });
  }

  // ============ FILTER BAR / SEARCH ============
  function initFilters() {
    document.querySelectorAll('#type-chips .filter-chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        if (chip.hasAttribute('disabled')) return;
        setTypeFilter(chip.getAttribute('data-type'));
        applyFilters();
      });
    });
    document.getElementById('proc-filter').addEventListener('change', function (e) {
      filterState.process = e.target.value; applyFilters();
    });
    document.getElementById('sev-filter').addEventListener('change', function (e) {
      filterState.severity = e.target.value; applyFilters();
    });
    document.getElementById('search-inp').addEventListener('input', function (e) {
      filterState.search = e.target.value; applyFilters();
    });
    document.getElementById('sort-sel').addEventListener('change', function (e) {
      filterState.sort = e.target.value; applyFilters();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
        e.preventDefault();
        var inp = document.getElementById('search-inp');
        inp.focus(); inp.select();
      } else if (e.key === 'Escape') {
        var inp2 = document.getElementById('search-inp');
        if (document.activeElement === inp2) {
          inp2.value = ''; filterState.search = ''; applyFilters(); inp2.blur();
        }
      }
    });
  }

  // ============ LANGUAGE TOGGLE ============
  function initLang() {
    var root = document.documentElement;
    var buttons = document.querySelectorAll('[data-lang-btn]');
    function paint() {
      var cur = getLang();
      buttons.forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-lang-btn') === cur); });
    }
    buttons.forEach(function (b) {
      b.addEventListener('click', function () {
        root.setAttribute('data-lang', b.getAttribute('data-lang-btn'));
        paint();
        renderProcessTable();
        applyFilters();
        if (window.Plotly) renderTimeline();
        renderFilesTree();
        renderBookmarks();
      });
    });
    paint();
  }

  // ============ COPY PKG ============
  function initCopy() {
    var btn = document.getElementById('copy-pkg');
    if (!btn) return;
    btn.addEventListener('click', async function () {
      try { await navigator.clipboard.writeText(run.package || ''); } catch (e) {}
      btn.classList.add('ok');
      btn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3.5,8.5 6.5,11.5 12.5,5"/></svg>';
      setTimeout(function () {
        btn.classList.remove('ok');
        btn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="8" height="9" rx="1.5"/><path d="M2.5 11V3.5A1.5 1.5 0 0 1 4 2h6"/></svg>';
      }, 1400);
    });
  }

  // ============ BOOKMARKS ACCORDION ============
  function renderBookmarks() {
    var cnt  = document.getElementById('bookmark-count');
    var body = document.getElementById('bookmark-body');
    cnt.innerHTML = '<span class="zh">' + bookmarks.length + ' 条</span><span class="en">' + bookmarks.length + ' entr' + (bookmarks.length === 1 ? 'y' : 'ies') + '</span>';
    if (bookmarks.length === 0) {
      body.innerHTML = '<div class="v-muted" style="padding: 14px 0;">' + tr('（无书签）', '(no bookmarks)') + '</div>';
      return;
    }
    body.innerHTML = '<table class="tbl" style="margin-top: 10px;">' +
      '<thead><tr>' +
        '<th><span class="zh">时间</span><span class="en">Time</span></th>' +
        '<th><span class="zh">标签</span><span class="en">Label</span></th>' +
        '<th><span class="zh">元数据</span><span class="en">Metadata</span></th>' +
      '</tr></thead>' +
      '<tbody>' +
      bookmarks.map(function (b) {
        var meta = b.metadata ? JSON.stringify(b.metadata) : '{}';
        return '<tr>' +
          '<td class="mono">' + escapeHtml(b.timestamp || '') + '</td>' +
          '<td><span class="chip blue">' + escapeHtml(b.label || '') + '</span></td>' +
          '<td class="mono v-muted">' + escapeHtml(meta) + '</td>' +
        '</tr>';
      }).join('') +
      '</tbody></table>';
  }

  // ============ EFFECTIVE CONFIG ============
  function renderConfigGrid() {
    var grid = document.getElementById('cfg-grid');
    var groups = [
      { title_zh: '基本', title_en: 'Basics',
        keys: ['package', 'device', 'output_dir', 'wait_timeout_sec', 'rescan_interval_sec', 'process_filter'] },
      { title_zh: '采集', title_en: 'Collection',
        keys: ['logcat_enabled', 'logcat_buffers', 'logcat_reconnect_backoff_sec', 'dropbox_enabled', 'dropbox_poll_interval_sec'] },
      { title_zh: '检测开关', title_en: 'Detectors',
        keys: ['enable_java_crash', 'enable_native_crash', 'enable_anr', 'enable_process_death', 'dedup_window_sec'] },
      { title_zh: 'Dump', title_en: 'Dump',
        keys: ['pre_context_sec', 'post_context_sec', 'max_incidents_per_type', 'max_concurrent_dumps', 'pull_tombstone', 'pull_anr_trace'] },
      { title_zh: '输出', title_en: 'Output',
        keys: ['emit_html', 'status_interval_sec'] },
    ];
    var presented = {};
    function fmtVal(v) {
      if (v === null || v === undefined) return '<span class="v v-muted">null</span>';
      if (v === true)  return '<span class="v bool-y">true</span>';
      if (v === false) return '<span class="v bool-n">false</span>';
      if (Array.isArray(v)) return '<span class="v">[' + v.map(escapeHtml).join(',') + ']</span>';
      if (typeof v === 'object') return '<span class="v">' + escapeHtml(JSON.stringify(v)) + '</span>';
      return '<span class="v">' + escapeHtml(v) + '</span>';
    }
    var sections = groups.map(function (g) {
      var present = g.keys.filter(function (k) { return Object.prototype.hasOwnProperty.call(configEff, k); });
      if (present.length === 0) return '';
      var rows = present.map(function (k) {
        presented[k] = true;
        return '<div class="kv"><span class="k">' + escapeHtml(k) + '</span>' + fmtVal(configEff[k]) + '</div>';
      }).join('');
      return '<div class="cfg-group">' +
        '<div class="g-title"><span class="zh">' + g.title_zh + '</span><span class="en">' + g.title_en + '</span></div>' +
        rows + '</div>';
    }).filter(Boolean);

    // catch-all for any unknown keys
    var extras = Object.keys(configEff).filter(function (k) { return !presented[k]; }).sort();
    if (extras.length > 0) {
      var rows = extras.map(function (k) {
        return '<div class="kv"><span class="k">' + escapeHtml(k) + '</span>' + fmtVal(configEff[k]) + '</div>';
      }).join('');
      sections.push('<div class="cfg-group">' +
        '<div class="g-title"><span class="zh">其他</span><span class="en">Other</span></div>' +
        rows + '</div>');
    }
    grid.innerHTML = sections.join('');
    document.getElementById('cfg-count').innerHTML =
      '<span class="zh">' + Object.keys(configEff).length + ' 项</span>' +
      '<span class="en">' + Object.keys(configEff).length + ' keys</span>';
  }

  // ============ FILES TREE ============
  function renderFilesTree() {
    var tree = document.getElementById('files-tree');
    var totalFiles = 0;
    var rows = [];

    rows.push(fileRow('report.json',     tr('权威结构化结果', 'authoritative structured result')));
    totalFiles++;
    rows.push(fileRow('report.html',     tr('本文件（已在当前页面）', "this file (you're looking at it)"), true));
    rows.push(fileRow('status.json',     tr('运行心跳（仅 runtime）', 'heartbeat (runtime only)')));
    totalFiles++;
    rows.push(fileRow('bookmarks.jsonl', tr('书签追加写文件', 'append-only bookmarks file')));
    totalFiles++;

    (dataFiles.events || []).forEach(function (f) {
      rows.push(fileRow(f, tr('事件时序流', 'event stream')));
      totalFiles++;
    });
    (dataFiles.lifecycle || []).forEach(function (f) {
      rows.push(fileRow(f, tr('进程生命周期', 'process lifecycle')));
      totalFiles++;
    });
    (dataFiles.logcat || []).forEach(function (f) {
      rows.push(fileRow(f, tr('原始 logcat', 'raw logcat')));
      totalFiles++;
    });

    // incidents/ — special dir entry
    var jsonCount = incidents.length;
    var txtCount  = incidents.filter(function (i) { return i.evidence && i.evidence.logcat_slice_file; }).length;
    var tombCount = incidents.filter(function (i) { return i.type === 'native_crash' && i.evidence && i.evidence.trace_file; }).length;
    var traceCount= incidents.filter(function (i) { return i.type === 'anr' && i.evidence && i.evidence.trace_file; }).length;
    var descIncZh = '现场快照 · ' + jsonCount + ' .json · ' + txtCount + ' .txt';
    var descIncEn = 'evidence dir · ' + jsonCount + ' .json · ' + txtCount + ' .txt';
    if (tombCount > 0)  { descIncZh += ' · ' + tombCount  + ' .tombstone'; descIncEn += ' · ' + tombCount  + ' .tombstone'; }
    if (traceCount > 0) { descIncZh += ' · ' + traceCount + ' .trace';     descIncEn += ' · ' + traceCount + ' .trace'; }
    rows.push(
      '<div class="fr" data-dir="incidents/">' +
        '<span class="name dir">incidents/</span>' +
        '<span class="desc"><span class="zh">' + descIncZh + '</span><span class="en">' + descIncEn + '</span></span>' +
        '<span class="open-icon" aria-hidden="true">⊞</span>' +
      '</div>'
    );

    tree.innerHTML = rows.join('');

    document.getElementById('files-count').innerHTML =
      '<span class="zh">' + totalFiles + ' 个文件 + incidents/ · 点击查看</span>' +
      '<span class="en">' + totalFiles + ' files + incidents/ · click to view</span>';

    initFileTreeHandlers();
  }

  function fileRow(name, desc, disabled) {
    return '<div class="fr' + (disabled ? ' disabled' : '') + '" data-file="' + escapeAttr(name) + '">' +
      '<span class="name">' + escapeHtml(name) + '</span>' +
      '<span class="desc">' + escapeHtml(desc) + '</span>' +
      '<span class="open-icon" aria-hidden="true">' + (disabled ? '' : '↗') + '</span>' +
    '</div>';
  }

  function buildIncidentDirItems() {
    var items = [];
    incidents.forEach(function (inc) {
      var ev = inc.evidence || {};
      // The .json on disk shares the slice's base name (per dumpers/__init__.py).
      var base = ev.logcat_slice_file ? ev.logcat_slice_file.replace(/\.txt$/, '') : (inc.type + '_' + inc.id);
      items.push({ name: base + '.json',      type: 'json', incident: inc.id });
      if (ev.logcat_slice_file) {
        items.push({ name: ev.logcat_slice_file, type: 'txt', incident: inc.id });
      }
      if (inc.type === 'native_crash' && ev.trace_file) {
        items.push({ name: ev.trace_file, type: 'tomb', incident: inc.id });
      }
      if (inc.type === 'anr' && ev.trace_file) {
        items.push({ name: ev.trace_file, type: 'trc', incident: inc.id });
      }
    });
    return items;
  }

  function dirListingHtml(items) {
    var css =
      '*{box-sizing:border-box;margin:0;padding:0}' +
      'body{background:#f7f8fa;color:#0b1220;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px;line-height:1.6;padding:24px 32px 60px}' +
      '.head{display:flex;align-items:baseline;gap:14px;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #d1d5db}' +
      '.head h1{font-family:"SF Mono",Menlo,monospace;font-size:22px;font-weight:600;color:#0b1220}' +
      '.head .c{font-size:13px;color:#6b7280;font-family:"SF Mono",Menlo,monospace}' +
      '.wrap{background:#fff;border:1px solid #d1d5db;border-radius:6px;overflow:hidden}' +
      'table{width:100%;border-collapse:collapse}' +
      'th,td{text-align:left;padding:12px 22px;border-bottom:1px solid #e5e7eb;font-family:"SF Mono",Menlo,monospace;font-size:14px;white-space:nowrap}' +
      'thead th{background:#f7f8fa;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#6b7280}' +
      'tbody tr:last-child td{border-bottom:0}' +
      'tbody tr:hover td{background:#f7f8fa}' +
      '.fname{color:#1d4ed8;text-decoration:none}' +
      '.fname:hover{text-decoration:underline}' +
      '.tag{display:inline-block;padding:2px 8px;border-radius:3px;font-weight:600;font-size:11px;letter-spacing:0.04em;text-transform:uppercase}' +
      '.tag.json{background:rgba(29,78,216,0.10);color:#1d4ed8}' +
      '.tag.txt{background:#eef1f4;color:#6b7280}' +
      '.tag.tomb{background:rgba(127,29,29,0.10);color:#7f1d1d}' +
      '.tag.trc{background:rgba(194,65,12,0.10);color:#c2410c}' +
      '.hint{margin-top:18px;font-size:13px;color:#6b7280}';
    var body = '<div class="head"><h1>incidents/</h1><span class="c">' + items.length + ' ' + tr('个文件', 'files') + '</span></div>' +
      '<div class="wrap"><table><thead><tr>' +
        '<th>' + tr('文件名', 'name') + '</th>' +
        '<th>' + tr('类型', 'type') + '</th>' +
      '</tr></thead><tbody>' +
      items.map(function (it) {
        return '<tr>' +
          '<td><a class="fname" href="' + escapeAttr(it.name) + '" target="_blank">' + escapeHtml(it.name) + '</a></td>' +
          '<td><span class="tag ' + it.type + '">' + it.type + '</span></td>' +
        '</tr>';
      }).join('') +
      '</tbody></table></div>' +
      '<div class="hint">' + tr('点击文件名 → 浏览器中查看文件内容', 'Click a file name → open it in the browser') + '</div>';
    return '<!doctype html><html><head><meta charset="utf-8"/><title>incidents/</title><base href="incidents/"/>' +
           '<style>' + css + '</style></head><body>' + body + '</body></html>';
  }

  function initFileTreeHandlers() {
    document.querySelectorAll('#files-tree .fr').forEach(function (row) {
      if (row.classList.contains('disabled')) return;
      row.addEventListener('click', function () {
        var dir = row.getAttribute('data-dir');
        if (dir === 'incidents/') {
          var items = buildIncidentDirItems();
          var url = URL.createObjectURL(new Blob([dirListingHtml(items)], { type: 'text/html;charset=utf-8' }));
          var w = window.open(url, '_blank');
          if (!w) alert(tr('请允许弹出窗口以查看目录', 'Please allow pop-ups to view the folder'));
          return;
        }
        var file = row.getAttribute('data-file');
        if (file) window.open(file, '_blank');
      });
    });
  }

  // ============ BOOT ============
  function boot() {
    initLang();
    initCopy();
    renderHeader();
    renderVerdictBar();
    renderHeroCards();
    renderDerivedStrip();
    renderTypeChipCounts();
    renderProcessSelect();
    renderProcessTable();
    renderBookmarks();
    renderConfigGrid();
    renderFilesTree();
    initHeroCards();
    initFilters();

    // default selection
    filterState.selectedId = incidents.slice().sort(function (a, b) {
      return String(b.triggered_at).localeCompare(a.triggered_at);
    })[0] && incidents.slice().sort(function (a, b) { return String(b.triggered_at).localeCompare(a.triggered_at); })[0].id || null;
    applyFilters();

    if (window.Plotly) {
      renderTimeline();
    } else {
      var wait = setInterval(function () {
        if (window.Plotly) { clearInterval(wait); renderTimeline(); }
      }, 50);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
"""


def render(result: Dict) -> str:
    """Render the full HTML report from a `report.json` result dict."""
    run = result.get("run", {}) or {}
    config_eff = run.get("config_effective", {}) or {}
    pkg = run.get("package", "")

    # Build the embedded JSON blocks. We split incidents/lifecycle from `report`
    # so each block is small and the JS reads them independently.
    report_block = {
        "schema_version": result.get("schema_version", "1.0"),
        "run": {
            "package": pkg,
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "duration_sec": run.get("duration_sec"),
            "exit_code": run.get("exit_code"),
            "exit_reason": run.get("exit_reason"),
            "device": run.get("device") or {},
        },
        "processes": result.get("processes", []) or [],
        "bookmarks": result.get("bookmarks", []) or [],
    }
    incidents_block = result.get("incidents", []) or []
    lifecycle_block = result.get("lifecycle_events", []) or []
    files_block = result.get("data_files", {}) or {"events": [], "lifecycle": [], "logcat": []}
    config_block = config_eff

    parts = [
        '<!doctype html>',
        '<html lang="zh-CN" data-lang="zh">',
        '<head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>Stability report — {_html.escape(pkg)}</title>',
        '<script src="https://cdn.plot.ly/plotly-2.30.0.min.js" charset="utf-8"></script>',
        '<style>', _CSS, '</style>',
        '</head>',
        '<body>',
        _BODY_SKELETON,
        '<script type="application/json" id="report-data">', _safe_json(report_block), '</script>',
        '<script type="application/json" id="incidents-data">', _safe_json(incidents_block), '</script>',
        '<script type="application/json" id="lifecycle-data">', _safe_json(lifecycle_block), '</script>',
        '<script type="application/json" id="files-data">', _safe_json(files_block), '</script>',
        '<script type="application/json" id="config-data">', _safe_json(config_block), '</script>',
        '<script>', _JS, '</script>',
        '</body></html>',
    ]
    return "\n".join(parts)


def write(result: Dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / HTML_FILENAME
    path.write_text(render(result), encoding="utf-8")
    return path
