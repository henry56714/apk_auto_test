# Android APK Performance Auto-Test

**[中文](README.zh.md)** | **English**

> Automated long-running performance monitoring for any Android APK. No app modification, no root required — pure Python + adb.

**Use cases**: Pre-release performance regression / stability soak testing / CI pipeline integration / real-time monitoring during load testing

---

## What it does

```bash
python -m perf_auto_test --package com.example.app --duration 30m --output ./reports/run1
```

One command handles everything:

1. **Auto-discovers processes** — main process + all child processes (multi-process apps like `:remote`, `:push` supported out of the box)
2. **Parallel collection** — CPU% (every 1 s) + memory PSS (every 5 s), per-process, written to hourly-rolling CSVs
3. **Threshold-triggered dumps** — CPU spike → thread snapshot (`top -H`); memory breach → `dumpsys meminfo`; debuggable apps get `.hprof` on top
4. **Structured reports** — `report.json` (machine/AI-readable) + `report.html` (Plotly interactive charts, open in browser)

---

## Key features

| Feature | Description |
|---|---|
| **Package-agnostic** | Any third-party app or system service — just the package name |
| **Non-invasive** | No APK modification, no root, no debuggable build required |
| **Long-run stable** | Hourly CSV rotation, adb retry with backoff, handles 1 h–24 h runs |
| **Spike-resistant** | Alerts only fire after threshold is held for `sustain_sec`, ignoring single-sample spikes |
| **Dual mode** | Standalone CLI or Python library embedded in a larger test framework |
| **CI-ready** | `--fail-on "alerts>=1"` gates the pipeline with a non-zero exit code; JUnit XML output supported |
| **AI-ready** | Every incident includes a `.txt` (human) and `.json` (machine); `report.json` is the single source of truth |

---

## Requirements

- Python 3.9+
- `adb` available (`adb devices` shows the target device)
- Target app already running on device (this tool does not launch apps)

---

## Quick start

```bash
# Install
pip install -e perf_auto_test/scripts/

# 5-minute smoke run
python -m perf_auto_test \
  --package com.example.app \
  --duration 5m \
  --output ./reports/smoke

# Open report
open ./reports/smoke/report.html
```

**Multiple devices / custom thresholds**

```bash
python -m perf_auto_test \
  --package com.example.app \
  --duration 30m \
  --device emulator-5554 \
  --cpu-threshold-percent 60 \
  --mem-threshold-pss-mb 400 \
  --output ./reports/run1
```

**CI pipeline**

```bash
python -m perf_auto_test \
  --package com.example.app \
  --duration 30m \
  --output ./reports/ci \
  --fail-on "alerts>=1,restarts>=2" \
  --emit-junit \
  --no-html
# exit 0 = pass  |  exit 1 = fail-on triggered
```

---

## Report output

```
reports/run1/
├── report.json         ← authoritative result, read by AI / CI
├── report.html         ← Plotly interactive charts (CPU / Mem / lifecycle, shared x-axis)
├── *.csv               ← raw time-series, hourly rotation
└── incidents/
    ├── cpu_<ts>_<proc>_pid<n>.json     ← top-N threads + trigger metadata
    ├── heap_<ts>_<proc>_pid<n>.json    ← memory categories + evaluation
    └── ...                             ← corresponding raw .txt files
```

---

## Claude Code Skill integration

This tool is also a **Claude Code Skill**. Trigger it with natural language inside Claude Code — Claude handles execution, opens the HTML report, and outputs a structured test summary:

```
/perf-auto-test com.example.app 30m
```

Skill definition: [`perf_auto_test/SKILL.md`](perf_auto_test/SKILL.md)

---

## Full documentation

Complete CLI reference, YAML config, Python library API, CI exit codes: [`perf_auto_test/README.md`](perf_auto_test/README.md)
