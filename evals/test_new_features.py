"""Tests for the three new Boom features: gmail_tool, draft-not-send approval,
and search_tools meta tool.

Run:  python3 evals/test_new_features.py   (or: pytest evals/test_new_features.py)
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

# Repo layout: evals/ sits next to agent/, so agent/src is importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent" / "src"))

from tools import gmail_tool
from tools.tool_discovery import search_tools
from security.approval import ApprovalGate, SAFE_TOOLS


class _FakeEntry:
    def __init__(self, name, description, toolset, schema):
        self.name = name
        self.description = description
        self.toolset = toolset
        self.schema = schema


class _FakeRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, name, func, toolset="default", description="", schema=None):
        self._tools[name] = _FakeEntry(name, description, toolset, schema or {})

    def get_all_tool_names(self):
        return sorted(self._tools.keys())

    def get_tool(self, name):
        return self._tools.get(name)

    def get_registered_toolset_names(self):
        return sorted({e.toolset for e in self._tools.values()})

    def get_tools_in_toolset(self, toolset):
        return [n for n, e in self._tools.items() if e.toolset == toolset]


def _install_fake_registry():
    """Inject a fake tools.registry so search_tools works without AgentEnvironment."""
    import types
    fake_registry_module = types.ModuleType("tools.registry")
    fake_registry_module.registry = _FakeRegistry()
    sys.modules["tools.registry"] = fake_registry_module
    return fake_registry_module.registry


def test_search_tools_finds_gmail():
    """search_tools('gmail') must return the gmail_* tools with schemas."""
    # search_tools reads the global registry; seed a fake one so the test
    # does not need a full AgentEnvironment.
    registry = _install_fake_registry()

    registry.register(
        "gmail_search", lambda query: {}, toolset="gmail",
        description="Search Gmail messages.",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    registry.register(
        "gmail_send", lambda draft_id: {}, toolset="gmail",
        description="Send a draft email. DANGER.",
        schema={"type": "object", "properties": {"draft_id": {"type": "string"}}},
    )
    registry.register(
        "read_file", lambda path: "", toolset="file",
        description="Read a file from disk.",
        schema={"type": "object"},
    )

    result = search_tools("gmail")
    assert "error" not in result, result
    names = [t["name"] for t in result["tools"]]
    assert "gmail_search" in names, names
    assert "gmail_send" in names, names
    assert "read_file" not in names, names  # unrelated tool must not match
    # Schemas come along so the model can call the tool next.
    by_name = {t["name"]: t for t in result["tools"]}
    assert by_name["gmail_search"]["schema"]["type"] == "object"
    print("[PASS] search_tools finds gmail tools by keyword, with schemas")


def test_search_tools_empty_keyword():
    result = search_tools("   ")
    assert "error" in result
    print("[PASS] search_tools rejects empty keyword")


def test_gmail_draft_has_no_side_effects():
    """gmail_draft must NOT touch the Gmail API — draft stays local."""
    with patch.object(gmail_tool, "_api") as mock_api:
        result = gmail_tool.gmail_draft(
            to="boss@example.com",
            subject="Q3 report",
            body="Here is the draft body.",
        )
    mock_api.assert_not_called()
    assert "error" not in result, result
    assert result["draft_id"].startswith("draft_")
    assert result["to"] == "boss@example.com"
    assert "requires approval" in result["note"]
    print("[PASS] gmail_draft creates a local draft with zero API calls")


def test_gmail_draft_validation():
    assert "error" in gmail_tool.gmail_draft(to="not-an-email", subject="s", body="b")
    assert "error" in gmail_tool.gmail_draft(to="a@b.com", subject="s", body="   ")
    print("[PASS] gmail_draft validates inputs")


def test_gmail_send_requires_approval():
    """gmail_send must NOT be in SAFE_TOOLS — the gate always prompts."""
    assert "gmail_send" not in SAFE_TOOLS, "gmail_send must require approval!"
    # And the read/draft helpers ARE safe (no prompt spam for harmless calls).
    for safe in ("gmail_search", "gmail_read", "gmail_draft", "gmail_discard",
                 "search_tools", "list_toolsets"):
        assert safe in SAFE_TOOLS, f"{safe} should be a safe tool"
    print("[PASS] gmail_send is approval-gated; read/draft/search are safe")


def test_gmail_send_denied_makes_no_api_call():
    """Simulate the ReActLoop refusal path: gate denies -> _api never called."""
    # First make a draft (no API).
    with patch.object(gmail_tool, "_api") as mock_api:
        draft = gmail_tool.gmail_draft(to="boss@example.com", subject="s", body="b")
    mock_api.assert_not_called()
    draft_id = draft["draft_id"]

    # The loop calls gate.check_approval BEFORE running the tool function.
    gate = ApprovalGate(yolo=False, interactive_prompt_fn=lambda name, args: False)
    approved = gate.check_approval("gmail_send", {"draft_id": draft_id})
    assert approved is False, "gate should deny when the user says no"

    # Because approval was denied, the loop never calls gmail_send at all.
    # The draft is still there, unsent — nothing leaked.
    assert draft_id in gmail_tool._DRAFTS
    print("[PASS] denied approval -> gmail_send never runs -> zero API calls")


def test_gmail_send_approved_calls_api_once():
    """When approved, gmail_send calls the Gmail API exactly once and consumes the draft."""
    with patch.object(gmail_tool, "_api") as mock_api:
        draft = gmail_tool.gmail_draft(to="boss@example.com", subject="s", body="b")
    draft_id = draft["draft_id"]

    fake_response = {"id": "msg_123", "threadId": "thread_456"}
    with patch.object(gmail_tool, "_api", return_value=fake_response) as mock_api:
        gate = ApprovalGate(yolo=False, interactive_prompt_fn=lambda name, args: True)
        assert gate.check_approval("gmail_send", {"draft_id": draft_id}) is True
        result = gmail_tool.gmail_send(draft_id)

    assert mock_api.call_count == 1, f"expected 1 API call, got {mock_api.call_count}"
    call = mock_api.call_args
    assert call.args[0] == "/users/me/messages/send"
    assert call.kwargs.get("method") == "POST"
    assert result.get("sent") is True
    assert result["gmail_id"] == "msg_123"
    assert draft_id not in gmail_tool._DRAFTS, "sent draft must be consumed"
    print("[PASS] approved gmail_send calls the API once and consumes the draft")


def test_gmail_send_unknown_draft():
    result = gmail_tool.gmail_send("draft_nonexistent")
    assert "error" in result
    print("[PASS] gmail_send rejects unknown draft_id")


def test_gmail_missing_dep_error():
    """Without google-auth-oauthlib, functions return a clear install hint."""
    with patch.object(gmail_tool, "_get_credentials", side_effect=ImportError("no module")):
        # _api catches ImportError from _get_credentials and returns the hint.
        result = gmail_tool._api("/users/me/messages")
    assert "error" in result
    assert "pip install" in result["error"] and "boom[gmail]" in result["error"]
    print("[PASS] missing google-auth-oauthlib -> clear pip install hint")


def main():
    print("Testing new Boom features (gmail, draft-not-send, search_tools)...")
    test_search_tools_finds_gmail()
    test_search_tools_empty_keyword()
    test_gmail_draft_has_no_side_effects()
    test_gmail_draft_validation()
    test_gmail_send_requires_approval()
    test_gmail_send_denied_makes_no_api_call()
    test_gmail_send_approved_calls_api_once()
    test_gmail_send_unknown_draft()
    test_gmail_missing_dep_error()
    print("ALL NEW-FEATURE TESTS PASSED.")


if __name__ == "__main__":
    main()
