"""Session file checkpoints: undo what this session did to files.

The loop snapshots a file's content the first time a session writes to it, so what is kept
is the state *before* the session touched it, and `rewind` puts every touched file back —
restoring the ones that existed, deleting the ones the session created. First snapshot wins:
a second write to the same path is not re-snapshotted, which is what makes the rewind go all
the way back rather than one step.

Limits, stated plainly: only paths inside the project root are covered, only the two file
writers are watched (`sandbox_exec` and `process_start` can rewrite anything and are not
tracked), and only files are rewound — the conversation transcript is not, so this is a
file-level undo, not a state-level one.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# The tools that destroy file content. Read-only tools are not worth the copy.
MUTATING_TOOLS = {"write_file", "patch_file"}

MANIFEST = "manifest.json"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class CheckpointStore:
    """`memories/checkpoints/<session>/` holds the manifest and the original file bytes."""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir).resolve()
        self.base_dir = self.root_dir / "memories" / "checkpoints"
        # Set by the loop that owns this store, so a tool can rewind without being told the id.
        self.session_id = "session"

    # ------------------------------------------------------------------ paths

    def _session_dir(self, session_id: str) -> Path:
        return self.base_dir / _UNSAFE.sub("_", session_id or "session")

    def _relative(self, path: Any) -> Optional[str]:
        """The path as a root-relative string, or None when it is not a file we may touch.

        A path outside the project root is refused rather than snapshotted: a checkpoint
        that can write anywhere on the disk is a worse tool than no checkpoint at all.
        """
        if not path or not isinstance(path, (str, os.PathLike)):
            return None
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = self.root_dir / candidate
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.root_dir):
                return None
            if resolved.is_dir():
                return None
        except (OSError, ValueError):
            return None
        return resolved.relative_to(self.root_dir).as_posix()

    def _blob(self, session_id: str, rel: str) -> Path:
        return self._session_dir(session_id) / "blobs" / rel

    # --------------------------------------------------------------- manifest

    def _load(self, session_id: str) -> Dict[str, Any]:
        path = self._session_dir(session_id) / MANIFEST
        if not path.exists():
            return {"session_id": session_id, "created_at": time.time(), "files": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"session_id": session_id, "created_at": time.time(), "files": {}}
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            return {"session_id": session_id, "created_at": time.time(), "files": {}}
        return data

    def _save(self, session_id: str, data: Dict[str, Any]):
        directory = self._session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        # Written through a temp file: a crash mid-write must not leave a half manifest,
        # because a manifest that does not parse is a rewind that silently does nothing.
        tmp = directory / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, directory / MANIFEST)

    # ---------------------------------------------------------------- capture

    def handles(self, tool_name: str) -> bool:
        return tool_name in MUTATING_TOOLS

    def snapshot_for(self, session_id: str, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Snapshot before a write. Called by the loop; a no-op for anything else."""
        if not self.handles(tool_name):
            return None
        return self.snapshot(session_id, (arguments or {}).get("path"))

    def snapshot(self, session_id: str, path: Any) -> Optional[str]:
        """Keep the pre-session content of `path`; returns its relative path, or None if skipped."""
        rel = self._relative(path)
        if rel is None:
            return None
        data = self._load(session_id)
        if rel in data["files"]:
            return rel
        target = self.root_dir / rel
        try:
            raw = target.read_bytes()
        except (OSError, ValueError):
            entry: Dict[str, Any] = {"existed": False, "sha256": None, "size": None}
        else:
            blob = self._blob(session_id, rel)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(raw)
            entry = {"existed": True, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        data["files"][rel] = entry
        self._save(session_id, data)
        return rel

    # ------------------------------------------------------------------ read

    def status(self, session_id: str) -> Dict[str, Any]:
        data = self._load(session_id)
        files = [
            {"path": rel, "existed": bool(entry.get("existed"))}
            for rel, entry in sorted(data["files"].items())
        ]
        return {
            "session_id": session_id,
            "created_at": data.get("created_at"),
            "count": len(files),
            "files": files,
        }

    # ----------------------------------------------------------------- write

    def rewind(self, session_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """Put every file this session touched back; `dry_run` reports without touching."""
        data = self._load(session_id)
        restored: List[str] = []
        deleted: List[str] = []
        skipped: List[str] = []

        for rel, entry in sorted(data["files"].items()):
            target = self.root_dir / rel
            if entry.get("existed"):
                blob = self._blob(session_id, rel)
                if not blob.exists():
                    skipped.append(rel)  # the original bytes are gone; refusing to guess
                    continue
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob.read_bytes())
                restored.append(rel)
            else:
                if target.is_file():
                    if not dry_run:
                        target.unlink()
                    deleted.append(rel)
                elif target.exists():
                    # It is a directory now. Not something this store created, so not its to remove.
                    skipped.append(rel)

        return {
            "session_id": session_id,
            "dry_run": dry_run,
            "restored": restored,
            "deleted": deleted,
            "skipped": skipped,
        }
