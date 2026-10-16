# Boom — Native Android App

Native Android client for the Boom AI agent harness (Kotlin + Jetpack Compose).
It talks to the Boom Python backend over HTTP: `http://<server>:18080/api/*`.

> This folder also contains the web PWA (`index.html`, `manifest.json`, `sw.js`).
> Those are a separate deliverable and are intentionally left untouched.

## Open in Android Studio

1. In Android Studio: **File → Open…**
2. Select the `apps/mobile/android` directory (the folder containing `settings.gradle.kts`).
3. Let Gradle sync finish (needs internet on first run to download the Android Gradle
   Plugin 8.5.2, Kotlin 1.9.24, and dependencies).

## Set the server URL

The backend base URL is configurable from inside the app (Settings screen —
persisted via DataStore). Defaults and quick values:

- **Android emulator:** `http://10.0.2.2:18080` (host loopback alias)
- **Real device on the same Wi-Fi/LAN:** `http://<your-PC-LAN-IP>:18080`
  (find your PC's LAN IP, e.g. `ip addr` on Linux / `ipconfig` on Windows;
  the backend must listen on `0.0.0.0`, not just `127.0.0.1`)
- The manifest sets `android:usesCleartextTraffic="true"` so plain HTTP works
  for local/LAN backends.

## Build an APK

In Android Studio: **Build → Build App Bundle(s) / APK(s) → Build APK(s)**.

Or from the command line (with Android SDK configured):

```bash
cd apps/mobile/android
./gradlew assembleDebug
```

The APK lands in:

```
apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk
```

For release builds, `assembleRelease` produces:

```
apps/mobile/android/app/build/outputs/apk/release/app-release.apk
```

## Requirements

- The Boom backend must be running and reachable from the device:
  `gateway/api.py` on port `18080`.
- Android Studio Hedgehog (2023.1.1) or newer; JDK 17 (bundled is fine).
- Minimum supported Android version: **API 26 (Android 8.0)**.
