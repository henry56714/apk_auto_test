// Stability Fault Lab native faults (TEST ONLY).
#include <jni.h>
#include <android/log.h>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <unistd.h>
#include <vector>

#define LOG_TAG "SAT"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

namespace {

std::vector<void*> g_leaked_blocks;

void mark(JNIEnv* env, const char* fault, jstring jfaultId) {
    const char* id = env->GetStringUTFChars(jfaultId, nullptr);
    LOGI("SAT_FAULT_BEGIN id=%s type=%s", id, fault);
    env->ReleaseStringUTFChars(jfaultId, id);
}

__attribute__((noinline)) int deep_recursion(int depth) {
    volatile char pad[4096] = {};
    (void)pad;
    if (depth == 0) {
        return 42;
    }
    return deep_recursion(depth - 1) + 1;
}

}  // namespace

extern "C" JNIEXPORT void JNICALL
Java_com_example_stabilityfaultlab_NativeLib_trigger(
    JNIEnv* env, jobject /*thiz*/, jstring jfault, jstring jfaultId) {
    const char* fault = env->GetStringUTFChars(jfault, nullptr);
    mark(env, fault, jfaultId);

    if (strcmp(fault, "NATIVE_SIGSEGV") == 0) {
        // Deliberate null-pointer write: deterministic SIGSEGV.
        volatile int* p = reinterpret_cast<int*>(0x0);
        *p = 0xdead;
    } else if (strcmp(fault, "NATIVE_SIGABRT") == 0) {
        LOGI("SAT_FAULT_READY id=%s", env->GetStringUTFChars(jfaultId, nullptr));
        // Fixed abort message so the test can assert on it.
        __android_log_assert("SAT injected abort", LOG_TAG, "fixed abort message: sat-abort-42");
        abort();
    } else if (strcmp(fault, "NATIVE_STACK_OVERFLOW") == 0) {
        deep_recursion(1 << 24);
    } else if (strcmp(fault, "NATIVE_SELFKILL") == 0) {
        // Self SIGKILL: produces REASON_SIGNALED, never a crash/LMK.
        kill(getpid(), SIGKILL);
    } else if (strcmp(fault, "NATIVE_HEAP_LEAK") == 0) {
        for (int i = 0; i < 40; ++i) {
            g_leaked_blocks.push_back(malloc(1024 * 1024));
            if (g_leaked_blocks.back() != nullptr) {
                memset(g_leaked_blocks.back(), 0xAB, 1024 * 1024);
            }
        }
        LOGI("SAT_FAULT_READY id=%s", env->GetStringUTFChars(jfaultId, nullptr));
        LOGI("SAT_FAULT_END id=%s", env->GetStringUTFChars(jfaultId, nullptr));
    }
    env->ReleaseStringUTFChars(jfault, fault);
}

extern "C" JNIEXPORT void JNICALL
Java_com_example_stabilityfaultlab_NativeLib_nativeHeapLeak(
    JNIEnv* /*env*/, jobject /*thiz*/, jint blocks, jint blockBytes) {
    for (int i = 0; i < blocks; ++i) {
        g_leaked_blocks.push_back(malloc(blockBytes));
    }
}
