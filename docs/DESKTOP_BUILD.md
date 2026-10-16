# Boom Desktop — Tauri Build Guide

Tauri 2 wrapper for the Boom web dashboard (`apps/web`). This doc covers system
requirements, build steps, output locations, and the Python backend story.

## 1. System requirements

### Linux (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install -y \
  libwebkit2gtk-4.1-dev \
  libappindicator3-dev \
  librsvg2-dev \
  patchelf
```

Also needed: Rust toolchain (`rustup`, stable) and Node.js 20+ (CI uses npm).

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
# restart shell, then:
rustup default stable
```

### macOS

1. Install Xcode Command Line Tools:

   ```bash
   xcode-select --install
   ```

2. Install the Rust toolchain (`rustup`, stable) and Node.js 20+.

### Windows

1. **WebView2** — ships with Windows 10 1803+ and Windows 11; no install needed on
   supported versions.
2. **Visual Studio Build Tools** — install "Desktop development with C++"
   workload from the Visual Studio Installer.
3. Install the Rust toolchain (rustup-init.exe, MSVC target) and Node.js 20+.

## 2. Build steps

```bash
cd apps/web
npm install
npx tauri build
```

For development (hot reload):

```bash
cd apps/web
npm install
npx tauri dev
```

The Tauri config (`src-tauri/tauri.conf.json`) uses `npm run dev` /
`npm run build` as `beforeDevCommand` / `beforeBuildCommand` — intentionally
`npm`, not `pnpm`, to match CI.

## 3. Build outputs

Bundles land under `apps/web/src-tauri/target/release/bundle/`:

| OS      | Artifact                                   |
|---------|--------------------------------------------|
| Linux   | `deb/*.deb`, `appimage/*.AppImage`         |
| macOS   | `dmg/*.dmg`, `macos/*.app`                 |
| Windows | `msi/*.msi`, `nsis/*.exe`                  |

(`targets: "all"` in `tauri.conf.json` enables every platform-native bundle.)

## 4. Python backend (`gateway/api.py`)

The Tauri window only ships the **frontend** (the Vite build in `../dist`).
The Python gateway API (`gateway/api.py`, default `:18080`) is a separate
process and must be available for the desktop app to be functional.

Two options:

- **Dev mode** — run the API separately, then `npx tauri dev`:

  ```bash
  # terminal 1
  cd gateway && python3 api.py
  # terminal 2
  cd apps/web && npx tauri dev
  ```

- **Production bundle** — bundle the Python backend as a sidecar binary.
  Recommended approach: PyInstaller single-file build of `gateway/api.py`,
  registered in `tauri.conf.json` under `bundle.externalBin` /
  `app` sidecar resources.

  > **TODO:** implement the PyInstaller sidecar bundle. Not done yet — the
  > desktop app currently assumes the API is reachable at `http://localhost:18080`.

## 5. Notes

- App identifier: `com.theowlops.boom` (reversed-DNS for TheOwlOps org).
- Window title: `Boom — AI Agent Harness`, default size 1280×800.
- Icons: all required formats present in `src-tauri/icons/`
  (`32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.icns`, `icon.ico`).
- Version is pinned to `0.1.0` in both `tauri.conf.json` and `Cargo.toml`
  (matches `apps/web/package.json`).
## 6. Release builds & auto-update (`latest.json`)

### Triggering a release build

The `desktop.yml` workflow builds installers for all 4 targets on:

1. **Tag push** — push a tag named `desktop-v*`:
   ```bash
   # IMPORTANT: bump the app version first so installer filenames match the tag.
   # Update the version in apps/web/src-tauri/tauri.conf.json,
   # apps/web/src-tauri/Cargo.toml and apps/web/package.json, commit, then:
   git tag desktop-v0.2.0
   git push origin desktop-v0.2.0
   ```
2. **Manual dispatch** — Actions → "Desktop Build" → "Run workflow", entering the
   version (e.g. `0.2.0`, without the `v` prefix).

### What the `release` job does

After all 4 build jobs succeed, the `release` job (in the same workflow) runs:

1. Downloads the 4 build artifacts and locates the installers
   (`Boom_<version>_amd64.AppImage`, `Boom_<version>_x64_en-US.msi`,
   `Boom_<version>_x64.dmg`, `Boom_<version>_aarch64.dmg`).
2. Creates the GitHub release `v<version>` if it does not exist yet
   (with `--generate-notes`); if it already exists, assets are re-uploaded
   with `--clobber`, so re-running the workflow is safe.
3. Generates `latest.json` via `scripts/generate-latest-json.sh <version>`
   (Tauri updater v2 format — `version`, `notes` from the release body,
   `pub_date`, and a `platforms` map with `signature` + `url` per platform),
   filling `signature` from the `.sig` files `tauri build` produced.
4. Uploads the 4 installers, their `.sig` files, and `latest.json` to the release.

The desktop app polls the updater endpoint
`https://github.com/TheOwlOps/Boom/releases/latest/download/latest.json`
(configured in `apps/web/src-tauri/tauri.conf.json` under `plugins.updater.endpoints`),
which always serves the `latest.json` of the newest release.

### Generating `latest.json` by hand

```bash
# version arg accepts 0.2.0 or v0.2.0; JSON "version" never has the "v" prefix
./scripts/generate-latest-json.sh 0.2.0
./scripts/generate-latest-json.sh v0.2.0 release-notes.md -o dist/latest.json

# With real signatures (reads <file>.sig from SIG_DIR):
SIG_DIR=path/to/bundle ./scripts/generate-latest-json.sh 0.2.0

# If installer filenames drift from the Tauri default naming, override them:
APPIMAGE_FILE=... MSI_FILE=... DMG_X64_FILE=... DMG_AARCH64_FILE=... \
  ./scripts/generate-latest-json.sh 0.2.0
```
