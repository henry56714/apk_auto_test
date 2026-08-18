# stability_auto_test

可信的 Android 稳定性黑盒诊断工具：给定包名后通过 ADB 监听 logcat（Java /
Native Crash、ANR、进程异常退出），采集现场证据，输出可审计、可接入 CI 的
结构化报告。不修改 App、不要求 root（无 root 时自动降级并用 DropBox 证据），
断网可用。

## 设计原则

- **事件不静默丢失**：每个事件先写入 `incident_journal.jsonl`，再执行证据
  采集；即使 dumper 失败，最终 `report.json` 仍保留占位 Incident 和失败计数。
- **已确认故障不会被覆盖不足抹掉**：只要观测到 Java/Native Crash 或 ANR，
  `verdict=unstable` 成立；覆盖不足只降低 `verdict_confidence=partial`，
  只有“没发现故障但观测不完整”才判 `inconclusive`（三层语义分离）。
- **跨源融合**：logcat / DropBox / ApplicationExitInfo / 生命周期统一进入
  Observation→Fusion 层 —— 同一物理故障只计一次，全部来源可追溯
  （`supporting_sources`）；logcat 断线期间的 Crash/ANR/LMK 由 ExitInfo 补回。
- **stop() 后产物冻结**：证据先写 staging，任务在 deadline 内成功才原子发布；
  迟到 worker 无法再写输出目录。
- **报告单一权威**：`report.json` 是唯一事实来源，HTML / JUnit / 终端摘要均
  从同一结果模型派生；每个运行附带 `capabilities[]`（设备能看见什么、看不见
  什么以及降级路径）。
- **默认安全分享**：`sat export` 默认脱敏（allowlist + 全包 canary 扫描），
  原始导出必须显式 `--raw --acknowledge-sensitive`。
- **可离线**：`report.html` 内嵌 Plotly JS，无任何 CDN 依赖。
- **可恢复 / 可离线复盘**：进程被杀后可用 `sat recover --output <dir>` 重建
  报告；没有设备时可用 `sat analyze-bugreport <zip>` 解析 bugreport 归档。

## 安装

```bash
cd stability_auto_test/scripts
pip install -e .
# 开发 / 测试
pip install -r requirements-dev.txt
```

设备侧只需可用的 `adb`（`adb devices` 能看到目标设备）。

## 最短可用路径（<10 分钟）

```bash
cd stability_auto_test/scripts

# 1. 自检：设备、包、权限、logcat、DropBox、输出目录
python -m sat doctor --package com.android.settings --json | python -m json.tool

# 2. 30 秒 smoke
python -m sat --package com.android.settings --duration 30s \
  --output /tmp/sat-smoke

# 3. 查看报告（离线可打开）
open /tmp/sat-smoke/report.html

# 4.（可选）确定性故障复现：安装 Fault Lab 后触发
adb install -r -t ../test_apps/stability_fault_lab/app/build/outputs/apk/debug/app-debug.apk
adb shell am broadcast -n com.example.stabilityfaultlab/.FaultReceiver \
  -a com.example.stabilityfaultlab.TRIGGER --es fault JAVA_MAIN_CRASH \
  --es fault_id java-main-001
```

Fault Lab（`test_apps/stability_fault_lab/`）是仓库自带的故障注入 APK，
包含 Java/Native Crash、ANR、OOM、FD/线程泄漏、self-exit、敏感日志等
30+ 种确定性故障；每个 action 输出 `SAT_FAULT_BEGIN id=<id> type=<type>`
marker，供 fusion / action window / replay 关联。详见其 README。

## CLI 参数

```bash
python -m sat --package <pkg> [--duration 30s|5m|1h] [--device <serial>] \
  [--output <dir>] [--config <yaml>] [--config-lenient]
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--package` | 目标包名（必填） | — |
| `--duration` | 运行时长（`30s`/`5m`/`1h`/`24h`） | `5m` |
| `--device` | ADB serial（多设备时必填） | 唯一在线设备 |
| `--output` | 输出目录 | `./reports/<pkg>_<ts>` |
| `--config` | YAML 配置（CLI 覆盖 YAML） | — |
| `--config-lenient` | 允许未知 YAML 字段 | 关闭 |
| `--wait-timeout` | 等待目标进程秒数 | 60 |
| `--dedup-window` | 同事件去重窗口秒数 | 5 |
| `--max-incidents-per-type` | 每类事件最大 Incident 数 | 200 |
| `--dump-shutdown-timeout` | stop() 等待在途 dump 的秒数 | 60 |
| `--context-retention` | logcat 上下文环形缓冲保留秒数 | pre+post+60 |
| `--context-buffer-max-lines` | 环形缓冲最大行数 | 5000 |
| `--context-buffer-max-bytes` | 环形缓冲最大字节数 | 4194304 |
| `--min-coverage` | 可信结论的最低覆盖率 | 0.99 |
| `--mapping-file` | ProGuard/R8 mapping（Java 反混淆） | 关闭 |
| `--retrace-command` | 外部 retrace 工具命令 | 内置解析器 |
| `--native-symbols-dir` | 未 strip 的 .so 目录树 | 关闭 |
| `--llvm-symbolizer` | llvm-symbolizer 路径 | 关闭 |
| `--no-java-crash` 等 | 关闭对应检测 | 全部开启 |
| `--no-tombstone-pull` / `--no-anr-trace-pull` | 关闭 trace 拉取 | 开启 |
| `--no-html` | 不生成 HTML | 生成 |
| `--status-interval` | `status.json` 心跳间隔 | 10s |
| `--junit <path>` | 输出 JUnit XML（每个 issue group 一个 testcase） | 关闭 |
| `--dashboard` | 启动 localhost-only 实时看板 | 关闭 |
| `--redact` / `--redaction-regex` | 输出脱敏（内置 email/手机号/token/坐标） | 关闭 |
| `--webhook-url` / `--webhook-event` | 通用 webhook 通知 | 关闭 |
| `--enable-plugins` | 启用 `sat.plugins` 第三方插件 | 关闭 |
| `--profile` | smoke / soak / overnight / automotive 预设 | 关闭 |
| `--device-reboot-policy` | continue / fail-fast / wait-and-resume | wait-and-resume |

子命令：

```bash
python -m sat doctor --package <pkg> [--device <serial>] [--json]
python -m sat recover --output <run_dir>
```

`doctor` 只读诊断：ADB、设备状态、Android 版本、包安装、进程状态、logcat
buffers、DropBox、tombstone/ANR 权限、输出目录与磁盘、符号化工具。无 root
权限时对应检查标记为 `unavailable`，不会让整体 doctor 失败。

## CI 门禁与 JUnit

默认保持“仅采集”：发现 Crash 仍返回 0（报告 verdict=unstable）。加 `--ci`
后，策略失败返回 1，观测不完整返回 4。策略可用 `policy:` YAML 或 CLI
（`--fail-on` / `--max-anr` / `--max-restarts` / `--min-coverage`）覆盖：

```bash
python -m sat --package com.example.app --duration 30s --ci \
  --junit /tmp/run/junit.xml --output /tmp/run
```

JUnit 语义固定：每个 issue group 一个 testcase；门禁失败为 `<failure>`；
观测不完整为 `<error>`。存在 `$GITHUB_STEP_SUMMARY` 时自动追加 Markdown 摘要。
示例 workflow 见仓库根目录 `.github/workflows/stability-smoke.yml`（含报告与
JUnit 的 artifact 上传步骤）。

## Python 库 API

```python
from sat.api import StabilityConfig

cfg = StabilityConfig(
    package="com.example.app",
    output_dir="./reports/run1",
)
print(cfg.package, cfg.output_dir)
# 嵌入测试框架：with StabilityTest(cfg) as t: t.bookmark("x")
# t.result 是完整 report.json 数据（含 run / processes / incidents / verdict）
```

## 配置校验

`--config` YAML 使用严格校验：未知字段默认报错并提示合法字段；时间、数量、
buffer、过滤器和路径参数做范围检查。显式传 `--config-lenient` 可忽略未知
字段（值校验仍生效）。完整示例见 `scripts/config.example.yaml`。

## 预设与实时看板

`--profile smoke|soak|overnight|automotive` 提供合理默认值（时长、上下文窗口、
覆盖率阈值、采样间隔），显式 YAML/CLI 仍然覆盖；`--print-effective-config`
输出最终配置与每个值的来源。`--dashboard` 启动只绑定 `127.0.0.1` 的实时看板
（SSE 状态流、bookmark、确认后停止、报告下载）。

## 输出目录

```
reports/run1/
├── report.json               权威结果（schema 版本化）
├── report.html               自包含离线 HTML（内嵌 Plotly）
├── status.json               实时心跳（进程 / 计数 / collector 状态）
├── incident_journal.jsonl    事件事实日志（recover 依据）
├── events_*.csv              事件流（按小时滚动，含 event_id/run_id）
├── lifecycle_*.csv           进程生命周期（按小时滚动）
├── logcat_*.log              原始 logcat（按小时滚动）
└── incidents/
    ├── <type>_<ts>_<proc>_pid<n>.json   结构化 Incident
    ├── <type>_<ts>_<proc>_pid<n>.txt    logcat 事件块
    ├── <type>_<ts>_<proc>_pid<n>_context.txt  PRE/EVENT/POST 上下文切片
    ├── ..._dropbox.txt       DropBox 证据（可用时）
    ├── ..._tombstone         tombstone（root/eng 且置信匹配时）
    └── ..._trace             ANR trace（同上）
```

## 质量门禁

```bash
cd stability_auto_test/scripts
python -m pytest tests -q
python -m ruff check sat tests
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir /tmp/sat-dist
```

## 与 perf_auto_test 的关系

本工具只做稳定性诊断（Crash / ANR / 退出原因 / 前后文证据），常规 CPU/内存
性能曲线由 `perf_auto_test` 负责，两者互补不重复。
