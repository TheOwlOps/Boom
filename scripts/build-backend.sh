#!/usr/bin/env bash
# Build the Boom Python backend into a standalone one-file binary with
# PyInstaller, then copy it to gateway/binaries/ where the Tauri app bundles
# it as a sidecar (tauri.conf.json `bundle.externalBin`).
#
# Usage: scripts/build-backend.sh [linux|windows|macos] [target-triple]
#   - builds on the CURRENT machine; for another OS, run this script there
#     (PyInstaller cannot cross-compile).
#   - target-triple: Rust target triple, e.g. x86_64-unknown-linux-gnu.
#     Defaults are derived from the OS arg (macos arch auto-detected).
#   - output: gateway/binaries/boom-backend-<triple>      (linux, macos)
#             gateway/binaries/boom-backend-<triple>.exe  (windows)
#
# The binary reads its port from the BOOM_API_PORT env var (default 18080);
# there is no --port CLI flag. Tauri spawns it as a sidecar.

set -euo pipefail

TARGET_OS="${1:-linux}"
case "$TARGET_OS" in
    linux|windows|macos) ;;
    *) echo "usage: $0 [linux|windows|macos] [target-triple]" >&2; exit 2 ;;
esac

# Target triple: explicit 2nd arg wins, otherwise derive from OS.
if [[ $# -ge 2 ]]; then
    TARGET_TRIPLE="$2"
else
    case "$TARGET_OS" in
        linux)   TARGET_TRIPLE="x86_64-unknown-linux-gnu" ;;
        windows) TARGET_TRIPLE="x86_64-pc-windows-msvc" ;;
        macos)
            case "$(uname -m)" in
                arm64)  TARGET_TRIPLE="aarch64-apple-darwin" ;;
                *)      TARGET_TRIPLE="x86_64-apple-darwin" ;;
            esac
            ;;
    esac
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$REPO/gateway/boom-backend.spec"
OUT_DIR="$REPO/gateway/binaries"

if [[ "$TARGET_OS" == "windows" ]]; then
    SPEC_BIN="boom-backend.exe"
    BIN_NAME="boom-backend-${TARGET_TRIPLE}.exe"
else
    SPEC_BIN="boom-backend"
    BIN_NAME="boom-backend-${TARGET_TRIPLE}"
fi

echo "[build-backend] target OS     : $TARGET_OS"
echo "[build-backend] target triple : $TARGET_TRIPLE"
echo "[build-backend] repo          : $REPO"

# --- 1. ensure PyInstaller is available -------------------------------------
if ! python3 -c "import PyInstaller" >/dev/null 2>&1; then
    echo "[build-backend] installing pyinstaller..."
    # PEP 668 (externally managed) distros need --break-system-packages when
    # installing outside a venv; harmless elsewhere.
    python3 -m pip install --break-system-packages pyinstaller \
        || python3 -m pip install pyinstaller
fi
python3 -m PyInstaller --version

# --- 2. build ----------------------------------------------------------------
# Run from the repo root so the spec's SPECPATH-relative paths resolve.
cd "$REPO"
python3 -m PyInstaller --noconfirm --clean "$SPEC"

BUILT="$REPO/dist/$SPEC_BIN"
if [[ ! -x "$BUILT" ]]; then
    echo "[build-backend] ERROR: expected binary not found at $BUILT" >&2
    exit 1
fi
echo "[build-backend] built: $BUILT ($(du -h "$BUILT" | cut -f1))"

# --- 3. copy to the location Tauri expects -----------------------------------
mkdir -p "$OUT_DIR"
cp -f "$BUILT" "$OUT_DIR/$BIN_NAME"
chmod +x "$OUT_DIR/$BIN_NAME"
echo "[build-backend] copied to: $OUT_DIR/$BIN_NAME"

# --- 4. smoke test -------------------------------------------------------------
PORT="${BOOM_API_PORT:-18999}"
echo "[build-backend] smoke test on 127.0.0.1:$PORT ..."
BOOM_API_PORT="$PORT" "$OUT_DIR/$BIN_NAME" >/tmp/boom-backend-smoke.log 2>&1 &
SMOKE_PID=$!
trap 'kill $SMOKE_PID 2>/dev/null || true' EXIT
sleep 6
if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null; then
    echo "[build-backend] smoke test PASSED (/api/health 200)"
else
    echo "[build-backend] smoke test FAILED — log:" >&2
    cat /tmp/boom-backend-smoke.log >&2
    exit 1
fi
