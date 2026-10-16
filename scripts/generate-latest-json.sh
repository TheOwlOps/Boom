#!/usr/bin/env bash
# Generate Tauri updater v2 `latest.json` for a Boom desktop release.
#
# Usage:
#   scripts/generate-latest-json.sh <version> [notes-file] [-o <output>]
#
# <version> may be given with or without a leading "v" (e.g. "0.2.0" or
# "v0.2.0"). The JSON "version" field never carries the "v" prefix, while
# download URLs always do (GitHub tag naming convention).
#
# [notes-file] is an optional file whose contents become the "notes" field.
#   -o <output> writes JSON to <output> (default: latest.json in cwd).
#
# Installer filenames follow the Tauri bundle naming "Boom_<version>_<arch>.<ext>"
# but can be overridden via env vars (useful when the real bundle names drift):
#   APPIMAGE_FILE, MSI_FILE, DMG_X64_FILE, DMG_AARCH64_FILE
# If SIG_DIR is set, "<filename>.sig" files are read from that directory to
# fill the "signature" fields; otherwise signatures stay empty.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: generate-latest-json.sh <version> [notes-file] [-o <output>]

Examples:
  scripts/generate-latest-json.sh 0.2.0
  scripts/generate-latest-json.sh v0.2.0 release-notes.md -o dist/latest.json
EOF
  exit 1
}

if [ $# -lt 1 ]; then
  usage
fi

VERSION_RAW="$1"
shift

NOTES_FILE=""
OUT="latest.json"
while [ $# -gt 0 ]; do
  case "$1" in
    -o)
      [ $# -ge 2 ] || usage
      OUT="$2"
      shift 2
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      usage
      ;;
    *)
      if [ -z "$NOTES_FILE" ]; then
        NOTES_FILE="$1"
      else
        echo "error: unexpected positional argument: $1" >&2
        usage
      fi
      shift
      ;;
  esac
done

# Strip a single leading "v" if present.
VERSION="${VERSION_RAW#v}"
if [ -z "$VERSION" ]; then
  echo "error: empty version after stripping 'v' prefix" >&2
  exit 1
fi

# Basic sanity check: expect something like 0.2.0 or 0.2.0-rc.1.
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "error: version '$VERSION' does not look like X.Y.Z" >&2
  exit 1
fi

# Release notes from the optional notes file.
NOTES=""
if [ -n "$NOTES_FILE" ]; then
  if [ ! -f "$NOTES_FILE" ]; then
    echo "error: notes file not found: $NOTES_FILE" >&2
    exit 1
  fi
  NOTES="$(cat "$NOTES_FILE")"
fi

PUB_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Default installer filenames produced by the desktop build workflow.
APPIMAGE_FILE="${APPIMAGE_FILE:-Boom_${VERSION}_amd64.AppImage}"
MSI_FILE="${MSI_FILE:-Boom_${VERSION}_x64_en-US.msi}"
DMG_X64_FILE="${DMG_X64_FILE:-Boom_${VERSION}_x64.dmg}"
DMG_AARCH64_FILE="${DMG_AARCH64_FILE:-Boom_${VERSION}_aarch64.dmg}"

# Read a .sig file produced by `tauri build` when SIG_DIR is provided.
read_sig() {
  local file="$1"
  if [ -n "${SIG_DIR:-}" ] && [ -f "${SIG_DIR}/${file}.sig" ]; then
    cat "${SIG_DIR}/${file}.sig"
  fi
}

APPIMAGE_SIG="$(read_sig "$APPIMAGE_FILE")"
MSI_SIG="$(read_sig "$MSI_FILE")"
DMG_X64_SIG="$(read_sig "$DMG_X64_FILE")"
DMG_AARCH64_SIG="$(read_sig "$DMG_AARCH64_FILE")"

export VERSION PUB_DATE NOTES \
  APPIMAGE_FILE MSI_FILE DMG_X64_FILE DMG_AARCH64_FILE \
  APPIMAGE_SIG MSI_SIG DMG_X64_SIG DMG_AARCH64_SIG

# Build the JSON with Python so notes are escaped correctly.
python3 - > "$OUT" <<'PYEOF'
import json
import os

version = os.environ["VERSION"]
pub_date = os.environ["PUB_DATE"]
notes = os.environ["NOTES"]
base = f"https://github.com/TheOwlOps/Boom/releases/download/v{version}"

platforms = {
    "linux-x86_64": {
        "signature": os.environ["APPIMAGE_SIG"],
        "url": f"{base}/{os.environ['APPIMAGE_FILE']}",
    },
    "windows-x86_64": {
        "signature": os.environ["MSI_SIG"],
        "url": f"{base}/{os.environ['MSI_FILE']}",
    },
    "darwin-x86_64": {
        "signature": os.environ["DMG_X64_SIG"],
        "url": f"{base}/{os.environ['DMG_X64_FILE']}",
    },
    "darwin-aarch64": {
        "signature": os.environ["DMG_AARCH64_SIG"],
        "url": f"{base}/{os.environ['DMG_AARCH64_FILE']}",
    },
}

data = {
    "version": version,
    "notes": notes,
    "pub_date": pub_date,
    "platforms": platforms,
}

print(json.dumps(data, indent=2, ensure_ascii=False))
PYEOF

echo "Wrote $OUT (version $VERSION, 4 platforms)"
