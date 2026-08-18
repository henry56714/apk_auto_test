# Fault Lab

专用故障注入 APK（仅测试用，会故意崩溃/卡死）。包名
`com.example.faultlab`，Kotlin + C++/CMake，minSdk 23 / target 35，
包含主进程与 `:remote` 进程。**无网络权限，无账号/联系人/位置权限。**

## 构建

```bash
cd test_apps/fault_lab
ANDROID_HOME=$HOME/Library/Android/sdk gradle assembleDebug
# 产物: app/build/outputs/apk/debug/app-debug.apk
```

## 统一 ADB 接口

```bash
FAULT_PKG=com.example.faultlab
FAULT_RECEIVER=com.example.faultlab/.FaultReceiver

adb install -r -t app-debug.apk
adb shell am start -W -n "$FAULT_PKG/.MainActivity"

adb shell am broadcast -n "$FAULT_RECEIVER" \
  -a com.example.faultlab.TRIGGER \
  --es fault JAVA_MAIN_CRASH --es fault_id java-main-001

adb shell am broadcast -n "$FAULT_RECEIVER" \
  -a com.example.faultlab.RESET
```

每个 action 触发前输出稳定 marker：

```text
SAT_FAULT_BEGIN id=<uuid> type=<FAULT_TYPE> process=<name>
```

可继续运行的 action 输出 `SAT_FAULT_READY` / `SAT_FAULT_END`；致命 fault
由 ExitInfo/lifecycle 确认结束。主线程 fault 一律在 receiver 返回后
`Handler.post` 执行，避免被系统归类为 broadcast ANR/crash。

Fault receiver 只存在于普通 manifest 但受 `android.permission.DUMP` 保护
（shell 可持有、普通三方 App 不持有）。

## Fault 清单

| Fault ID | 行为 |
|---|---|
| `JAVA_MAIN_CRASH` | main looper 抛 RuntimeException |
| `JAVA_BG_CRASH` | 后台线程未捕获异常 |
| `STARTUP_CRASH` | 写 flag，下一次 Application.onCreate 崩溃 |
| `NATIVE_SIGSEGV` / `NATIVE_SIGABRT` / `NATIVE_STACK_OVERFLOW` | JNI 崩溃（固定 abort 消息 `sat-abort-42`） |
| `ANR_INPUT_SLEEP` / `ANR_MAIN_DEADLOCK` / `ANR_MAIN_BUSY` / `ANR_BROADCAST` / `ANR_SERVICE` | 各类 ANR |
| `SELF_EXIT` / `REMOTE_PROCESS_EXIT` | 正常退出（主进程 / :remote） |
| `JAVA_OOM` | 受控分块分配直到 OOM |
| `FD_LEAK` / `THREAD_LEAK` / `NATIVE_HEAP_LEAK` | 资源泄漏（RESET 可恢复） |
| `WAKELOCK_LEAK` | 获取 partial wakelock 直到 reset |
| `SENSITIVE_LOG` | 固定 email/token/location canary（脱敏测试） |
| `LOG_STORM` | 可停止的日志风暴 |
| `DISK_FILL_APP` | 只填充 App sandbox 到安全上限 |
| `SQLITE_CORRUPT` | 破坏测试 DB header 后访问 |

`RESET` 停止 service、释放 FD/线程/wake lock、删除大文件/DB、清 startup
crash flag。进程已死时测试框架用 `am force-stop` + `pm clear` 兜底。

Fault Lab 不会尝试让整台设备 OOM、触发 kernel panic 或无限填满 `/data`。
