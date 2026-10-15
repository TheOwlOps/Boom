"""Tests for the connectors framework: registry, credentials, base contract.

No network, no real credentials: fake Connector subclasses stand in for real
service connectors. Run: python3 evals/test_connector_registry.py
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent" / "src"))

from connectors.base import Connector, _schema
from connectors.credentials import (
    connector_env,
    get_token,
    get_token_for,
    require_token,
    require_token_for,
)
from connectors.registry import (
    ConnectorRegistry,
    DuplicateToolError,
    load_default_registry,
)

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


class FakeMail(Connector):
    name = "fakemail"

    def __init__(self, connected=True):
        self._connected = connected

    def status(self):
        return {"connected": self._connected,
                "detail": "ok" if self._connected else "BOOM_FAKEMAIL_TOKEN not set"}

    def build_tools(self):
        def _search(query: str):
            if not self._connected:
                return {"error": "fakemail not connected"}
            return {"results": [f"hit for {query}"]}

        return {
            "fakemail_search": {
                "name": "fakemail_search",
                "func": _search,
                "description": "Search fake mail.",
                "schema": _schema({"query": {"type": "string"}}, ["query"]),
            }
        }


class FakeCal(Connector):
    name = "fakecal"

    def status(self):
        return {"connected": False, "detail": "BOOM_FAKECAL_TOKEN not set"}

    def build_tools(self):
        return {
            "fakecal_list": {
                "name": "fakecal_list",
                "func": lambda: {"events": []},
                "description": "List fake events.",
                "schema": _schema(),
            }
        }


class Exploding(Connector):
    name = "exploding"

    def status(self):
        raise RuntimeError("boom")

    def build_tools(self):
        return {}


class NameClash(Connector):
    name = "clash"

    def status(self):
        return {"connected": True, "detail": "ok"}

    def build_tools(self):  # same tool name as FakeMail -> must raise
        return {"fakemail_search": {"name": "x", "func": lambda: {},
                                    "description": "x", "schema": _schema()}}


def test_register_and_names():
    r = ConnectorRegistry()
    r.register(FakeMail())
    r.register(FakeCal())
    check("names in order", r.names() == ["fakemail", "fakecal"])
    check("get returns connector", isinstance(r.get("fakemail"), FakeMail))
    check("get unknown -> None", r.get("nope") is None)


def test_register_rejects_bad():
    r = ConnectorRegistry()
    r.register(FakeMail())
    try:
        r.register(FakeMail())
        check("duplicate connector name raises", False)
    except ValueError:
        check("duplicate connector name raises", True)
    try:
        r.register(object())
        check("non-Connector raises TypeError", False)
    except TypeError:
        check("non-Connector raises TypeError", True)

    class Nameless(Connector):
        name = ""
        def status(self): return {"connected": False, "detail": ""}
        def build_tools(self): return {}
    try:
        r.register(Nameless())
        check("empty name raises", False)
    except ValueError:
        check("empty name raises", True)


def test_status_all():
    r = ConnectorRegistry()
    r.register(FakeMail(connected=True))
    r.register(FakeCal())
    r.register(Exploding())
    st = r.status_all()
    check("connected True", st["fakemail"] == {"connected": True, "detail": "ok"})
    check("missing cred -> connected False",
          st["fakecal"] == {"connected": False, "detail": "BOOM_FAKECAL_TOKEN not set"})
    check("raising status() -> connected False",
          st["exploding"]["connected"] is False and "RuntimeError" in st["exploding"]["detail"])


def test_build_tools_merge():
    r = ConnectorRegistry()
    r.register(FakeMail())
    r.register(FakeCal())
    tools = r.build_tools()
    check("both tools merged", set(tools) == {"fakemail_search", "fakecal_list"})
    check("tool dict shape",
          tools["fakemail_search"]["name"] == "fakemail_search"
          and callable(tools["fakemail_search"]["func"]))
    check("tool func really runs",
          tools["fakemail_search"]["func"]("hello") == {"results": ["hit for hello"]})


def test_duplicate_tool_raises():
    r = ConnectorRegistry()
    r.register(FakeMail())
    r.register(NameClash())
    try:
        r.build_tools()
        check("duplicate tool name raises", False)
    except DuplicateToolError as e:
        msg = str(e)
        check("duplicate tool name raises",
              "fakemail_search" in msg and "fakemail" in msg and "clash" in msg)


def test_load_default_skips_missing():
    r = load_default_registry(module_names=("connectors.does_not_exist_xyz",))
    check("missing module skipped, no crash", r.names() == [])


def test_load_default_picks_up_connector():
    mod = types.ModuleType("fake_conn_mod_for_test")
    mod.CONNECTOR = FakeMail()
    sys.modules["fake_conn_mod_for_test"] = mod
    try:
        r = load_default_registry(module_names=("fake_conn_mod_for_test",))
        check("CONNECTOR picked up", r.names() == ["fakemail"])
        check("tools available", "fakemail_search" in r.build_tools())
    finally:
        del sys.modules["fake_conn_mod_for_test"]


def test_load_default_skips_no_connector_attr():
    mod = types.ModuleType("fake_empty_mod_for_test")
    sys.modules["fake_empty_mod_for_test"] = mod
    try:
        r = load_default_registry(module_names=("fake_empty_mod_for_test",))
        check("module without CONNECTOR skipped", r.names() == [])
    finally:
        del sys.modules["fake_empty_mod_for_test"]


def test_credentials():
    os.environ.pop("BOOM_TEST_UNSET_ABC", None)
    check("get_token unset -> ''", get_token("BOOM_TEST_UNSET_ABC") == "")
    check("get_token empty name -> ''", get_token("") == "")
    os.environ["BOOM_TEST_SET_ABC"] = "  secret  "
    try:
        check("get_token strips", get_token("BOOM_TEST_SET_ABC") == "secret")
        check("require_token returns value",
              require_token("BOOM_TEST_SET_ABC") == "secret")
    finally:
        del os.environ["BOOM_TEST_SET_ABC"]
    try:
        require_token("BOOM_TEST_UNSET_ABC")
        check("require_token raises when missing", False)
    except RuntimeError as e:
        check("require_token raises when missing", "BOOM_TEST_UNSET_ABC" in str(e))
    try:
        require_token("BOOM_TEST_UNSET_ABC", hint="Get one at example.com.")
        check("hint included", False)
    except RuntimeError as e:
        check("hint included", "example.com" in str(e))


def test_connector_env_names():
    check("canonical name", connector_env("gmail", "api_key") == "BOOM_GMAIL_API_KEY")
    check("dashes normalised", connector_env("http-api", "base-url") == "BOOM_HTTP_API_BASE_URL")
    os.environ.pop("BOOM_GMAIL_API_KEY", None)
    check("get_token_for -> '' when unset", get_token_for("gmail", "api_key") == "")
    os.environ["BOOM_GMAIL_API_KEY"] = "tok"
    try:
        check("require_token_for returns value",
              require_token_for("gmail", "api_key") == "tok")
    finally:
        del os.environ["BOOM_GMAIL_API_KEY"]


if __name__ == "__main__":
    print("== connector registry ==")
    test_register_and_names()
    test_register_rejects_bad()
    test_status_all()
    test_build_tools_merge()
    test_duplicate_tool_raises()
    test_load_default_skips_missing()
    test_load_default_picks_up_connector()
    test_load_default_skips_no_connector_attr()
    test_credentials()
    test_connector_env_names()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        raise SystemExit("FAILED: " + ", ".join(FAIL))
    print("ALL CONNECTOR REGISTRY TESTS PASSED")
