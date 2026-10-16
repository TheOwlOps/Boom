"""Version and update-check utilities for Boom.

Stdlib only. Used by ``gateway/api.py`` to expose
``GET /api/version`` and ``GET /api/updates/check`` so the web UI
(non-Tauri mode) can check for new releases.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

FALLBACK_VERSION = "0.1.0"
RELEASE_NOTES_LIMIT = 2000


class UpdateCheckError(Exception):
    """Raised when an update check fails (network, API, or parse error)."""


def _repo_root() -> Path:
    """Repo root, found by walking up from this file.

    Layout: <root>/agent/src/version.py  ->  root is 3 levels up.
    """
    return Path(__file__).resolve().parent.parent.parent


def get_current_version() -> str:
    """Return the current version read from ``apps/desktop/package.json``.

    Never hardcodes the version; falls back to ``FALLBACK_VERSION``
    when the file is missing or unreadable.
    """
    try:
        pkg = _repo_root() / "apps" / "desktop" / "package.json"
        data = json.loads(pkg.read_text(encoding="utf-8"))
        version = str(data.get("version", "")).strip()
        if version:
            return version
    except Exception:
        pass
    return FALLBACK_VERSION


def _split_version(v: str):
    """Split a version string into ([int parts], prerelease string).

    Strips a leading 'v'/'V'. The prerelease suffix is whatever follows
    the first '-' or '+'.
    """
    v = v.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    prerelease = ""
    for sep in ("-", "+"):
        if sep in v:
            v, prerelease = v.split(sep, 1)
            break
    parts = []
    for chunk in v.split("."):
        chunk = chunk.strip()
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return parts, prerelease


def compare_versions(a: str, b: str) -> int:
    """Compare two version strings numerically.

    Returns 1 if ``a`` > ``b``, -1 if ``a`` < ``b``, 0 if equal.
    Leading 'v' is ignored; numeric parts compare as integers
    (``2.0.0`` < ``10.0.0``). A plain release outranks its prerelease;
    two prereleases compare by plain string ordering of the suffix.
    """
    pa, pra = _split_version(a)
    pb, prb = _split_version(b)
    for x, y in zip(pa, pb):
        if x < y:
            return -1
        if x > y:
            return 1
    # Longer tail wins when all shared parts are equal (1.0.1 > 1.0).
    if len(pa) < len(pb):
        if any(pb[len(pa):]):
            return -1
    elif len(pa) > len(pb):
        if any(pa[len(pb):]):
            return 1
    if pra == prb:
        return 0
    if not pra:
        return 1  # release > prerelease
    if not prb:
        return -1
    return (pra > prb) - (pra < prb)


def check_github_update(repo: str = "TheOwlOps/Boom", timeout: int = 10) -> dict:
    """Check GitHub for the latest release newer than the current version.

    Uses ``urllib`` (stdlib) — no third-party HTTP dependency.

    Returns a dict with keys: ``has_update``, ``current_version``,
    ``latest_version`` (leading 'v' stripped), ``download_url``,
    ``release_notes`` (truncated to ``RELEASE_NOTES_LIMIT`` chars),
    ``published_at``.

    Raises:
        UpdateCheckError: on network failure, API error, or bad payload.
        Never returns a fabricated dict on failure.
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "boom-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                raise UpdateCheckError(
                    f"GitHub API returned HTTP {getattr(resp, 'status', '?')}"
                )
            payload = json.loads(resp.read().decode("utf-8"))
    except UpdateCheckError:
        raise
    except urllib.error.HTTPError as e:
        raise UpdateCheckError(f"GitHub API HTTP error: {e.code}") from e
    except urllib.error.URLError as e:
        raise UpdateCheckError(f"Network error contacting GitHub: {e.reason}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UpdateCheckError(f"Invalid JSON from GitHub API: {e}") from e
    except Exception as e:
        raise UpdateCheckError(f"Update check failed: {e}") from e

    if not isinstance(payload, dict) or not payload.get("tag_name"):
        raise UpdateCheckError("GitHub API response missing tag_name")

    tag = str(payload["tag_name"])
    latest = tag[1:] if tag[:1] in ("v", "V") else tag
    current = get_current_version()

    return {
        "has_update": compare_versions(latest, current) > 0,
        "current_version": current,
        "latest_version": latest,
        "download_url": str(payload.get("html_url", "")),
        "release_notes": str(payload.get("body", "") or "")[:RELEASE_NOTES_LIMIT],
        "published_at": str(payload.get("published_at", "") or ""),
    }
