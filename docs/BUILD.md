# BUILD.md — Building Boom from source

Covers three independent build targets. No commit or push is involved; all builds are local.

| Target | Directory | What it produces |
|---|---|---|
| Python backend (API + gateway + CLI) | repo root | `boom` CLI, API `:18080`, gateway `:8788` |
| Frontend web UI | `apps/web` | static bundle (Vite) |
| Desktop app | `apps/desktop` | Tauri bundle (backend = pure Rust `crates/boom-core` built in-process; **no Python/PyInstaller sidecar needed**) |

---

## 1. Prerequisites

### All targets
- **Node.js ≥ 20**, npm ≥ 10 (CI uses Node 22; `devbox.json` ships `nodejs_22`)
- **Python ≥ 3.12** — deps are minimal: `httpx`, `pydantic` (see `pyproject.toml`)
- Git

### Desktop only (Tauri)
- **Rust stable** with the target triple for your platform:
  ```bash
  rustup target add x86_64-unknown-linux-gnu   # Linux
  rustup target add x86_64-pc-windows-msvc      # Windows
  rustup target add aarch64-apple-darwin        # macOS (M1+)
  ```
- **Linux system deps** (Debian/Ubuntu — same as CI `desktop.yml`):
  ```bash
  sudo apt-get update && sudo apt-get install -y \
    libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf
  ```
  Fedora/RHEL: `webkit2gtk4.1-devel`, `libappindicator-gtk3-devel`, `librsvg2-devel`, `patchelf`. Arch: `webkit2gtk-4.1`, `libappindicator-gtk3`, `librsvg`, `patchelf`.
- **Windows**: MSVC Build Tools (the `x86_64-pc-windows-msvc` target needs them), WebView2 (pre-installed on Win10 1803+/Win11).
- **macOS**: Xcode Command Line Tools (`xcode-select --install`).
- Note: `tauri-plugin-updater` requires a `pubkey` in `tauri.conf.json` to verify signed updates; the config currently has an empty `pubkey`, so the auto-update flow is inert until a key is added.

### Easiest setup (recommended)
```bash
devbox shell        # installs Python 3.12, Node 22, tesseract, deps
./start.sh all      # starts API + gateway
```

---

## 2. Build: backend (Python)

```bash
pip install -e .                 # or: pip install httpx pydantic
./start.sh all                   # API :18080 + gateway :8788
./start.sh status                # check what is running
./stop.sh                        # stop everything
```

`start.sh` puts pid/log files in `/tmp/boom-<repohash>/` (namespaced per checkout so two clones don't clash).

First-time interactive setup:
```bash
boom setup                       # wizard: provider, API key, channels -> ~/.boom/config.json (mode 0600)
```

## 3. Build: frontend only (web)

```bash
cd apps/web
npm install --no-audit --no-fund
npm run build                    # tsc && vite build -> apps/web/dist
npm run dev                      # local dev server
```

No backend required — the web UI can point at any running API (default `http://127.0.0.1:18080`).

## 4. Build: desktop app (Tauri)

```bash
cd apps/desktop
npm install --no-audit --no-fund
npm run build                    # tsc && vite build -> apps/desktop/dist (frontend only)
npx tauri build                  # full app: builds frontend, compiles Rust, produces installer
# optional: npx tauri dev        # dev mode (uses npm run dev as devUrl, localhost:5173)
```

Bundle output: `apps/desktop/src-tauri/target/<target>/release/bundle/` (`.AppImage`, `.deb` on Linux; `.msi` on Windows; `.dmg` on macOS).

`tauri.conf.json` (`apps/desktop/src-tauri/`): app id `com.theowlops.boom`, product name `Boom`, version mirrors `apps/desktop/package.json` (currently `0.3.3`). Icons live in `apps/desktop/src-tauri/icons/` — a missing or corrupt icon aborts the bundle step.

---

## 5. Environment variables

| Variable | Default | Used by |
|---|---|---|
| `BOOM_API_PORT` | `18080` | `start.sh`, Python API |
| `BOOM_INGRESS_PORT` | `8788` | `start.sh`, gateway |
| `BOOM_HOME` | `~/.boom` | config, OAuth token cache (`~/.boom/gmail_token.json`) |
| `PYTHONPATH` | — | must include repo root + `agent/src` (devbox sets this automatically) |
| `GITHUB_TOKEN` | — | CI only: release asset upload |

Config file `~/.boom/config.json` takes precedence over env for provider/channel settings (env overrides only where `agent/src/config.py` says so).

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `npx tauri build` fails with GTK/WebKit errors on Linux | Missing system deps — install the `libwebkit2gtk-4.1-dev` etc. list in §1. `cargo` errors mentioning `gdk`/`soup` always mean this. |
| `npm run build` fails in `apps/desktop` | Run `npm install` first; check Node ≥ 20 (`node -v`). `tsc` errors are real — fix them, don't bypass. |
| Bundle step fails: icon missing | `apps/desktop/src-tauri/icons/` must contain `32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.icns`, `icon.ico`. |
| `boom setup` / API won't start | Python < 3.12, or `PYTHONPATH` missing `agent/src`. Use `devbox shell` to eliminate both. |
| `start.sh` says port in use | Another checkout or stale process. `./start.sh status`, `./stop.sh`, then retry. Pid files are namespaced per repo path in `/tmp/boom-<hash>/`. |
| Desktop app launches but shows connection error | The desktop frontend talks to the API; if you need the Python backend too, run `./start.sh api` first (Tauri build itself does **not** bundle Python). |
| `nix flake check` can't run locally | Machine has no nix — expected. Use `devbox shell` instead. `flake.nix` still contains 2 unresolved `fakeHash` values (known). |
| Windows unsigned MSI warning | Expected unless SignPath secrets are configured in CI. The workflow silently skips signing and still produces a working installer. |
| Updater reports "no updates" / fails | `tauri.conf.json` updater `pubkey` is empty and the `latest.json` endpoint requires signed assets. Fill `pubkey` + configure signing before relying on in-app updates. |

---

## 7. Release process

Releases are produced by `.github/workflows/desktop.yml` (job `build` + `release`).

**Trigger** — either:
1. Push a tag `desktop-v<X.Y.Z>`: `git tag desktop-v0.4.0 && git push origin desktop-v0.4.0`
2. Manual: Actions → *Desktop Build* → *Run workflow*, enter version (e.g. `0.4.0`, no `v` prefix)

**Version bump locations** (keep in sync before tagging):
- `apps/desktop/package.json` → `version`
- `apps/desktop/src-tauri/tauri.conf.json` → `version`

The release job creates/updates GitHub release `v<X.Y.Z>` with `.AppImage` (Linux), `.msi` (Windows, SignPath-signed when secrets are set), 2× `.dmg` (macOS x64 + arm64), plus checksums and `latest.json` for the Tauri updater (attached to the `latest` release). Re-running is idempotent — assets are re-uploaded with `--clobber`.

No local commit is needed for a release — tag the already-pushed `main` state.
