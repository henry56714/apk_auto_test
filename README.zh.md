# Android APK 自动化测试套件

**中文** | **[English](README.md)**

两个独立的 Python + adb 工具，对 Android APK 进行自动化测试——无需修改 App，无需 root，无需可调试版本。两个工具均可自动采集现场证据，并输出结构化的、对 AI 友好的报告。

| 工具 | 监控目标 | 采集的证据 |
|---|---|---|
| **perf_auto_test** | CPU 飙升 · 内存增长 · 超阈值 | 线程快照 · 堆转储 · Plotly 时序图 |
| **stability_auto_test** | Java Crash · Native Crash · ANR · 进程异常退出 | Logcat 切片 · tombstone · ANR trace · 事件时间轴 |

两个工具均**包名无关**（只需提供包名，自动发现所有进程）、**无侵入**（纯 adb，设备上无需安装任何东西）、**长跑稳定**（文件按小时滚动，adb 断线自动重连退避，已验证 1 h–24 h 连续跑测）。支持三种接入方式：独立 CLI、Python 库（嵌入现有测试框架）和 Claude Code Skill（自然语言触发）。

---

## AI 友好的输出

每次跑测产出两个文件：

**`report.json`** 是权威输出——经 JSON Schema Draft-07 验证、有版本号、为下游消费设计。包含跑测元数据、各进程统计，以及每条 incident 的触发值、峰值、持续时长、证据文件路径和一句话摘要。可直接送入 LLM、CI 脚本或自定义看板分析。

**`report.html`** 是人看的配套报告——单一自包含文件，内嵌 Plotly 交互图表、可过滤的主从 incident 面板和悬浮详情框。无需服务器，无需构建。

---

## 报告预览

### perf_auto_test

**告警总结 · KPI 卡片 · 运行时间轴**

![Overview](docs/screenshots/overview.png)

一屏看清测试结果：顶部告警栏（正常 / 超阈详情）、六个 KPI 卡片（监控进程数、CPU 峰值 / p95、内存峰值、告警次数、生命周期事件），以及交互式运行时间轴。鼠标悬停告警标记（×）或生命周期圆点可弹出详情浮框，点击告警标记可直跳事件详情。

**告警事件列表 + 单条证据详情**

![Incidents](docs/screenshots/incidents.png)

按类型（CPU 超阈 / 内存超阈）过滤，或按进程名和事件 ID 搜索。详情面板展示触发值、峰值、持续时长，CPU 告警显示触发时刻 Top 线程占比条形图，内存告警显示 `dumpsys meminfo` 内存分类分布。

**CPU & 内存时序图**

![Charts](docs/screenshots/charts.png)

每个被监控进程独立曲线：CPU%（单核归一化）和内存 PSS（MB）。红色虚线为告警阈值，告警标记直接叠加在数据曲线上，点击标记跳转到对应事件详情。

---

### stability_auto_test

**告警总结 · 事件类型计数 · 事件时间轴**

![SAT Overview](docs/screenshots/sat_overview.png)

告警栏用一句话总结结果（"检测到 3 次 Crash 和 2 次 ANR"）。四个计数卡片按类型拆分并给出一行摘要提示。Plotly 时间轴有七条泳道——四种事件类型加三种生命周期状态，书签线叠加其上。

**事件列表 + 崩溃详情（调用栈）**

![SAT Incidents](docs/screenshots/sat_incidents.png)

按事件类型、严重级别、进程名或关键字自由筛选。详情面板展示异常类、数据来源（logcat / dropbox）、设备时间戳、一句话摘要，以及完整 Java / Native 调用栈——业务包帧以橙色高亮。证据文件（logcat 切片、tombstone、ANR trace）均可直接点击查看。

**进程稳定性总表**

![SAT Process table](docs/screenshots/sat_process_table.png)

每个进程显示在线率进度条（绿色 → 橙色随在线率下降）、重启次数，以及各类型事件计数 chip——点击 chip 立即跳转到对应筛选后的事件列表。

---

## 使用方式

### 环境准备

- Python 3.9+
- `adb` 可用（`adb devices` 能看到目标设备）
- 目标 App 已在设备上运行

### 方式一：独立 CLI

最直接的用法，安装依赖后在终端运行：

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

> stability_auto_test 不负责启动 App——目标进程须在工具启动前已在运行。

### 方式二：Python 库

以 `with` 语句嵌入现有测试框架，与自动化用例串联：

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
# t.result 即完整的 report.json 数据
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
# t.result 即完整的 report.json 数据
```

### 方式三：Claude Code Skill

在 Claude Code 中用自然语言触发，Claude 自动执行、打开报告并输出总结：

```
/perf-auto-test com.example.app 30m
/stability-auto-test com.example.app 1h
```

Skill 定义：[`perf_auto_test/SKILL.md`](perf_auto_test/SKILL.md) · [`stability_auto_test/SKILL.md`](stability_auto_test/SKILL.md)

### 产物目录

**perf_auto_test**

```
reports/run1/
├── report.json         ← 权威结果（AI / CI 可直接读）
├── report.html         ← Plotly 交互图
├── *.csv               ← 原始时序，按小时滚动
└── incidents/
    ├── cpu_<ts>_<proc>_pid<n>.json   ← Top-N 线程 + 触发元数据
    ├── heap_<ts>_<proc>_pid<n>.json  ← 内存分类 + 评估结果
    └── ...
```

**stability_auto_test**

```
reports/run1/
├── report.json               ← 权威结果（AI / CI 可直接读）
├── report.html               ← Plotly 事件时间轴 + 进程稳定性总表
├── events_*.csv              ← 事件流，按小时滚动
├── lifecycle_*.csv           ← 进程生命周期，按小时滚动
├── logcat_*.log              ← 原始 logcat，按小时滚动
└── incidents/
    ├── java_crash_<ts>_<proc>_pid<n>.json  ← 异常类 + 调用栈 + 元数据
    ├── native_crash_<ts>_<proc>_pid<n>.tombstone  （可访问时）
    ├── anr_<ts>_<proc>_pid<n>.trace               （可访问时）
    └── ...
```

详细文档：[`perf_auto_test/README.md`](perf_auto_test/README.md) · [`stability_auto_test/README.md`](stability_auto_test/README.md)
