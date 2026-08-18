# Keep fault entry points so release-variant retrace tests have real mappings.
-keep class com.example.stabilityfaultlab.FaultRunner { *; }
-keep class com.example.stabilityfaultlab.FaultReceiver { *; }
-keep class com.example.stabilityfaultlab.NativeLib { *; }
