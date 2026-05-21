# perf_auto_test

通用 Android APK 性能自动化测试工具。给定包名，自动发现该包的全部进程（含多进程 App），
并行采集 CPU / 内存，超阈值时自动抓取线程快照和内存 dump，测试结束后产出结构化报告与
交互式图表。

**设计原则**

- **包名无关** — 任意 App / 系统服务 / 多进程 App，只需包名
- **纯本地** — Python + adb，无云端依赖，断网可用
- **长跑友好** — CSV 按小时滚动，自恢复 adb 抖动，1 h–24 h 无压力
- **双模式** — CLI 独立运行 / Python 库嵌入更大的自动化体系
- **AI 友好** — 单一权威 `report.json`，每条 incident 都有 `.txt`（人看）+ `.json`（机器 / AI 看）

---

## 目录结构

```
perf_auto_test/
├── SKILL.md              Claude Code Skill 定义（skill 入口）
├── README.md             本文档
└── scripts/              Python 包（源码 + 测试，命令在此目录下运行）
    ├── config.example.yaml   配置示例（复制后按需修改）
    ├── pyproject.toml
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── pat/   核心包
    │   ├── api.py            PerfConfig / PerfTest（库 API）
    │   ├── cli.py            CLI 入口（薄壳）
    │   ├── collectors/       CPU / 内存采集线程
    │   ├── dumpers/          线程 CPU dump / heap dump
    │   ├── reporter/         report.json 构建 + HTML 渲染
    │   └── ...
    ├── schemas/
    │   └── report.schema.json
    └── tests/
```

---

## 两种调用方式

### 方式一：通过 Claude Code Skill（推荐）

本工具可注册为 Claude Code Skill，**无需手动安装依赖**，直接在 Claude Code 中发出自然语言
指令即可触发。Claude 会自动完成参数解析、执行采集、打开 HTML 报告、并输出结构化测试总结。

**触发示例**

```
/perf-auto-test com.example.app 5m
/perf-auto-test com.example.app 30m --device emulator-5554
帮我测一下 com.example.app 的内存，跑 10 分钟
```

**Skill 自动完成**

1. 检查 adb 环境与依赖
2. 执行 `python -m pat` 并实时展示日志
3. 采集结束后打开 `report.html` 交互式图表
4. 输出本次测试的结构化总结（整体状态 / 进程概览 / 报警事件 / 结论）

> Skill 定义文件见 `SKILL.md`，可配合 `scripts/config.example.yaml` 覆盖阈值。

---

### 方式二：直接调用 CLI

#### 安装

```bash
# 生产环境
pip install -e scripts/

# 开发 / 跑测试（含 pytest）
pip install -r scripts/requirements-dev.txt
```

依赖：`PyYAML`、`pandas`、`plotly`、`jsonschema`。设备侧只需可用的 `adb`。

#### 基本用法

以下命令均在 `perf_auto_test/scripts/` 目录下执行。

```bash
# 确认设备已连，App 已启动
adb devices

# 5 分钟采集，默认阈值（CPU 80%、内存 500 MB）
python -m pat \
  --package com.example.app \
  --duration 5m \
  --output ./reports/run-001

# 使用配置文件覆盖阈值（推荐长跑场景）
python -m pat \
  --config config.example.yaml \
  --package com.example.app \
  --duration 30m \
  --output ./reports/run-002

# 多设备时必须指定 serial
python -m pat \
  --package com.example.app \
  --duration 5m \
  --device emulator-5554 \
  --output ./reports/run-003
```

#### 输出目录结构

```
reports/run-001/
├── report.json               ★ 权威结构化结果（AI 分析唯一数据源）
├── report.html               ★ Plotly 交互式图（CPU / Mem / 生命周期）
├── status.json                 心跳：当前 pid 列表、dump 计数（每 10 s 刷新）
├── bookmarks.jsonl             时间轴打点（库 API 或外部进程追加）
├── cpu_2026-05-21_10.csv       时序原始，按小时滚动
├── mem_2026-05-21_10.csv
├── lifecycle_2026-05-21_10.csv
└── incidents/
    ├── cpu_<ts>_<process>_pid<pid>.txt          top -H 原始输出（人看）
    ├── cpu_<ts>_<process>_pid<pid>.task_stat.txt  /proc task 快照（离线重建）
    ├── cpu_<ts>_<process>_pid<pid>.json         Top-N 线程 + 触发元数据（机器看）
    ├── heap_<ts>_<process>_pid<pid>.meminfo.txt   dumpsys meminfo -d 原始
    ├── heap_<ts>_<process>_pid<pid>.meminfo.json  内存分类解析结果
    ├── heap_<ts>_<process>_pid<pid>.hprof         仅可 debug App 才有
    └── heap_<ts>_<process>_pid<pid>.json        触发元数据 + 评估结果（机器看）
```

---

## CLI 参数速查

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--package` | 必填 | 目标包名 |
| `--output` | `./reports/<包名末段>_<YYYYMMDD_HHMMSS>` | 报告输出目录（不填时自动生成） |
| `--duration` | `5m` | 采集时长，支持 `30s`、`5m`、`1h`、`24h` |
| `--device` | 自动取唯一设备 | ADB serial；多设备时必填 |
| `--config` | — | YAML 配置路径，CLI 参数始终优先 |
| `--cpu-threshold-percent` | `80` | CPU% 阈值（单核归一化；4 核全满 = 400%） |
| `--cpu-sustain-sec` | `60` | 必须持续超过阈值的秒数才触发 |
| `--cpu-cooldown-sec` | `300` | 触发后冷却秒数 |
| `--mem-threshold-pss-mb` | `500` | 内存 PSS MB 阈值 |
| `--mem-sustain-sec` | `120` | 同上，内存维度 |
| `--mem-cooldown-sec` | `600` | 同上，内存维度 |
| `--processes` | 全部 | 进程名过滤，逗号分隔（如 `:remote,:push`） |
| `--no-heap-dumps` | — | 禁用 `am dumpheap`（仍保留 meminfo dump） |
| `--no-html` | — | 跳过 report.html |
| `-q` / `--quiet` | — | 仅 WARNING 及以上日志 |
| `-v` / `--verbose` | — | DEBUG 日志 |
| `--log-json` | — | 日志以 JSON lines 写到 stderr |

---

## 配置文件（YAML）

### 最小配置（推荐）

`scripts/config.example.yaml` 只覆盖常用的阈值，其余参数由代码常量控制：

```yaml
package: com.example.app
device: null                      # null = 取唯一在线设备；多台则填 serial

thresholds:
  cpu:
    percent: 10                   # 单核归一化（4 核全满 = 400%）
    sustain_sec: 60
  mem:
    pss_mb: 207
    sustain_sec: 120
```

### 完整配置（高级用法）

所有支持的 YAML 键（CLI 参数始终优先）：

```yaml
package: com.example.app
device: null

discovery:
  wait_timeout_sec: 60
  rescan_interval_sec: 5
  process_filter: null            # 或 [":remote", ":push"]

sampling:
  cpu_interval_sec: 1
  mem_interval_sec: 5

thresholds:
  cpu:
    percent: 80
    sustain_sec: 60
    cooldown_sec: 300
  mem:
    pss_mb: 500
    sustain_sec: 120
    cooldown_sec: 600

dumps:
  enable_heap: true
  max_heap_dumps: 20
  max_thread_dumps: 50

output:
  emit_html: true
  status_interval_sec: 10
```

---

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 正常结束 |
| `2` | 启动前置失败（adb 不可用、包未安装、参数错误等） |
| `3` | 等待进程超时（`wait_timeout_sec` 内未发现目标进程） |
| `130` | SIGINT（Ctrl+C） |

---

## 库 API 模式（嵌入测试框架）

```python
from pat import PerfConfig, PerfTest

cfg = PerfConfig(
    package="com.example.app",
    output_dir="./reports/lib-run",
    cpu_threshold_percent=80,
    mem_threshold_pss_mb=500,
)

with PerfTest(cfg) as t:
    run_scenario_a()
    t.bookmark("scenario_a_done")   # 在时间轴打锚点
    run_scenario_b()
    t.bookmark("scenario_b_done")

# 退出 with 块时自动 stop + 落盘
result = t.result
cpu_alerts = sum(p["alerts"]["cpu"] for p in result["processes"])
mem_alerts = sum(p["alerts"]["mem"] for p in result["processes"])
print(f"CPU alerts={cpu_alerts}  Mem alerts={mem_alerts}")
```

`bookmark()` 让父测试框架在时间轴上打锚点，便于 AI 做"X 场景是否引发了 CPU 飙升"类相关性归因。

完整可运行示例见 `scripts/tests/integration/lib_api_example.py`。

---

## 报告产物说明

### `report.json`（权威数据源）

所有其它报告都从它派生。Schema 见 `scripts/schemas/report.schema.json`（JSON Schema Draft-07）。

```jsonc
{
  "schema_version": "1.0",
  "run": {
    "started_at": "2026-05-21 02:59:42.051",
    "duration_sec": 1812.161,
    "exit_code": 0,
    "exit_reason": "duration_elapsed",
    "device": { "serial": "...", "android_version": "15", "cpu_cores": 4 },
    "config_effective": { ... }
  },
  "processes": [
    {
      "name": "com.example.app",
      "uptime_ratio": 0.9999,
      "restart_count": 0,
      "stats": {
        "cpu_pct":    { "mean": 16.65, "p50": 8.76, "p90": 58.93, "p95": 65.37, "max": 133.95 },
        "mem_pss_mb": { "mean": 514.81, "p50": 515.18, "p95": 538.15, "max": 546.45 }
      },
      "alerts": { "cpu": 2, "mem": 3 }
    }
  ],
  "incidents": [
    {
      "id": "incident-001",
      "type": "cpu_threshold",          // 或 mem_threshold
      "process": "com.example.app",
      "pid": 12345,
      "triggered_at": "...",
      "threshold": { "metric": "cpu_pct", "value": 80, "sustain_sec": 60 },
      "observed":   { "value_at_trigger": 95.3, "peak": 98.1, "duration_above_sec": 72 },
      "evidence":   { "top_threads": [...] }   // cpu；或 top_categories + heap_status（mem）
    }
  ],
  "lifecycle_events": [...],
  "bookmarks":        [...],
  "data_files":       { "cpu": [...], "mem": [...], "lifecycle": [...] }
}
```

### `report.html`（交互图表）

Plotly 多子图（CPU% / Mem PSS / 生命周期），共享 X 轴。阈值以虚线标出，incident 触发点
以红 X 标记（hover 展示 id、峰值）；进程重启以橙色竖线穿过所有面板。

### Heap dump 降级机制

`am dumpheap` 需要 App 可 debug 或设备 root。不满足时自动降级：

- 仍然保存 `dumpsys meminfo <pid> -d` 的文本 + 解析后 JSON（含 Top 内存分类）
- incident 中 `evidence.heap_status = "fallback"`，`fallback_reason` 记录具体原因
- 跑测继续进行，不中断

---

## 单元测试（无需真机）

```bash
cd perf_auto_test
pytest scripts/tests/ -v
```

覆盖：`adb shell ps` / `dumpsys meminfo` 解析（Android 8/10/12 多版本 fixture）、
阈值状态机（sustain / cooldown / 边界）、CSV 按小时滚动、`report.json` schema 校验、
PerfTest 上下文管理器、bookmark、status 心跳、SIGINT 优雅退出、退出码矩阵。

---

## 已知边界

- **进程名截断**：`/proc/[pid]/comm` 最多保留 15 个字符；discovery 用 `/proc/[pid]/cmdline`
  验证候选，避免截断导致误匹配
- **进程崩溃重启**：watcher 在 `rescan_interval_sec` 内捕获，新旧 pid 对均记录进
  `lifecycle_events[]`
- **adb 抖动**：每次调用含 3 次重试 + 指数退避；持续失败仅记录采样缺口，不退出
- **多设备**：必须 `--device <serial>` 显式指定，否则 preflight 以 exit 2 退出
- **CPU% 单位**：单核归一化，与 `top` 一致；4 核全满 = 400%
