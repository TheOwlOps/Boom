# ProGuard rules for the Boom Android app.
# Shrinking is currently disabled (isMinifyEnabled = false in
# app/build.gradle.kts); these rules take effect when it is enabled.

# Retrofit: keep service interfaces and their annotations.
-keepattributes Signature, InnerClasses, EnclosingMethod
-keepattributes RuntimeVisibleAnnotations, RuntimeVisibleParameterAnnotations
-keepattributes AnnotationDefault
-keepclassmembers,allowshrinking,allowobfuscation interface * {
    @retrofit2.http.* <methods>;
}

# Gson: keep the API model classes (fields are mapped via @SerializedName).
-keep class com.theowlops.boom.api.** { *; }

# OkHttp / Okio.
-dontwarn okhttp3.**
-dontwarn okio.**
-keep class okhttp3.** { *; }
-keep class okio.** { *; }

# DataStore / kotlinx.serialization-free: keep the repository class names
# referenced by name only if R8 strips them (they are referenced directly,
# so this is just belt and braces).
-keep class com.theowlops.boom.data.SettingsRepository { *; }
