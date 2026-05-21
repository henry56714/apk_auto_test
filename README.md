# Android APK Performance Auto-Test

**[中文](#中文)** | **[English](#english)**

---

<a name="中文"></a>

## Android APK 性能自动测试工具

> 给定包名，自动对任意 Android APK 做长时性能监控。无需修改 App、无需 root，纯 Python + adb，开箱即用。

**适用场景**：App 发版前性能回归 / 长跑稳定性测试 / 自动化测试流水线集成 / 线下压测期间实时监控

### 它做什么

```bash
python -m perf_auto_test --package com.example.app --duration 30m --output ./reports/run1
```

一条命令完成全程：

1. **自动发现进程** — 主进程 + 所有子进程（`:remote`、`:push` 等多进程 App 直接支持）
2. **并行采集** — CPU%（每秒）+ 内存 PSS（每 5 秒），多进程并行，数据写入按小时滚动的 CSV
3. **超阈值自动 dump** — CPU 飙升抓线程快照（`top -H`），内存超限抓 `dumpsys meminfo`；可 debug App 额外抓 `.hprof`
4. **结构化报告** — `report.json`（AI / CI 可直接读）+ `report.html`（Plotly 交互图表，浏览器打开）

### 核心特性

| 特性 | 说明 |
|---|---|
| **包名无关** | 任意第三方 App / 系统服务，只需知道包名 |
| **无侵入** | 不需要修改 APK，不需要 root，不需要可调试版本 |
| **长跑稳定** | CSV 按小时滚动，adb 抖动自动重试，支持 1 h–24 h 不间断跑测 |
| **防误报** | 阈值需持续触发（`sustain_sec`）才报警，单次毛刺不触发 |
| **双模式** | 独立 CLI 运行 / Python 库嵌入现有测试框架 |
| **CI 友好** | `--fail-on "alerts>=1"` 按条件返回非零退出码，支持 JUnit XML |
| **AI 友好** | 每条 incident 含 `.txt`（人看）和 `.json`（机器读），`report.json` 是唯一权威数据源 |

### 环境要求

- Python 3.9+
- `adb` 可用（`adb devices` 能看到目标设备）
- 目标 App 已在设备上运行（本工具不负责启动 App）

### 快速开始

```bash
# 安装
pip install -e perf_auto_test/scripts/

# 5 分钟冒烟
python -m perf_auto_test \
  --package com.example.app \
  --duration 5m \
  --output ./reports/smoke

# 查看报告
open ./reports/smoke/report.html
```

**多设备 / 自定义阈值**

```bash
python -m perf_auto_test \
  --package com.example.app \
  --duration 30m \
  --device emulator-5554 \
  --cpu-threshold-percent 60 \
  --mem-threshold-pss-mb 400 \
  --output ./reports/run1
```

**CI 流水线**

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

### 报告产物

```
reports/run1/
├── report.json         ← 权威结果，AI / CI 直接读
├── report.html         ← Plotly 交互图（CPU / Mem / 生命周期，共享时间轴）
├── *.csv               ← 原始时序，按小时滚动
└── incidents/
    ├── cpu_<ts>_<proc>_pid<n>.json     ← Top-N 线程 + 触发元数据
    ├── heap_<ts>_<proc>_pid<n>.json    ← 内存分类 + 评估结果
    └── ...                             ← 对应原始 .txt 文件
```

### 与 Claude Code 集成（Skill 模式）

本工具同时是一个 **Claude Code Skill**，可通过自然语言直接触发，Claude 自动完成采集、打开报告并输出测试总结：

```
/perf-auto-test com.example.app 30m
```

Skill 定义见 [`perf_auto_test/SKILL.md`](perf_auto_test/SKILL.md)。

### 详细文档

完整参数说明、YAML 配置、库 API 模式、CI 退出码等见 [`perf_auto_test/README.md`](perf_auto_test/README.md)。

---

<a name="english"></a>

## Android APK Performance Auto-Test

> Automated long-running performance monitoring for any Android APK. No app modification, no root required — pure Python + adb.

**Use cases**: Pre-release performance regression / stability soak testing / CI pipeline integration / real-time monitoring during load testing

### What it does

```bash
python -m perf_auto_test --package com.example.app --duration 30m --output ./reports/run1
```

One command handles everything:

1. **Auto-discovers processes** — main process + all child processes (multi-process apps like `:remote`, `:push` supported out of the box)
2. **Parallel collection** — CPU% (every 1 s) + memory PSS (every 5 s), per-process, written to hourly-rolling CSVs
3. **Threshold-triggered dumps** — CPU spike → thread snapshot (`top -H`); memory breach → `dumpsys meminfo`; debuggable apps get `.hprof` on top
4. **Structured reports** — `report.json` (machine/AI-readable) + `report.html` (Plotly interactive charts, open in browser)

### Key features

| Feature | Description |
|---|---|
| **Package-agnostic** | Any third-party app or system service — just the package name |
| **Non-invasive** | No APK modification, no root, no debuggable build required |
| **Long-run stable** | Hourly CSV rotation, adb retry with backoff, handles 1 h–24 h runs |
| **Spike-resistant** | Alerts only fire after threshold is held for `sustain_sec`, ignoring single-sample spikes |
| **Dual mode** | Standalone CLI or Python library embedded in a larger test framework |
| **CI-ready** | `--fail-on "alerts>=1"` gates the pipeline with a non-zero exit code; JUnit XML output supported |
| **AI-ready** | Every incident includes a `.txt` (human) and `.json` (machine); `report.json` is the single source of truth |

### Requirements

- Python 3.9+
- `adb` available (`adb devices` shows the target device)
- Target app already running on device (this tool does not launch apps)

### Quick start

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

### Report output

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

### Claude Code Skill integration

This tool is also a **Claude Code Skill**. Trigger it with natural language inside Claude Code — Claude handles execution, opens the HTML report, and outputs a structured test summary:

```
/perf-auto-test com.example.app 30m
```

Skill definition: [`perf_auto_test/SKILL.md`](perf_auto_test/SKILL.md)

### Full documentation

Complete CLI reference, YAML config, Python library API, CI exit codes: [`perf_auto_test/README.md`](perf_auto_test/README.md)
