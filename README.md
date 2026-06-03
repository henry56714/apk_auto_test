# Android APK Auto-Test Suite

**[中文](README.zh.md)** | **English**

Two independent Python + adb tools that automate Android APK testing — no app modification, no root, no debuggable build required. Both capture evidence automatically and produce structured, AI-readable reports.

| Tool | What it monitors | Evidence captured | Usage |
|---|---|---|---|
| **perf_auto_test** | CPU spikes · memory growth · threshold breaches | Thread snapshots · heap dumps · Plotly time-series charts | CLI · Python lib · Skill |
| **stability_auto_test** | Java crash · Native crash · ANR · process death | Logcat slices · tombstones · ANR traces · event timeline | CLI · Python lib · Skill |

Both tools are **package-agnostic** (supply a package name, they find all processes), **non-invasive** (pure adb, nothing installed on device), and **long-run stable** (hourly rolling files, adb reconnect with backoff, tested at 1 h–24 h).

---

## AI-ready output

Every test run produces two files:

**`report.json`** is the authoritative output — schema-validated (JSON Schema Draft-07), versioned, and structured for downstream consumption. It includes run metadata, per-process statistics, and for every incident: trigger value, peak, duration, evidence file paths, and a plain-English summary. Feed it directly to an LLM, a CI script, or a custom dashboard.

**`report.html`** is the human companion — a single self-contained file with Plotly interactive charts, a filterable master-detail incident panel, and hover popovers. No server, no build step.

---

## Report preview

### perf_auto_test

**Verdict · KPI cards · run timeline**

![Overview](docs/screenshots/overview.png)

One-screen verdict (all-clear or breach details), six KPI cards (processes monitored, CPU peak / p95, memory peak, incident count, lifecycle events), and an interactive run timeline. Hover any incident marker (×) for an instant detail popover; click to jump to the incident panel.

**Incident list + per-incident deep-dive**

![Incidents](docs/screenshots/incidents.png)

Filter by type (CPU threshold / memory threshold) or search by process name and ID. The detail panel shows trigger value, peak, time above threshold, and — depending on type — top CPU threads with usage bars or memory category breakdown from `dumpsys meminfo`.

**CPU & memory time-series charts**

![Charts](docs/screenshots/charts.png)

Plotly charts for every monitored process: CPU% (single-core normalised) and memory PSS in MB. Red dashed threshold lines and incident markers overlay directly on the curves. Click any marker to jump to its incident detail.

---

### stability_auto_test

**Verdict · event type counters · event timeline**

![SAT Overview](docs/screenshots/sat_overview.png)

Verdict bar in plain English ("3 crashes and 2 ANRs detected"). Four counters break events down by type with a one-line hint each. The Plotly timeline has seven lanes — four event types and three lifecycle states — with bookmark lines overlaid.

**Incident list + crash detail (stack trace)**

![SAT Incidents](docs/screenshots/sat_incidents.png)

Filter by event type, severity, process, or free text. The detail panel shows exception class, source (logcat / dropbox), device timestamp, one-line summary, and the full Java or native stack — business-package frames highlighted in amber. Evidence files (logcat slice, tombstone, ANR trace) are linked directly.

**Process stability table**

![SAT Process table](docs/screenshots/sat_process_table.png)

Per-process uptime bar (green → orange as uptime falls), restart count, and per-type event counts as clickable chips that filter the incident list instantly.

---

## Usage

### Prerequisites

- Python 3.9+
- `adb` in PATH (`adb devices` shows the target device)
- Target app already running on device

### Option 1 — Standalone CLI

Install dependencies and run directly from the terminal:

**perf_auto_test**

```bash
cd perf_auto_test/scripts
pip install -r requirements-dev.txt

python -m pat \
  --package com.example.app \
  --duration 30m \
  --cpu-threshold-percent 60 \
  --mem-threshold-pss-mb 400 \
  --output ./reports/run1
```

**stability_auto_test**

```bash
cd stability_auto_test/scripts
pip install -r requirements-dev.txt

python -m sat \
  --package com.example.app \
  --duration 30m \
  --output ./reports/run1
```

> stability_auto_test monitors a running app — it does not launch it. The target process must already be running before the tool starts.

### Option 2 — Python library

Use the `with`-statement API to embed either tool in an existing test framework:

**perf_auto_test**

```python
from pat import PerfConfig, PerfTest

cfg = PerfConfig(
    package="com.example.app",
    duration_sec=1800,
    output_dir="./reports/run1",
    cpu_threshold_percent=60,
    mem_threshold_pss_mb=400,
)
with PerfTest(cfg) as t:
    t.run()
# t.result holds the full report.json data
```

**stability_auto_test**

```python
from sat import StabilityConfig, StabilityTest

cfg = StabilityConfig(
    package="com.example.app",
    duration_sec=1800,
    output_dir="./reports/run1",
)
with StabilityTest(cfg) as t:
    t.run()
# t.result holds the full report.json data
```

### Option 3 — Claude Code Skill

Trigger from Claude Code with natural language. Claude runs the test, opens the report, and returns a structured summary:

```
/perf-auto-test com.example.app 30m
/stability-auto-test com.example.app 1h
```

Skill definitions: [`perf_auto_test/SKILL.md`](perf_auto_test/SKILL.md) · [`stability_auto_test/SKILL.md`](stability_auto_test/SKILL.md)

### Output layout

**perf_auto_test**

```
reports/run1/
├── report.json         ← authoritative result (AI / CI readable)
├── report.html         ← Plotly interactive charts
├── *.csv               ← raw time-series, hourly rotation
└── incidents/
    ├── cpu_<ts>_<proc>_pid<n>.json   ← top-N threads + trigger metadata
    ├── heap_<ts>_<proc>_pid<n>.json  ← memory categories + evaluation
    └── ...
```

**stability_auto_test**

```
reports/run1/
├── report.json               ← authoritative result (AI / CI readable)
├── report.html               ← Plotly event timeline + process stability table
├── events_*.csv              ← event stream, hourly rotation
├── lifecycle_*.csv           ← process lifecycle, hourly rotation
├── logcat_*.log              ← raw logcat, hourly rotation
└── incidents/
    ├── java_crash_<ts>_<proc>_pid<n>.json  ← exception class + frames + metadata
    ├── native_crash_<ts>_<proc>_pid<n>.tombstone  (when accessible)
    ├── anr_<ts>_<proc>_pid<n>.trace               (when accessible)
    └── ...
```

Full docs: [`perf_auto_test/README.md`](perf_auto_test/README.md) · [`stability_auto_test/README.md`](stability_auto_test/README.md)
