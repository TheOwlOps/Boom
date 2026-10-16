"""Tests for agent/src/providers/manager.py.

Stdlib only. Uses a temp dir instead of the real ~/.boom, and mocks
``_request_json`` so no network is touched.

Run: python3 evals/test_provider_manager.py
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent" / "src"))

from providers.manager import (  # noqa: E402
    PROVIDER_DEFS,
    ProviderError,
    ProviderManager,
    UnknownProviderError,
    _request_json,
)

PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        raise AssertionError(name)


def make_manager():
    tmp = tempfile.mkdtemp(prefix="boom-providers-test-")
    return ProviderManager(base_dir=Path(tmp)), Path(tmp)


# ------------------------------------------------------------- metadata

mgr, _ = make_manager()
defs = mgr.list_providers()
check("list_providers_returns_7", len(defs) == 7, f"got {len(defs)}")
check(
    "list_providers_ids",
    [d["id"] for d in defs]
    == ["openai", "anthropic", "google", "xai", "deepseek", "ollama", "custom"],
)
required = {"id", "name", "models", "needs_key", "default_base_url",
            "show_base_url", "allow_custom_model"}
check("metadata_has_all_fields", all(required <= set(d) for d in defs))
check("metadata_has_no_secrets", not any("api_key" in d for d in defs))
by_id = {d["id"]: d for d in defs}
check("ollama_needs_no_key", by_id["ollama"]["needs_key"] is False)
check("openai_needs_key", by_id["openai"]["needs_key"] is True)
check("ollama_shows_base_url", by_id["ollama"]["show_base_url"] is True)
check("custom_shows_base_url", by_id["custom"]["show_base_url"] is True)
check("openai_hides_base_url", by_id["openai"]["show_base_url"] is False)
check("every_provider_has_models_or_custom",
      all(d["models"] or d["allow_custom_model"] for d in defs))

# ------------------------------------------------------------- storage

mgr, tmpdir = make_manager()
check("get_config_missing_returns_none", mgr.get_config("openai") is None)
check("has_key_false_initially", mgr.has_key("openai") is False)
check("active_none_initially", mgr.get_active_provider() is None)

mgr.save_config("openai", "sk-test-123", "gpt-4o")
cfg = mgr.get_config("openai")
check("get_config_returns_saved_key", cfg is not None and cfg["api_key"] == "sk-test-123")
check("get_config_returns_saved_model", cfg["model"] == "gpt-4o")
check("get_config_defaults_base_url",
      cfg["base_url"] == "https://api.openai.com/v1", cfg["base_url"])
check("has_key_true_after_save", mgr.has_key("openai") is True)

# Custom base URL is honored.
mgr.save_config("ollama", "", "llama3.3", base_url="http://nas:11434/v1")
cfg = mgr.get_config("ollama")
check("custom_base_url_saved", cfg["base_url"] == "http://nas:11434/v1", cfg["base_url"])

# Config file permissions: owner-only.
cfg_path = tmpdir / "providers.json"
check("config_file_exists", cfg_path.exists())
mode = stat.S_IMODE(cfg_path.stat().st_mode)
check("config_file_0600", mode == 0o600, oct(mode))

# Persistence across instances.
mgr2 = ProviderManager(base_dir=tmpdir)
cfg2 = mgr2.get_config("openai")
check("config_survives_reload", cfg2 is not None and cfg2["api_key"] == "sk-test-123")

# Errors.
try:
    mgr.save_config("nope", "k", "m")
    check("save_unknown_provider_raises", False)
except UnknownProviderError:
    check("save_unknown_provider_raises", True)
try:
    mgr.save_config("openai", "", "gpt-4o")
    check("save_requires_key_for_keyed_provider", False)
except ValueError:
    check("save_requires_key_for_keyed_provider", True)
# Ollama needs no key.
mgr.save_config("ollama", "", "qwen3")
check("save_keyless_provider_ok", mgr.get_config("ollama")["model"] == "qwen3")
try:
    mgr.set_active_provider("nope")
    check("set_active_unknown_raises", False)
except UnknownProviderError:
    check("set_active_unknown_raises", True)

mgr.set_active_provider("deepseek")
check("get_active_provider", mgr.get_active_provider() == "deepseek")
check("active_survives_reload",
      ProviderManager(base_dir=tmpdir).get_active_provider() == "deepseek")

# ------------------------------------------------------------- test_connection

mgr, _ = make_manager()


def patched(status=None, body=None, exc=None):
    """Return a context manager patching _request_json."""
    if exc is not None:
        return mock.patch("providers.manager._request_json", side_effect=exc)
    return mock.patch("providers.manager._request_json", return_value=(status, body))


ok, msg = mgr.test_connection("openai")
check("test_no_config_fails", ok is False and "no configuration" in msg, msg)

mgr.save_config("openai", "sk-live", "gpt-4o")
with patched(status=200, body={"data": []}):
    ok, msg = mgr.test_connection("openai")
check("test_200_ok", ok is True and msg == "connected", msg)

with patched(status=401, body={"error": {"message": "bad key"}}):
    ok, msg = mgr.test_connection("openai")
check("test_401_fails", ok is False and "401" in msg and "API key" in msg, msg)
check("test_401_message_hides_key", "sk-live" not in msg, msg)

with patched(exc=ProviderError("connection failed: refused")):
    ok, msg = mgr.test_connection("openai")
check("test_transport_error_fails", ok is False and "refused" in msg, msg)

with patched(status=500, body=None):
    ok, msg = mgr.test_connection("openai")
check("test_500_fails", ok is False and "500" in msg, msg)

ok, msg = mgr.test_connection("nope")
check("test_unknown_provider_fails", ok is False and "unknown provider" in msg, msg)

# Anthropic probe shape: POST /v1/messages with the x-api-key header.
mgr.save_config("anthropic", "sk-ant-test", "claude-sonnet-4-5")
with mock.patch("providers.manager._request_json",
                return_value=(200, {"content": []})) as m:
    ok, _ = mgr.test_connection("anthropic")
    args, kwargs = m.call_args
    check("anthropic_probe_method_post", args[0] == "POST", args)
    check("anthropic_probe_url", args[1].endswith("/v1/messages"), args[1])
    check("anthropic_probe_header", kwargs["headers"].get("x-api-key") == "sk-ant-test")
    check("anthropic_probe_body_max_tokens", kwargs["body"]["max_tokens"] == 1)

# Ollama (keyless) probe: GET /models, no Authorization header.
mgr.save_config("ollama", "", "llama3.3")
with mock.patch("providers.manager._request_json",
                return_value=(200, {"data": []})) as m:
    ok, _ = mgr.test_connection("ollama")
    args, kwargs = m.call_args
    check("ollama_probe_get_models", args[0] == "GET" and args[1].endswith("/models"))
    check("ollama_probe_no_auth_header",
          "Authorization" not in (kwargs.get("headers") or {}))
    check("ollama_probe_ok", ok is True)

# Gemini probe: key goes in the query string, never in the message.
mgr.save_config("google", "AIza-secret", "gemini-2.5-flash")
with mock.patch("providers.manager._request_json",
                return_value=(200, {"models": []})) as m:
    ok, _ = mgr.test_connection("google")
    url = m.call_args[0][1]
    check("gemini_probe_models_url", "v1beta/models" in url, url)
with patched(status=400, body={"error": {}}):
    ok, msg = mgr.test_connection("google")
check("gemini_400_hides_key", ok is False and "AIza-secret" not in msg, msg)

print(f"\n{ PASS } assertions passed")
