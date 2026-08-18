plugins {
    id("com.android.application")  // AGP 9: built-in Kotlin support
}

android {
    namespace = "com.example.stabilityfaultlab"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.example.stabilityfaultlab"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
        ndk {
            abiFilters += listOf("x86_64", "arm64-v8a")
        }
        externalNativeBuild {
            cmake {
                cppFlags += "-std=c++17"
            }
        }
    }

    buildTypes {
        debug {
            // Keep symbols for host-side symbolication tests.
            ndk { debugSymbolLevel = "FULL" }
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
}
