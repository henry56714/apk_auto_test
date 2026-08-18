package com.example.stabilityfaultlab

/** JNI facade for the native fault library. */
object NativeLib {
    init {
        System.loadLibrary("faultlab")
    }

    external fun trigger(fault: String, faultId: String)

    external fun nativeHeapLeak(blocks: Int, blockBytes: Int)
}
