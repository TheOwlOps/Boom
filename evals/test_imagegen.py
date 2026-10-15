"""Tests for media/imagegen.py (Boom image generation).

Run:  python3 evals/test_imagegen.py   (or: pytest evals/test_imagegen.py)

TẤT CẢ network đều được mock bằng unittest.mock -- không có request thật
nào ra internet trong test này.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def _bootstrap():
    """Tìm media/imagegen.py ở một trong 3 layout:
    1) repo:  evals/test_x.py -> ROOT/agent/src/media/imagegen.py
    2) local clone read-only: /tmp/boom_git/agent/src/media/imagegen.py
    3) standalone: media/ nằm cạnh file test (dùng khi dev ngoài repo)
    """
    for base in (ROOT / "agent" / "src",
                 Path("/tmp/boom_git/agent/src"), HERE):
        if (base / "media" / "imagegen.py").exists():
            sys.path.insert(0, str(base))
            return
    raise ImportError("không tìm thấy media/imagegen.py ở các layout đã biết")


_bootstrap()

from media import imagegen  # noqa: E402


FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class _FakeResp:
    """Fake httpx.Response đủ dùng cho imagegen."""

    def __init__(self, status_code=200, content=b"", text="",
                 content_type="image/png"):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode("latin1", "replace")
        self.headers = {"content-type": content_type}

    def json(self):
        import json
        return json.loads(self.text)


def _with_clean_env(fn):
    """Decorator: chạy test với env không có OPENAI_API_KEY."""
    def wrapper():
        env = {k: v for k, v in os.environ.items()
               if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            fn()
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------
# 1. URL Pollinations build đúng format
# --------------------------------------------------------------------------

@_with_clean_env
def test_pollinations_url_format():
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResp(200, FAKE_PNG)

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        r = imagegen.generate("a cute cat", size="1024x1024")

    expected = ("https://image.pollinations.ai/prompt/%s"
                "?width=1024&height=1024&nologo=true"
                % quote("a cute cat", safe=""))
    assert r["ok"] is True, r
    assert r["provider"] == "pollinations"
    assert r["image_bytes"] == FAKE_PNG
    assert captured["url"] == expected, captured["url"]


@_with_clean_env
def test_pollinations_url_special_chars():
    """Prompt có dấu / tiếng Việt, ký tự đặc biệt phải được encode hết."""
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResp(200, FAKE_PNG)

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        r = imagegen.generate("mèo/café & trà")

    assert r["ok"] is True, r
    encoded = quote("mèo/café & trà", safe="")
    assert "/prompt/" + encoded + "?" in captured["url"], captured["url"]
    assert " " not in captured["url"]  # không được lọt space raw


# --------------------------------------------------------------------------
# 2. Parse size WxH
# --------------------------------------------------------------------------

@_with_clean_env
def test_size_parsing():
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResp(200, FAKE_PNG)

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        imagegen.generate("x", size="512x768")
    assert "width=512" in captured["url"] and "height=768" in captured["url"], \
        captured["url"]

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        imagegen.generate("x", size=" 256X256 ")  # case + space ok
    assert "width=256" in captured["url"] and "height=256" in captured["url"], \
        captured["url"]


@_with_clean_env
def test_size_invalid():
    for bad in ["abc", "1024", "x1024", "1024x", "1x1", "99999x99999",
                "", "1024 x"]:
        r = imagegen.generate("x", size=bad)
        assert r["ok"] is False and "invalid size" in r["error"], (bad, r)


@_with_clean_env
def test_prompt_validation():
    r = imagegen.generate("")
    assert r["ok"] is False and "non-empty" in r["error"], r
    r = imagegen.generate("x" * 1001)
    assert r["ok"] is False and "too long" in r["error"], r


# --------------------------------------------------------------------------
# 3. Error handling: timeout / HTTP error -- fail-soft, không raise
# --------------------------------------------------------------------------

@_with_clean_env
def test_timeout_failsoft():
    def fake_get(url, **kwargs):
        raise httpx.TimeoutException("timed out")

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        r = imagegen.generate("a cat")
    assert r["ok"] is False, r
    assert "timed out" in r["error"], r
    assert r["provider"] == "pollinations"


@_with_clean_env
def test_http_error_failsoft():
    def fake_get(url, **kwargs):
        return _FakeResp(502, b"bad gateway", content_type="text/plain")

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        r = imagegen.generate("a cat")
    assert r["ok"] is False, r
    assert "502" in r["error"], r


@_with_clean_env
def test_empty_body_failsoft():
    def fake_get(url, **kwargs):
        return _FakeResp(200, b"")

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        r = imagegen.generate("a cat")
    assert r["ok"] is False and "empty" in r["error"], r


@_with_clean_env
def test_json_error_body():
    """Pollinations quá tải có thể trả JSON thay vì ảnh."""

    def fake_get(url, **kwargs):
        return _FakeResp(200, b'{"error":"overloaded"}',
                         content_type="application/json")

    with patch("media.imagegen.httpx.get", side_effect=fake_get):
        r = imagegen.generate("a cat")
    assert r["ok"] is False and "overloaded" in r["error"], r


# --------------------------------------------------------------------------
# 4. OpenAI fallback khi Pollinations lỗi (có key)
# --------------------------------------------------------------------------

def test_openai_fallback():
    def fake_get(url, **kwargs):
        if "pollinations" in url:
            return _FakeResp(500, b"boom", content_type="text/plain")
        captured_dl["url"] = url
        return _FakeResp(200, FAKE_PNG)

    captured_dl = {}

    def fake_post(url, **kwargs):
        captured_post["url"] = url
        captured_post["json"] = kwargs.get("json")
        return _FakeResp(
            200, b'{"data":[{"url":"https://cdn.example/x.png"}]}',
            content_type="application/json")

    captured_post = {}
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        with patch("media.imagegen.httpx.get", side_effect=fake_get), \
                patch("media.imagegen.httpx.post", side_effect=fake_post):
            r = imagegen.generate("a dog")

    assert r["ok"] is True, r
    assert r["provider"] == "openai"
    assert r["image_bytes"] == FAKE_PNG
    assert captured_post["url"] == imagegen.OPENAI_IMAGES_URL
    assert captured_post["json"]["prompt"] == "a dog"
    assert captured_post["json"]["size"] == "1024x1024"
    assert captured_dl["url"] == "https://cdn.example/x.png"


def test_openai_forced_without_key():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OPENAI_API_KEY", None)
        r = imagegen.generate("x", model="openai")
    assert r["ok"] is False and "OPENAI_API_KEY" in r["error"], r


def test_openai_post_failure_failsoft():
    def fake_post(url, **kwargs):
        return _FakeResp(401, b'{"error":{"message":"bad key"}}',
                         content_type="application/json")

    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-bad"}):
        with patch("media.imagegen.httpx.post", side_effect=fake_post):
            r = imagegen.generate("x", model="openai")
    assert r["ok"] is False, r
    assert "401" in r["error"], r


# --------------------------------------------------------------------------
# 5. generate_variation
# --------------------------------------------------------------------------

@_with_clean_env
def test_variation_not_supported_without_key():
    r = imagegen.generate_variation(FAKE_PNG, "happier")
    assert r["ok"] is False, r
    assert r["error"] == "not supported by provider: " + \
        "image variation needs OpenAI (OPENAI_API_KEY), " \
        "Pollinations free tier only does text-to-image", r


def test_variation_openai():
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["files"] = kwargs.get("files")
        return _FakeResp(
            200, b'{"data":[{"url":"https://cdn.example/v.png"}]}',
            content_type="application/json")

    def fake_get(url, **kwargs):
        return _FakeResp(200, FAKE_PNG)

    captured = {}
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        with patch("media.imagegen.httpx.post", side_effect=fake_post), \
                patch("media.imagegen.httpx.get", side_effect=fake_get):
            r = imagegen.generate_variation(FAKE_PNG, "happier")
    assert r["ok"] is True, r
    assert r["provider"] == "openai"
    assert captured["url"] == imagegen.OPENAI_VARIATIONS_URL
    assert captured["files"]["image"][2] == "image/png"


def test_variation_empty_bytes():
    r = imagegen.generate_variation(b"")
    assert r["ok"] is False and "required" in r["error"], r


# --------------------------------------------------------------------------
# 6. save_image / available_providers / build_tools
# --------------------------------------------------------------------------

def test_save_image():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sub", "out.png")
        ret = imagegen.save_image(FAKE_PNG, p)
        assert ret == p
        with open(p, "rb") as f:
            assert f.read() == FAKE_PNG


def test_save_image_invalid():
    try:
        imagegen.save_image(b"", "/tmp/x.png")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty bytes")


@_with_clean_env
def test_available_providers_no_key():
    assert imagegen.available_providers() == ["pollinations"]


def test_available_providers_with_key():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        assert imagegen.available_providers() == ["pollinations", "openai"]


def test_build_tools():
    tools = imagegen.build_tools(env=None)
    assert "generate_image" in tools
    assert "generate_image_variation" in tools
    assert "prompt" in tools["generate_image"]["schema"]["required"]
    assert callable(tools["generate_image"]["func"])


# --------------------------------------------------------------------------
# Runner (pytest cũng chạy được vì các hàm tên test_*)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print("FAIL %s: %r" % (t.__name__, e))
        else:
            passed += 1
            print("ok   %s" % t.__name__)
    print("\n%d passed, %d failed (%d total)" % (passed, failed,
                                                passed + failed))
    sys.exit(1 if failed else 0)
