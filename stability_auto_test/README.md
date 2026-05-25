# stability_auto_test

通用 Android APK 稳定性自动化测试工具。给定包名，自动监听 logcat 与 dropbox，
捕获 Java Crash / Native Crash / ANR / 进程异常退出，并在事件发生时落盘
现场快照（logcat slice + tombstone/ANR trace），测试结束后产出结构化报告与
交互式时间轴图。

**设计原则**

- **包名无关** — 任意 App / 系统服务 / 多进程 App，只需包名
- **不启动 APK** — 仅监控；启动 App 由其他模块负责
- **纯本地** — Python + adb，无云端依赖，断网可用
- **长跑友好** — logcat 流断线自动重连 + 续接、CSV/LOG 按小时滚动，1h–24h 无压力
- **双模式 + Skill 三入口** — CLI 独立运行 / Python 库嵌入 / Claude Code Skill
- **AI 友好** — 单一权威 `report.json`，每条 incident 都有 `.txt`（人看）+ `.json`（AI 看）

---

## 目录结构

```
stability_auto_test/
├── SKILL.md              Claude Code Skill 定义（skill 入口）
├── README.md             本文档
└── scripts/              Python 包（源码 + 测试，命令在此目录下运行）
    ├── config.example.yaml   配置示例（复制后按需修改）
    ├── pyproject.toml
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── sat/   核心包
    │   ├── api.py            StabilityConfig / StabilityTest（库 API）
    │   ├── cli.py            CLI 入口（薄壳）
    │   ├── detection.py      logcat 行解析 + 事件去重
    │   ├── collectors/       logcat 流 / dropbox 轮询
    │   ├── dumpers/          4 类事件的现场快照
    │   ├── pool.py           3 条管线编排
    │   ├── reporter/         report.json 构建 + HTML 渲染
    │   └── ...
    ├── schemas/
    │   └── report.schema.json
    └── tests/
```

---

## 两种调用方式

### 方式一：通过 Claude Code Skill（推荐）

```
/stability-auto-test com.example.app 30m
/stability-auto-test com.example.app 1h --device emulator-5554
帮我测一下 com.example.app 的稳定性，跑 30 分钟
```

Skill 自动完成：
1. 检查 adb 环境与依赖
2. 执行 `python -m sat` 并实时展示日志
3. 采集结束后打开 `report.html` 交互式时间轴
4. 输出本次测试的结构化总结（整体状态 / 进程概览 / 关键事件 / 结论）

> Skill 定义见 `SKILL.md`，可配合 `scripts/config.example.yaml` 覆盖采集/检测开关。

---

### 方式二：直接调用 CLI

#### 安装

```bash
# 生产环境
pip install -e scripts/

# 开发 / 跑测试
pip install -r scripts/requirements-dev.txt
```

依赖：`PyYAML`、`pandas`、`plotly`、`jsonschema`。设备侧只需可用的 `adb`。

#### 基本用法

以下命令均在 `stability_auto_test/scripts/` 目录下执行。

```bash
adb devices                              # 确认设备已连

# 5 分钟监控
python -m sat \
  --package com.example.app \
  --duration 5m \
  --output ./reports/run-001

# 使用配置文件（推荐长跑场景）
python -m sat \
  --config config.example.yaml \
  --package com.example.app \
  --duration 30m \
  --output ./reports/run-002

# 多设备时必须指定 serial
python -m sat \
  --package com.example.app \
  --duration 5m \
  --device emulator-5554 \
  --output ./reports/run-003
```

#### 输出目录结构

```
reports/run-001/
├── report.json               ★ 权威结构化结果（AI 分析唯一数据源）
├── report.html               ★ Plotly 事件时间轴 + 进程稳定性表
├── status.json                 心跳：当前 pid 列表、各类事件计数
├── bookmarks.jsonl             时间轴打点（库 API 或外部进程追加）
├── events_2026-05-21_10.csv    时序事件流，按小时滚动
├── lifecycle_2026-05-21_10.csv 进程生命周期，按小时滚动
├── logcat_2026-05-21_10.log    原始 logcat 流，按小时滚动
└── incidents/
    ├── java_crash_<ts>_<process>_pid<pid>.txt    crash 块 logcat（人看）
    ├── java_crash_<ts>_<process>_pid<pid>.json   异常类 + Top 栈 + 元数据
    ├── native_crash_<ts>_<process>_pid<pid>.txt
    ├── native_crash_<ts>_<process>_pid<pid>.tombstone  仅可访问时
    ├── native_crash_<ts>_<process>_pid<pid>.json
    ├── anr_<ts>_<process>_pid<pid>.txt
    ├── anr_<ts>_<process>_pid<pid>.trace               仅可访问时
    ├── anr_<ts>_<process>_pid<pid>.json
    └── proc_death_<ts>_<process>_pid<pid>.json
```

---

## CLI 参数速查

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--package` | 必填 | 目标包名 |
| `--output` | `./reports/<包名末段>_<YYYYMMDD_HHMMSS>` | 报告输出目录 |
| `--duration` | `5m` | 采集时长，支持 `30s`、`5m`、`1h`、`24h` |
| `--device` | 自动取唯一设备 | ADB serial |
| `--config` | — | YAML 配置路径，CLI 参数始终优先 |
| `--wait-timeout` | `60` | 等待目标进程出现的最长秒数 |
| `--rescan-interval` | `5` | 进程 re-discovery 间隔秒数 |
| `--processes` | 全部 | 进程名过滤（如 `:remote,:push`） |
| `--no-dropbox` | — | 禁用 dropbox 轮询（只用 logcat）|
| `--dropbox-interval` | `30` | dropbox 轮询间隔秒数 |
| `--no-java-crash` / `--no-native-crash` / `--no-anr` / `--no-process-death` | — | 关闭对应事件检测 |
| `--dedup-window` | `5` | 同 (process,pid,type) 去重窗口秒数 |
| `--max-incidents-per-type` | `200` | 每类事件最多记录数 |
| `--no-tombstone-pull` | — | 跳过 `/data/tombstones/` pull |
| `--no-anr-trace-pull` | — | 跳过 `/data/anr/` pull |
| `--no-html` | — | 跳过 report.html |
| `--status-interval` | `10` | status.json 心跳秒数 |
| `-q` / `--quiet` | — | WARNING 及以上日志 |
| `-v` / `--verbose` | — | DEBUG 日志 |
| `--log-json` | — | JSON lines 日志（stderr） |

---

## 配置文件（YAML）

```yaml
package: com.example.app
device: null                          # null = 取唯一在线设备

discovery:
  wait_timeout_sec: 60
  rescan_interval_sec: 5
  process_filter: null                # 或 [":remote", ":push"]

collectors:
  logcat:
    enabled: true
    buffers: [main, system, events, crash]
    reconnect_backoff_sec: 2
  dropbox:
    enabled: true
    poll_interval_sec: 30

detection:
  enable_java_crash: true
  enable_native_crash: true
  enable_anr: true
  enable_process_death: true
  dedup_window_sec: 5

dumps:
  pre_context_sec: 30
  post_context_sec: 10
  max_incidents_per_type: 200
  pull_tombstone: true
  pull_anr_trace: true

output:
  emit_html: true
  status_interval_sec: 10
```

---

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 正常结束（**包含检测到稳定性问题**——发现问题是测试目标，不视为错误）|
| `2` | 启动前置失败（adb 不可用、包未安装、参数错误等） |
| `3` | 等待进程超时（`wait_timeout_sec` 内未发现目标进程） |
| `130` | SIGINT（Ctrl+C） |

---

## 库 API 模式（嵌入测试框架）

```python
from sat import StabilityConfig, StabilityTest

cfg = StabilityConfig(
    package="com.example.app",
    output_dir="./reports/lib-run",
)

with StabilityTest(cfg) as t:
    run_scenario_a()
    t.bookmark("scenario_a_done")   # 在时间轴打锚点
    run_scenario_b()
    t.bookmark("scenario_b_done")

# 退出 with 块时自动 stop + 落盘
result = t.result
total_crashes = sum(
    p["events"]["java_crash"] + p["events"]["native_crash"]
    for p in result["processes"]
)
print(f"crashes={total_crashes}")
```

`bookmark()` 让父测试框架在时间轴上标注业务场景，便于 AI 做"X 场景触发了
ANR/Crash"类相关性归因。

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
    "device": { "serial": "...", "android_version": "14", "cpu_cores": 4 },
    "package": "com.example.app",
    "config_effective": { ... }
  },
  "processes": [
    {
      "name": "com.example.app",
      "uptime_ratio": 0.9999,
      "restart_count": 0,
      "events": {
        "java_crash": 1,
        "native_crash": 0,
        "anr": 2,
        "process_death": 2
      },
      "sample_failures": { "logcat": 0, "dropbox": 1 }
    }
  ],
  "incidents": [
    {
      "id": "incident-001",
      "type": "java_crash",
      "process": "com.example.app",
      "pid": 1234,
      "triggered_at": "...",
      "severity": "fatal",
      "summary": "java.lang.NullPointerException: ...",
      "evidence": {
        "logcat_slice_file": "java_crash_..._.txt",
        "trace_file": null,
        "exception_class": "java.lang.NullPointerException",
        "signal": null,
        "top_frames": [...],
        "source": "logcat",
        "dedup_count": 1
      }
    }
  ],
  "lifecycle_events": [...],
  "bookmarks":        [...],
  "data_files":       { "events": [...], "lifecycle": [...], "logcat": [...] }
}
```

### `report.html`（交互图表）

单页面：
- 顶部 4 个事件计数（Java/Native/ANR/进程异常退出）
- Plotly 时间轴：x = 时间，y = 事件类型，每条 incident 一个红 X；进程重启以橙色竖线穿过；bookmark 以蓝色虚线穿过
- 进程稳定性总表
- 可折叠的 incident 列表（点开看堆栈 + 现场文件链接）

### Tombstone / ANR trace 降级

`/data/tombstones/` 与 `/data/anr/` 在 user 构建上默认仅 root 可读。
不可访问时：

- incident 中 `evidence.trace_file=null`，`fallback_reason` 写入原因
- logcat 中捕获到的崩溃块仍保存到 `<base>.txt`
- 跑测继续进行，不中断

---

## 单元测试（无需真机）

```bash
cd stability_auto_test/scripts
pytest tests/ -v
```

覆盖：4 类事件的 logcat 行解析（多版本 fixture）、dropbox dump 解析与水位
线、Deduper 窗口、CSV/LOG 按小时滚动与线程安全、4 类 dumper（含 root 不可
访问的降级路径）、CollectorPool 3 条管线编排、StabilityTest 上下文管理器、
CLI 退出码矩阵、`report.json` schema 校验。

---

## 已知边界

- **不启动 APK**：目标进程未运行时 `wait_for_processes` 在 `wait_timeout_sec` 内
  超时，exit 3 退出
- **logcat 抖动**：流断线后 backoff 重连 + `-T '<last_ts>'` 续接，不丢失
- **tombstone / ANR trace 需 root**：user 构建上多数情况下落到 fallback
  路径，仅保留 logcat slice 与文本元数据
- **进程名截断**：`/proc/[pid]/comm` 最多保留 15 字符；discovery 用
  cmdline 验证候选
- **多设备**：必须 `--device <serial>` 显式指定，否则 preflight 以 exit 2 退出
- **dropbox 与 logcat 去重**：同 (process, pid, event_type) 在
  `dedup_window_sec`（默认 5s）内合并为一条 incident
