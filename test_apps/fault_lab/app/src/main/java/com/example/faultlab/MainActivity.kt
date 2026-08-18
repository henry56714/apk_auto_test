package com.example.faultlab

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.widget.TextView

/**
 * Minimal foreground activity. Kept intentionally dumb so fault handling
 * lives in [FaultRunner]; the activity only provides a visible window for
 * input-dispatch ANR tests and a human-readable "READY" state.
 */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = TextView(this)
        text.setBackgroundColor(Color.WHITE)
        text.setTextColor(Color.BLACK)
        text.text = "Fault Lab READY\nfaults are triggered via broadcast"
        text.textSize = 18f
        text.setPadding(64, 128, 64, 64)
        setContentView(text)
        android.util.Log.i("SAT", "SAT_FAULT_READY process=${android.os.Process.myProcessName()}")
    }
}
