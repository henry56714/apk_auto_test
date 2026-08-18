package com.example.stabilityfaultlab

import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log

/**
 * Runs in the `:remote` process. Modes:
 *  - block: block the main thread (service ANR in the remote process)
 *  - exit:  exit the process normally (remote self-exit test)
 *  - binder: export a Binder for DeadObject tests
 */
class RemoteService : Service() {
    private val binder = BinderApi()

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val faultId = intent?.getStringExtra("fault_id") ?: "remote-unknown"
        // FGS contract: post the notification immediately.
        startAsForeground()
        when (intent?.getStringExtra("mode")) {
            "block" -> {
                Log.i(
                    "SAT",
                    "SAT_FAULT_BEGIN id=$faultId type=ANR_SERVICE " +
                            "process=${android.os.Process.myProcessName()}"
                )
                Log.i("SAT", "SAT_FAULT_READY id=$faultId process=${android.os.Process.myProcessName()}")
                Thread.sleep(60_000)
                Log.i("SAT", "SAT_FAULT_END id=$faultId process=${android.os.Process.myProcessName()}")
            }
            "exit" -> {
                Log.i(
                    "SAT",
                    "SAT_FAULT_BEGIN id=$faultId type=REMOTE_PROCESS_EXIT " +
                            "process=${android.os.Process.myProcessName()}"
                )
                Log.i("SAT", "SAT_FAULT_END id=$faultId process=${android.os.Process.myProcessName()}")
                android.os.Process.killProcess(android.os.Process.myPid())
            }
        }
        return START_NOT_STICKY
    }

    private fun startAsForeground() {
        runCatching {
            val channelId = "sat_faults"
            val nm = getSystemService(android.app.NotificationManager::class.java)
            nm.createNotificationChannel(
                android.app.NotificationChannel(
                    channelId, "SAT faults", android.app.NotificationManager.IMPORTANCE_LOW,
                )
            )
            val notification = android.app.Notification.Builder(this, channelId)
                .setContentTitle("Stability Fault Lab")
                .setContentText("fault running in :remote")
                .setSmallIcon(android.R.drawable.stat_sys_warning)
                .build()
            startForeground(1, notification)
        }
    }
}

/** Binder API for DeadObject / binder-fault tests. */
class BinderApi : android.os.Binder() {
    fun ping(): String = "pong"
}
