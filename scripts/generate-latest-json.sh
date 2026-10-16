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
# Updater bundle filenames (Tauri v2, createUpdaterArtifacts=true):
#   linux-x86_64   -> the .AppImage itself (v2 installs it directly)
#   windows-x86_64 -> the .msi itself     (v2 installs it directly)
#   darwin-*       -> the .app.tar.gz updater archive (NOT the .dmg; the
#                     updater cannot install a DMG)
# Filenames can be overridden via env vars (useful when the real bundle
# names drift):
#   APPIMAGE_FILE, MSI_FILE, MACOS_X64_FILE, MACOS_AARCH64_FILE
# If SIG_DIR is set, "<filename>.sig" files are read from that directory to
# fill the "signature" fields. Signatures are REQUIRED by the updater and
# cannot be disabled: the script aborts if any platform's signature is
# missing or empty, unless ALLOW_EMPTY_SIGNATURES=1 is exported (only for
# local dry-runs; never in CI).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: generate-latest-json.sh <version> [notes-file] [-o <output>]

Examples:
  scripts/generate-latest-json.sh 0.2.0
  scripts/generate-latest-json.sh v0.2.0 release-notes.md -o dist/latest.json

Env overrides:
  APPIMAGE_FILE, MSI_FILE, MACOS_X64_FILE, MACOS_AARCH64_FILE
  SIG_DIR                directory holding "<filename>.sig" files
  ALLOW_EMPTY_SIGNATURES=1   permit empty signatures (local dry-run only)
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

# Default updater bundle filenames produced by the desktop build workflow.
# macOS uses the .app.tar.gz updater archive: the updater cannot install a .dmg.
APPIMAGE_FILE="${APPIMAGE_FILE:-Boom_${VERSION}_amd64.AppImage}"
MSI_FILE="${MSI_FILE:-Boom_${VERSION}_x64_en-US.msi}"
MACOS_X64_FILE="${MACOS_X64_FILE:-Boom_${VERSION}_x64.app.tar.gz}"
MACOS_AARCH64_FILE="${MACOS_AARCH64_FILE:-Boom_${VERSION}_aarch64.app.tar.gz}"

# Read a .sig file produced by `tauri build` (or `tauri signer sign`)
# when SIG_DIR is provided.
read_sig() {
  local file="$1"
  if [ -n "${SIG_DIR:-}" ] && [ -f "${SIG_DIR}/${file}.sig" ]; then
    cat "${SIG_DIR}/${file}.sig"
  fi
}

APPIMAGE_SIG="$(read_sig "$APPIMAGE_FILE")"
MSI_SIG="$(read_sig "$MSI_FILE")"
MACOS_X64_SIG="$(read_sig "$MACOS_X64_FILE")"
MACOS_AARCH64_SIG="$(read_sig "$MACOS_AARCH64_FILE")"

# The updater cannot verify (and therefore will reject) an update whose
# signature is missing or empty. Fail loudly instead of shipping a manifest
# that silently breaks auto-update for every user.
if [ "${ALLOW_EMPTY_SIGNATURES:-0}" != "1" ]; then
  MISSING=""
  [ -n "$APPIMAGE_SIG" ] || MISSING="${MISSING} linux-x86_64"
  [ -n "$MSI_SIG" ] || MISSING="${MISSING} windows-x86_64"
  [ -n "$MACOS_X64_SIG" ] || MISSING="${MISSING} darwin-x86_64"
  [ -n "$MACOS_AARCH64_SIG" ] || MISSING="${MISSING} darwin-aarch64"
  if [ -n "$MISSING" ]; then
    echo "error: missing updater signatures for:$MISSING" >&2
    echo "  Expected '<filename>.sig' files under SIG_DIR='${SIG_DIR:-<unset>}'." >&2
    echo "  tauri build only writes .sig files when TAURI_SIGNING_PRIVATE_KEY" >&2
    echo "  is set in its environment (see .github/workflows/desktop.yml)." >&2
    exit 1
  fi
fi

export VERSION PUB_DATE NOTES \
  APPIMAGE_FILE MSI_FILE MACOS_X64_FILE MACOS_AARCH64_FILE \
  APPIMAGE_SIG MSI_SIG MACOS_X64_SIG MACOS_AARCH64_SIG

# Build the JSON with Python so notes are escaped correctly.
python3 - > "$OUT" <<'PYEOF'
import json
import os
import re
import sys

version = os.environ["VERSION"]
pub_date = os.environ["PUB_DATE"]
notes = os.environ["NOTES"]
base = f"https://github.com/TheOwlOps/Boom/releases/download/v{version}"

# RFC 3339 UTC, e.g. 2026-10-17T02:30:00Z
if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", pub_date):
    sys.exit(f"error: pub_date is not RFC 3339 UTC: {pub_date!r}")

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
        "signature": os.environ["MACOS_X64_SIG"],
        "url": f"{base}/{os.environ['MACOS_X64_FILE']}",
    },
    "darwin-aarch64": {
        "signature": os.environ["MACOS_AARCH64_SIG"],
        "url": f"{base}/{os.environ['MACOS_AARCH64_FILE']}",
    },
}

for target, entry in platforms.items():
    if not entry["url"].startswith("https://"):
        sys.exit(f"error: {target} url is not https: {entry['url']!r}")

data = {
    "version": version,
    "notes": notes,
    "pub_date": pub_date,
    "platforms": platforms,
}

print(json.dumps(data, indent=2, ensure_ascii=False))
PYEOF

echo "Wrote $OUT (version $VERSION, 4 platforms)"
