#!/usr/bin/env bash
#
# generate-checksums.sh — Create SHA256SUMS.txt for Boom desktop installers.
#
# Usage:
#   bash scripts/generate-checksums.sh [DIR]
#
# Arguments:
#   DIR  Directory scanned recursively for installer files (default: dist/).
#
# Behavior:
#   - Recursively finds all *.AppImage, *.msi and *.dmg files under DIR,
#     plus latest.json when present (e.g. copied there by the release job).
#   - Writes their SHA256 checksums to <DIR>/SHA256SUMS.txt in the standard
#     sha256sum format: "<hash><two spaces><filename>".
#   - Filenames are plain basenames (sorted), so the file verifies with
#     `sha256sum -c SHA256SUMS.txt` next to the downloaded installers.
#   - Duplicate basenames pointing at identical content are deduplicated;
#     duplicates with different content abort the script, because the
#     checksum file would be ambiguous.

set -euo pipefail

DIST_DIR="${1:-dist}"
# Drop a trailing slash so paths in messages stay tidy (find handles both).
DIST_DIR="${DIST_DIR%/}"
OUTPUT="$DIST_DIR/SHA256SUMS.txt"

if [ ! -d "$DIST_DIR" ]; then
  echo "error: directory '$DIST_DIR' does not exist" >&2
  exit 1
fi

# Recursively collect installer files. NUL-delimited so filenames with
# spaces are handled safely.
FILES=()
while IFS= read -r -d '' f; do
  FILES+=("$f")
done < <(
  find "$DIST_DIR" -type f \( \
    -name '*.AppImage' -o \
    -name '*.msi' -o \
    -name '*.dmg' -o \
    -name 'latest.json' \
  \) -print0 | sort -z
)

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "error: no installer files found under '$DIST_DIR'" >&2
  exit 1
fi

TMP_RAW="$(mktemp)"
trap 'rm -f "$TMP_RAW"' EXIT

# Stage as tab-separated "hash<TAB>basename"; tabs survive spaces in names.
for f in "${FILES[@]}"; do
  printf '%s\t%s\n' "$(sha256sum "$f" | awk '{print $1}')" "$(basename "$f")" >> "$TMP_RAW"
done

# A basename must never map to two different hashes: that means two
# different files share a name and the checksum file would be ambiguous.
CONFLICT=""
while IFS= read -r n; do
  [ -z "$n" ] && continue
  COUNT="$(awk -F '\t' -v name="$n" '$2 == name {print $1}' "$TMP_RAW" | sort -u | wc -l)"
  if [ "$COUNT" -gt 1 ]; then
    CONFLICT="${CONFLICT}${n}\n"
  fi
done < <(cut -f2 "$TMP_RAW" | sort | uniq -d)

if [ -n "$CONFLICT" ]; then
  printf 'error: these basenames have conflicting checksums (different files, same name):\n%b' "$CONFLICT" >&2
  exit 1
fi

# Emit standard sha256sum lines, deduplicated and sorted by filename.
awk -F '\t' '{printf "%s  %s\n", $1, $2}' "$TMP_RAW" | sort -u -k2 > "$OUTPUT"

echo "Wrote $OUTPUT with $(wc -l < "$OUTPUT") checksums:"
cat "$OUTPUT"
