# JNA / lazysodium use reflection
-keep class com.sun.jna.** { *; }
-keep class * implements com.sun.jna.** { *; }
-keep class com.goterl.lazysodium.** { *; }
-dontwarn java.awt.**
# SQLCipher
-keep class net.zetetic.** { *; }
