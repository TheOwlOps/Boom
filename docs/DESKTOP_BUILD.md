# Boom Desktop — Tauri Build Guide

Tauri 2 wrapper for the Boom desktop app (`apps/desktop`). This doc covers system
requirements, build steps, output locations, and the Rust backend.

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
cd apps/desktop
npm install
npx tauri build
```

For development (hot reload):

```bash
cd apps/desktop
npm install
npx tauri dev
```

The Tauri config (`src-tauri/tauri.conf.json`) uses `npm run dev` /
`npm run build` as `beforeDevCommand` / `beforeBuildCommand` — intentionally
`npm`, not `pnpm`, to match CI.

## 3. Build outputs

Bundles land under `apps/desktop/src-tauri/target/release/bundle/`:

| OS      | Artifact                                   |
|---------|--------------------------------------------|
| Linux   | `deb/*.deb`, `appimage/*.AppImage`         |
| macOS   | `dmg/*.dmg`, `macos/*.app`                 |
| Windows | `msi/*.msi`, `nsis/*.exe`                  |

(`targets: "all"` in `tauri.conf.json` enables every platform-native bundle.)

## 4. Rust backend (`crates/boom-core`)

The Tauri window ships the **frontend** (the Vite build in `../dist`) plus the
**boom-core** Rust backend, which runs in-process (default `:18080`) — no
separate process, no sidecar, no Python. The server starts automatically with
the app via `boom_core::server::spawn_server`.

## 5. Notes

- App identifier: `com.theowlops.boom` (reversed-DNS for TheOwlOps org).
- Window title: `Boom — AI Agent Harness`, default size 1280×800.
- Icons: all required formats present in `src-tauri/icons/`
  (`32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.icns`, `icon.ico`).
- Version is pinned to `0.1.0` in both `tauri.conf.json` and `Cargo.toml`
  (matches `apps/desktop/package.json`).
## 6. Release builds & auto-update (`latest.json`)

### Triggering a release build

The `desktop.yml` workflow builds installers for all 4 targets on:

1. **Tag push** — push a tag named `desktop-v*`:
   ```bash
   # IMPORTANT: bump the app version first so installer filenames match the tag.
   # Update the version in apps/desktop/src-tauri/tauri.conf.json,
   # apps/desktop/src-tauri/Cargo.toml and apps/desktop/package.json, commit, then:
   git tag desktop-v0.2.0
   git push origin desktop-v0.2.0
   ```
2. **Manual dispatch** — Actions → "Desktop Build" → "Run workflow", entering the
   version (e.g. `0.2.0`, without the `v` prefix).

### What the `release` job does

After all 4 build jobs succeed, the `release` job (in the same workflow) runs:

1. Downloads the 4 build artifacts (kept in per-target directories — the two
   macOS legs produce the same `Boom.app.tar.gz` basename, so they are never
   merged) and locates the installers
   (`Boom_<version>_amd64.AppImage`, `Boom_<version>_x64_en-US.msi`,
   `Boom_<version>_x64.dmg`, `Boom_<version>_aarch64.dmg`) plus the Tauri
   updater bundles (`Boom_<version>_x64.app.tar.gz`,
   `Boom_<version>_aarch64.app.tar.gz`; Linux/Windows updaters re-use the
   `.AppImage`/`.msi` directly).
2. Creates the GitHub release `v<version>` if it does not exist yet
   (with `--generate-notes`); if it already exists, assets are re-uploaded
   with `--clobber`, so re-running the workflow is safe.
3. Flattens everything into `dist/flat/`, renaming the macOS updater archives
   with an arch suffix (minisign signatures cover file bytes, not names).
   When the SignPath-signed MSI is shipped, its `.sig` is regenerated with
   `tauri signer sign --app-version <version>` because Authenticode signing
   changes the file bytes the build-time `.sig` was made for.
4. Generates `latest.json` via `scripts/generate-latest-json.sh <version>`
   (Tauri updater v2 format — `version`, `notes` from the release body,
   `pub_date`, and a `platforms` map with `signature` + `url` per platform),
   filling `signature` from the `.sig` files. The script aborts if any
   platform's signature is missing — a manifest with an empty signature is
   rejected by every client, so failing loudly beats shipping it.
5. Uploads installers, updater bundles, `.sig` files, `latest.json` and
   `SHA256SUMS.txt` to the `v<version>` release, then publishes
   `latest.json` to the floating `desktop-latest` release (see below).

The desktop app polls the updater endpoint
`https://github.com/TheOwlOps/Boom/releases/download/desktop-latest/latest.json`
(configured in `apps/desktop/src-tauri/tauri.conf.json` under
`plugins.updater.endpoints`). The floating `desktop-latest` tag+release is
maintained by the workflow (tag moved, `latest.json` re-uploaded with
`--clobber`) because `release.yml` also creates `vX.Y.Z` releases, which
would otherwise steal GitHub's `/releases/latest/` redirect and 404 the
updater for every user. The manifest's asset URLs always point at the
versioned `vX.Y.Z` release, so only `latest.json` itself floats.

### Updater signing keys

Update signatures cannot be disabled. One keypair signs all releases:

- Private key: stored as the `TAURI_SIGNING_PRIVATE_KEY` repository secret.
  The build job fails fast with instructions if it is missing; `tauri build`
  only writes `.sig` files when this variable is set.
- Public key: in `apps/desktop/src-tauri/tauri.conf.json` under
  `plugins.updater.pubkey` (the base64 content of the `.pub` file produced by
  `tauri signer generate` — it is base64-wrapped, that is the exact format the
  updater plugin decodes).

Generate a new keypair with:

```bash
cd apps/desktop
npx tauri signer generate -w ~/.tauri/boom.key
# put the printed public key (content of boom.key.pub) into tauri.conf.json
# and the private key (content of boom.key) into the TAURI_SIGNING_PRIVATE_KEY secret
```

### Generating `latest.json` by hand

```bash
# version arg accepts 0.2.0 or v0.2.0; JSON "version" never has the "v" prefix
./scripts/generate-latest-json.sh 0.2.0
./scripts/generate-latest-json.sh v0.2.0 release-notes.md -o dist/latest.json

# With real signatures (reads <file>.sig from SIG_DIR):
SIG_DIR=path/to/bundle ./scripts/generate-latest-json.sh 0.2.0

# If installer filenames drift from the Tauri default naming, override them:
# (macOS updater bundles are the .app.tar.gz archives — the updater cannot install a .dmg)
APPIMAGE_FILE=... MSI_FILE=... MACOS_X64_FILE=... MACOS_AARCH64_FILE=... \
  ./scripts/generate-latest-json.sh 0.2.0
```
