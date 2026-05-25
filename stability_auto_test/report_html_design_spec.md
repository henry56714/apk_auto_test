# report.html 设计说明文档（stability_auto_test）

**目标读者**：Claude Design（或任何负责重新设计 `report.html` 的开发者 / 设计工具）
**文档作用**：完整描述 `stability_auto_test` 工具产出的 `report.html` 所需展示的全部内容、数据来源、交互行为和视觉设计要求，确保设计结果能完整呈现一次 Android APK 稳定性监控跑测的所有信息，并兼具美观性与可读性。

> 与同仓库的 `perf_auto_test` 的报告设计文档**结构对称**，但业务核心不同：perf 关心连续指标（CPU/内存）+ 超阈值告警，stability 关心**离散稳定性事件**（Java/Native crash、ANR、进程异常退出），所以页面布局以"事件 (incident)"为中心，而非"时序图"为中心。

---

## 一、背景与数据来源

`stability_auto_test` 是一个 Android APK 稳定性自动化测试工具。给定包名，监听 logcat 与 dropbox，捕获稳定性事件（Java crash / Native crash / ANR / 进程异常退出），并在事件发生时落盘现场快照。**不负责启动 APK**，仅做监控。

`report.html` 是**唯一面向人的报告**，数据来自同一目录下的若干文件：

| 文件 | 内容 |
|------|------|
| `report.json` | 权威结构化结果：运行元数据、进程统计、事件 (incidents)、生命周期事件、书签 |
| `events_YYYY-MM-DD_HH.csv` | 事件时序流，按小时滚动，可能存在多个文件 |
| `lifecycle_YYYY-MM-DD_HH.csv` | 进程生命周期事件（new/restart/gone），同上 |
| `logcat_YYYY-MM-DD_HH.log` | 原始 logcat 流，按小时滚动；体积可观（一次 1 h 跑测可达数十 MB）|
| `incidents/` 子目录 | 每条 incident 的现场快照：`.txt` (logcat slice) / `.json` (元数据) / `.tombstone` / `.trace` |
| `bookmarks.jsonl` | 时间轴打点（库 API 或外部进程追加） |
| `status.json` | 跑测期间心跳文件（runtime-only，不需要进报告页）|

> **实现注意**：`reporter/html.py` 的 `render(result)` 当前接收已解析的 `result` dict，并不读 CSV；本次重设计建议改为 `render(result, output_dir)`，使 HTML 能在客户端 lazy-load 大文件（如 logcat slice、tombstone）。所有时间戳为 "YYYY-MM-DD HH:MM:SS.SSS" 字符串（UTC，无时区后缀，与 `utils.utc_now_iso()` 一致）。

---

## 二、report.json 完整数据结构

以下是 HTML 页面所有数据的唯一来源，需要完整展示。**与 perf 不同，stability 没有时序指标**，所有"时间维度"的展示都基于事件离散点 + 生命周期 + 书签。

```jsonc
{
  "schema_version": "1.0",

  "run": {
    "package": "com.example.app",
    "started_at": "2026-05-21 10:00:00.000",
    "ended_at":   "2026-05-21 10:30:00.000",
    "duration_sec": 1800.0,
    "exit_code": 0,
    "exit_reason": "duration_elapsed",         // 见下表
    "device": {
      "serial": "emulator-5554",
      "android_version": "14",
      "sdk_int": 34,
      "cpu_cores": 4
    },
    "config_effective": {
      "package": "com.example.app",
      "wait_timeout_sec": 60,
      "rescan_interval_sec": 5,
      "process_filter": null,
      "logcat_enabled": true,
      "logcat_buffers": ["main", "system", "events", "crash"],
      "logcat_reconnect_backoff_sec": 2,
      "dropbox_enabled": true,
      "dropbox_poll_interval_sec": 30,
      "enable_java_crash": true,
      "enable_native_crash": true,
      "enable_anr": true,
      "enable_process_death": true,
      "dedup_window_sec": 5,
      "pre_context_sec": 30,
      "post_context_sec": 10,
      "max_incidents_per_type": 200,
      "max_concurrent_dumps": 2,
      "pull_tombstone": true,
      "pull_anr_trace": true,
      "emit_html": true,
      "status_interval_sec": 10
    }
  },

  "processes": [                               // 跑测期间观察到的所有进程
    {
      "name": "com.example.app",
      "first_seen_at": "2026-05-21 10:00:00.500",
      "last_seen_at":  "2026-05-21 10:30:00.000",
      "uptime_ratio": 0.985,                    // 存活时长 / 总监控时长，0~1
      "restart_count": 1,
      "events": {                               // 该进程下各类事件的触发次数
        "java_crash": 1,
        "native_crash": 0,
        "anr": 2,
        "process_death": 1
      },
      "sample_failures": { "logcat": 0, "dropbox": 1 }
    }
  ],

  "incidents": [                                // 所有捕获的稳定性事件
    {
      "id": "incident-001",                     // 按时间排序后赋的稳定 ID
      "type": "java_crash",                     // java_crash | native_crash | anr | process_death
      "process": "com.example.app",
      "pid": 1234,
      "triggered_at": "2026-05-21 10:05:12.345",
      "severity": "fatal",                      // fatal | error | warning
      "summary": "java.lang.NullPointerException: Attempt to invoke virtual method ...",
      "evidence": {
        "logcat_slice_file": "java_crash_2026-05-21_10-05-12.345_com.example.app_pid1234.txt",
        "trace_file": null,                     // native 时 = tombstone 文件名；anr 时 = .trace 文件名
        "exception_class": "java.lang.NullPointerException",
        "signal": null,                         // 仅 native_crash 有：SIGSEGV / SIGABRT / ...
        "fault_addr": null,                     // 仅 native_crash
        "reason": null,                         // anr / process_death 才有：Input dispatching timed out 等
        "top_frames": [
          "com.example.app.MainActivity.onResume(MainActivity.java:42)",
          "android.app.Activity.performResume(Activity.java:7117)"
        ],
        "source": "logcat",                     // logcat | dropbox | watcher
        "dedup_count": 1,                       // 5s 窗口内合并的同事件数（v1 永远=1）
        "fallback_reason": null,                // tombstone / ANR trace 拉取失败时填原因
        "device_ts": "05-21 10:05:12.345"       // logcat 行上的原始设备侧时间戳（已无年份）
      }
    }
    // 可能数十条
  ],

  "lifecycle_events": [
    {
      "timestamp": "2026-05-21 10:05:13.000",
      "process": "com.example.app",
      "event": "gone",                          // new | restart | gone
      "old_pid": 1234,
      "new_pid": 0,
      "gap_sec": 0.0                            // restart 时：消失到重新出现的间隔秒数
    }
  ],

  "bookmarks": [                                // 用户手动打的时间轴锚点（可为空数组）
    { "timestamp": "2026-05-21 10:10:00.000", "label": "scenario_login_done", "metadata": {} }
  ],

  "data_files": {
    "events":    ["events_2026-05-21_10.csv"],
    "lifecycle": ["lifecycle_2026-05-21_10.csv"],
    "logcat":    ["logcat_2026-05-21_10.log"]
  }
}
```

### 字段补充

**`run.exit_reason` 取值与颜色**：

| exit_code | exit_reason | 含义 | 颜色 |
|---|---|---|---|
| 0 | `duration_elapsed` | 正常跑完时长 | 绿色 |
| 0 | `interrupted_user` | Ctrl+C 等正常中止（如有这种状态）| 绿色 |
| 1 | `exception` | `with StabilityTest(...) as t:` 内业务代码抛出异常 | 橙色 |
| 2 | `setup_failed` / `adb_unavailable` | preflight 失败 | 红色 |
| 3 | `wait_timeout` | 等待目标进程超时 | 红色 |

> **重要**：检测到稳定性问题（即使大量 incidents）**不会**导致 exit_code ≠ 0。退出码 0 反映的是"测试是否跑完"，而非"测试结果是否健康"。报告需要在视觉上明确区分这两个概念——**"测试正常结束"和"测试发现问题"是正交的**。

**`severity` 取值约定**：

- `fatal` — 进程级致命：java_crash、native_crash
- `error` — 应用功能性失败：anr
- `warning` — 非确定性问题：process_death（可能是正常退出，也可能是异常）

---

## 三、CSV / LOG 时序数据结构

### events CSV（`events_YYYY-MM-DD_HH.csv`）

```
# stability_auto_test/events/v1
timestamp,event_type,process_name,pid,severity,summary
2026-05-21 10:05:12.345,java_crash,com.example.app,1234,fatal,"java.lang.NullPointerException: ..."
```

- 每行一个事件（与 incident 一一对应；incidents/*.json 是完整体，events.csv 是索引行）
- 不需要在 HTML 直接展示这个表（已被 incidents 列表覆盖），但**数据文件索引区**要列出文件名

### lifecycle CSV（`lifecycle_YYYY-MM-DD_HH.csv`）

```
# stability_auto_test/lifecycle/v1
timestamp,process_name,event,old_pid,new_pid,gap_sec
2026-05-21 10:00:00.500,com.example.app,new,0,1234,0.000
2026-05-21 10:05:13.000,com.example.app,gone,1234,0,0.000
2026-05-21 10:05:15.000,com.example.app,restart,1234,5678,2.000
```

- 同 perf 的 lifecycle CSV
- 在 HTML 的"生命周期事件表"区域全量展示

### logcat LOG（`logcat_YYYY-MM-DD_HH.log`）

```
# stability_auto_test/logcat/v1
05-21 10:00:00.123  1234  1234 I MyApp   : starting up
05-21 10:05:12.345  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main
...
```

- 原始 logcat 流，**体积大**（可能数十 MB / h），不能整段嵌入 HTML
- HTML 上仅提供文件名链接，让用户在文件管理器/编辑器中打开
- 每条 incident 自带"现场快照 slice"（`incidents/<base>.txt`），已是该 incident 周边的相关行，无需查全量 logcat

---

## 四、设计原则与视觉灵魂

### 4.1 整体调性

参考主流崩溃监控平台（**Sentry / Bugsnag / Firebase Crashlytics**）和 Material/PatternFly 的告警 UI 模式，本报告页面应该：

1. **3 秒判定**：用户打开页面，3 秒内必须能回答"这次跑测的稳定性结论是什么"。这意味着顶部必须有一个**英雄状态条 (hero status)**，颜色 + 大字号 + 极简文字告诉用户：✅ 稳定 / ⚠️ 有问题 / 🔴 严重异常。
2. **事件为中心**：相比 perf 的"时序图为中心"，stability 的核心是**离散事件**，所以 incidents 是页面主角。每条 incident 都要清晰展示"何时、何处、是什么、有多严重"。
3. **空状态友好**：零事件是稳定性测试的**理想结果**，页面在零事件时不应显得空洞或"出错了"，而要让用户感到"测试已完成，目标 App 表现稳定"。
4. **渐进式信息密度**：默认视图只展示"摘要 + 关键事件"；详情（堆栈、原始 logcat、tombstone）默认折叠或弹层（modal/drawer）打开。

### 4.2 信息层级（自上而下）

```
┌─────────────────────────────────────────────────────────────────┐
│ A. 顶部 Header 栏（粘性 sticky-top）                              │
│    - 报告标题（包名）                                              │
│    - 运行元数据（设备 / Android 版本 / 时长 / 退出状态徽章）        │
├─────────────────────────────────────────────────────────────────┤
│ B. 英雄状态条（HERO） ★★★ — 3 秒判定区                            │
│    - 整体稳定性结论（一句话）                                      │
│    - 4 个事件计数卡片（Java/Native crash, ANR, process_death）    │
│    - 关键派生指标（uptime_ratio 平均、总重启次数）                  │
├─────────────────────────────────────────────────────────────────┤
│ C. 事件时间轴 (Plotly) — 1 张图回答"何时发生了什么"                │
│    - X = 时间，Y = 事件类型（4 行 swimlanes）                      │
│    - 每条 incident 一个标记，颜色按类型                            │
│    - 生命周期 restart 以橙色竖线穿过                               │
│    - bookmark 以蓝色虚线穿过 + 文字标注                            │
├─────────────────────────────────────────────────────────────────┤
│ D. 进程稳定性总表                                                  │
│    - 每个进程一行                                                  │
│    - 关键列：uptime% / 重启 / 4 类事件计数                         │
├─────────────────────────────────────────────────────────────────┤
│ E. Incidents 区 — 页面占比最大的内容区                             │
│    - 顶部筛选条：按类型/进程过滤 + 搜索框                          │
│    - 卡片列表，每张卡片代表一条 incident                           │
│    - 卡片默认显示摘要，点击展开/弹出 modal 看堆栈与现场             │
├─────────────────────────────────────────────────────────────────┤
│ F. 生命周期事件表                                                  │
│    - 全量 lifecycle_events，可按类型筛选                           │
├─────────────────────────────────────────────────────────────────┤
│ G. 底部折叠抽屉（默认收起）                                        │
│    - G1. 书签 (bookmarks)                                          │
│    - G2. 跑测配置 (config_effective)                              │
│    - G3. 文件索引 (data_files + incidents/ 列表)                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 颜色语义

按"语义优先"原则统一全站颜色（参考 PatternFly status & severity tokens）：

| 语义 | 颜色 | HEX | 用途 |
|------|------|------|------|
| 稳定 / 成功 | 绿 | `#10b981` | 0 incidents 时英雄态、`new` 生命周期、`heap_status=ok`（如有）|
| 警告 / 重启 | 橙 | `#f59e0b` | `restart` 生命周期、ANR 标记、uptime 偏低警示、`fallback_reason` |
| 致命 / 错误 | 深红 | `#dc2626` | `java_crash`、`native_crash`、exit_code ≥ 2 |
| 严重危险 | 暗红 | `#7f1d1d` | `native_crash` 专用（区别于 Java，强调"更严重"）|
| 进程死亡 | 中性灰 | `#6b7280` | `process_death`、`gone` 生命周期 |
| 书签 | 蓝 | `#3b82f6` | bookmark 竖线 + 文字 |
| 中性背景 | 暖灰 | `#f9fafb` / `#f3f4f6` | 表格行底、抽屉背景 |
| 主文字 | 深 | `#111827` | 标题、关键字段 |
| 次文字 | 灰 | `#6b7280` | 元数据、注释 |

**重要**：所有"严重程度"标记都至少同时使用 **颜色 + 图标/文字**，避免色盲用户无法分辨（WCAG AA）。Java crash 标 🔴 + "Java crash"，不只是红色圆点。

### 4.4 字体

- 普通文字：系统 sans-serif 栈
  `-apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", "PingFang SC", sans-serif`
- 进程名 / PID / 文件名 / 异常类全限定名 / 堆栈 / 时间戳：等宽字体
  `"SF Mono", "JetBrains Mono", Consolas, "Liberation Mono", monospace`
- 大数字（英雄区计数卡）：等宽数字字体（`font-variant-numeric: tabular-nums`），避免数字宽度跳动。

### 4.5 间距与节奏

- 区块之间 **48px** 间距，块内组件 **16~24px**，避免视觉密集
- 卡片圆角 **8px**，hover 时投影从 `0 1px 2px` 提升到 `0 4px 12px`
- 移动端不要求适配，**最小宽度 1024px**；窄视口下表格允许横向滚动

### 4.6 响应式与暗色模式

- 浅色模式为主；可选支持 `prefers-color-scheme: dark` 自动切换（暗色背景 `#0f172a`、主文字 `#e2e8f0`）
- 暗色模式下事件颜色保持饱和度但略调暗（红色用 `#ef4444`、绿色用 `#34d399`）

---

## 五、各区域详细规格

### A. 顶部 Header（粘性 sticky-top，z-index 高）

**功能**：始终告诉用户"这是哪个 App、哪次跑测、是否正常结束"。

**布局**：左对齐标题 + 右对齐元数据条。

| 元素 | 内容 | 样式 |
|------|------|------|
| 标题 | "Stability Report — `com.example.app`" | 24px 粗体，包名等宽字体 |
| 子标题 | "5m · 2026-05-21 10:00 → 10:30 UTC" | 13px 灰色（duration 由 `duration_sec` 格式化为 `Xh Ym Zs`）|
| 设备徽章 | "📱 emulator-5554 · Android 14 (SDK 34) · 4 cores" | 13px 灰色 |
| 退出状态徽章 | "✓ duration_elapsed" / "✗ wait_timeout" | 按 4.2 退出码表着色，圆角 pill |

滚动至下方时，Header 收缩为单行"标题 + 退出徽章"，节省视觉空间。

---

### B. 英雄状态条（HERO）★★★ 重点区

**这是整页最重要的视觉焦点**。打开报告 3 秒内必须看清结论。

#### B.1 结论横幅

横跨整页的一行横幅，根据事件总数和退出码自动决定文案与颜色：

| 条件 | 横幅 | 颜色 |
|------|------|------|
| `exit_code != 0` | "⚠ 测试未正常完成（`<exit_reason>`），数据可能不完整" | 红 |
| `exit_code == 0` 且所有进程 events 全为 0 | "✅ 未发现稳定性问题，目标 App 表现稳定" | 绿 |
| `exit_code == 0` 且只发现 `process_death` | "⚠ 测试期间观察到进程退出/重启，未捕获 crash / ANR" | 橙 |
| `exit_code == 0` 且发现 `anr` 但无 crash | "⚠ 测试期间触发 N 次 ANR，未捕获 crash" | 橙 |
| `exit_code == 0` 且发现 `java_crash` 或 `native_crash` | "🔴 测试期间发生 N 次崩溃 / M 次 ANR" | 红 |

文案下方紧跟"建议下一步"小字（仅在有事件时显示）：
> "查看下方 Incidents 区域定位首条 incident · 检查 incidents/ 目录中的 logcat slice"

#### B.2 4 个事件计数卡片

横向排列 4 张大卡片，每张展示一类事件的总数：

```
┌─ Java crash ──────┐  ┌─ Native crash ─────┐  ┌─ ANR ──────────────┐  ┌─ Process death ────┐
│                   │  │                    │  │                    │  │                    │
│       3           │  │       0            │  │       2            │  │       5            │
│                   │  │                    │  │                    │  │                    │
│ 涉及 1 个进程     │  │ —                  │  │ 涉及 1 个进程       │  │ 含 4 次 restart    │
│ ▁▂▁▃▁▁▂▁ (火花)  │  │                    │  │ ▁▁▂▁▁ (火花)        │  │ ▁▂▂▁▁▃▁          │
└───────────────────┘  └────────────────────┘  └────────────────────┘  └────────────────────┘
```

每张卡片：
- 头部：图标 + 类型名（颜色按 4.3）
- 中部：大号事件总数（最大字号 48px，零值灰显）
- 底部 1：派生信息（涉及进程数；process_death 卡片显示"含 N 次 restart"）
- 底部 2：**火花线 (sparkline) — 时间分布**：把跑测时长等分为 ~30 个桶，每个桶统计该类事件数，画成迷你直方图。直观回答"事件是集中爆发还是均匀分散"。零事件时不显示火花线。
- **点击整张卡片** → 跳转/滚动到 Incidents 区域并自动应用对应类型过滤器（联动）

#### B.3 派生指标小条（可选）

英雄区底部一行：
> "进程平均在线率 99.5% · 共发生 1 次重启 · 监听了 logcat 4 个 buffer · 取样失败 logcat=0 dropbox=1"

`uptime_ratio` 平均、重启总数、logcat buffer 数、采样失败计数 4 个指标。`sample_failures.dropbox > 0` 时该数字渲染为橙色。

---

### C. 事件时间轴（Plotly）

**目的**：用一张图回答"事件何时发生 / 是否集中爆发 / 与 bookmark / 重启有没有相关性"。

**实现**：单个 Plotly `Scatter` 图（不是 swimlanes 的 subplot，而是单图 Y 轴为分类轴）。

#### 子图规格

- **Y 轴**：分类轴，4 个固定类别（从下到上：`process_death` / `anr` / `native_crash` / `java_crash`），即使该类零事件也保留行（保证布局稳定）
- **X 轴**：时间轴，范围固定为 `[run.started_at, run.ended_at]`，无论有没有事件
- **每条 incident**：在 `(triggered_at, type)` 位置画一个标记
  - 形状：✕ (`marker.symbol="x"`)
  - 大小：18px，hover 时 22px
  - 颜色：按 4.3 类型颜色
  - hover tooltip 多行：
    ```
    incident-001
    java_crash · com.example.app (pid=1234)
    2026-05-21 10:05:12.345 UTC
    java.lang.NullPointerException: ...
    ```
- **bookmarks**：每条 bookmark 在 `timestamp` 处画一条**蓝色虚线**，从图底贯穿到图顶；线上方加文字标注（`label`，最多 20 字符）
- **lifecycle restart**：每条 `event="restart"` 在 `timestamp` 处画一条**橙色实线**，贯穿全图（不加文字，避免拥挤）
- **lifecycle gone**：不画线（已经有 process_death incident 表示），避免重复
- **空状态**：4 条 swimlane 显示为 4 条灰色虚线，中央文字"No stability events recorded during this run."（淡灰，居中）

#### 全局要求

- 图高 **320~400px**
- 启用 Plotly 工具栏（缩放 / 重置 / 下载 PNG），不需要 lasso/select
- 不需要降采样（事件数 ≪ perf 的连续指标），即使长跑也通常 ≤ 数百条 incidents
- `hovermode="closest"`（不是 `x unified`，因为 Y 轴是分类，不应聚合）
- 点击图上某个 ✕ 标记 → 滚动到 Incidents 区域，并把对应 incident 卡片高亮 1~2 秒（"💡" 闪烁）

---

### D. 进程稳定性总表

**位置**：时间轴下方。

**表格列**：

| 列 | 数据来源 | 备注 |
|---|---|---|
| 进程名 | `name` | 等宽字体；过长截断显示，hover 显示全名；左 4px 颜色条颜色为该进程衍生（同色保证多处一致）|
| 首次发现 | `first_seen_at` | 格式化 `HH:mm:ss` |
| 最后发现 | `last_seen_at` | 同上 |
| 在线率 | `uptime_ratio` | 百分比 + 水平进度条；`< 0.90` 橙色 |
| 重启次数 | `restart_count` | 0 灰色 "—"，> 0 橙色徽章 |
| Java crash | `events.java_crash` | 0 灰，> 0 红色徽章；点击 → Incidents 区域过滤为"该进程 + java_crash" |
| Native crash | `events.native_crash` | 同上，暗红 |
| ANR | `events.anr` | 同上，橙 |
| Process death | `events.process_death` | 同上，灰 |
| 采样失败 | `sample_failures.logcat + sample_failures.dropbox` | 灰；> 0 时橙色，hover tooltip 拆分显示两源各自次数 |

**多进程**：超过 5 个进程时表格分页或显示前 5 + "展开剩余 N 个"。
**单进程**：表格仍然渲染（不要变成单卡片），保持版面一致。

---

### E. Incidents 区（页面占比最大）

**目的**：把每条 incident 的"是什么 / 发生在哪 / 在哪里看现场"清晰、可筛选地呈现。

#### E.1 顶部筛选条（粘性 sticky）

```
┌─────────────────────────────────────────────────────────────────────┐
│  [ All (N) ]  [ Java crash (3) ]  [ Native crash (0) ]  [ ANR (2) ]  │
│  [ Process death (5) ]                                                │
│                                                                       │
│  进程：[ All processes ▾ ]   严重程度：[ All ▾ ]                      │
│  搜索：[ 🔍 异常类 / 摘要 / 进程名 ___________ ]   排序：[ 时间倒序 ▾ ]│
└─────────────────────────────────────────────────────────────────────┘
```

- **类型 chips**：可多选切换；当前激活 chip 用对应类型颜色填充背景；零事件 chip 灰显且不可点击
- **进程下拉**：从所有 `incidents[*].process` 提取去重列表
- **严重程度下拉**：fatal / error / warning
- **搜索框**：客户端 fuzzy 匹配 `summary` / `exception_class` / `process` / `top_frames`
- **排序**：时间倒序（默认）/ 时间正序 / 严重程度

**Chip 联动**：英雄区 B.2 计数卡片点击 / 时间轴 C 上 ✕ 标记点击 → 自动设置对应 chip + 滚动到此区。

#### E.2 incident 卡片（默认折叠摘要）

每条 incident 渲染一张卡片：

```
┌────────────────────────────────────────────────────────────────────────┐
│ [ID]incident-001  [TYPE_BADGE]Java crash  [SEV_BADGE]fatal             │ ← 左侧 4px 颜色条
│ 2026-05-21 10:05:12 UTC  ·  com.example.app  ·  pid=1234              │
│                                                                          │
│  java.lang.NullPointerException: Attempt to invoke virtual method ...   │ ← summary，单行，超长截断 + …
│                                                                          │
│  at com.example.app.MainActivity.onResume(MainActivity.java:42)         │ ← top_frames[0]，等宽
│  at android.app.Activity.performResume(Activity.java:7117)              │ ← top_frames[1]
│  + 5 more frames                                                         │ ← 折叠提示
│                                                                          │
│  source: logcat  ·  dedup: 1                                            │
│                                          [ 查看详情 → ]                  │ ← 点击打开 modal/drawer
└────────────────────────────────────────────────────────────────────────┘
```

- 左侧 **4px 垂直颜色条** = 类型颜色（视觉锚点）
- 摘要区只显示前 2 条 top_frames，剩余用 `+ N more frames` 折叠
- 卡片整体可点击（光标 pointer），点击 → **打开详情侧滑抽屉（Drawer）或全屏 Modal**（任选其一，推荐右侧 Drawer 600~720px 宽，能与时间轴 / 列表并排观察）
- 卡片右下角的 "查看详情" 按钮重复打开抽屉，主要供键盘可访问

#### E.3 详情 Drawer / Modal（每类 incident 内容不同）

**通用 header**：

```
[ ← 返回列表 ]
incident-001
─────────────────────────────────────────────────────
java_crash · fatal
2026-05-21 10:05:12.345 UTC
com.example.app · pid=1234

源 (source): logcat (with dedup_count = 1)
原始设备时间戳: 05-21 10:05:12.345
```

##### Java crash 详情

```
摘要
java.lang.NullPointerException: Attempt to invoke virtual method 'java.lang.String com.example.Foo.bar()' on a null object reference

异常类
java.lang.NullPointerException

Top 栈帧（最多 10 条）
┌──────────────────────────────────────────────────────────────────┐
│ #0  com.example.app.MainActivity.onResume(MainActivity.java:42)  │
│ #1  android.app.Activity.performResume(Activity.java:7117)       │
│ #2  android.app.ActivityThread.performResumeActivity(...)        │
│ ...                                                                │
└──────────────────────────────────────────────────────────────────┘
（每行可点击复制；用户的应用包名所在行用浅黄高亮，便于一眼定位业务代码）

原始 logcat 现场快照
┌──────────────────────────────────────────────────────────────────┐
│ 05-21 10:05:12.345  1234  1234 E AndroidRuntime: FATAL EXCEPTION │
│ 05-21 10:05:12.345  1234  1234 E AndroidRuntime: Process: ...    │
│ ...                                                                │
└──────────────────────────────────────────────────────────────────┘
[ ⬇ 在新标签打开 incidents/java_crash_..._.txt ]
```

##### Native crash 详情

新增字段：
- **信号 (signal)**：`SIGSEGV` 等，旁边附中文说明（"段错误：内存非法访问"）
- **fault addr**：`0x0`（如果有）
- **tombstone 文件**：若 `evidence.trace_file` 不为 null，提供"在新标签打开"链接；若为 null，显示橙色提示 `evidence.fallback_reason`（如 "no accessible tombstone (likely non-root user build)"）
- 栈帧来自 `top_frames`，每条形如 `#NN pc XXX /system/lib/.../libfoo.so (sym+0x10)`，等宽显示

##### ANR 详情

新增字段：
- **原因 (reason)**：`evidence.reason`（如 "Input dispatching timed out (..., ...)"）
- **ANR trace 文件**：若 `evidence.trace_file` 不为 null，链接打开；若为 null，显示 `fallback_reason`
- ANR 的 top_frames 通常为空（除非从主 buffer 解析得到，大多情况依赖 trace file）

##### Process death 详情

新增字段：
- **来源 (source)**：通常是 `watcher`（进程 watcher 检测 pid 消失），也可能是 `logcat`（events buffer `am_proc_died` / `am_kill`）
- **原因 (reason)**：`evidence.reason`（events buffer 给出时填，watcher 不给）
- 若 `raw_lines` 非空（来自 events buffer），展示这些行

#### E.4 空状态

零 incidents 时整个 E 区域显示一个大空状态：

```
┌─────────────────────────────────────────────────┐
│                                                  │
│              ✅                                   │
│   No stability events captured.                  │
│   Run completed without crashes, ANRs,           │
│   or process deaths.                             │
│                                                  │
└─────────────────────────────────────────────────┘
```

绿色调，居中，不要展示空筛选条（隐藏整个 E.1）。

---

### F. 生命周期事件表

**位置**：Incidents 区下方。

**表格列**（数据来自 `lifecycle_events[]`）：

| 列 | 数据 | 备注 |
|---|---|---|
| 时间 | `timestamp` | `HH:mm:ss.SSS` |
| 进程 | `process` | 等宽，左 4px 颜色条按进程衍生 |
| 事件 | `event` | 徽章：🟢 new / 🟠 restart / ⬜ gone |
| 旧 PID | `old_pid` | 0 显示 `—` |
| 新 PID | `new_pid` | 0 显示 `—` |
| 离线间隔 | `gap_sec` | restart 时显示 `<value>s`；其他显示 `—` |

**筛选条**：按事件类型多选 chip（与 Incidents 区同款样式）。
**空状态**：显示 "No lifecycle events recorded."。

---

### G. 底部抽屉（默认收起）

3 个 `<details><summary>` 折叠面板，纵向堆叠：

#### G.1 书签 (Bookmarks)

仅当 `bookmarks` 非空时显示。表格：

| 时间 | 标签 | 元数据 |
|---|---|---|
| `2026-05-21 10:10:00.000 UTC` | `scenario_login_done` | `{}` (JSON 折叠展示) |

#### G.2 配置 (Effective Config)

`run.config_effective` 全量，按业务分组：

**基本**
- 包名 / 设备 / 等待目标进程超时 / 重扫间隔 / 进程过滤

**采集**
- logcat enabled / buffers / reconnect backoff
- dropbox enabled / poll interval

**检测开关**
- enable_java_crash / enable_native_crash / enable_anr / enable_process_death
- dedup window

**Dump**
- pre/post context / max_incidents_per_type / max_concurrent_dumps
- pull_tombstone / pull_anr_trace

**输出**
- emit_html / status_interval_sec

每项为 `key: value` 行，等宽。

#### G.3 文件索引 (Files)

```
report.json          权威结构化结果
report.html          本文件
status.json          运行心跳（runtime-only）
bookmarks.jsonl      书签追加写文件
events_*.csv         事件时序流（按小时滚动，共 N 个文件）
lifecycle_*.csv      进程生命周期（共 N 个文件）
logcat_*.log         原始 logcat 流（共 N 个文件，总大小 X MB）
incidents/           现场快照目录（共 M 个 .json / N 个 .txt / ... 个 .tombstone / .trace）
```

数据来自 `data_files` + `incidents/` 目录扫描（在 `render` 时统计），文件名渲染为本地相对路径文字（不强制做超链接，但浏览器若打开本地路径可点开则更好）。

---

## 六、交互行为汇总

| 触发 | 行为 |
|------|------|
| 顶部 Header 包名旁的"复制"图标 | 复制 `run.package` 到剪贴板 |
| 英雄区计数卡片整张点击 | 滚动到 Incidents 区 + 应用对应类型 chip |
| 时间轴 ✕ 标记点击 | 滚动到 Incidents 区 + 高亮对应卡片 1.5s |
| 进程稳定性总表的事件计数单元格点击 | 滚动到 Incidents 区 + 应用"该进程 + 该类型"组合过滤 |
| Incidents chip / 下拉 / 搜索框 | 客户端实时过滤（无需刷新）|
| Incidents 卡片整体点击 | 打开右侧详情 Drawer |
| Drawer 中堆栈帧行点击 | 复制该行到剪贴板，按钮闪一下表示成功 |
| Drawer 中"打开文件"链接 | 在新标签打开 `incidents/<file>`（浏览器视情况下载或预览）|
| 顶部 Header 滚动出视口 | Header 变薄（28px 高），只保留包名 + 退出徽章 |
| 键盘 `/` | 聚焦 Incidents 搜索框 |
| 键盘 `Esc` | 关闭 Drawer / 清空搜索 |
| 键盘 `↑ ↓` | Incidents 列表中切换 / 选中（可选实现）|

---

## 七、视觉素材与技术建议

### 7.1 图标

优先使用内联 SVG 或 Unicode：
- Java crash: ☕ / `</>` / 红色实心圆
- Native crash: ⚙ / 暗红色齿轮
- ANR: ⏱ / 橙色沙漏
- Process death: ⊘ / 灰色禁止符
- 退出 OK: ✓ Heroicons `check-circle`
- 退出失败: ✗ Heroicons `x-circle`
- 重启: ⟳
- 书签: 🔖 / Heroicons `bookmark`

不要外链字体图标库（CSP / 离线访问考虑）。

### 7.2 Plotly 引入

- CDN：`https://cdn.plot.ly/plotly-2.30.0.min.js`（与现有代码一致）
- 数据通过 `<script type="application/json" id="report-data">...</script>` 嵌入，避免内联到 onload
- 客户端 JS 读取并初始化 Plotly + 交互（事件 chip 过滤、Drawer 等）

### 7.3 渲染体积

- 极端情况（200 incidents × 10 frames × 100 字符 + 30 个 raw_lines）≈ 200 × 4KB ≈ 800KB 嵌入数据
- 加上 HTML/CSS/JS 约 1.2 MB 总体积，可接受
- 真要进一步压缩，可用 `data_files` 引用而非嵌入 raw_lines，让 Drawer 用 `fetch` 加载 `incidents/<base>.txt`（需在文件协议下也工作 → fallback 到嵌入数据）

### 7.4 入口函数签名（建议）

```python
def render(result: dict, output_dir: Path | None = None) -> str:
    """返回完整的 HTML 字符串。
    - result: report.json 解析后的 dict
    - output_dir: 可选，若提供则用来统计 incidents/ 目录文件
    """

def write(result: dict, output_dir: Path) -> Path:
    """调用 render(result, output_dir) 并写入 output_dir/report.html，返回文件路径。"""
```

---

## 八、边界情况处理

| 情况 | 期望行为 |
|------|----------|
| `incidents` 为空数组 | 英雄区显示绿色稳定横幅；时间轴显示 4 条空 swimlane + 居中文字；Incidents 区显示绿色空状态 |
| `bookmarks` 为空 | 时间轴不画蓝色竖线；底部抽屉不显示 G.1 |
| `lifecycle_events` 为空 | F 表显示空提示；时间轴不画橙色竖线 |
| `exit_code != 0` 但 incidents 为空 | 英雄区显示红色"测试未正常完成"横幅；其余区域仍然渲染（exit_reason 在 Header 徽章里清晰可见） |
| `processes` 为空（极端：preflight 都过了但 wait_timeout 没等到进程） | D 表显示空提示"No processes observed during this run." |
| `evidence.trace_file` 为 null | Drawer 详情显示橙色提示 + `fallback_reason`，**不要**显示坏掉的下载链接 |
| `evidence.top_frames` 为空 | Drawer 显示 "No stack frames captured."（ANR 常见，因为依赖外部 trace file）|
| `evidence.raw_lines` 嵌入数据为空（如 process_death from watcher） | Drawer 不显示"原始 logcat 现场快照"区块，避免空表 |
| 多进程跑测（如 Chrome 沙箱 N 个进程） | D 表分页 / 折叠；时间轴上事件颜色仍按 type 着色（不需要按 process 区分颜色，避免色彩混乱）|
| 长跑（24h，1000+ incidents） | 列表虚拟滚动（virtual scroll）或分页 50/页；时间轴 ✕ 标记可能堆叠，启用 hover 聚合提示 |
| 同时间多事件 | 时间轴 ✕ 标记可能重叠，hover 时聚合显示 "3 events at this time, click to expand"，点击 → 列表过滤到这一时刻 ±1s |
| dropbox/logcat 取样失败计数大 | 英雄区底部派生指标条以橙色提示；进程表"采样失败"列加红色徽章 |
| HTML 在文件协议 (`file://`) 下打开 | `fetch` 跨源被禁，需要 fallback 到嵌入数据；测试时验证 `open report.html` 也能完整工作 |

---

## 九、设计风格参考

以下是本设计借鉴的成熟产品 / 设计系统，Claude Design 可参考其细节：

- **Sentry Issues**：事件分组、严重度色码、Top frames 高亮业务包名行
- **Bugsnag Timeline**：事件时间轴 + 上下文标记
- **Firebase Crashlytics**：Crash-Free Sessions 大字号 KPI + sparkline 趋势
- **PatternFly Status & Severity**：图标 + 颜色 + 文字三位一体的严重度标识
- **Material 3 Cards**：卡片圆角、elevation 层级、hover 过渡
- **Heroicons** / **Lucide**：内联 SVG 图标库（MIT 协议）
- **Tailwind CSS**：颜色 token 系统（实际实现可不依赖 Tailwind，但颜色比例可借鉴）

---

## 十、最小可行实现 (MVP) 与扩展项

如果一次性实现工作量过大，按优先级分两批：

### MVP 必做

1. A 区 Header 完整
2. B 区结论横幅 + 4 计数卡（无 sparkline 也可）
3. C 区 Plotly 时间轴（标记 + bookmark 竖线，最低限度）
4. D 区进程总表
5. E 区 incidents 列表卡片（默认折叠 + 点击展开 / 简单 modal）
6. G 区底部 3 个折叠面板

### 扩展项（v1.1）

1. B.2 计数卡片的火花线 sparkline
2. C 时间轴 ✕ 标记 ↔ E 卡片高亮联动
3. E.1 多维筛选（chip + 进程 + 严重度 + 搜索 + 排序）
4. 详情 Drawer（右侧滑入，键盘可访问）
5. 业务包名行的浅黄高亮
6. 暗色模式
7. 长列表虚拟滚动

---

## 附录：当前 html.py 实现概览（便于对比）

当前 `sat/reporter/html.py` 已实现（**作为最小骨架**，不算最终设计）：
- Header（标题 + 单行 meta）
- 4 个 counters 大数字卡（无 sparkline）
- 单个 Plotly Scatter（无 swimlane 行序固定）
- 进程稳定性总表
- Incidents 列表使用 `<details>` 折叠（无搜索、无类型 chip）
- 无 Drawer / Modal / 联动 / 暗色 / 富交互

重设计的目标：基于上述 MVP 骨架，按本文档要求**升级为完整产品级报告**。
