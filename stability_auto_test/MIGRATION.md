# Migration notes

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
