# Boom for Android

Native Android client for the **Boom** AI agent harness — Kotlin + Jetpack Compose,
talking to the Boom Python backend over HTTP.

> The web PWA (`index.html`, `manifest.json`, `sw.js`) lives in this same folder
> but is a separate deliverable and is intentionally left untouched.

## Download the APK

### Option 1 — GitHub Releases (recommended)

1. Go to [Boom releases](https://github.com/TheOwlOps/Boom/releases).
2. Download the latest `Boom-android-*.apk` asset.
3. Copy it to your phone and install.

### Option 2 — GitHub Actions artifact

1. Go to the [repo](https://github.com/TheOwlOps/Boom) → **Actions** tab.
2. Open the latest run of the **Mobile** workflow.
3. Under **Artifacts**, download `boom-android-debug.apk`.

> The debug APK is **not signed with the official release certificate**,
> so Android will warn during install. Tap **Install anyway** to proceed.

## Build locally with Android Studio

1. Open `apps/mobile/android/` in Android Studio (**Hedgehog 2023.1.1 or newer**).
2. Wait for Gradle sync to finish (internet required on first run to fetch the
   Android Gradle Plugin 8.5.2, Kotlin 1.9.24, and dependencies).
3. Run on an emulator or a physical device.

Or build an APK from the command line (with the Android SDK configured):

```bash
cd apps/mobile/android
./gradlew assembleDebug
```

Output:

```
apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk
```

For release builds:

```bash
./gradlew assembleRelease
# app/build/outputs/apk/release/app-release.apk
```

Requirements: Android Studio Hedgehog+ / JDK 17 (bundled is fine), minSdk **API 26
(Android 8.0)**.

## Configure the server

On first launch, the app asks for the backend server URL (changeable anytime in
Settings; persisted via DataStore):

| Environment                       | Server URL                  |
| --------------------------------- | --------------------------- |
| Android emulator                  | `http://10.0.2.2:18080`     |
| Physical device on the same LAN   | `http://<your-PC-LAN-IP>:18080` |

The backend must be running and reachable from the device:

```bash
python3 gateway/api.py        # listens on :18080
```

(or just open the Boom desktop app, which spawns the backend automatically).

- For a physical device, find your PC's LAN IP (`ip addr` on Linux,
  `ipconfig` on Windows) and make sure the backend listens on `0.0.0.0`,
  not just `127.0.0.1`.
- The manifest sets `android:usesCleartextTraffic="true"` so plain HTTP works
  for local/LAN backends.

## Features

- **Chat** — full conversation UI against the Boom backend (`/api/*`).
- **Setup wizard** — first-launch flow: server URL + provider configuration.
- **Settings** — server URL, persisted via DataStore.
- **Provider config** — configure the LLM provider and API key from the app.

## Changelog

- **v0.1.0** — first release: chat, setup wizard, settings, provider config.

## Known limitations

- Not yet compile-tested on CI — a GitHub Actions workflow is coming after the
  Mobile workflow merges.
- App icon is a placeholder.
