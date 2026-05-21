---
name: perf-auto-test
description: 对 Android APK 做性能测试、跑 CPU/内存监控、压测期间抓取性能数据时使用。给定包名，连接安卓设备/模拟器后自动采集 CPU 和内存指标，超阈值自动 dump 现场，测试结束后输出性能总结并弹出网页图表报告。
when_to_use: 用户说"帮我跑性能测试"、"测一下这个 APK 的内存"、"监控 CPU 使用率"、"压测期间跑 perf"、"跑 perf-auto-test"时触发。
argument-hint: <package> [duration] [--device serial] [--config path] [--output dir]
---

# perf-auto-test

## 参数解析

从 args 或对话上下文中提取。**只有 `--package` 是必填的**，其余均有代码默认值。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--package` | 目标 APK 包名（**必填**）| — |
| `--duration` | 采集时长，如 `30s`、`5m`、`1h` | `5m` |
| `--output` | 报告输出目录 | `./reports/<包名末段>_<YYYYMMDD_HHMMSS>` |
| `--device` | ADB serial（多设备时必填）| 自动取唯一在线设备 |
| `--config` | YAML 配置路径（覆盖阈值等）| — |
| `--cpu-threshold-percent` | CPU% 报警阈值（单核归一化，4核全满=400%）| 80 |
| `--mem-threshold-pss-mb` | 内存 PSS MB 报警阈值 | 500 |
| `--fail-on` | CI 失败条件，如 `"alerts>0"` | — |

> 采样间隔、cooldown、dump 上限、输出格式等进阶参数均在代码中有默认值，需要时可通过 `--config` YAML 覆盖，见 `${CLAUDE_SKILL_DIR}/examples/config.example.yaml`。

**简写识别**：`/perf-auto-test com.example.app 5m` → `--package com.example.app --duration 5m`

## 步骤 1：环境检查

```bash
python -m perf_auto_test --help
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
> 开始对 `<package>` 进行 `<duration>` 性能采集，输出至 `<output_dir>`…

## 步骤 3：执行采集

```bash
python -m perf_auto_test \
  --package <package> \
  --output <output_dir> \
  --duration <duration> \
  [--device <serial>] \
  [--config <config_path>] \
  [其他可选参数]
```

在前台执行，实时显示日志。采集期间 `<output_dir>/status.json` 每 10 秒刷新一次心跳。

## 步骤 4：弹出报告 + 输出总结

采集命令结束后，**先弹出网页报告，再输出文字总结**。

### 4.1 弹出网页报告

```bash
open <output_dir>/report.html
```

### 4.2 读取数据

读取 `<output_dir>/report.json`，提取以下字段：
- `run`：时长、退出原因、设备信息
- `processes[*]`：`stats.cpu_pct`（mean/p95/max）、`stats.mem_pss_mb`（mean/max）、`alerts`、`restart_count`
- `incidents[]`：触发时间、进程名、类型、observed vs threshold
- `lifecycle_events[]`：new / gone / restart 事件

### 4.3 输出性能总结

总结要**简短、有逻辑、有结论**，格式如下：

---

**性能测试总结 — `<package>`（`<duration>`）**

**整体状态**：正常 / ⚠️ 有报警 / 🔴 异常（一句话说明）

**进程概览**

| 进程 | CPU均值 | CPU P95 | CPU峰值 | 内存均值 | 内存峰值 | 报警 | 重启 |
|---|---|---|---|---|---|---|---|

**报警 & 异常事件**（无则省略此节）
- 逐条列出：时间 — 进程 — 类型 — 观测值 vs 阈值

**结论**
一到两句话：是否存在性能问题、最需要关注的点是什么。

*详细图表和 dump 文件见已弹出的 report.html*

---

> 总结不要罗列所有字段，不需要重复网页报告里的完整数据——只给出判断和关键数字。

## 命令行直接调用参考

```bash
# 基本用法
python -m perf_auto_test --package com.example.app --duration 5m --output ./reports/run01

# 使用配置文件（推荐，阈值在 YAML 中管理）
python -m perf_auto_test --config examples/config.example.yaml --package com.example.app --output ./reports/r1

# CI 模式
python -m perf_auto_test --package com.example.app --duration 30m \
  --output ./reports/ci --fail-on "alerts>0" --emit-junit --no-html

# 冒烟（30秒验证连通性）
python -m perf_auto_test --package com.android.settings --duration 30s --output ./reports/smoke
```

完整参数：`python -m perf_auto_test --help`
