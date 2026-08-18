package com.example.stabilityfaultlab

import android.app.Application
import android.util.Log

/**
 * Application entry — crash on startup when the STARTUP_CRASH flag is set.
 */
class FaultApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Log.i("SAT", "SAT_FAULT_READY process=${android.os.Process.myProcessName()}")
        FaultRunner.startupCrashIfRequested(this)
    }
}
