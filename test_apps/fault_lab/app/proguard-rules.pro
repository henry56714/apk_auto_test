# Keep fault entry points so release-variant retrace tests have real mappings.
-keep class com.example.faultlab.FaultRunner { *; }
-keep class com.example.faultlab.FaultReceiver { *; }
-keep class com.example.faultlab.NativeLib { *; }
