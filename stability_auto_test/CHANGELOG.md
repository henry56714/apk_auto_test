# Changelog

## v0.2.0 (2026-08-13)

### 可信结论与确定性设备测试（spec 第一阶段）

- **verdict 三层语义修正（IMP-01）**：已确认 failure 在覆盖不足时仍为
  `unstable`（`verdict_confidence=partial`）；`verdict_reason[]` /
  `expected_exit_count` 进入报告；JUnit failure/error 与 verdict 对齐。
- **可取消可冻结的证据任务（IMP-02）**：staging 目录 + 共享 deadline +
  原子发布；`stop()` 返回后输出目录冻结（T-L1-001..004）。
- **Observation + Fusion 层（IMP-03）**：`sat/observations.py`、
  `sat/fusion.py`；ExitInfo 补回 logcat 漏检的 Crash/ANR/LMK；三源去重
  `supporting_sources`；run-start 水位 ≥ 设备 epoch，能力探测只做一次。
- **时间/证据匹配修复（IMP-04/05/06/09）**：native 帧保留 PC；logcat
  stderr drain + 首行 collecting + stale heartbeat；设备时区换算（ExitInfo /
  tombstone ls）；验证失败的 trace 隔离（`verification_failed`）；
  DropBox 全日期匹配；pre/observe/teardown 阶段计时。
- **安全导出与真实配额（IMP-07/12）**：默认脱敏 allowlist + canary 扫描
  （命中即删包失败）；`--raw --acknowledge-sensitive`；`min_free_bytes` /
  `max_run_bytes` / `max_file_bytes` + 按大小滚动 + 周期 retention 审计。
- **workload/矩阵/统一 stop（IMP-08/13/16/17）**：统一 duration 预算；
  workload 异步 + stdout drain + 失败默认非 0；action window 才标 expected；
  矩阵全配置序列化 + 每设备聚合；`boot_id` 用内核源 + uptime 回退；
  统一 `_validate()` 配置校验。
- **Stability Fault Lab APK（S1-07）**：`test_apps/stability_fault_lab/`
  Kotlin+CMake，`SAT_FAULT_BEGIN` marker 协议，Java/Native Crash、ANR、
  OOM、FD/线程泄漏、self-exit、sensitive log 等 25+ 故障；`--device` L2
  套件（T-L2-001..037 覆盖主链路）。

### 检测与诊断深度（spec 第二阶段）

- S2-01: crashing thread / cause chain / startup crash / `crash_loop` 分组；
  OOM 细分（heap/bitmap/native/GC overhead）。
- S2-02/04: build ID + ABI 符号匹配；exit taxonomy expected/failure/unknown。
- S2-03: ANR 类型（input/broadcast/service/...）+ 根因（lock holder、
  binder_wait、busy_loop、io_wait、late_or_non_actionable）。
- S2-05: 资源采样 value+capability+error（拒绝=unavailable 非 0）、
  多风险同报、进程 epoch baseline、RSS。
- S2-07: FGS / Binder / SQLite / ENOSPC 崩溃细分。

### 离线与体验（spec 第三/四阶段部分）

- S3-01: `sat analyze-bugreport <zip>` 离线复盘（同 parser/fusion/报告
  schema，`source_mode=offline_bugreport`）。
- IMP-20: DropBox 风暴缓存（dumpsys 调用有界）。
- IMP-23: 报告 `capabilities[]` 能力清单。
- 报告 schema 升级 v1.14 → v1.15；events CSV v4；配置校验统一。

## v0.1.0 (2026-08-10)

### 第一阶段：检测到就不静默丢失

- S1-01: dump 任务改为 ThreadPoolExecutor 受管队列，stop() 固定顺序排空并支持 `dump_shutdown_timeout_sec`。
- S1-02: 新增 `incident_journal.jsonl` 事件事实日志、`event_id`、`event_pipeline` 计数与失败占位 Incident。
- S1-03: logcat 时间环形缓冲，输出 `PRE_CONTEXT / EVENT_BLOCK / POST_CONTEXT` 现场切片。
- S1-04: tombstone/ANR trace 置信度评分匹配与 pull 后二次校验。
- S1-05: `collection_health` / `coverage_ratio` / `verdict`，观测不完整时判 `inconclusive`。
- S1-06: 原子 JSON 写入、`.sat-run.lock`、`sat recover`。
- S1-07: 严格 YAML 校验与 `sat doctor`。
- S1-08: 离线 HTML（内嵌 Plotly）、文档示例测试、wheel 资源与发布元数据。

### 第二阶段：可回答为什么/是否回归/是否拦截

- S2-01: ApplicationExitInfo 采集与多源退出归一化、水位过滤、跨源关联。
- S2-02: 稳定 fingerprint 与 `issue_groups` 聚类。
- S2-03: Java 反混淆、Native 符号化、ANR 根因摘要（`diagnosis`）。
- S2-04: 可配置 CI 门禁与退出码 0/1/2/3/4/130。
- S2-05: JUnit XML 与 GitHub Actions 摘要、示例 workflow。
- S2-06: `sat compare` baseline 回归识别。

### 第三阶段：自动施压、复现与设备矩阵

- S3-01: launch / monkey / external workload 与 `workload_manifest.json`。
- S3-02: `replay.yaml` 与 `sat replay`。
- S3-03: `--devices` 多设备矩阵与 aggregate 报告。
- S3-04: 设备健康监控（reboot/offline/watchdog）、断线恢复与 fail-fast 策略。
- S3-05: FD/线程资源风险预警与 incident 关联。
- S3-06: 磁盘配额、log 保留、有界队列与证据采样。

### 第四阶段：开源生态、隐私与日常使用

- S4-01: localhost-only 实时 Dashboard（SSE）。
- S4-02: smoke / soak / overnight / automotive 预设与 `--print-effective-config`。
- S4-03: 内置脱敏规则、`sat export --redacted`。
- S4-04: 通用 webhook 通知（限速、失败隔离）。
- S4-05: Collector/Analyzer/EvidenceProvider/Reporter 插件接口（默认禁用）。
- S4-06: `sat index` / `sat trend` 本地报告索引与趋势。

## Schema 演进

- `report.json` schema 由 1.0 演进至 1.12；每次结构变化均同步 JSON Schema。
- `events_*.csv` schema tag 由 v1 演进至 v3（新增 `event_id`、`run_id`）。
