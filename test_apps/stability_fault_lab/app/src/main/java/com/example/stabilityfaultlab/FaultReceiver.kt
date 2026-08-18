package com.example.stabilityfaultlab

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper

/**
 * Receives `am broadcast -n com.example.stabilityfaultlab/.FaultReceiver`
 * with action TRIGGER and extras `fault` (fault id) + `fault_id` (unique
 * marker). Every fault runs on the main looper via `Handler.post` so the
 * receiver itself never blocks (and broadcast ANRs never pollute results).
 */
class FaultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            ACTION_RESET -> FaultRunner.reset(context)
            ACTION_TRIGGER -> {
                val fault = intent.getStringExtra(EXTRA_FAULT) ?: return
                val faultId = intent.getStringExtra(EXTRA_FAULT_ID)
                    ?: "fault-${System.currentTimeMillis()}"
                if (fault == "REMOTE_PROCESS_EXIT" || fault == "ANR_SERVICE") {
                    // The system may have just started this process to
                    // deliver the broadcast: start the :remote service while
                    // the background-start grace window is still open.
                    FaultRunner.mark(faultId, fault)
                    runCatching {
                        FaultRunner.startRemoteService(
                            context,
                            mode = if (fault == "REMOTE_PROCESS_EXIT") "exit" else "block",
                            faultId = faultId,
                        )
                    }
                    return
                }
                // Run on the main looper, after the receiver returns.
                Handler(Looper.getMainLooper()).post {
                    FaultRunner.trigger(context.applicationContext, fault, faultId)
                }
            }
        }
    }

    companion object {
        const val ACTION_TRIGGER = "com.example.stabilityfaultlab.TRIGGER"
        const val ACTION_RESET = "com.example.stabilityfaultlab.RESET"
        const val EXTRA_FAULT = "fault"
        const val EXTRA_FAULT_ID = "fault_id"
    }
}
