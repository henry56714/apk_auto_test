"""Render an interactive HTML performance report (v3 design).

Reads CSV time-series (raw data) + result dict (metadata) and emits a
single self-contained HTML file:
  §01 Timeline rail (SVG)
  §02 Process overview (3 tables: meta, CPU stats, mem stats)
  §03 Time-series charts (Plotly Scattergl, loaded from CDN)
  §04 Incidents master-detail panel
  §05 Additional info accordion (bookmarks / config / files)
"""

from __future__ import annotations

import html as _html
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

HTML_FILENAME = "report.html"
_TARGET_POINTS = 5_000

_PROC_COLORS = [
    "#1d4ed8", "#c2410c", "#15803d", "#7c3aed", "#b45309",
    "#0e7490", "#be123c", "#4338ca", "#0f766e", "#b91c1c",
]

# ── CSS ──────────────────────────────────────────────────────────────────────
_CSS = """\
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
    --gray:          #4b5563;
    --fs-xs:   13px;
    --fs-sm:   14px;
    --fs-md:   15px;
    --fs-base: 16px;
    --fs-lg:   20px;
    --fs-xl:   24px;
    --fs-2xl:  32px;
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
                 "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: var(--fs-base);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .mono {
    font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 0.94em;
    font-variant-numeric: tabular-nums;
  }
  .page { max-width: 1280px; margin: 0 auto; padding: 48px 56px 96px; }

  /* HEADER */
  .doc-head {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: 32px; padding-bottom: 28px; border-bottom: 1px solid var(--rule);
  }
  .doc-head .eyebrow {
    font-size: var(--fs-xs); letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--muted); font-weight: 600; margin-bottom: 10px;
  }
  .doc-head h1 {
    margin: 0;
    font-size: var(--fs-2xl); font-weight: 600; letter-spacing: -0.005em; color: var(--ink);
    line-height: 1.2;
  }
  .doc-head .meta { margin-top: 14px; font-size: var(--fs-md); color: var(--ink-3); }
  .doc-head .meta span + span::before { content: "·"; margin: 0 10px; color: var(--faint); }
  .verdict { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; min-width: 220px; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px; padding: 7px 16px;
    border: 1px solid var(--green); color: var(--green);
    background: rgba(21,128,61,0.08); border-radius: 999px;
    font-size: var(--fs-md); font-weight: 600;
  }
  .pill::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
  .pill.red    { color: var(--red);      border-color: var(--red);      background: rgba(185,28,28,0.08); }
  .pill.orange { color: var(--accent-2); border-color: var(--accent-2); background: rgba(194,65,12,0.08); }
  .verdict .micro { font-size: var(--fs-sm); color: var(--muted); }

  /* SECTION */
  section { margin-top: 60px; }
  .sec-head {
    display: flex; align-items: baseline; gap: 16px;
    margin-bottom: 22px; flex-wrap: wrap;
  }
  .sec-head h2 { margin: 0; font-size: var(--fs-lg); font-weight: 700; color: var(--ink); letter-spacing: -0.005em; }
  .sec-head .num-tag { font-family: "SF Mono","JetBrains Mono",Menlo,monospace; font-size: var(--fs-sm); color: var(--faint); font-weight: 500; }
  .sec-head .desc { margin-left: auto; font-size: var(--fs-md); color: var(--muted); }

  /* KPI ROW */
  .kpis {
    display: grid; grid-template-columns: repeat(6, 1fr);
    gap: 0; border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; background: var(--bg);
  }
  .kpi { padding: 22px; border-right: 1px solid var(--rule); }
  .kpi:last-child { border-right: 0; }
  .kpi .k { font-size: var(--fs-xs); color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px; font-weight: 600; }
  .kpi .v { font-size: var(--fs-xl); font-weight: 600; color: var(--ink); letter-spacing: -0.01em; font-variant-numeric: tabular-nums; line-height: 1.2; }
  .kpi .v .u { font-size: var(--fs-md); font-weight: 400; color: var(--muted); margin-left: 6px; }
  .kpi.alert .v { color: var(--red); }
  .kpi .delta { font-size: var(--fs-sm); color: var(--ink-3); margin-top: 10px; font-variant-numeric: tabular-nums; }

  /* TIMELINE RAIL */
  .rail { border: 1px solid var(--rule); border-radius: 6px; padding: 24px 28px 20px; background: var(--bg); }
  .rail-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 18px; flex-wrap: wrap; gap: 16px; }
  .rail-head .span { font-family: "SF Mono","JetBrains Mono",Menlo,monospace; font-size: var(--fs-md); color: var(--ink-2); font-weight: 500; }
  .rail-head .legend { display: flex; gap: 20px; font-size: var(--fs-sm); color: var(--ink-3); flex-wrap: wrap; }
  .rail-head .legend .item { display: inline-flex; align-items: center; gap: 7px; }
  .sw { width: 11px; height: 11px; display: inline-block; border-radius: 999px; }
  .sw.x  { position: relative; width: 12px; height: 12px; background: transparent; border-radius: 0; transform: none; }
  .sw.x::before, .sw.x::after { content: ""; position: absolute; width: 12px; height: 2px; background: var(--red); top: 5px; left: 0; border-radius: 1px; }
  .sw.x::before { transform: rotate(45deg); }
  .sw.x::after  { transform: rotate(-45deg); }
  .sw.g  { background: var(--green); }
  .sw.o  { background: var(--accent-2); }
  .sw.gr { background: var(--gray); }
  .sw.b  { background: var(--accent); width: 2px; height: 13px; border-radius: 0; }
  .rail-svg { width: 100%; height: 70px; display: block; }
  .rail-axis { display: flex; justify-content: space-between; font-family: "SF Mono","JetBrains Mono",Menlo,monospace; font-size: var(--fs-xs); color: var(--muted); margin-top: 8px; }
  .rail-axis.slanted { align-items: flex-start; height: 44px; margin-top: 6px; overflow: hidden; }
  .rail-axis.slanted span { display: inline-block; transform: rotate(-32deg); transform-origin: top left; white-space: nowrap; font-size: var(--fs-xs); color: var(--muted); }

  /* TABLE */
  .table-wrap { border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; background: var(--bg); }
  .table-scroll { overflow-x: auto; }
  table.tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-md); }
  table.tbl th, table.tbl td { text-align: left; padding: 14px 18px; border-bottom: 1px solid var(--rule-2); white-space: nowrap; color: var(--ink-2); }
  table.tbl thead th { background: var(--bg-soft); font-size: var(--fs-xs); font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-3); border-bottom: 1px solid var(--rule); }
  table.tbl tbody tr:last-child td { border-bottom: 0; }
  table.tbl tbody tr:hover td { background: var(--bg-soft); }
  table.tbl td.r, table.tbl th.r { text-align: right; font-variant-numeric: tabular-nums; }
  table.tbl td.mono { font-family: "SF Mono","JetBrains Mono",Menlo,monospace; font-size: calc(var(--fs-md) - 0.5px); color: var(--ink); }

  .chip { display: inline-flex; align-items: center; gap: 7px; padding: 3px 10px; border-radius: 4px; background: var(--bg-tint); color: var(--ink-2); font-size: var(--fs-sm); font-weight: 600; border: 1px solid var(--rule); }
  .chip::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: var(--faint); }
  .chip.red    { color: var(--red);      background: rgba(185,28,28,0.08); border-color: rgba(185,28,28,0.3); }
  .chip.red::before    { background: var(--red); }
  .chip.orange { color: var(--accent-2); background: rgba(194,65,12,0.08); border-color: rgba(194,65,12,0.3); }
  .chip.orange::before { background: var(--accent-2); }
  .chip.green  { color: var(--green);    background: rgba(21,128,61,0.08); border-color: rgba(21,128,61,0.3); }
  .chip.green::before  { background: var(--green); }
  .chip.gray   { color: var(--muted);    background: var(--bg-soft); }
  .chip.gray::before   { background: var(--faint); }

  .v-red    { color: var(--red); font-weight: 600; }
  .v-orange, table.tbl td.v-orange { color: var(--accent-2); font-weight: 600; }
  .v-muted  { color: var(--faint); }

  /* CHARTS */
  .charts { border: 1px solid var(--rule); border-radius: 6px; background: var(--bg); padding: 8px 20px; }
  .chart { padding: 22px 0 10px; border-bottom: 1px solid var(--rule-2); }
  .chart:last-child { border-bottom: 0; }
  .chart-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; flex-wrap: wrap; gap: 12px; }
  .chart-head .title { font-size: var(--fs-md); font-weight: 700; color: var(--ink); }
  .chart-head .legend { display: flex; gap: 18px; font-size: var(--fs-sm); color: var(--ink-2); flex-wrap: wrap; }
  .chart-head .legend .item { display: inline-flex; align-items: center; gap: 7px; }
  .chart-head .legend .line { width: 18px; height: 2px; display: inline-block; }
  .chart-plotly { width: 100%; }
  .chart.lane .chart-svg { height: 150px; }
  .chart-svg { width: 100%; display: block; }

  /* INCIDENTS */
  .md { display: grid; grid-template-columns: 360px 1fr; border: 1px solid var(--rule); border-radius: 6px; background: var(--bg); overflow: hidden; }
  .md .list { border-right: 1px solid var(--rule); background: var(--bg-soft); }
  .md .list-head { padding: 16px 22px; border-bottom: 1px solid var(--rule); display: flex; justify-content: space-between; align-items: baseline; }
  .md .list-head .t { font-size: var(--fs-md); font-weight: 700; color: var(--ink); }
  .md .list-head .c { font-size: var(--fs-sm); color: var(--muted); font-variant-numeric: tabular-nums; }
  .md .list-scroll { max-height: 480px; overflow-y: auto; }
  .md .list-scroll::-webkit-scrollbar { width: 8px; }
  .md .list-scroll::-webkit-scrollbar-thumb { background: var(--rule); border-radius: 4px; }
  .md .list-scroll::-webkit-scrollbar-thumb:hover { background: var(--faint); }
  .inc-item { padding: 18px 22px; border-bottom: 1px solid var(--rule-2); cursor: pointer; background: transparent; transition: background 0.12s ease; position: relative; }
  .inc-item:hover { background: rgba(29,78,216,0.04); }
  .inc-item.active { background: var(--bg); border-left: 3px solid var(--accent); padding-left: 19px; }
  .inc-item .chev { position: absolute; right: 18px; top: 22px; width: 8px; height: 8px; border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted); transform: rotate(-45deg); transition: transform 0.15s ease; }
  .inc-item.active .chev { transform: rotate(45deg); border-color: var(--accent); }
  .inc-item .row1 { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .inc-item .row1 .id { font-family: "SF Mono",Menlo,monospace; font-size: var(--fs-md); font-weight: 600; color: var(--ink); }
  .inc-item .row2 { font-family: "SF Mono",Menlo,monospace; font-size: var(--fs-sm); color: var(--ink-3); margin-bottom: 10px; }
  .inc-item .row3 { display: flex; gap: 18px; font-size: var(--fs-sm); color: var(--ink-2); flex-wrap: wrap; }
  .inc-item .row3 .lbl { color: var(--muted); margin-right: 5px; font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .inc-item .row3 .val { font-family: "SF Mono",Menlo,monospace; font-variant-numeric: tabular-nums; }
  .detail { padding: 30px 34px; }
  .detail[hidden] { display: none; }
  .detail-empty { padding: 60px 34px; text-align: center; color: var(--muted); font-size: var(--fs-md); line-height: 1.7; }
  .detail-empty .hint { color: var(--faint); font-size: var(--fs-sm); margin-top: 8px; }
  .detail .hd { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; padding-bottom: 18px; border-bottom: 1px solid var(--rule); margin-bottom: 26px; }
  .detail .hd h3 { margin: 0; font-family: "SF Mono",Menlo,monospace; font-size: var(--fs-lg); font-weight: 700; color: var(--ink); }
  .detail .hd .meta { font-family: "SF Mono",Menlo,monospace; font-size: var(--fs-sm); color: var(--muted); }
  .detail .hd .spacer { flex: 1; }
  .stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 24px; margin-bottom: 30px; }
  .stat .k { font-size: var(--fs-xs); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; font-weight: 600; }
  .stat .v { font-family: "SF Mono",Menlo,monospace; font-size: var(--fs-lg); font-weight: 700; color: var(--ink); font-variant-numeric: tabular-nums; }
  .stat .v.red { color: var(--red); }
  .sub-title { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; gap: 12px; flex-wrap: wrap; }
  .sub-title .t { font-size: var(--fs-md); font-weight: 700; color: var(--ink); }
  .sub-title .c { font-size: var(--fs-sm); color: var(--muted); }
  .bars .head, .bars .row { display: grid; grid-template-columns: 72px 1fr 84px 1.4fr; gap: 18px; padding: 10px 0; align-items: center; border-bottom: 1px solid var(--rule-2); font-size: var(--fs-md); }
  .bars .head { font-size: var(--fs-xs); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 700; border-bottom: 1px solid var(--rule); }
  .bars .row:last-child { border-bottom: 0; }
  .bars .row .tid { font-family: "SF Mono",Menlo,monospace; color: var(--muted); }
  .bars .row .name { font-family: "SF Mono",Menlo,monospace; color: var(--ink); overflow: hidden; text-overflow: ellipsis; }
  .bars .row .val { text-align: right; font-family: "SF Mono",Menlo,monospace; font-variant-numeric: tabular-nums; color: var(--ink); font-weight: 600; }
  .bars .row .barwrap { height: 10px; background: var(--rule-2); border-radius: 2px; overflow: visible; position: relative; }
  .bars .row .fill { display: block; height: 100%; background: var(--accent); border-radius: 2px; }
  .bars.mem .row .fill { background: var(--red); opacity: 0.78; }
  .bar-pct { position: absolute; top: 50%; transform: translateY(-50%); font-size: 11px; font-weight: 600; color: var(--ink-3); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .files { display: grid; grid-template-columns: repeat(2, 1fr); gap: 26px; margin-top: 26px; padding-top: 22px; border-top: 1px solid var(--rule); }
  .file .k { font-size: var(--fs-xs); color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; font-weight: 600; }
  .file .v { font-family: "SF Mono",Menlo,monospace; font-size: var(--fs-sm); color: var(--ink-2); word-break: break-all; }

  /* FOOTER ACCORDION */
  details.acc { border: 1px solid var(--rule); border-radius: 6px; margin-bottom: 14px; background: var(--bg); overflow: hidden; }
  details.acc > summary { list-style: none; cursor: pointer; padding: 18px 26px; display: flex; justify-content: space-between; align-items: center; font-size: var(--fs-md); font-weight: 700; color: var(--ink); }
  details.acc > summary::-webkit-details-marker { display: none; }
  details.acc > summary::after { content: ""; width: 9px; height: 9px; border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted); transform: rotate(45deg); transition: transform 0.15s ease; }
  details.acc[open] > summary::after { transform: rotate(-135deg); }
  details.acc > .body { padding: 10px 26px 26px; border-top: 1px solid var(--rule); }
  .cfg-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 38px; padding-top: 18px; }
  .cfg-group .g-title { font-size: var(--fs-xs); color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--rule); font-weight: 700; }
  .cfg-group .kv { display: flex; justify-content: space-between; padding: 9px 0; font-size: var(--fs-md); }
  .cfg-group .kv .k { color: var(--ink-3); }
  .cfg-group .kv .v { font-family: "SF Mono",Menlo,monospace; color: var(--ink); font-weight: 500; }

  /* COLLAPSIBLE SECTION */
  .sec-head.collapsible { cursor: pointer; user-select: none; }
  .sec-head.collapsible .chev { width: 9px; height: 9px; border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted); transform: rotate(-45deg); transition: transform 0.15s ease; margin-left: 4px; }
  .sec-head.collapsible.open .chev { transform: rotate(45deg); }
  .sec-body[hidden] { display: none; }

  /* LANG TOGGLE */
  .lang-toggle { position: fixed; top: 20px; right: 24px; z-index: 50; display: inline-flex; border: 1px solid var(--rule); border-radius: 999px; background: var(--bg); padding: 3px; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }
  .lang-toggle button { appearance: none; border: 0; background: transparent; color: var(--muted); font-family: inherit; font-size: var(--fs-sm); font-weight: 600; padding: 5px 14px; border-radius: 999px; cursor: pointer; transition: background 0.12s ease, color 0.12s ease; }
  .lang-toggle button:hover { color: var(--ink); }
  .lang-toggle button.active { background: var(--ink); color: #fff; }

  /* FOOT */
  .foot { margin-top: 80px; padding-top: 26px; border-top: 1px solid var(--rule); font-size: var(--fs-sm); color: var(--muted); display: flex; justify-content: space-between; gap: 16px; }

  /* POPOVERS */
  .help { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; margin-left: 6px; border: 1px solid var(--rule); border-radius: 999px; background: var(--bg); color: var(--muted); font-size: 11px; font-weight: 700; line-height: 1; cursor: pointer; vertical-align: middle; transition: all 0.12s ease; user-select: none; }
  .help:hover { color: var(--ink); border-color: var(--muted); }
  .help.is-open { color: #fff; background: var(--ink); border-color: var(--ink); }
  .popover { position: absolute; z-index: 100; max-width: 360px; padding: 12px 14px; background: #fff; color: var(--ink); border: 1px solid var(--rule); border-radius: 4px; box-shadow: 0 4px 12px rgba(15,23,42,0.12); font-size: var(--fs-sm); line-height: 1.55; pointer-events: auto; }
  .popover[hidden] { display: none; }
  .popover .pop-title { font-weight: 700; font-size: var(--fs-md); margin-bottom: 6px; color: var(--ink); word-break: break-word; overflow-wrap: break-word; }
  .popover .pop-body { color: var(--ink-2); }
  .popover .pop-body .row { display: flex; gap: 8px; margin-top: 4px; }
  .popover .pop-body .row .lbl { color: var(--muted); min-width: 76px; flex-shrink: 0; }
  .popover .pop-body .row .mono { min-width: 0; color: var(--ink); word-break: break-all; overflow-wrap: break-word; }
  .popover::before { content: ""; position: absolute; width: 10px; height: 10px; background: #fff; border-left: 1px solid var(--rule); border-top: 1px solid var(--rule); transform: rotate(45deg); left: 20px; top: -6px; }
"""

# ── JS ───────────────────────────────────────────────────────────────────────
_JS = """\
  (function () {
    const KEY = 'perf-report-lang';
    const root = document.documentElement;
    const buttons = document.querySelectorAll('.lang-toggle button[data-lang-btn]');
    function setLang(lang) {
      root.setAttribute('data-lang', lang);
      try { localStorage.setItem(KEY, lang); } catch (e) {}
      buttons.forEach(b => b.classList.toggle('active', b.getAttribute('data-lang-btn') === lang));
    }
    let initial = 'zh';
    try { initial = localStorage.getItem(KEY) || root.getAttribute('data-lang') || 'zh'; } catch (e) {}
    setLang(initial);
    buttons.forEach(b => { b.addEventListener('click', () => setLang(b.getAttribute('data-lang-btn'))); });
  })();

  (function () {
    const items = document.querySelectorAll('.inc-item[data-incident]');
    const details = document.querySelectorAll('.detail[data-incident-detail]');
    const empty = document.getElementById('detail-empty');
    function showDetail(id) {
      let any = false;
      details.forEach(d => { const match = d.getAttribute('data-incident-detail') === id; d.hidden = !match; if (match) any = true; });
      items.forEach(it => { it.classList.toggle('active', it.getAttribute('data-incident') === id); });
      if (empty) empty.hidden = any;
    }
    function collapseAll() {
      details.forEach(d => { d.hidden = true; });
      items.forEach(it => it.classList.remove('active'));
      if (empty) empty.hidden = false;
    }
    collapseAll();
    items.forEach(it => {
      it.addEventListener('click', () => {
        const id = it.getAttribute('data-incident');
        if (it.classList.contains('active')) collapseAll(); else showDetail(id);
      });
    });
    document.addEventListener('keydown', (e) => {
      const active = document.querySelector('.inc-item.active');
      if (!active) return;
      const arr = Array.from(items);
      const idx = arr.indexOf(active);
      if (e.key === 'ArrowDown' && idx < arr.length - 1) { e.preventDefault(); showDetail(arr[idx + 1].getAttribute('data-incident')); }
      else if (e.key === 'ArrowUp' && idx > 0) { e.preventDefault(); showDetail(arr[idx - 1].getAttribute('data-incident')); }
      else if (e.key === 'Escape') collapseAll();
    });

    ['sec-additional'].forEach(function(headId) {
      const head = document.getElementById(headId);
      const body = document.getElementById(headId + '-body');
      if (!head || !body) return;
      head.addEventListener('click', () => {
        const open = !body.hidden;
        body.hidden = open;
        head.classList.toggle('open', !open);
      });
    });

    const host = document.getElementById('popover-host');
    const templates = {};
    document.querySelectorAll('#popovers template[data-popover-id]').forEach(t => {
      templates[t.getAttribute('data-popover-id')] = t.innerHTML;
    });
    let currentTrigger = null;
    function placeNear(el) {
      const rect = el.getBoundingClientRect();
      const scrollX = window.scrollX || window.pageXOffset;
      const scrollY = window.scrollY || window.pageYOffset;
      host.style.top = (rect.bottom + scrollY + 10) + 'px';
      const left = rect.left + scrollX - 18;
      const maxLeft = scrollX + document.documentElement.clientWidth - host.offsetWidth - 16;
      host.style.left = Math.min(left, maxLeft) + 'px';
    }
    function openPop(id, trigger) {
      const html = templates[id];
      if (!html) return;
      host.innerHTML = html;
      host.hidden = false;
      currentTrigger = trigger;
      document.querySelectorAll('.help.is-open').forEach(e => e.classList.remove('is-open'));
      if (trigger.classList && trigger.classList.contains('help')) trigger.classList.add('is-open');
      placeNear(trigger);
    }
    function closePop() {
      host.hidden = true;
      host.innerHTML = '';
      document.querySelectorAll('.help.is-open').forEach(e => e.classList.remove('is-open'));
      currentTrigger = null;
    }
    document.addEventListener('click', (e) => {
      const trigger = e.target.closest('[data-popover]');
      if (trigger) {
        const id = trigger.getAttribute('data-popover');
        if (currentTrigger === trigger) closePop(); else openPop(id, trigger);
        e.stopPropagation(); return;
      }
      if (!host.hidden && !host.contains(e.target)) closePop();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !host.hidden) closePop();
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[data-popover]')) { e.preventDefault(); e.target.click(); }
    });
    window.addEventListener('resize', closePop);
    window.addEventListener('scroll', () => { if (!host.hidden && currentTrigger) placeNear(currentTrigger); }, { passive: true });
  })();
"""


# ── helpers ──────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return _html.escape(str(s) if s is not None else "—")


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _fmt_hms(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    sec = int(round(sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _fmt_ts(ts: Optional[str], fmt: str = "%H:%M:%S") -> str:
    dt = _parse_ts(ts)
    if dt is None:
        return "—"
    return dt.strftime(fmt)


def _fmt_ts_ms(ts: Optional[str]) -> str:
    dt = _parse_ts(ts)
    if dt is None:
        return "—"
    return dt.strftime("%H:%M:%S") + f".{dt.microsecond // 1000:03d}"


def _rail_x(ts: Optional[str], t0: datetime, duration_sec: float, width: float = 1200.0) -> float:
    if not ts or duration_sec <= 0:
        return 0.0
    dt = _parse_ts(ts)
    if dt is None:
        return 0.0
    offset = (dt - t0).total_seconds()
    return max(0.0, min(width, offset / duration_sec * width))


def _proc_colors(names: List[str]) -> Dict[str, str]:
    return {n: _PROC_COLORS[i % len(_PROC_COLORS)] for i, n in enumerate(sorted(names))}


def _zh_en(zh: str, en: str) -> str:
    return f'<span class="zh">{zh}</span><span class="en">{en}</span>'


def _chip(content: str, cls: str = "") -> str:
    return f'<span class="chip {cls}">{content}</span>'


def _fmt_gap(gap_sec: float) -> str:
    if gap_sec <= 0:
        return "—"
    if gap_sec < 60:
        return f"{gap_sec:.1f} s"
    return _fmt_hms(gap_sec)


# ── CSV reading (unchanged) ───────────────────────────────────────────────────

def _read_csvs(paths: List[Path]) -> pd.DataFrame:
    dfs = []
    for p in paths:
        try:
            df = pd.read_csv(p, comment="#")
        except (pd.errors.EmptyDataError, FileNotFoundError):
            continue
        except pd.errors.ParserError:
            continue
        if df.empty:
            continue
        dfs.append(df)
    if not dfs:
        return pd.DataFrame()
    out = pd.concat(dfs, ignore_index=True)
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        out = out.dropna(subset=["timestamp"])
    return out


def _downsample(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Time-window max-aggregation. Preserves peaks; no-ops when points <= target."""
    if len(df) <= _TARGET_POINTS:
        return df
    total_sec = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds()
    window_sec = max(1, int(total_sec / _TARGET_POINTS))
    agg: dict = {col: "max"}
    for extra in ("process_name", "pid"):
        if extra in df.columns:
            agg[extra] = "first"
    return (
        df.set_index("timestamp")
        .resample(f"{window_sec}s")
        .agg(agg)
        .dropna(subset=[col])
        .reset_index()
    )


# ── section builders ──────────────────────────────────────────────────────────

def _build_header(result: dict) -> str:
    run = result.get("run", {})
    package = run.get("package", "?")
    app_name = run.get("app_name") or package
    device = run.get("device", {})
    serial = device.get("serial", "?")
    android_ver = device.get("android_version", "?")
    sdk = device.get("sdk_int", "?")
    cores = device.get("cpu_cores", "?")
    started = run.get("started_at", "")
    ended = run.get("ended_at", "")
    duration_sec = run.get("duration_sec", 0)
    exit_code = run.get("exit_code", 0)
    exit_reason = run.get("exit_reason", "")

    started_fmt = _fmt_ts(started, "%Y-%m-%d %H:%M:%S")
    ended_fmt = _fmt_ts(ended, "%H:%M:%S")
    dur_fmt = _fmt_hms(duration_sec)

    total_incidents = len(result.get("incidents", []))

    reason_zh_map = {
        "duration_elapsed": "正常结束 · 时长跑完",
        "setup_failed": "启动失败",
        "wait_timeout": "等待进程超时",
        "exception": "异常终止",
    }
    reason_zh = reason_zh_map.get(exit_reason, exit_reason)
    pill_cls = "" if exit_code == 0 else "red"

    return f"""\
  <header class="doc-head">
    <div class="title-block">
      <div class="eyebrow">{_zh_en('性能自动化测试报告', 'PERFORMANCE TEST REPORT')}</div>
      <h1>{_e(app_name)}</h1>
      <div class="meta">
        <span class="mono">{_e(serial)}</span>
        <span>Android {_e(android_ver)} (SDK {_e(sdk)})</span>
        <span>{_zh_en(f'{_e(cores)} 核', f'{_e(cores)} cores')}</span>
        <span class="mono">{_e(started_fmt)} → {_e(ended_fmt)}</span>
        <span>{_e(dur_fmt)}</span>
      </div>
    </div>
    <div class="verdict">
      <span class="pill {pill_cls}" title="run.exit_reason">
        <span class="zh">{_e(reason_zh)}</span>
        <span class="en">{_e(exit_reason)}</span>
      </span>
      <span class="micro">
        <span class="zh">exit_code = {exit_code} · {total_incidents} 个告警</span>
        <span class="en">exit_code = {exit_code} · {total_incidents} incident(s)</span>
      </span>
    </div>
  </header>"""


def _build_kpis(result: dict) -> str:
    run = result.get("run", {})
    cfg = run.get("config_effective", {})
    processes = result.get("processes", [])
    incidents = result.get("incidents", [])
    lifecycle = result.get("lifecycle_events", [])
    duration_sec = run.get("duration_sec", 0)

    proc_count = len(processes)
    restart_count = sum(p.get("restart_count", 0) for p in processes)
    gone_count = sum(1 for e in lifecycle if e.get("event") == "gone")

    # CPU peak across all processes
    cpu_peak = None
    cpu_peak_proc = None
    for p in processes:
        s = (p.get("stats") or {}).get("cpu_pct") or {}
        v = s.get("max")
        if v is not None and (cpu_peak is None or v > cpu_peak):
            cpu_peak = v
            cpu_peak_proc = p.get("name", "")

    # CPU p95 of main process (shortest name = main process)
    main_proc = None
    if processes:
        main_proc = min(processes, key=lambda p: len(p.get("name", "")))
    cpu_p95 = None
    cpu_p95_samples = None
    if main_proc:
        s = (main_proc.get("stats") or {}).get("cpu_pct") or {}
        cpu_p95 = s.get("p95")
        cpu_p95_samples = s.get("samples")

    # Mem peak — track which process has the highest memory
    mem_peak = None
    mem_peak_proc = None
    for p in processes:
        s = (p.get("stats") or {}).get("mem_pss_mb") or {}
        v = s.get("max")
        if v is not None and (mem_peak is None or v > mem_peak):
            mem_peak = v
            mem_peak_proc = p.get("name", "")

    cpu_thr = cfg.get("cpu_threshold_percent")
    mem_thr = cfg.get("mem_threshold_pss_mb")

    cpu_alerts = sum(1 for i in incidents if i.get("type") == "cpu_threshold")
    mem_alerts = sum(1 for i in incidents if i.get("type") == "mem_threshold")
    total_alerts = len(incidents)

    lc_new = sum(1 for e in lifecycle if e.get("event") == "new")
    lc_restart = sum(1 for e in lifecycle if e.get("event") == "restart")
    lc_gone = sum(1 for e in lifecycle if e.get("event") == "gone")

    cpu_peak_str = f"{cpu_peak:.1f}" if cpu_peak is not None else "—"
    cpu_p95_str = f"{cpu_p95:.1f}" if cpu_p95 is not None else "—"
    mem_peak_str = f"{mem_peak:.1f}" if mem_peak is not None else "—"
    cpu_thr_str = f"{cpu_thr:.0f} %" if cpu_thr is not None else "—"
    mem_thr_str = f"{mem_thr:.0f} MB" if mem_thr is not None else "—"

    def _proc_short(name: str) -> str:
        if not name:
            return "—"
        return f":{name.split(':')[-1]}" if ":" in name else name.split(".")[-1]

    # Per-process breakdowns for delta lines. The main process value is already
    # shown in the KPI's .v above, so we exclude it here and prefix the rest
    # with a "子进程：" header. Returns pre-escaped HTML with <br> per process.
    main_proc_name = main_proc.get("name", "") if main_proc else ""
    sub_header = _zh_en("子进程：", "Sub-processes:")

    def _per_proc_delta(stat_path: tuple, fmt: str) -> str:
        parts = []
        for p in sorted(processes,
                        key=lambda p: (((p.get("stats") or {}).get(stat_path[0]) or {}).get(stat_path[1]) or 0),
                        reverse=True):
            name = p.get("name", "")
            if name == main_proc_name:
                continue
            v = ((p.get("stats") or {}).get(stat_path[0]) or {}).get(stat_path[1])
            if v is not None:
                parts.append(_e(f'{_proc_short(name)} {fmt % v}'))
        if not parts:
            return "—"
        return sub_header + "<br>" + "<br>".join(parts)

    cpu_peak_delta = _per_proc_delta(("cpu_pct", "max"), "%.0f%%")
    cpu_p95_delta  = _per_proc_delta(("cpu_pct", "p95"), "%.0f%%")
    mem_peak_delta = _per_proc_delta(("mem_pss_mb", "max"), "%.0f MB")

    cpu_alert_cls = "alert" if cpu_peak is not None and cpu_thr is not None and cpu_peak > cpu_thr else ""
    mem_alert_cls = "alert" if mem_peak is not None and mem_thr is not None and mem_peak > mem_thr else ""
    inc_cls = "alert" if total_alerts > 0 else ""

    return f"""\
  <section style="margin-top: 40px;">
    <div class="kpis">
      <div class="kpi">
        <div class="k">{_zh_en('进程数', 'Processes')}</div>
        <div class="v">{proc_count}<span class="u">{_zh_en('已监控', 'monitored')}</span></div>
        <div class="delta">{_zh_en(f'{restart_count} 次重启 · {gone_count} 次消失', f'{restart_count} restart · {gone_count} gone')}</div>
      </div>
      <div class="kpi {cpu_alert_cls}">
        <div class="k">{_zh_en('CPU 峰值', 'CPU peak')}</div>
        <div class="v">{_e(cpu_peak_str)}<span class="u">%</span></div>
        <div class="delta">{cpu_peak_delta}</div>
      </div>
      <div class="kpi">
        <div class="k">
          {_zh_en('CPU p95', 'CPU p95')}
          <span class="help" data-popover="help-p95" tabindex="0" aria-label="what is p95">?</span>
        </div>
        <div class="v">{_e(cpu_p95_str)}<span class="u">%</span></div>
        <div class="delta">{cpu_p95_delta}</div>
      </div>
      <div class="kpi {mem_alert_cls}">
        <div class="k">{_zh_en('内存峰值', 'Mem peak')}</div>
        <div class="v">{_e(mem_peak_str)}<span class="u">MB</span></div>
        <div class="delta">{mem_peak_delta}</div>
      </div>
      <div class="kpi {inc_cls}">
        <div class="k">{_zh_en('告警事件', 'Incidents')}</div>
        <div class="v">{total_alerts}<span class="u">{_zh_en('次', 'events')}</span></div>
        <div class="delta">{_zh_en(f'内存 {mem_alerts} · CPU {cpu_alerts}', f'cpu {cpu_alerts} · mem {mem_alerts}')}</div>
      </div>
      <div class="kpi">
        <div class="k">
          {_zh_en('生命周期', 'Lifecycle')}
          <span class="help" data-popover="help-lifecycle" tabindex="0" aria-label="what are lifecycle events">?</span>
        </div>
        <div class="v">{len(lifecycle)}<span class="u">{_zh_en('事件', 'events')}</span></div>
        <div class="delta">{_zh_en(f'新增 {lc_new} · 重启 {lc_restart} · 消失 {lc_gone}', f'new {lc_new} · restart {lc_restart} · gone {lc_gone}')}</div>
      </div>
    </div>
  </section>"""


def _build_timeline(result: dict) -> str:
    run = result.get("run", {})
    t0 = _parse_ts(run.get("started_at", ""))
    duration_sec = run.get("duration_sec", 0)
    if t0 is None or duration_sec <= 0:
        return ""

    incidents = result.get("incidents", [])
    lifecycle = result.get("lifecycle_events", [])
    bookmarks = result.get("bookmarks", [])

    t_end = _parse_ts(run.get("ended_at", ""))
    span_str = f"{_fmt_ts(run.get('started_at'), '%H:%M:%S')}  —  {_fmt_hms(duration_sec)}  —  {_fmt_ts(run.get('ended_at'), '%H:%M:%S')}"

    # SVG elements
    svg_parts = [
        '<line x1="0" y1="48" x2="1200" y2="48" stroke="#d1d5db" stroke-width="1"/>',
        '<g stroke="#9ca3af" stroke-width="1">',
    ]
    # 11 tick marks
    for i in range(11):
        x = i * 120
        svg_parts.append(f'<line x1="{x}" y1="46" x2="{x}" y2="50"/>')
    svg_parts.append('</g>')

    # lifecycle dots — clamp x so dots don't clip at SVG edges
    # Stack dots vertically when multiple events share the same x pixel position.
    ev_color = {"new": "#15803d", "restart": "#c2410c", "gone": "#4b5563"}
    x_slots: dict = {}
    for i, ev in enumerate(lifecycle):
        x = max(6.0, min(1194.0, _rail_x(ev.get("timestamp"), t0, duration_sec)))
        x_key = int(x)
        slot = x_slots.get(x_key, 0)
        x_slots[x_key] = slot + 1
        cy = 30 - slot * 12
        color = ev_color.get(ev.get("event", ""), "#4b5563")
        svg_parts.append(
            f'<circle cx="{x:.1f}" cy="{cy}" r="5" fill="{color}" '
            f'data-popover="lc-{i}" style="cursor:pointer"/>'
        )

    # incident X marks — stagger text labels vertically when incidents are close
    _INC_TEXT_Y = [11, 22]   # two alternating y levels (baseline px from SVG top)
    _INC_TEXT_MIN_X = 70     # estimated text width + buffer to detect overlap
    _inc_placed: list = []   # (x, text_y) of already-placed labels

    for inc in incidents:
        x = max(8.0, min(1192.0, _rail_x(inc.get("triggered_at"), t0, duration_sec)))
        inc_id = inc.get("id", "")
        # Pick the first y level that has no horizontal conflict with placed labels
        text_y = _INC_TEXT_Y[-1]
        for ty in _INC_TEXT_Y:
            if not any(abs(px - x) < _INC_TEXT_MIN_X and py == ty
                       for px, py in _inc_placed):
                text_y = ty
                break
        _inc_placed.append((x, text_y))
        svg_parts.append(
            f'<g transform="translate({x:.1f},30) rotate(45)" '
            f'data-popover="{_e(inc_id)}" style="cursor:pointer">'
            f'<rect x="-7" y="-1.3" width="14" height="2.6" fill="#b91c1c"/>'
            f'<rect x="-1.3" y="-7" width="2.6" height="14" fill="#b91c1c"/>'
            f'</g>'
        )
        # Flip text anchor near the right edge so label stays within SVG bounds.
        # "incident-009" is ~70px wide at font-size 11; flip when x+8+70 > 1200.
        if x > 1120:
            txt_x, anchor = x - 8, "end"
        else:
            txt_x, anchor = x + 8, "start"
        svg_parts.append(
            f'<text x="{txt_x:.1f}" y="{text_y}" text-anchor="{anchor}" '
            f'font-family="-apple-system,sans-serif" '
            f'font-size="11" font-weight="600" fill="#b91c1c" style="pointer-events:none">{_e(inc_id)}</text>'
        )

    # bookmark lines
    for bm in bookmarks:
        x = _rail_x(bm.get("timestamp"), t0, duration_sec)
        label = bm.get("label", "")
        svg_parts.append(f'<line x1="{x:.1f}" y1="10" x2="{x:.1f}" y2="64" stroke="#1d4ed8" stroke-width="1.5"/>')
        svg_parts.append(
            f'<text x="{x+4:.1f}" y="18" font-family="-apple-system,sans-serif" '
            f'font-size="11" font-weight="600" fill="#1d4ed8">{_e(label)}</text>'
        )

    svg_inner = "\n        ".join(svg_parts)

    # Axis labels: 11 evenly spaced timestamps
    axis_labels = []
    for i in range(11):
        frac = i / 10.0
        offset_sec = frac * duration_sec
        import datetime as _dt_mod
        ts_label = (t0 + _dt_mod.timedelta(seconds=offset_sec)).strftime("%H:%M")
        axis_labels.append(f'<span>{_e(ts_label)}</span>')
    axis_str = "".join(axis_labels)

    return f"""\
  <section>
    <div class="sec-head" id="sec-timeline">
      <h2>{_zh_en('运行时间轴', 'Run timeline')}</h2>
      <span class="num-tag">01</span>
      <span class="desc">{_zh_en(f'{_e(_fmt_hms(duration_sec))} · 事件概览', f'{_e(_fmt_hms(duration_sec))} · event overview')}</span>
    </div>
    <div class="sec-body" id="sec-timeline-body">
    <div class="rail">
      <div class="rail-head">
        <span class="span">{_e(span_str)}</span>
        <div class="legend">
          <span class="item"><span class="sw x"></span>{_zh_en('告警', 'incident')}</span>
          <span class="item"><span class="sw g"></span>{_zh_en('进程新增', 'new')}</span>
          <span class="item"><span class="sw o"></span>{_zh_en('进程重启', 'restart')}</span>
          <span class="item"><span class="sw gr"></span>{_zh_en('进程消失', 'gone')}</span>
          {'<span class="item"><span class="sw b"></span>' + _zh_en('书签', 'bookmark') + '</span>' if bookmarks else ''}
        </div>
      </div>
      <svg class="rail-svg" viewBox="0 0 1200 70" preserveAspectRatio="none">
        {svg_inner}
      </svg>
      <div class="rail-axis">
        {axis_str}
      </div>
    </div>
    </div>
  </section>"""


def _build_process_tables(result: dict) -> str:
    run = result.get("run", {})
    cfg = run.get("config_effective", {})
    processes = result.get("processes", [])
    duration_sec = run.get("duration_sec", 0)

    cpu_thr = cfg.get("cpu_threshold_percent")
    mem_thr = cfg.get("mem_threshold_pss_mb")
    cpu_interval = cfg.get("cpu_interval_sec", 1)
    mem_interval = cfg.get("mem_interval_sec", 5)
    cores = run.get("device", {}).get("cpu_cores", "?")

    proc_count = len(processes)
    dur_str = _fmt_hms(duration_sec)

    # Meta table rows
    meta_rows = []
    for p in processes:
        name = p.get("name", "?")
        first = _fmt_ts(p.get("first_seen_at"), "%H:%M:%S")
        last = _fmt_ts(p.get("last_seen_at"), "%H:%M:%S")
        uptime = p.get("uptime_ratio", 0)
        uptime_pct = uptime * 100
        uptime_cls = "v-orange" if uptime_pct < 90 else ""
        restarts = p.get("restart_count", 0)
        restart_cls = "orange" if restarts > 0 else "gray"
        alerts = p.get("alerts", {})
        cpu_al = alerts.get("cpu", 0)
        mem_al = alerts.get("mem", 0)
        alert_chips = []
        if cpu_al > 0:
            alert_chips.append(_chip(f"cpu {cpu_al}", "red"))
        if mem_al > 0:
            alert_chips.append(_chip(f"mem {mem_al}", "orange"))
        alert_html = " ".join(alert_chips) if alert_chips else _chip("0", "gray")
        restart_html = _chip(str(restarts), restart_cls) if restarts > 0 else str(restarts)
        meta_rows.append(f"""\
            <tr>
              <td class="mono">{_e(name)}</td>
              <td class="mono">{_e(first)}</td>
              <td class="mono">{_e(last)}</td>
              <td class="r {''+uptime_cls}">{uptime_pct:.1f} %</td>
              <td class="r">{restart_html}</td>
              <td class="r">{alert_html}</td>
            </tr>""")

    # CPU stats table rows
    cpu_rows = []
    for p in processes:
        name = p.get("name", "?")
        s = (p.get("stats") or {}).get("cpu_pct") or {}
        if not s:
            cpu_rows.append(f'<tr><td class="mono">{_e(name)}</td>' + '<td class="r">—</td>' * 6 + '</tr>')
            continue
        mx = s.get("max", 0)
        mx_cls = "v-red" if cpu_thr is not None and mx > cpu_thr else ""
        cpu_rows.append(f"""\
            <tr>
              <td class="mono">{_e(name)}</td>
              <td class="r">{s.get('mean', 0):.1f} %</td>
              <td class="r">{s.get('p50', 0):.1f} %</td>
              <td class="r">{s.get('p90', 0):.1f} %</td>
              <td class="r">{s.get('p95', 0):.1f} %</td>
              <td class="r {mx_cls}">{mx:.1f} %</td>
              <td class="r">{s.get('samples', 0)}</td>
            </tr>""")

    # Mem stats table rows
    mem_rows = []
    for p in processes:
        name = p.get("name", "?")
        s = (p.get("stats") or {}).get("mem_pss_mb") or {}
        if not s:
            mem_rows.append(f'<tr><td class="mono">{_e(name)}</td>' + '<td class="r">—</td>' * 6 + '</tr>')
            continue
        mx = s.get("max", 0)
        mx_cls = "v-red" if mem_thr is not None and mx > mem_thr else ""
        mem_rows.append(f"""\
            <tr>
              <td class="mono">{_e(name)}</td>
              <td class="r">{s.get('mean', 0):.1f}</td>
              <td class="r">{s.get('p50', 0):.1f}</td>
              <td class="r">{s.get('p90', 0):.1f}</td>
              <td class="r">{s.get('p95', 0):.1f}</td>
              <td class="r {mx_cls}">{mx:.1f}</td>
              <td class="r">{s.get('samples', 0)}</td>
            </tr>""")

    cpu_thr_label = f"{cpu_thr:.0f} %" if cpu_thr is not None else "—"
    mem_thr_label = f"{mem_thr:.0f} MB" if mem_thr is not None else "—"

    return f"""\
  <section>
    <div class="sec-head">
      <h2>{_zh_en('进程概览', 'Process overview')}</h2>
      <span class="num-tag">02</span>
      <span class="desc">{_zh_en('超阈值以红色显示 · 在线率 &lt; 90 % 以橙色显示', 'over threshold in red · uptime &lt; 90 % in orange')}</span>
    </div>

    <div class="sub-title" style="margin: 0 0 12px;">
      <span class="t">{_zh_en('基本信息', 'Process meta')}</span>
      <span class="c">{_zh_en(f'{proc_count} 个进程 · 总监控时长 {_e(dur_str)}', f'{proc_count} processes · run {_e(dur_str)}')}</span>
    </div>
    <div class="table-wrap">
      <div class="table-scroll">
        <table class="tbl">
          <thead><tr>
            <th>{_zh_en('进程', 'Process')}</th>
            <th>{_zh_en('首次发现', 'First seen')}</th>
            <th>{_zh_en('最后发现', 'Last seen')}</th>
            <th class="r">{_zh_en('在线率', 'Uptime')}</th>
            <th class="r">{_zh_en('进程重启', 'Restart')}</th>
            <th class="r">{_zh_en('告警', 'Alerts')}</th>
          </tr></thead>
          <tbody>
            {''.join(meta_rows)}
          </tbody>
        </table>
      </div>
    </div>

    <div class="sub-title" style="margin: 28px 0 12px;">
      <span class="t">{_zh_en('CPU 统计', 'CPU stats')}</span>
      <span class="c">{_zh_en(f'单核归一化百分比 ({_e(cores)} 核满载 = {int(cores)*100 if str(cores).isdigit() else "N×100"} %) · 阈值 {_e(cpu_thr_label)}', f'single-core normalised % · threshold {_e(cpu_thr_label)}')}</span>
    </div>
    <div class="table-wrap">
      <div class="table-scroll">
        <table class="tbl">
          <thead><tr>
            <th>{_zh_en('进程', 'Process')}</th>
            <th class="r">{_zh_en('均值', 'Mean')}</th>
            <th class="r">p50</th>
            <th class="r">p90</th>
            <th class="r">p95</th>
            <th class="r">{_zh_en('最大', 'Max')}</th>
            <th class="r">{_zh_en('样本数', 'N')}</th>
          </tr></thead>
          <tbody>{''.join(cpu_rows)}</tbody>
        </table>
      </div>
    </div>

    <div class="sub-title" style="margin: 28px 0 12px;">
      <span class="t">{_zh_en('内存统计', 'Memory stats')}</span>
      <span class="c">{_zh_en(f'PSS · 单位 MB · 阈值 {_e(mem_thr_label)}', f'PSS · unit MB · threshold {_e(mem_thr_label)}')}</span>
    </div>
    <div class="table-wrap">
      <div class="table-scroll">
        <table class="tbl">
          <thead><tr>
            <th>{_zh_en('进程', 'Process')}</th>
            <th class="r">{_zh_en('均值', 'Mean')}</th>
            <th class="r">p50</th>
            <th class="r">p90</th>
            <th class="r">p95</th>
            <th class="r">{_zh_en('最大', 'Max')}</th>
            <th class="r">{_zh_en('样本数', 'N')}</th>
          </tr></thead>
          <tbody>{''.join(mem_rows)}</tbody>
        </table>
      </div>
    </div>
  </section>"""


def _build_charts(result: dict, output_dir: Path) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    output_dir = Path(output_dir)
    run = result.get("run", {})
    cfg = run.get("config_effective", {})
    cpu_thr = cfg.get("cpu_threshold_percent")
    mem_thr = cfg.get("mem_threshold_pss_mb")

    cpu_df = _read_csvs(sorted(output_dir.glob("cpu_*.csv")))
    mem_df = _read_csvs(sorted(output_dir.glob("mem_*.csv")))

    processes = result.get("processes", [])
    proc_names = sorted(set(p.get("name", "") for p in processes))
    colors = _proc_colors(proc_names)

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.5, 0.5],
        subplot_titles=("CPU %", "Memory PSS (MB)"),
        vertical_spacing=0.10,
    )

    if not cpu_df.empty and "process_name" in cpu_df.columns:
        for name in proc_names:
            sub = _downsample(cpu_df[cpu_df["process_name"] == name].copy(), "cpu_pct")
            if sub.empty:
                continue
            fig.add_trace(go.Scattergl(
                x=sub["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
                y=sub["cpu_pct"].tolist(),
                mode="lines", name=name,
                line=dict(color=colors.get(name, "#1d4ed8"), width=1.5),
                legendgroup="cpu", legendgrouptitle_text="CPU",
            ), row=1, col=1)

    if cpu_thr is not None:
        fig.add_hline(y=cpu_thr, line_dash="dash", line_color="#b91c1c",
                      annotation_text=f"threshold {cpu_thr} %",
                      annotation_position="top right", row=1, col=1)

    if not mem_df.empty and "process_name" in mem_df.columns:
        for name in proc_names:
            sub = _downsample(mem_df[mem_df["process_name"] == name].copy(), "pss_mb")
            if sub.empty:
                continue
            fig.add_trace(go.Scattergl(
                x=sub["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
                y=sub["pss_mb"].tolist(),
                mode="lines", name=name,
                line=dict(color=colors.get(name, "#1d4ed8"), width=1.5),
                legendgroup="mem", legendgrouptitle_text="Memory",
                showlegend=False,
            ), row=2, col=1)

    if mem_thr is not None:
        fig.add_hline(y=mem_thr, line_dash="dash", line_color="#b91c1c",
                      annotation_text=f"threshold {mem_thr} MB",
                      annotation_position="top right", row=2, col=1)

    for inc in result.get("incidents", []):
        row = 1 if inc.get("type") == "cpu_threshold" else 2
        x = inc.get("triggered_at")
        obs = inc.get("observed", {})
        y = obs.get("value_at_trigger", 0)
        fig.add_trace(go.Scattergl(
            x=[x], y=[y], mode="markers",
            marker=dict(symbol="x", size=14, color="#b91c1c", line=dict(width=2, color="#b91c1c")),
            name=inc.get("id", ""), showlegend=False,
            hoverinfo="skip",
        ), row=row, col=1)

    for ev in result.get("lifecycle_events", []):
        if ev.get("event") == "restart":
            fig.add_vline(x=ev.get("timestamp"), line_dash="dot",
                          line_color="#c2410c", opacity=0.4, row=1, col=1)
            fig.add_vline(x=ev.get("timestamp"), line_dash="dot",
                          line_color="#c2410c", opacity=0.4, row=2, col=1)

    for bm in result.get("bookmarks", []):
        for r in (1, 2):
            fig.add_vline(x=bm.get("timestamp"), line_dash="solid",
                          line_color="#1d4ed8", opacity=0.5, row=r, col=1)

    fig.update_layout(
        height=560,
        hovermode="x unified",
        hoverlabel=dict(namelength=-1),
        legend=dict(orientation="h", y=-0.04),
        margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"),
    )
    fig.update_yaxes(title_text="CPU %", row=1, col=1, gridcolor="#e5e7eb")
    fig.update_yaxes(title_text="MB", row=2, col=1, gridcolor="#e5e7eb")
    fig.update_xaxes(gridcolor="#e5e7eb")

    plotly_div = fig.to_html(full_html=False, include_plotlyjs=False)

    return f"""\
  <section>
    <div class="sec-head">
      <h2>{_zh_en('时序图表', 'Time series')}</h2>
      <span class="num-tag">03</span>
      <span class="desc">{_zh_en('2 个子图 · 共享 X 轴 · WebGL 渲染', '2 sub-plots · shared X axis · ScatterGL')}</span>
    </div>
    <div class="charts">
      <div class="chart-plotly">
        {plotly_div}
      </div>
    </div>
  </section>"""


def _build_incident_detail_cpu(inc: dict) -> str:
    inc_id = inc.get("id", "?")
    process = inc.get("process", "?")
    pid = inc.get("pid", "?")
    triggered = _fmt_ts(inc.get("triggered_at"), "%Y-%m-%d %H:%M:%S UTC")
    obs = inc.get("observed", {})
    thr = inc.get("threshold", {})
    ev = inc.get("evidence", {})

    val_trigger = obs.get("value_at_trigger", 0)
    peak = obs.get("peak", 0)
    dur_above = obs.get("duration_above_sec", 0)
    cooldown = thr.get("cooldown_sec", 0)
    sustain = thr.get("sustain_sec", 0)
    thr_val = thr.get("value", 0)

    top_threads = ev.get("top_threads", [])
    thread_count = ev.get("top_threads_count", len(top_threads))
    raw_file = ev.get("raw_file", "")
    task_file = ev.get("task_stat_file", "")

    # Thread bars: width relative to value_at_trigger
    denom = val_trigger if val_trigger > 0 else max((t.get("cpu_pct", 0) for t in top_threads), default=1)
    thread_rows = []
    for t in top_threads:
        cpu = t.get("cpu_pct", 0)
        pct = min(100, cpu / denom * 100) if denom > 0 else 0
        thread_rows.append(f"""\
          <div class="row">
            <span class="tid">{_e(t.get('tid', ''))}</span>
            <span class="name">{_e(t.get('name', ''))}</span>
            <span class="val">{cpu:.1f}</span>
            <span class="barwrap">
              <span class="fill" style="width:{pct:.1f}%;"></span>
              <span class="bar-pct" style="left:calc({pct:.1f}% + 4px)">{pct:.0f}%</span>
            </span>
          </div>""")

    shown = len(top_threads)
    files_html = ""
    if raw_file or task_file:
        f1 = f'<div class="file"><div class="k">{_zh_en("原始 · top -H", "Raw · top -H")}</div><div class="v">{_e("incidents/" + raw_file)}</div></div>' if raw_file else ""
        f2 = f'<div class="file"><div class="k">{_zh_en("Task 统计", "Task stat")}</div><div class="v">{_e("incidents/" + task_file)}</div></div>' if task_file else ""
        files_html = f'<div class="files">{f1}{f2}</div>'

    return f"""\
      <div class="detail" data-incident-detail="{_e(inc_id)}" hidden>
        <div class="hd">
          <h3>{_e(inc_id)}</h3>
          {_chip(_zh_en('cpu_threshold', 'cpu_threshold'), 'red')}
          <span class="meta">{_e(process)} · pid {_e(pid)} · {_e(triggered)}</span>
          <span class="spacer"></span>
          <span class="chip">{_zh_en(f'阈值 &gt; {thr_val:.0f} % 持续 {sustain:.0f} s', f'threshold &gt; {thr_val:.0f} % for {sustain:.0f} s')}</span>
        </div>
        <div class="stat-row">
          <div class="stat"><div class="k">{_zh_en('触发值', 'Value at trigger')}</div><div class="v">{val_trigger:.1f} %</div></div>
          <div class="stat"><div class="k">{_zh_en('峰值', 'Peak')}</div><div class="v red">{peak:.1f} %</div></div>
          <div class="stat"><div class="k">{_zh_en('持续超阈', 'Duration above')}</div><div class="v">{_e(_fmt_hms(dur_above))}</div></div>
          <div class="stat"><div class="k">{_zh_en('冷却时间', 'Cooldown')}</div><div class="v">{cooldown:.0f} s</div></div>
        </div>
        <div class="sub-title">
          <span class="t">{_zh_en('触发时 Top 线程', 'Top threads at trigger')}</span>
          <span class="c">{_zh_en(f'显示 {shown} 条 · 共 {thread_count} 个线程', f'{shown} shown · {thread_count} total threads')}</span>
        </div>
        <div class="bars">
          <div class="head">
            <span>TID</span>
            <span>{_zh_en('线程名', 'Name')}</span>
            <span style="text-align:right;">CPU %</span>
            <span>{_zh_en('占进程总 CPU 比例', 'Share of process')}</span>
          </div>
          {''.join(thread_rows)}
        </div>
        {files_html}
      </div>"""


def _build_incident_detail_mem(inc: dict) -> str:
    inc_id = inc.get("id", "?")
    process = inc.get("process", "?")
    pid = inc.get("pid", "?")
    triggered = _fmt_ts(inc.get("triggered_at"), "%Y-%m-%d %H:%M:%S UTC")
    obs = inc.get("observed", {})
    thr = inc.get("threshold", {})
    ev = inc.get("evidence", {})

    val_trigger = obs.get("value_at_trigger", 0)
    peak = obs.get("peak", 0)
    dur_above = obs.get("duration_above_sec", 0)
    cooldown = thr.get("cooldown_sec", 0)
    sustain = thr.get("sustain_sec", 0)
    thr_val = thr.get("value", 0)

    heap_status = ev.get("heap_status", "skipped")
    hprof_file = ev.get("hprof_file", "")
    hprof_bytes = ev.get("hprof_size_bytes", 0)
    meminfo_file = ev.get("meminfo_file", "")
    categories = ev.get("top_categories", [])

    heap_chip_cls = {"ok": "green", "fallback": "orange", "skipped": "gray"}.get(heap_status, "gray")
    heap_label_zh = {"ok": "✓ 已成功生成", "fallback": "⚠ 降级为 meminfo", "skipped": "未抓取"}.get(heap_status, heap_status)
    heap_label_en = {"ok": "✓ captured", "fallback": "⚠ fallback to meminfo", "skipped": "skipped"}.get(heap_status, heap_status)

    total_pss = sum(c.get("pss_mb", 0) for c in categories)
    cat_rows = []
    for cat in categories:
        pss = cat.get("pss_mb", 0)
        pct = min(100, pss / total_pss * 100) if total_pss > 0 else 0
        cat_rows.append(f"""\
          <div class="row">
            <span></span>
            <span class="name">{_e(cat.get('name', ''))}</span>
            <span class="val">{pss:.1f}</span>
            <span class="barwrap">
              <span class="fill" style="width:{pct:.1f}%;"></span>
              <span class="bar-pct" style="left:calc({pct:.1f}% + 4px)">{pct:.0f}%</span>
            </span>
          </div>""")

    files_parts = []
    if meminfo_file:
        files_parts.append(f'<div class="file"><div class="k">{_zh_en("原始 · dumpsys meminfo", "Raw · dumpsys meminfo")}</div><div class="v">{_e("incidents/" + meminfo_file)}</div></div>')
    if hprof_file:
        size_mb = hprof_bytes / 1_048_576 if hprof_bytes else 0
        files_parts.append(f'<div class="file"><div class="k">Hprof {_zh_en("堆转储", "heap dump")}</div><div class="v">{_e("incidents/" + hprof_file)} · {size_mb:.1f} MB</div></div>')
    files_html = f'<div class="files">{"".join(files_parts)}</div>' if files_parts else ""

    cats_section = ""
    if categories:
        cats_section = f"""\
        <div class="sub-title">
          <span class="t">{_zh_en('内存分类', 'Memory categories')}</span>
          <span class="c">dumpsys meminfo -d</span>
        </div>
        <div class="bars mem">
          <div class="head">
            <span></span>
            <span>{_zh_en('类别', 'Category')}</span>
            <span style="text-align:right;">PSS (MB)</span>
            <span>{_zh_en('占进程总内存的比例', 'Share of process memory')}</span>
          </div>
          {''.join(cat_rows)}
        </div>"""

    return f"""\
      <div class="detail" data-incident-detail="{_e(inc_id)}" hidden>
        <div class="hd">
          <h3>{_e(inc_id)}</h3>
          {_chip(_zh_en('mem_threshold', 'mem_threshold'), 'orange')}
          <span class="meta">{_e(process)} · pid {_e(pid)} · {_e(triggered)}</span>
          <span class="spacer"></span>
          <span class="chip">{_zh_en(f'阈值 &gt; {thr_val:.0f} MB 持续 {sustain:.0f} s', f'threshold &gt; {thr_val:.0f} MB for {sustain:.0f} s')}</span>
        </div>
        <div class="stat-row">
          <div class="stat"><div class="k">{_zh_en('触发值', 'Value at trigger')}</div><div class="v">{val_trigger:.1f} MB</div></div>
          <div class="stat"><div class="k">{_zh_en('峰值', 'Peak')}</div><div class="v red">{peak:.1f} MB</div></div>
          <div class="stat"><div class="k">{_zh_en('持续超阈', 'Duration above')}</div><div class="v">{_e(_fmt_hms(dur_above))}</div></div>
          <div class="stat"><div class="k">{_zh_en('冷却时间', 'Cooldown')}</div><div class="v">{cooldown:.0f} s</div></div>
        </div>
        <div class="sub-title">
          <span class="t">{_zh_en('Heap 状态', 'Heap snapshot')}</span>
          <span class="c">{_zh_en('触发时是否成功抓取 hprof', 'whether hprof was captured')}</span>
        </div>
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:24px;flex-wrap:wrap;font-size:var(--fs-md);">
          {_chip(_zh_en(heap_label_zh, heap_label_en), heap_chip_cls)}
        </div>
        {cats_section}
        {files_html}
      </div>"""


def _build_incidents(result: dict) -> str:
    incidents = result.get("incidents", [])
    total = len(incidents)

    # List items
    list_items = []
    for inc in incidents:
        inc_id = inc.get("id", "?")
        inc_type = inc.get("type", "")
        process = inc.get("process", "?")
        pid = inc.get("pid", "?")
        triggered = _fmt_ts(inc.get("triggered_at"), "%H:%M:%S")
        obs = inc.get("observed", {})
        peak = obs.get("peak", 0)
        dur = obs.get("duration_above_sec", 0)

        type_cls = "red" if inc_type == "cpu_threshold" else "orange"
        peak_unit = "%" if inc_type == "cpu_threshold" else " MB"

        list_items.append(f"""\
        <div class="inc-item" data-incident="{_e(inc_id)}">
          <span class="chev"></span>
          <div class="row1">
            <span class="id">{_e(inc_id)}</span>
            {_chip(_e(inc_type), type_cls)}
          </div>
          <div class="row2">{_e(process)} · pid {_e(pid)}</div>
          <div class="row3">
            <span><span class="lbl">{_zh_en('时间', 'At')}</span><span class="val">{_e(triggered)}</span></span>
            <span><span class="lbl">{_zh_en('峰值', 'Peak')}</span><span class="val v-red">{peak:.1f}{_e(peak_unit)}</span></span>
            <span><span class="lbl">{_zh_en('持续', 'Dur')}</span><span class="val">{_e(_fmt_hms(dur))}</span></span>
          </div>
        </div>""")

    # Detail panels
    detail_panels = []
    for inc in incidents:
        if inc.get("type") == "cpu_threshold":
            detail_panels.append(_build_incident_detail_cpu(inc))
        else:
            detail_panels.append(_build_incident_detail_mem(inc))

    list_html = "\n".join(list_items)
    details_html = "\n".join(detail_panels)

    desc = _zh_en(f'{total} 个事件 · 点击左侧条目查看证据', f'{total} event(s) · click a row to view evidence')

    return f"""\
  <section>
    <div class="sec-head">
      <h2>{_zh_en('告警事件', 'Incidents')}</h2>
      <span class="num-tag">04</span>
      <span class="desc">{desc}</span>
    </div>
    <div class="md">
      <aside class="list">
        <div class="list-head">
          <span class="t">{_zh_en('全部事件', 'All incidents')}</span>
        </div>
        <div class="list-scroll">
          {list_html}
        </div>
      </aside>
      <div class="detail-empty" id="detail-empty">
        <div>{_zh_en('点击左侧任一事件查看证据', 'Select an incident on the left to view evidence')}</div>
        <div class="hint">{_zh_en('支持键盘 ↑↓ 切换 · 再次点击当前事件可折叠', '↑↓ to navigate · click the active item again to collapse')}</div>
      </div>
      {details_html}
    </div>
  </section>"""




def _build_footer_section(result: dict) -> str:
    run = result.get("run", {})
    cfg = run.get("config_effective", {})
    bookmarks = result.get("bookmarks", [])
    data_files = result.get("data_files", {})

    # Bookmarks accordion
    bm_rows = []
    for bm in bookmarks:
        ts = _fmt_ts_ms(bm.get("timestamp"))
        label = bm.get("label", "")
        bm_rows.append(f'<tr><td class="mono">{_e(ts)}</td><td>{_e(label)}</td></tr>')
    bm_count = len(bookmarks)
    bm_body = f"""\
      <table class="tbl">
        <thead><tr><th>{_zh_en('时间', 'Time')}</th><th>{_zh_en('标签', 'Label')}</th></tr></thead>
        <tbody>{''.join(bm_rows) if bm_rows else '<tr><td colspan="2" style="text-align:center;color:var(--faint)">—</td></tr>'}</tbody>
      </table>"""

    # Config accordion
    pf = cfg.get("process_filter")
    pf_html = _zh_en("全部进程（不过滤）", "All processes (no filter)") if not pf else _e(", ".join(pf))
    heap_yn = _zh_en("是", "yes") if cfg.get("enable_heap_dumps", True) and not cfg.get("no_heap_dumps") else _zh_en("否", "no")

    def _kv(k_zh: str, k_en: str, v: str) -> str:
        return f'<div class="kv"><span class="k">{_zh_en(k_zh, k_en)}</span><span class="v">{_e(v)}</span></div>'

    def _kv_html(k_zh: str, k_en: str, v_html: str) -> str:
        return f'<div class="kv"><span class="k">{_zh_en(k_zh, k_en)}</span><span class="v">{v_html}</span></div>'

    cfg_body = f"""\
      <div class="cfg-grid">
        <div class="cfg-group">
          <div class="g-title">{_zh_en('采集配置', 'Sampling')}</div>
          {_kv('CPU 采样间隔', 'CPU interval', f"{cfg.get('cpu_interval_sec', '?')} s")}
          {_kv('内存采样间隔', 'Mem interval', f"{cfg.get('mem_interval_sec', '?')} s")}
          {_kv('进程重扫间隔', 'Rescan interval', f"{cfg.get('rescan_interval_sec', '?')} s")}
          {_kv_html('进程过滤器', 'Process filter', pf_html)}
        </div>
        <div class="cfg-group">
          <div class="g-title">{_zh_en('告警阈值', 'Alert thresholds')}</div>
          {_kv('CPU 告警阈值', 'CPU threshold', f"{cfg.get('cpu_threshold_percent', '?')} %")}
          {_kv('CPU 持续时间', 'CPU sustain', f"{cfg.get('cpu_sustain_sec', '?')} s")}
          {_kv('CPU 冷却时间', 'CPU cooldown', f"{cfg.get('cpu_cooldown_sec', '?')} s")}
          {_kv('内存告警阈值', 'Mem threshold', f"{cfg.get('mem_threshold_pss_mb', '?')} MB")}
          {_kv('内存持续时间', 'Mem sustain', f"{cfg.get('mem_sustain_sec', '?')} s")}
          {_kv('内存冷却时间', 'Mem cooldown', f"{cfg.get('mem_cooldown_sec', '?')} s")}
        </div>
        <div class="cfg-group">
          <div class="g-title">{_zh_en('Dump 配置', 'Dump caps')}</div>
          {_kv_html('启用 Heap Dump', 'Enable heap dumps', heap_yn)}
          {_kv('max_cpu_dumps', 'max_cpu_dumps', str(cfg.get('max_cpu_dumps', '?')))}
          {_kv('max_heap_dumps', 'max_heap_dumps', str(cfg.get('max_heap_dumps', '?')))}
        </div>
      </div>"""

    # Output files accordion
    file_rows = [
        ("report.json", _zh_en("权威结构化结果（供 AI / 脚本分析）", "Authoritative structured result (for AI / scripts)")),
        ("report.html", _zh_en("本文件", "This file")),
    ]
    for f in data_files.get("cpu", []):
        file_rows.append((f, _zh_en("CPU% 原始时序数据（按小时滚动）", "Raw CPU% time-series (hourly rotated)")))
    for f in data_files.get("mem", []):
        file_rows.append((f, _zh_en("内存 PSS 原始时序数据", "Raw memory PSS time-series")))
    for f in data_files.get("lifecycle", []):
        file_rows.append((f, _zh_en("进程生命周期原始数据", "Raw process lifecycle events")))
    file_rows += [
        ("incidents/", _zh_en("每个告警的现场证据（top -H · meminfo · hprof）", "Per-incident evidence (top -H · meminfo · hprof)")),
        ("status.json", _zh_en("跑测期间的实时心跳", "Live heartbeat during the run")),
        ("bookmarks.jsonl", _zh_en("书签追加写文件", "Append-only bookmark log")),
    ]
    file_trs = "".join(
        f'<tr><td class="mono">{_e(path)}</td><td>{desc}</td></tr>'
        for path, desc in file_rows
    )

    return f"""\
  <section>
    <div class="sec-head collapsible" id="sec-additional">
      <h2>{_zh_en('附属信息', 'Additional information')}</h2>
      <span class="num-tag">05</span>
      <span class="chev"></span>
      <span class="desc">{_zh_en('书签 · 配置 · 输出文件', 'bookmarks · configuration · output files')}</span>
    </div>
    <div class="sec-body" id="sec-additional-body" hidden>
    <details class="acc">
      <summary><span>{_zh_en(f'书签 · {bm_count} 条', f'Bookmarks · {bm_count}')}</span></summary>
      <div class="body">{bm_body}</div>
    </details>
    <details class="acc">
      <summary><span>{_zh_en('生效的跑测配置', 'Effective configuration')}</span></summary>
      <div class="body">{cfg_body}</div>
    </details>
    <details class="acc">
      <summary><span>{_zh_en('输出文件', 'Output files')}</span></summary>
      <div class="body">
        <table class="tbl">
          <thead><tr><th>{_zh_en('路径', 'Path')}</th><th>{_zh_en('说明', 'Description')}</th></tr></thead>
          <tbody>{file_trs}</tbody>
        </table>
      </div>
    </details>
    </div>
  </section>"""


def _build_popovers(result: dict) -> str:
    incidents = result.get("incidents", [])

    inc_templates = []
    for inc in incidents:
        inc_id = inc.get("id", "?")
        inc_type = inc.get("type", "")
        process = inc.get("process", "?")
        pid = inc.get("pid", "?")
        triggered = _fmt_ts(inc.get("triggered_at"), "%H:%M:%S UTC")
        obs = inc.get("observed", {})
        peak = obs.get("peak", 0)
        dur = obs.get("duration_above_sec", 0)
        ev = inc.get("evidence", {})

        if inc_type == "cpu_threshold":
            top = ev.get("top_threads", [])
            top_thread = top[0].get("name", "?") + f" ({top[0].get('cpu_pct', 0):.0f} %)" if top else "—"
            extra = f'<div class="row"><span class="lbl">{_zh_en("主因线程", "Top thread")}</span><span class="mono">{_e(top_thread)}</span></div>'
            title_zh = "CPU 告警"
            title_en = "CPU alert"
            peak_str = f"{peak:.1f} %"
        else:
            title_zh = "内存告警"
            title_en = "Mem alert"
            peak_str = f"{peak:.1f} MB"
            heap = ev.get("heap_status", "skipped")
            extra = f'<div class="row"><span class="lbl">Heap</span><span class="mono">{_e(heap)}</span></div>'

        inc_templates.append(f"""\
  <template data-popover-id="{_e(inc_id)}">
    <div class="pop-title">{_e(inc_id)} · {_zh_en(title_zh, title_en)}</div>
    <div class="pop-body">
      <div class="row"><span class="lbl">{_zh_en("进程", "Process")}</span><span class="mono">{_e(process)} · pid {_e(pid)}</span></div>
      <div class="row"><span class="lbl">{_zh_en("触发时间", "Triggered")}</span><span class="mono">{_e(triggered)}</span></div>
      <div class="row"><span class="lbl">{_zh_en("峰值", "Peak")}</span><span class="mono">{_e(peak_str)}</span></div>
      <div class="row"><span class="lbl">{_zh_en("持续", "Duration")}</span><span class="mono">{_e(_fmt_hms(dur))}</span></div>
      {extra}
    </div>
  </template>""")

    lc_ev_zh = {"new": "进程新增", "restart": "进程重启", "gone": "进程消失"}
    lc_ev_en = {"new": "new process", "restart": "process restart", "gone": "process gone"}
    lc_templates = []
    for i, ev in enumerate(result.get("lifecycle_events", [])):
        ev_event = ev.get("event", "")
        ev_process = ev.get("process", "")
        ev_ts = _fmt_ts_ms(ev.get("timestamp"))
        old_pid = ev.get("old_pid") or 0
        new_pid = ev.get("new_pid") or 0
        gap_sec = ev.get("gap_sec") or 0.0
        title_zh = lc_ev_zh.get(ev_event, ev_event)
        title_en = lc_ev_en.get(ev_event, ev_event)

        if ev_event == "restart" and old_pid:
            pid_row = f'<div class="row"><span class="lbl">PID</span><span class="mono">{_e(str(old_pid))} → {_e(str(new_pid))}</span></div>'
        elif ev_event == "gone":
            pid_row = f'<div class="row"><span class="lbl">PID</span><span class="mono">{_e(str(old_pid))}</span></div>'
        else:
            pid_row = f'<div class="row"><span class="lbl">PID</span><span class="mono">{_e(str(new_pid))}</span></div>'

        gap_row = (f'<div class="row"><span class="lbl">{_zh_en("中断时长", "Gap")}</span>'
                   f'<span class="mono">{_e(_fmt_hms(gap_sec))}</span></div>'
                   if ev_event == "restart" and gap_sec > 0 else "")

        lc_templates.append(f"""\
  <template data-popover-id="lc-{i}">
    <div class="pop-title">{_zh_en(title_zh, title_en)}</div>
    <div class="pop-body">
      <div class="row"><span class="lbl">{_zh_en("进程", "Process")}</span><span class="mono">{_e(ev_process)}</span></div>
      <div class="row"><span class="lbl">{_zh_en("时间", "Time")}</span><span class="mono">{_e(ev_ts)}</span></div>
      {pid_row}
      {gap_row}
    </div>
  </template>""")

    return f"""\
<div id="popovers" hidden>
  <template data-popover-id="help-p95">
    <div class="pop-title">{_zh_en('什么是 p95？', 'What is p95?')}</div>
    <div class="pop-body">
      <span class="zh">p95 = 95% 分位数。把所有采样按大小排序，取第 95 个百分位的值。它过滤掉了极端尖刺，更能代表"绝大多数时间下"的真实表现。</span>
      <span class="en">95th percentile of all samples. Filters out brief spikes — represents the worst case that 95% of samples stay under.</span>
    </div>
  </template>
  <template data-popover-id="help-lifecycle">
    <div class="pop-title">{_zh_en('什么是生命周期事件？', 'What are lifecycle events?')}</div>
    <div class="pop-body">
      <span class="zh">监控过程中进程的状态变化，共三类：</span>
      <span class="en">State changes captured during the run, three kinds:</span>
      <div class="row"><span class="lbl">{_zh_en('进程新增', 'new')}</span><span>{_zh_en('扫描时首次发现该进程', 'process appeared for the first time')}</span></div>
      <div class="row"><span class="lbl">{_zh_en('进程重启', 'restart')}</span><span>{_zh_en('PID 变化，进程崩溃后被拉起', 'PID changed — process was re-launched')}</span></div>
      <div class="row"><span class="lbl">{_zh_en('进程消失', 'gone')}</span><span>{_zh_en('进程退出，未在后续扫描中出现', 'process exited and did not return')}</span></div>
    </div>
  </template>
  {''.join(inc_templates)}
  {''.join(lc_templates)}
</div>
<div id="popover-host" class="popover" hidden></div>"""


# ── entry points ──────────────────────────────────────────────────────────────

def render(result: dict, output_dir: Path) -> str:
    """Build the full HTML string from result dict + CSV files in output_dir."""
    output_dir = Path(output_dir)
    run = result.get("run", {})
    package = run.get("package", "perf_auto_test")

    header = _build_header(result)
    kpis = _build_kpis(result)
    timeline = _build_timeline(result)
    proc_tables = _build_process_tables(result)
    incidents = _build_incidents(result)
    footer_sec = _build_footer_section(result)
    popovers = _build_popovers(result)

    try:
        charts = _build_charts(result, output_dir)
    except Exception as exc:
        log.warning("chart rendering failed: %s", exc)
        charts = """\
  <section>
    <div class="sec-head">
      <h2><span class="zh">时序图表</span><span class="en">Time series</span></h2>
      <span class="num-tag">03</span>
    </div>
    <div class="charts" style="padding:40px;text-align:center;color:var(--faint);">
      Chart unavailable — Plotly not installed or no CSV data found.
    </div>
  </section>"""

    return f"""\
<!doctype html>
<html lang="zh-CN" data-lang="zh">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>perf_auto_test · {_e(package)}</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
{_CSS}
</style>
</head>
<body>

<nav class="lang-toggle" role="tablist" aria-label="Language">
  <button type="button" data-lang-btn="zh">中文</button>
  <button type="button" data-lang-btn="en">EN</button>
</nav>

<main class="page">
{header}
{kpis}
{timeline}
{proc_tables}
{charts}
{incidents}
{footer_sec}

  <footer class="foot">
    <span>perf_auto_test · schema 1.0</span>
    <span class="mono">report.html · generated by html.py</span>
  </footer>
</main>

{popovers}

<script>
{_JS}
</script>
</body>
</html>"""


def write(result: dict, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    html = render(result, output_dir)
    path = output_dir / HTML_FILENAME
    path.write_text(html, encoding="utf-8")
    return path
