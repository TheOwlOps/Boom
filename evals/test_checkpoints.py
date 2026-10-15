"""Check session file checkpoints: the snapshot is taken before the write, and rewind undoes it.

First half drives the store directly (containment, first-snapshot-wins, dry run, a corrupt
manifest). Second half runs a real ReActLoop against a real AgentEnvironment with a scripted
provider, so the hook in `execute_step` — not just the store — is what gets tested.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from environment import AgentEnvironment
from loop.react import ReActLoop
from memory.checkpoints import CheckpointStore
from providers.llm import LLMResponse, LLMUsage, ToolCallSpec


def test_store():
    tmp = Path(tempfile.mkdtemp(prefix="boom-ckpt-"))
    try:
        store = CheckpointStore(tmp)
        note = tmp / "note.txt"
        note.write_text("human wrote this", encoding="utf-8")
        new = tmp / "sub" / "new.txt"

        assert store.snapshot("s1", note) == "note.txt"
        assert store.snapshot("s1", new) == "sub/new.txt"
        assert store.status("s1")["count"] == 2

        # First snapshot wins: a later write must not overwrite the pre-session content.
        note.write_text("agent wrote this", encoding="utf-8")
        store.snapshot("s1", note)
        assert store.status("s1")["count"] == 2
        note.write_text("agent wrote again", encoding="utf-8")

        # Containment: outside the root, a directory, and traversal are all refused.
        outside = Path(tempfile.mkdtemp(prefix="boom-outside-")) / "x.txt"
        outside.write_text("elsewhere", encoding="utf-8")
        try:
            assert store.snapshot("s1", outside) is None
            assert store.snapshot("s1", "../escape.txt") is None
            assert store.snapshot("s1", tmp) is None
            assert store.snapshot("s1", None) is None
            assert store.status("s1")["count"] == 2
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

        # Dry run reports the plan and changes nothing.
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text("created by the agent", encoding="utf-8")
        plan = store.rewind("s1", dry_run=True)
        assert plan["dry_run"] is True and plan["restored"] == ["note.txt"], plan
        assert plan["deleted"] == ["sub/new.txt"], plan
        assert note.read_text(encoding="utf-8") == "agent wrote again"
        assert new.exists()

        # The real rewind: the file the session overwrote goes back, the one it created goes away.
        done = store.rewind("s1")
        assert done["restored"] == ["note.txt"] and done["deleted"] == ["sub/new.txt"], done
        assert done["skipped"] == [], done
        assert note.read_text(encoding="utf-8") == "human wrote this"
        assert not new.exists()

        # A rewind is idempotent and a second one has nothing left to do.
        assert store.rewind("s1")["restored"] == ["note.txt"]

        # A session with no snapshots, and a corrupt manifest, must not raise.
        assert store.status("nobody")["count"] == 0
        assert store.rewind("nobody")["restored"] == []
        (store.base_dir / "s1" / "manifest.json").write_text("{not json", encoding="utf-8")
        assert store.status("s1")["count"] == 0
        assert store.rewind("s1")["deleted"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[PASS] store: snapshot-before-write, containment, dry run, corrupt manifest, idempotent rewind.")


class ScriptedLLM:
    """Writes two files — one the human made, one new — then rewinds the session."""

    def __init__(self, overwrite: Path, created: Path):
        self.model = "scripted"
        self.overwrite = overwrite
        self.created = created
        self.turn = 0

    async def chat_complete(self, messages, tools_schema=None):
        self.turn += 1
        if self.turn == 1:
            call = ToolCallSpec(
                id="c1", name="write_file",
                arguments={"path": str(self.overwrite), "content": "agent wrote this"},
            )
        elif self.turn == 2:
            call = ToolCallSpec(
                id="c2", name="write_file",
                arguments={"path": str(self.created), "content": "created by the agent"},
            )
        elif self.turn == 3:
            call = ToolCallSpec(id="c3", name="rewind", arguments={})
        else:
            return LLMResponse(
                content="Rewound.", usage=LLMUsage(input_tokens=1, output_tokens=1), finish_reason="stop"
            )
        return LLMResponse(
            content="", tool_calls=[call], usage=LLMUsage(input_tokens=2, output_tokens=2),
            finish_reason="tool_calls",
        )


async def test_loop_hook():
    tmp = Path(tempfile.mkdtemp(prefix="boom-loop-ckpt-"))
    note = tmp / "note.txt"
    note.write_text("human wrote this", encoding="utf-8")
    created = tmp / "made.txt"
    db = tmp / "state.db"

    env = AgentEnvironment(root_dir=tmp, db_path=db, yolo=True)
    llm = ScriptedLLM(note, created)
    loop = ReActLoop(env, llm, accountant=env.token_accountant, session_id="ckpt-session")
    try:
        result = await loop.step("Edit both files, then undo your edits.")
        assert result["success"] is True, result
        assert env.checkpoints.session_id == "ckpt-session"

        # Asserted after the loop: the rewind tool actually reached the store.
        assert note.read_text(encoding="utf-8") == "human wrote this", note.read_text(encoding="utf-8")
        assert not created.exists(), "the file the session created must be gone"
        assert env.checkpoints.status("ckpt-session")["count"] == 2

        obs = [entry["observation"] for entry in result["trace"] if entry.get("action")]
        rewind_obs = obs[-1]
        # The observation is the dict's repr, not JSON — that is how every tool result reaches
        # the model in this harness, so the test asserts on the text the model actually saw.
        assert "note.txt" in rewind_obs and "made.txt" in rewind_obs, rewind_obs
        assert "'dry_run': False" in rewind_obs, rewind_obs

        # The transcript would still read as if the edits stood, so the rewind is written into it:
        # a system message the next turn sees, and a timeline event an operator can read back.
        notes = [m for m in env.state_db.get_session_messages("ckpt-session") if m["role"] == "system"]
        assert any("rewind: 1 file(s) restored" in m["content"] for m in notes), notes
        events = env.state_db.read_timeline("ckpt-session", event_type="rewind")
        assert len(events) == 1 and events[0]["data"]["restored"] == ["note.txt"], events

        # A dry run changes nothing, so it must not claim a rewind in the transcript either.
        before = len(env.state_db.get_session_messages("ckpt-session"))
        await env.get_tools()["rewind"]["func"](dry_run=True)
        assert len(env.state_db.get_session_messages("ckpt-session")) == before
    finally:
        for closer in ("memory_store", "state_db", "delivery_ledger"):
            if hasattr(env, closer):
                getattr(env, closer).close()
        shutil.rmtree(tmp, ignore_errors=True)
    print("[PASS] loop hook: a real write is snapshotted before it happens and rewind restores it.")


async def main():
    test_store()
    await test_loop_hook()
    print("ALL CHECKPOINT TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(main())
