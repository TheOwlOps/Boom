"""Tests for agent/src/version.py (update-check backend).

Stdlib only. ``check_github_update`` is tested with a monkeypatched
``urllib.request.urlopen`` — no real network access in this suite.

Run: ``cd /tmp/boom_git && python3 evals/test_updater_backend.py``
"""

import io
import json
import sys
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "agent" / "src"))

from version import (  # noqa: E402
    UpdateCheckError,
    check_github_update,
    compare_versions,
    get_current_version,
)

_RESULTS = []


def _check(name, fn):
    try:
        fn()
    except AssertionError as e:
        _RESULTS.append((name, False, f"assertion: {e}"))
    except Exception as e:  # noqa: BLE001
        _RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
    else:
        _RESULTS.append((name, True, ""))


# ---------------------------------------------------------------------------
# compare_versions
# ---------------------------------------------------------------------------

def test_compare_basic_greater():
    assert compare_versions("1.0.0", "0.9.9") == 1


def test_compare_basic_less():
    assert compare_versions("0.1.0", "0.2.0") == -1


def test_compare_leading_v_equal():
    assert compare_versions("v0.1.0", "0.1.0") == 0


def test_compare_equal():
    assert compare_versions("1.0.0", "1.0.0") == 0


def test_compare_numeric_not_lexicographic():
    # '10' > '2' numerically; naive string compare would flip this.
    assert compare_versions("2.0.0", "10.0.0") == -1
    assert compare_versions("10.0.0", "2.0.0") == 1


def test_compare_longer_tail():
    assert compare_versions("1.0.1", "1.0") == 1
    assert compare_versions("1.0", "1.0.1") == -1


def test_compare_prerelease():
    assert compare_versions("1.0.0", "1.0.0-beta") == 1
    assert compare_versions("1.0.0-alpha", "1.0.0-beta") == -1


# ---------------------------------------------------------------------------
# get_current_version
# ---------------------------------------------------------------------------

def test_get_current_version_nonempty():
    v = get_current_version()
    assert isinstance(v, str) and v.strip(), "version must be a non-empty string"


def test_get_current_version_matches_package_json():
    pkg = json.loads((_ROOT / "apps" / "web" / "package.json").read_text())
    assert get_current_version() == pkg["version"]


# ---------------------------------------------------------------------------
# check_github_update (mocked urllib)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status=200):
        self._data = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake(payload=None, status=200, exc=None):
    import urllib.request

    orig = urllib.request.urlopen

    def fake(req, timeout=None):
        assert "api.github.com" in (
            req.full_url if hasattr(req, "full_url") else str(req)
        ), "unexpected URL: %r" % req
        if exc is not None:
            raise exc
        return _FakeResp(payload, status)

    urllib.request.urlopen = fake
    return orig


def _restore(orig):
    import urllib.request

    urllib.request.urlopen = orig


def test_check_update_newer_available():
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/TheOwlOps/Boom/releases/tag/v9.9.9",
        "body": "Big new release notes.",
        "published_at": "2026-10-16T00:00:00Z",
    }
    orig = _install_fake(payload)
    try:
        out = check_github_update()
    finally:
        _restore(orig)
    assert out["has_update"] is True
    assert out["latest_version"] == "9.9.9"  # leading 'v' stripped
    assert out["current_version"] == get_current_version()
    assert out["download_url"] == payload["html_url"]
    assert out["release_notes"] == "Big new release notes."
    assert out["published_at"] == "2026-10-16T00:00:00Z"


def test_check_update_already_latest():
    current = get_current_version()
    payload = {"tag_name": "v" + current, "html_url": "https://x", "body": ""}
    orig = _install_fake(payload)
    try:
        out = check_github_update()
    finally:
        _restore(orig)
    assert out["has_update"] is False
    assert out["latest_version"] == current


def test_check_update_truncates_long_notes():
    payload = {
        "tag_name": "v99.0.0",
        "html_url": "https://x",
        "body": "n" * 5000,
    }
    orig = _install_fake(payload)
    try:
        out = check_github_update()
    finally:
        _restore(orig)
    assert len(out["release_notes"]) == 2000


def test_check_update_network_error_raises():
    orig = _install_fake(exc=urllib.error.URLError("boom"))
    try:
        try:
            check_github_update()
        except UpdateCheckError:
            return
        raise AssertionError("expected UpdateCheckError")
    finally:
        _restore(orig)


def test_check_update_http_error_raises():
    err = urllib.error.HTTPError(
        "https://api.github.com/x", 404, "Not Found", {}, io.BytesIO(b"")
    )
    orig = _install_fake(exc=err)
    try:
        try:
            check_github_update()
        except UpdateCheckError:
            return
        raise AssertionError("expected UpdateCheckError")
    finally:
        _restore(orig)


def test_check_update_bad_payload_raises():
    orig = _install_fake({"not_a_release": True})
    try:
        try:
            check_github_update()
        except UpdateCheckError:
            return
        raise AssertionError("expected UpdateCheckError")
    finally:
        _restore(orig)


def main():
    cases = [
        test_compare_basic_greater,
        test_compare_basic_less,
        test_compare_leading_v_equal,
        test_compare_equal,
        test_compare_numeric_not_lexicographic,
        test_compare_longer_tail,
        test_compare_prerelease,
        test_get_current_version_nonempty,
        test_get_current_version_matches_package_json,
        test_check_update_newer_available,
        test_check_update_already_latest,
        test_check_update_truncates_long_notes,
        test_check_update_network_error_raises,
        test_check_update_http_error_raises,
        test_check_update_bad_payload_raises,
    ]
    for fn in cases:
        _check(fn.__name__, fn)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    for name, ok, msg in _RESULTS:
        print(("PASS" if ok else "FAIL"), name, msg)
    print(f"{passed}/{len(_RESULTS)} passed")
    sys.exit(0 if passed == len(_RESULTS) else 1)


if __name__ == "__main__":
    main()
