package com.example.stabilityfaultlab

import android.app.AlarmManager
import android.content.Context
import android.os.Process
import android.os.SystemClock
import android.util.Log
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.system.exitProcess

/**
 * Fault implementations. Every fault first prints a stable marker:
 *
 *     SAT_FAULT_BEGIN id=<uuid> type=<FAULT_TYPE> process=<name>
 *
 * which the host tool uses for fusion/action-window correlation. RESET stops
 * services, releases wake locks, closes leaked FDs, deletes big files and
 * clears the startup-crash flag.
 */
object FaultRunner {
    private const val TAG = "SAT"
    private const val STARTUP_CRASH_FLAG = "startup_crash.flag"
    private val leakedFds = java.util.concurrent.CopyOnWriteArrayList<java.io.FileOutputStream>()
    private var leakedThreadsActive = AtomicBoolean(false)
    private var wakelockHeld = AtomicBoolean(false)
    private var wakeLock: android.os.PowerManager.WakeLock? = null

    @Volatile
    private var logStormActive = AtomicBoolean(false)

    fun mark(faultId: String, faultType: String) {
        Log.i(
            TAG,
            "SAT_FAULT_BEGIN id=$faultId type=$faultType process=${Process.myProcessName()}"
        )
    }

    /** Self-reported resource sample: readable by the tool even where
     *  Android hides /proc/<pid> from shell (API 35). */
    fun reportResources(faultId: String) {
        val fdCount = runCatching {
            File("/proc/self/fd").listFiles()?.size ?: -1
        }.getOrDefault(-1)
        val threadCount = runCatching {
            File("/proc/self/task").listFiles()?.size ?: -1
        }.getOrDefault(-1)
        val rssKb = runCatching {
            val line = File("/proc/self/status").readLines()
                .firstOrNull { it.startsWith("VmRSS:") } ?: return@runCatching -1
            Regex("\\d+").find(line)?.value?.toInt() ?: -1
        }.getOrDefault(-1)
        Log.i(
            TAG,
            "SAT_RESOURCE_SAMPLE id=$faultId fd_count=$fdCount " +
                    "thread_count=$threadCount rss_kb=$rssKb"
        )
    }

    fun ready(faultId: String) {
        Log.i(TAG, "SAT_FAULT_READY id=$faultId process=${Process.myProcessName()}")
    }

    fun end(faultId: String) {
        Log.i(TAG, "SAT_FAULT_END id=$faultId process=${Process.myProcessName()}")
    }

    fun trigger(context: Context, fault: String, faultId: String) {
        when (fault) {
            "JAVA_MAIN_CRASH" -> {
                mark(faultId, fault)
                throw RuntimeException("SAT injected main-thread crash: $faultId")
            }
            "JAVA_BG_CRASH" -> {
                mark(faultId, fault)
                Thread({
                    throw RuntimeException("SAT injected background crash: $faultId")
                }, "sat-bg-crasher").start()
            }
            "JAVA_MAIN_CRASH_DELAYED" -> {
                // Device-side delayed crash: the host can queue it, then cut
                // the adb transport — the crash fires while logcat is down.
                mark(faultId, fault)
                android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                    throw RuntimeException("SAT injected delayed crash: $faultId")
                }, 10_000)
            }
            "STARTUP_CRASH" -> {
                mark(faultId, fault)
                // Write the flag and exit; the next launch crashes in onCreate.
                File(context.filesDir, STARTUP_CRASH_FLAG).writeText(faultId)
                end(faultId)
                exitProcess(0)
            }
            "NATIVE_SIGSEGV", "NATIVE_SIGABRT", "NATIVE_STACK_OVERFLOW",
            "NATIVE_HEAP_LEAK", "NATIVE_SELFKILL" -> {
                mark(faultId, fault)
                reportResources(faultId)  // pre-leak baseline (rss/fd/threads)
                NativeLib.trigger(fault, faultId)
                if (fault == "NATIVE_HEAP_LEAK") {
                    reportResources(faultId)  // post-leak sample
                }
            }
            "ANR_INPUT_SLEEP" -> {
                mark(faultId, fault)
                ready(faultId) // visible window is already up; now block main
                Thread.sleep(30_000)
                end(faultId)
            }
            "ANR_MAIN_DEADLOCK" -> {
                mark(faultId, fault)
                ready(faultId)
                val lock = Object()
                val holder = AtomicBoolean(false)
                val t = Thread({
                    synchronized(lock) {
                        holder.set(true)
                        Thread.sleep(30_000)
                    }
                }, "sat-lock-holder")
                t.start()
                while (!holder.get()) Thread.sleep(10)
                synchronized(lock) { // main waits forever on the held lock
                    end(faultId)
                }
            }
            "ANR_MAIN_BUSY" -> {
                mark(faultId, fault)
                ready(faultId)
                while (true) { /* CPU-bound main loop */ }
            }
            "ANR_BROADCAST" -> {
                mark(faultId, fault)
                ready(faultId)
                Thread.sleep(60_000) // receiver-poster runs on main
                end(faultId)
            }
            "ANR_SERVICE" -> {
                mark(faultId, fault)
                startRemoteService(context, mode = "block", faultId = faultId)
                end(faultId)
            }
            "SELF_EXIT" -> {
                mark(faultId, fault)
                end(faultId)
                exitProcess(0)
            }
            "REMOTE_PROCESS_EXIT" -> {
                mark(faultId, fault)
                startRemoteService(context, mode = "exit", faultId = faultId)
                end(faultId)
            }
            "JAVA_OOM" -> {
                mark(faultId, fault)
                val chunks = ArrayList<ByteArray>()
                try {
                    while (true) {
                        chunks.add(ByteArray(8 * 1024 * 1024)) // 8 MiB per chunk
                    }
                } catch (oom: OutOfMemoryError) {
                    throw oom
                }
            }
            "FD_LEAK" -> {
                mark(faultId, fault)
                ready(faultId)
                reportResources(faultId)  // pre-leak baseline
                val count = 200
                repeat(count) {
                    leakedFds.add(File("/dev/null").outputStream())
                }
                reportResources(faultId)  // post-leak sample
                end(faultId)
            }
            "THREAD_LEAK" -> {
                mark(faultId, fault)
                ready(faultId)
                reportResources(faultId)  // pre-leak baseline
                leakedThreadsActive.set(true)
                val stop = java.util.concurrent.CountDownLatch(1)
                repeat(60) { i ->
                    Thread({
                        try { stop.await() } catch (_: InterruptedException) {}
                    }, "sat-leak-$i").apply { isDaemon = true }.start()
                }
                // Keep a tiny latch count so RESET can release them.
                latchForThreads = stop
                reportResources(faultId)  // post-leak sample
                end(faultId)
            }
            "WAKELOCK_LEAK" -> {
                mark(faultId, fault)
                ready(faultId)
                reportResources(faultId)
                val pm = context.getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
                wakeLock = pm.newWakeLock(
                    android.os.PowerManager.PARTIAL_WAKE_LOCK, "sat:leak"
                ).apply {
                    acquire(10 * 60 * 1000L)
                }
                wakelockHeld.set(true)
                end(faultId)
            }
            "SENSITIVE_LOG" -> {
                mark(faultId, fault)
                Log.i(TAG, "canary email=alice@example.com token=sk-secret-123 " +
                        "location=31.2304,121.4737 path=/data/user/0/com.example.stabilityfaultlab")
                end(faultId)
            }
            "LOG_STORM" -> {
                mark(faultId, fault)
                logStormActive.set(true)
                Thread({
                    var i = 0
                    while (logStormActive.get() && i < 50_000) {
                        Log.i(TAG, "log storm line $i " + "x".repeat(200))
                        i++
                    }
                }, "sat-log-storm").start()
                end(faultId)
            }
            "DISK_FILL_APP" -> {
                mark(faultId, fault)
                ready(faultId)
                val dir = File(context.filesDir, "fill")
                dir.mkdirs()
                var i = 0
                try {
                    while (i < 60) {
                        File(dir, "blob-$i.bin").writeBytes(ByteArray(4 * 1024 * 1024))
                        i++
                    }
                } catch (_: java.io.IOException) {
                    // sandbox quota reached — the fault is complete
                }
                end(faultId)
            }
            "SQLITE_CORRUPT" -> {
                mark(faultId, fault)
                ready(faultId)
                val db = context.openOrCreateDatabase("corrupt.db", Context.MODE_PRIVATE, null)
                db.close()
                // Corrupt the header directly.
                val f = context.getDatabasePath("corrupt.db")
                val bytes = f.readBytes().toMutableList()
                repeat(minOf(64, bytes.size)) { bytes[it] = 0x55 }
                f.writeBytes(bytes.toByteArray())
                // Next access throws corruption.
                try {
                    android.database.sqlite.SQLiteDatabase.openDatabase(
                        f.path, null, android.database.sqlite.SQLiteDatabase.OPEN_READWRITE
                    ).use { it.rawQuery("select 1", null) }
                } catch (e: Exception) {
                    throw RuntimeException("SAT injected sqlite corruption: ${e.message}")
                }
                end(faultId)
            }
            else -> {
                Log.w(TAG, "unknown fault: $fault")
            }
        }
    }

    @Volatile
    private var latchForThreads: java.util.concurrent.CountDownLatch? = null

    /** Start the :remote service, preferring the FGS path (background-start
     *  restrictions on modern APIs) and falling back to plain startService. */
    fun startRemoteService(context: Context, mode: String, faultId: String) {
        val intent = android.content.Intent(context, RemoteService::class.java)
        intent.putExtra("mode", mode)
        intent.putExtra("fault_id", faultId)
        try {
            context.startForegroundService(intent)
        } catch (bg: IllegalStateException) {
            runCatching { context.startService(intent) }
        } catch (sec: SecurityException) {
            runCatching { context.startService(intent) }
        }
    }

    fun reset(context: Context) {
        Log.i(TAG, "SAT_FAULT_BEGIN id=RESET type=RESET process=${Process.myProcessName()}")
        // Fast synchronous cleanup: the receiver must return quickly or the
        // system kills the broadcast (IMP: RESET must never ANR).
        logStormActive.set(false)
        leakedThreadsActive.set(false)
        latchForThreads?.countDown()
        latchForThreads = null
        leakedFds.forEach { runCatching { it.close() } }
        leakedFds.clear()
        runCatching {
            if (wakeLock != null && wakelockHeld.get()) {
                wakeLock?.release()
            }
        }
        wakelockHeld.set(false)
        wakeLock = null
        // Heavy IO (a filled sandbox can be hundreds of MB) runs in the
        // background; SAT_FAULT_END id=RESET signals completion.
        Thread({
            runCatching { File(context.filesDir, STARTUP_CRASH_FLAG).delete() }
            runCatching { File(context.filesDir, "fill").deleteRecursively() }
            runCatching { context.getDatabasePath("corrupt.db").delete() }
            runCatching {
                context.stopService(android.content.Intent(context, RemoteService::class.java))
            }
            Log.i(TAG, "SAT_FAULT_END id=RESET type=RESET process=${Process.myProcessName()}")
        }, "sat-reset-cleanup").start()
    }

    fun startupCrashIfRequested(context: Context) {
        val flag = File(context.filesDir, STARTUP_CRASH_FLAG)
        if (flag.exists()) {
            val faultId = flag.readText().trim()
            Log.i(
                TAG,
                "SAT_FAULT_BEGIN id=$faultId type=STARTUP_CRASH process=${Process.myProcessName()}"
            )
            throw RuntimeException("SAT injected startup crash: $faultId")
        }
    }
}
