# Android APK 自动化测试套件

**中文** | **[English](README.md)**

> 两个独立的 Python + adb 工具，用于 Android APK 自动化测试，无需修改 App，无需 root。

---

## 工具一览

| 工具 | 检测目标 | 模块 | 路径 |
|---|---|---|---|
| **perf_auto_test** | CPU 飙升 · 内存泄漏 · 超阈值 | `pat` | `perf_auto_test/` |
| **stability_auto_test** | Java Crash · Native Crash · ANR · 进程异常退出 | `sat` | `stability_auto_test/` |

两个工具共享相同的设计原则：

- **包名无关** — 任意第三方 App / 系统服务，只需知道包名
- **无侵入** — 不修改 APK，不需要 root，不需要可调试版本
- **长跑稳定** — CSV/LOG 按小时滚动，adb 抖动自动重试，支持 1 h–24 h 不间断跑测
- **三种模式** — 独立 CLI 运行 / Python 库嵌入现有测试框架 / 通过 AI Skill 调用
- **AI 友好** — 结构化 `report.json` + 交互式 `report.html`（Plotly）

---

## 报告预览 — perf_auto_test

### 告警总结 · KPI 卡片 · 运行时间轴
![Overview](docs/screenshots/overview.png)

一屏看清测试结果：顶部告警栏（正常 / 超阈详情）、六个 KPI 卡片（监控进程数、CPU 峰值 / p95、内存峰值、告警次数、生命周期事件），以及交互式运行时间轴。鼠标悬停告警标记（×）或生命周期圆点可弹出详情浮框，点击告警标记可直跳对应的事件详情。

### 告警事件列表 + 单条证据详情
![Incidents](docs/screenshots/incidents.png)

按类型过滤（CPU 超阈 / 内存超阈），支持按进程名或事件 ID 搜索。主从布局右侧展示触发值、峰值、持续时长，以及——CPU 告警显示触发时刻 Top 线程占比条形图，内存告警显示 `dumpsys meminfo` 内存分类分布。

### CPU & 内存时序图（Plotly 交互）
![Charts](docs/screenshots/charts.png)

每个被监控进程独立曲线：CPU%（单核归一化）和内存 PSS（MB）。红色虚线为告警阈值，告警标记直接叠加在数据曲线上，点击标记跳转到对应事件详情。

---

## 环境要求

- Python 3.9+
- `adb` 可用（`adb devices` 能看到目标设备）
- 目标 App 已在设备上运行

---

## 与 Claude Code 集成（Skill 模式）

两个工具均提供 **Claude Code Skill**，可通过自然语言直接触发：

```
/perf-auto-test com.example.app 30m
/stability-auto-test com.example.app 1h
```

Claude 自动完成采集、打开 HTML 报告并输出结构化测试总结。

Skill 定义：[`perf_auto_test/SKILL.md`](perf_auto_test/SKILL.md) · [`stability_auto_test/SKILL.md`](stability_auto_test/SKILL.md)

---

## perf_auto_test — 性能监控

自动发现目标包名的所有进程，并行采集 CPU% 和内存 PSS，超阈值时自动触发 dump（线程快照 / 堆转储）。

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

**产物目录**

```
reports/run1/
├── report.json         ← 权威结果（AI / CI 可直接读）
├── report.html         ← Plotly 交互图（CPU / Mem / 生命周期，共享时间轴）
├── *.csv               ← 原始时序，按小时滚动
└── incidents/
    ├── cpu_<ts>_<proc>_pid<n>.json   ← Top-N 线程 + 触发元数据
    ├── heap_<ts>_<proc>_pid<n>.json  ← 内存分类 + 评估结果
    └── ...
```

详细文档：[`perf_auto_test/README.md`](perf_auto_test/README.md)

---

## stability_auto_test — 稳定性监控

并行流式接收 logcat 并轮询 dropbox，捕获 Java/Native Crash、ANR、进程异常退出 4 类事件，落盘现场快照（logcat 切片 + tombstone/ANR trace），产出结构化报告与交互式事件时间轴。

> **注意**：本工具不负责启动 App——目标进程须已在运行。

```bash
cd stability_auto_test/scripts
pip install -r requirements-dev.txt

python -m sat \
  --package com.example.app \
  --duration 30m \
  --output ./reports/run1
```

**产物目录**

```
reports/run1/
├── report.json               ← 权威结果（AI / CI 可直接读）
├── report.html               ← Plotly 事件时间轴 + 进程稳定性总表
├── events_*.csv              ← 事件流，按小时滚动
├── lifecycle_*.csv           ← 进程生命周期，按小时滚动
├── logcat_*.log              ← 原始 logcat，按小时滚动
└── incidents/
    ├── java_crash_<ts>_<proc>_pid<n>.txt   ← logcat 切片（人看）
    ├── java_crash_<ts>_<proc>_pid<n>.json  ← 异常类 + Top 栈 + 元数据
    ├── native_crash_<ts>_<proc>_pid<n>.tombstone  （可访问时）
    ├── anr_<ts>_<proc>_pid<n>.trace               （可访问时）
    └── ...
```

详细文档：[`stability_auto_test/README.md`](stability_auto_test/README.md)


