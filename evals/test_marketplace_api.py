"""Tests for agent/src/marketplace.py + the /api/marketplace* routes.

Run:  python3 evals/test_marketplace_api.py   (from the repo root)

NOTE: evals/test_marketplace.py already covers the registry packager
(agent/src/extensibility/marketplace.py); this file covers the web-UI
marketplace backend (agent/src/marketplace.py) and its gateway routes.

Uses the REAL PluginManager and the REAL AgentEnvironment tool list
(isolated into temp dirs); no network, no port binding — gateway/api.py
handlers are exercised directly via a stubbed ``self``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "agent" / "src"))

from agent.src.marketplace import (  # noqa: E402
    MarketplaceBackend,
    MarketplaceError,
    categorize_item,
)

PASSED = 0


def check(cond, label):
    global PASSED
    assert cond, "FAILED: %s" % label
    PASSED += 1
    print("ok - %s" % label)


def fresh_backend(env=None):
    """Backend isolated in temp dirs (never touches the real plugins/ or ~/.boom)."""
    tmp = tempfile.mkdtemp(prefix="marketplace_test_")
    plugins_dir = Path(tmp) / "plugins"
    state_path = Path(tmp) / "state.json"
    if env is None:
        from agent.src.environment import AgentEnvironment
        env = AgentEnvironment(root_dir=Path(tmp) / "root")
    backend = MarketplaceBackend(
        plugins_dir=plugins_dir, state_path=state_path, env=env
    )
    return backend, tmp


# ---------------------------------------------------------------------------
# 1. Catalog built from REAL tool descriptions (no hardcoded list)
# ---------------------------------------------------------------------------

backend, tmp = fresh_backend()

catalog = backend.get_catalog()
check(len(catalog) >= 10, "catalog is not empty (>= 10 items, got %d)" % len(catalog))

REQUIRED_FIELDS = {"id", "name", "icon", "description", "category", "featured", "kind"}
for item in catalog:
    missing = REQUIRED_FIELDS - set(item.keys())
    assert not missing, "FAILED: item %s missing fields %s" % (item.get("id"), missing)
PASSED += 1
print("ok - every catalog item has all required fields %s" % sorted(REQUIRED_FIELDS))

ids = [i["id"] for i in catalog]
check(len(ids) == len(set(ids)), "catalog ids are unique")
check(all(i["kind"] in ("plugin", "tool") for i in catalog),
      "every item kind is plugin or tool")
check(all(isinstance(i["featured"], bool) for i in catalog),
      "featured is a boolean on every item")
check(all(i["category"] for i in catalog),
      "every item has a non-empty category")
check(all(i["icon"] for i in catalog), "every item has an icon")
check(all(i["kind"] == "tool" for i in catalog),
      "no phantom plugins in a fresh plugins dir")

# ---------------------------------------------------------------------------
# 2. Category mapping is keyword-rule based and sane on real tools
# ---------------------------------------------------------------------------

by_name = {i["name"]: i for i in catalog}
gmail_tools = [n for n in by_name if "gmail" in n]
check(bool(gmail_tools), "real gmail tools exist in the environment")
check(all(by_name[n]["category"] == "Productivity" for n in gmail_tools),
      "gmail* tools -> Productivity")
tg_tools = [n for n in by_name if "telegram" in n]
if tg_tools:
    check(all(by_name[n]["category"] == "Communication" for n in tg_tools),
          "telegram* tools -> Communication")
else:
    print("skip - no telegram* tool in this environment")
    PASSED += 1

check(categorize_item("gmail_send", "Send an email") == "Productivity",
      "categorize_item: gmail_send -> Productivity")
check(categorize_item("shell_run", "Run a shell command") == "System",
      "categorize_item: shell_run -> System")
check(categorize_item("zzz_no_match_xyz", "nothing relevant here") == "Utilities",
      "categorize_item: unknown -> Utilities")

# ---------------------------------------------------------------------------
# 3. Plugin round-trip through the REAL PluginManager
# ---------------------------------------------------------------------------

pbackend, ptmp = fresh_backend()

# Build a minimal real plugin source dir.
src = Path(ptmp) / "srcplug"
src.mkdir()
(src / "plugin.json").write_text(json.dumps({
    "name": "test-market-plugin",
    "version": "1.0.0",
    "description": "A test plugin for the marketplace backend",
    "author": "boom-test",
    "icon": "🔌",
}), encoding="utf-8")
(src / "entry.py").write_text(
    "def on_enable(ctx):\n    ctx.setdefault('tools', {})['test_market_tool'] = {}\n",
    encoding="utf-8",
)

# Install via the real PluginManager (same object the backend delegates to).
res = pbackend.manager.install(src)
check(res.get("installed"), "PluginManager.install installs the test plugin")

# The catalog now builds the plugin entry from its REAL manifest.
cat2 = pbackend.get_catalog()
plug_items = [i for i in cat2 if i["kind"] == "plugin"]
check(len(plug_items) == 1, "catalog picks up the installed plugin")
plug = plug_items[0]
check(plug["id"] == "plugin:test-market-plugin", "plugin id is namespaced")
check(plug["description"] == "A test plugin for the marketplace backend",
      "plugin description comes from the real plugin.json")
check(plug["icon"] == "🔌", "plugin icon comes from the real plugin.json")
check(plug["featured"] is True, "plugins are featured by rule")
check(plug["version"] == "1.0.0", "plugin version from the manifest")

# install_item enables it through the manager.
r = pbackend.install_item("plugin:test-market-plugin")
check(r == {"ok": True, "id": "plugin:test-market-plugin"},
      "install_item(plugin) -> {ok, id}")
check("plugin:test-market-plugin" in pbackend.get_installed(),
      "installed plugin shows up in get_installed()")
check(pbackend.manager.plugins["test-market-plugin"]["enabled"] is True,
      "install_item really enabled the plugin via PluginManager")

# Bare-name resolution also works.
r = pbackend.install_item("test-market-plugin")
check(r["ok"] and r["id"] == "plugin:test-market-plugin",
      "install_item accepts a bare plugin name")

# uninstall_item removes it through the manager.
r = pbackend.uninstall_item("plugin:test-market-plugin")
check(r == {"ok": True, "id": "plugin:test-market-plugin"},
      "uninstall_item(plugin) -> {ok, id}")
check("plugin:test-market-plugin" not in pbackend.get_installed(),
      "uninstalled plugin disappears from get_installed()")
check("test-market-plugin" not in pbackend.manager.plugins,
      "uninstall_item really removed the plugin via PluginManager")
check(not (Path(ptmp) / "plugins" / "test-market-plugin").exists(),
      "plugin directory deleted from disk")

# ---------------------------------------------------------------------------
# 4. Builtin tool enable/disable round-trip (+ state persistence)
# ---------------------------------------------------------------------------

tbackend, ttmp = fresh_backend()
state_file = Path(ttmp) / "state.json"
tool_id = "tool:read_file"

check(tool_id in tbackend.get_installed(), "builtin tool starts enabled")
r = tbackend.uninstall_item(tool_id)
check(r == {"ok": True, "id": tool_id}, "uninstall_item(tool) -> {ok, id}")
check(tool_id not in tbackend.get_installed(),
      "disabled tool disappears from get_installed()")

# Disabled set is persisted to the state file.
saved = json.loads(state_file.read_text(encoding="utf-8"))
check("read_file" in saved.get("disabled_tools", []),
      "disabled tool persisted to the state file")

# A fresh backend with the same state file honors it.
from agent.src.environment import AgentEnvironment  # noqa: E402
env2 = AgentEnvironment(root_dir=Path(ttmp) / "root2")
b2 = MarketplaceBackend(
    plugins_dir=Path(ttmp) / "plugins", state_path=state_file, env=env2
)
check(tool_id not in b2.get_installed(),
      "new backend instance respects the persisted disabled set")
check([i for i in b2.get_catalog() if i["id"] == tool_id][0]["enabled"] is False,
      "catalog marks the disabled tool enabled=False")

r = b2.install_item(tool_id)
check(r == {"ok": True, "id": tool_id}, "install_item(tool) -> {ok, id}")
check(tool_id in b2.get_installed(), "re-enabled tool is back in get_installed()")

# uninstall is idempotent.
r = b2.uninstall_item(tool_id)
r = b2.uninstall_item(tool_id)
check(r["ok"], "double uninstall stays ok")

# ---------------------------------------------------------------------------
# 5. Error handling: unknown ids -> 404, bad input -> 400
# ---------------------------------------------------------------------------

ebackend, _ = fresh_backend()


def expect_error(item_id, status, label):
    for fn in (ebackend.install_item, ebackend.uninstall_item):
        try:
            fn(item_id)
        except MarketplaceError as e:
            assert e.status == status, (
                "FAILED: %s: expected status %s, got %s" % (label, status, e.status))
        else:
            raise AssertionError("FAILED: %s: no error raised" % label)
    global PASSED
    PASSED += 1
    print("ok - %s" % label)


expect_error("tool:does_not_exist", 404, "unknown tool -> 404")
expect_error("plugin:does_not_exist", 404, "unknown plugin -> 404")
expect_error("nope_nothing", 404, "unknown bare name -> 404")
expect_error("", 400, "empty id -> 400")
expect_error("bogus:read_file", 400, "invalid id prefix -> 400")

# ---------------------------------------------------------------------------
# 6. gateway/api.py handlers directly (no port binding)
# ---------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location(
    "boom_gateway_api", str(REPO_ROOT / "gateway" / "api.py"))
api_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_mod)

hbackend, htmp = fresh_backend()
api_mod._marketplace_backend = lambda: hbackend  # isolate from real ~/.boom


class _Stub:
    """Stands in for _Handler: only the response plumbing the routes use."""

    def __init__(self, body=None):
        self.body = body
        self.sent = []

    def _send_json(self, status, payload):
        self.sent.append((status, payload))

    def _error(self, status, message):
        self.sent.append((status, {"error": message}))

    def _read_json_body(self):
        return (self.body if self.body is not None else {}), None


def call_handler(method_name, body=None):
    stub = _Stub(body)
    getattr(api_mod._Handler, method_name)(stub)
    assert len(stub.sent) == 1, "FAILED: %s sent no response" % method_name
    return stub.sent[0]


status, payload = call_handler("_handle_marketplace_catalog")
check(status == 200, "GET /api/marketplace -> 200")
check(isinstance(payload.get("items"), list) and payload["count"] == len(payload["items"]),
      "GET /api/marketplace -> {items, count}")
check(payload["count"] >= 10, "handler catalog has >= 10 items")

status, payload = call_handler("_handle_marketplace_installed")
check(status == 200, "GET /api/marketplace/installed -> 200")
check(isinstance(payload.get("installed"), list),
      "GET /api/marketplace/installed -> {installed: [...]}")
check("tool:read_file" in payload["installed"],
      "installed list contains a real builtin tool")

status, payload = call_handler("_handle_marketplace_uninstall", {"id": "tool:read_file"})
check(status == 200 and payload == {"ok": True, "id": "tool:read_file"},
      "POST /api/marketplace/uninstall -> {ok, id}")
status, payload = call_handler("_handle_marketplace_installed")
check("tool:read_file" not in payload["installed"],
      "uninstalled tool gone from handler installed list")

status, payload = call_handler("_handle_marketplace_install", {"id": "tool:read_file"})
check(status == 200 and payload == {"ok": True, "id": "tool:read_file"},
      "POST /api/marketplace/install -> {ok, id}")

status, payload = call_handler("_handle_marketplace_install", {"id": "tool:nope"})
check(status == 404, "POST install unknown tool -> 404")
check("error" in payload, "404 body carries an error message")

status, payload = call_handler("_handle_marketplace_uninstall", {"id": "plugin:nope"})
check(status == 404, "POST uninstall unknown plugin -> 404")

status, payload = call_handler("_handle_marketplace_install", {"id": ""})
check(status == 400, "POST install empty id -> 400")

status, payload = call_handler("_handle_marketplace_install", {})
check(status == 400, "POST install missing id -> 400")

print("\n%d assertions PASSED" % PASSED)
