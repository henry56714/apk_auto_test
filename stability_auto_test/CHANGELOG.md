# Changelog

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
