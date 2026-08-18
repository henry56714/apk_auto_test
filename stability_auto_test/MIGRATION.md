# Migration notes

## From schema 1.14 → 1.15 (2026-08-13 spec S1/S2 hardening)

### report.json

- 新增顶层字段：`verdict_reason[]`、`verdict_confidence`（high/partial/none）、
  `expected_exit_count`、`phase_timings`（prepare/observe/teardown）、
  `capabilities[]`、`disk_audit[]`。
- **verdict 语义修正（IMP-01）**：已确认 failure（java/native crash、ANR）在
  覆盖不足时不再被降级为 `inconclusive` —— `verdict=unstable` 与
  `collection_health=degraded` 可同时成立，`verdict_confidence=partial`。
- 未知原因的非预期 process exit 会令干净 run 变成 `inconclusive`（4.3）。
- JUnit：`verdict=unstable` → `<failure>`（含 coverage 信息）；
  纯观测不完整 → `<error>`。
- 事件模型：新增 Observation/Fusion 层（`observations.py`/`fusion.py`），
  incident 证据新增 `supporting_sources`、`subtype`、`crashing_thread`、
  `startup_crash`、`cause_chain`、`exit_taxonomy`。
- ExitInfo 时间戳按设备时区换算为真实 UTC（`timestamp_epoch`）；API 35
  `APP CRASH(EXCEPTION)` 等 reason 格式被正确识别。
- Native 帧保留完整 `#xx pc <addr> module`（symbolizer 依赖 PC）。

### 行为变更

- `stop()` 返回后输出目录冻结：证据先写 staging，任务在 deadline 内成功才
  原子发布（dump worker 迟到写入只会留在 staging）。
- `export` 默认脱敏（allowlist + canary 扫描）；原始导出必须
  `--raw --acknowledge-sensitive`；二进制证据不进脱敏包。
- workload 与观测共用同一 duration 预算（不再累加）；workload 失败默认
  非 0 退出（`--ignore-workload-failure` 显式豁免）；仅 manifest action
  窗口内的 fault 才算 `workload_expected`。
- logcat 连接收到首条可解析行才算 collecting；静默连接按 stale 重连。
- 配额拆分为 `min_free_bytes` / `max_run_bytes` / `max_file_bytes`
  （`max_disk_bytes` 仍作为 `min_free_bytes` 的兼容别名）。
- `events_*.csv` 新增 `source`、`fault_id` 列（v4 tag）。
- 配置校验统一（CLI/YAML/Profile/Library 同一 `_validate()`）。

## From schema 1.x (pre-2026-08-10 baseline)

### report.json

- 新增顶层字段：`event_pipeline`、`collection_health`、`coverage_ratio`、`verdict`、
  `collectors`、`policy`、`issue_groups`、`exit_info`、`device_events`、
  `resource_risk`、`recovery_warnings`、`notifications`、`plugins`。
- `run` 新增 `run_id`、`recovered`、`recovered_at`。
- Incident 新增 `event_id`/`run_id`；证据新增 `evidence_status`、
  `evidence_match_confidence`、context 切片字段、`diagnosis`、`sampled`。
- 旧报告缺少新字段时仍可被渲染，但建议用 `sat recover --output <dir>` 重建
  当前版本的 `report.json`（从 `incident_journal.jsonl` 重建）。

### 文件布局

- 新增 `incident_journal.jsonl`（事件事实日志，recover 依据）。
- 新增 `status.json`（实时心跳，原子写入）。
- 新增 `replay.yaml` 与 `workload_manifest.json`（workload/replay 场景）。
- 新增 `*.context.txt`（pre/event/post 现场切片）。

### 配置

- YAML 未知字段默认报错；需要容忍时使用 `--config-lenient`。
- `collectors.dropbox.poll_interval_sec` 已删除（DropBox 仅为事件后证据拉取）。
- 新增 `dumps`、`health`、`diagnosis`、`policy`、`quota`、`redaction`、
  `webhook`、`plugins`、`output.dashboard` 等配置节。

### CLI

- 退出码：0 通过；1 门禁失败；2 前置/配置失败；3 等待进程超时；
  4 观测不完整；130 用户中断。
- 新增子命令：`doctor`、`recover`、`compare`、`replay`、`export`、`index`、
  `trend`。

## 破坏性变更

- `events_*.csv` 列新增 `event_id`、`run_id`；旧 CSV 仍可被读取（多余列忽略），
  但新写入使用 v3 tag。
- dump 任务不再使用 daemon 线程；`stop()` 会等待在途 dump 至
  `dump_shutdown_timeout_sec`（默认 60s）。
