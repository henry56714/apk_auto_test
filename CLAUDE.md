# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概览

本仓库包含两套**完全独立**的 Android APK 自动化测试工具，业务不同、各自独立 pyproject 与虚拟环境：

| 工具 | 业务 | 包名 | 主代码 |
|---|---|---|---|
| perf_auto_test | 性能（CPU/内存监控 + 超阈值 dump 现场）| `pat` | `perf_auto_test/scripts/pat/` |
| stability_auto_test | 稳定性（Java/Native crash + ANR + 进程异常退出）| `sat` | `stability_auto_test/scripts/sat/` |

两工程结构严格对称（CLI / Skill / 库 三入口、`report.json` 骨架、退出码、测试方式），但**代码不共享** —— 共用模块（adb / device / discovery / bookmark / status / utils）以拷贝方式各自维护一份。改一处通常两边都要改；**不要把它们抽成共享模块**。

详细架构与数据流见各工具的 `README.md`。

## 开发与测试

每套工具独立操作，命令在各自的 `scripts/` 目录下执行：

```bash
cd perf_auto_test/scripts        # 或 stability_auto_test/scripts
pip install -r requirements-dev.txt
pytest tests/ -v                 # 单元测试，全部无需真机
```

无需 `pip install -e .` —— `tests/conftest.py` 已将项目根加入 `sys.path`。

## 运行（需设备/模拟器）

用**模块名**调用（不是 pyproject 里的 entry-point 名），在对应 `scripts/` 目录下：

```bash
python -m pat --package com.example.app --duration 5m  --output ./reports/run-001
python -m sat --package com.example.app --duration 30m --output ./reports/run-001
```

也可经 Skill 触发：`/perf-auto-test`、`/stability-auto-test`。

## 关键约束

- `report.json` 是唯一权威数据源，schema 见各工具 `scripts/schemas/report.schema.json`（Draft-07）。
- 多设备时必须显式 `--device <serial>`，否则 preflight exit 2。
- **perf**：CPU% 为**单核归一化**（4 核全满 = 400%，与 `top` 一致）；`am dumpheap` 失败自动降级为 `dumpsys meminfo -d`。
- **stability**：**不启动 APK**（只做监控），目标进程未运行时 wait 超时 exit 3；即使发现稳定性问题退出码仍为 0（发现问题即测试目标，不算错误）；tombstone / ANR trace 在 user 构建上多不可访问，自动降级后继续跑测。
