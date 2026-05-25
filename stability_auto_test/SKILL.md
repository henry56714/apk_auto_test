---
name: stability-auto-test
description: 对 Android APK 做稳定性测试、监控 Java/Native 崩溃、ANR、进程异常退出时使用。给定包名，连接安卓设备/模拟器后自动监听 logcat 与 dropbox，捕获稳定性事件并落盘现场快照，测试结束后输出稳定性总结并弹出网页报告。
when_to_use: 用户说"帮我跑稳定性测试"、"测一下这个 APK 的稳定性"、"监控 crash 和 ANR"、"看下 APK 有没有崩溃"、"跑 stability-auto-test"时触发。
argument-hint: <package> [duration] [--device serial] [--config path] [--output dir]
---

# stability-auto-test

## 参数解析

从 args 或对话上下文中提取。**只有 `--package` 是必填的**，其余均有代码默认值。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--package` | 目标 APK 包名（**必填**）| — |
| `--duration` | 采集时长，如 `30s`、`5m`、`1h` | `5m` |
| `--output` | 报告输出目录（不填时自动生成） | `./reports/<包名末段>_<YYYYMMDD_HHMMSS>` |
| `--device` | ADB serial（多设备时必填）| 自动取唯一在线设备 |
| `--config` | YAML 配置路径 | — |
| `--dedup-window` | 同事件去重窗口秒数 | 5 |
| `--max-incidents-per-type` | 每类事件最大记录数 | 200 |

> 检测开关（`--no-java-crash` / `--no-native-crash` / `--no-anr` / `--no-process-death`）、采集开关（`--no-dropbox`）、dump 拉取开关（`--no-tombstone-pull` / `--no-anr-trace-pull`）等进阶参数均在代码中有默认值，需要时可通过 `--config` YAML 覆盖，见 `${CLAUDE_SKILL_DIR}/scripts/config.example.yaml`。

**简写识别**：`/stability-auto-test com.example.app 30m` → `--package com.example.app --duration 30m`

## 步骤 1：环境检查

```bash
python -m sat --help
```

若报 ModuleNotFoundError，安装依赖后重试：

```bash
pip install -e ${CLAUDE_SKILL_DIR}/scripts
```

若 `adb devices` 为空，提示用户连接设备或启动模拟器后再试。

## 步骤 2：确认参数

若 `--package` 缺失，询问用户目标包名。

若 `--output` 未指定，自动生成：`./reports/<包名末段>_<YYYYMMDD_HHMMSS>`

确认后告知用户：
> 开始对 `<package>` 进行 `<duration>` 稳定性监控，输出至 `<output_dir>`…

> 注意：本工具**不会启动 APK**，请确保目标 App 已运行（或将在采集期间被启动）。

## 步骤 3：执行采集

在 `stability_auto_test/scripts/` 目录下执行：

```bash
python -m sat \
  --package <package> \
  --output <output_dir> \
  --duration <duration> \
  [--device <serial>] \
  [--config <config_path>] \
  [其他可选参数]
```

在前台执行，实时显示日志。采集期间 `<output_dir>/status.json` 每 10 秒刷新一次心跳（含当前进程列表 + 各类事件累计计数）。

## 步骤 4：弹出报告 + 输出总结

采集命令结束后，**先弹出网页报告，再输出文字总结**。

### 4.1 弹出网页报告

```bash
open <output_dir>/report.html
```

### 4.2 读取数据

读取 `<output_dir>/report.json`，提取以下字段：
- `run`：时长、退出原因、设备信息
- `processes[*]`：`events`（4 类计数）、`restart_count`、`uptime_ratio`
- `incidents[]`：触发时间、进程、类型、severity、summary、evidence
- `lifecycle_events[]`：new / restart / gone 事件

### 4.3 输出稳定性总结

总结要**简短、有逻辑、有结论**，格式如下：

---

**稳定性测试总结 — `<package>`（`<duration>`）**

**整体状态**：✅ 稳定 / ⚠️ 有问题 / 🔴 严重异常（一句话说明）

**进程概览**

| 进程 | uptime% | 重启 | Java crash | Native crash | ANR | 异常退出 |
|---|---|---|---|---|---|---|

**关键事件**（无则省略此节）
- 逐条列出：时间 — 进程 — 类型 — 摘要

**结论**
一到两句话：是否存在稳定性问题、最严重的问题是什么。

*详细堆栈、tombstone、ANR trace、原始 logcat 见已弹出的 report.html 与 incidents/ 目录*

---

> 总结不要罗列所有字段，不需要重复网页报告里的完整数据——只给出判断和关键事件。

## 命令行直接调用参考

以下命令在 `stability_auto_test/scripts/` 目录下执行。

```bash
# 基本用法
python -m sat --package com.example.app --duration 30m --output ./reports/run01

# 使用配置文件（推荐，开关在 YAML 中管理）
python -m sat --config config.example.yaml --package com.example.app --output ./reports/r1

# 冒烟（30秒验证连通性）
python -m sat --package com.android.settings --duration 30s --output ./reports/smoke
```

完整参数：`python -m sat --help`
