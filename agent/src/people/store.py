"""People ledger: a name-keyed JSON ledger of the people the user knows.

A person is {"name", "relation", "notes", "updated_at"}. Names are matched
case-insensitively ("Son" == "son"); the display name keeps the form it was
first recorded in. `relation` is free text ("bạn", "đồng nghiệp", "gia đình",
...). Notes accumulate: every `add_note` / `upsert(..., note=...)` appends.

The ledger lives at memories/people.json and follows the CronStore pattern:
an absent or corrupt file reads as no people, and writes go through a .tmp
sibling swapped in with os.replace so a crash mid-write leaves the old file
whole. `list()` never raises.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("boom.people")

PEOPLE_FILE = "people.json"


def _people_path(root_dir) -> Path:
    """Where the ledger lives, beside the other memories/ state."""
    return Path(root_dir) / "memories" / PEOPLE_FILE


class PeopleStore:
    """Name-keyed people ledger. An absent or corrupt file reads as no people."""

    def __init__(self, people_file: Optional[Path] = None,
                 clock: Callable[[], datetime] = datetime.now):
        self.people_file = Path(people_file) if people_file else (Path.cwd() / "memories" / PEOPLE_FILE)
        self.clock = clock

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _key(name: str) -> str:
        """Case-insensitive identity for a name."""
        return name.strip().lower()

    @staticmethod
    def _checked_name(name: Any) -> str:
        """Stripped name, or ValueError when it is blank."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a person needs a non-empty name")
        return name.strip()

    @staticmethod
    def _checked_note(note: Any) -> str:
        """Stripped note, or ValueError when it is blank."""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("a note needs non-empty text")
        return note.strip()

    def _now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ io

    def list(self) -> List[Dict[str, Any]]:
        """Every person in ledger order. Never raises: an unreadable file is empty."""
        if not self.people_file.exists():
            return []
        try:
            raw = json.loads(self.people_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("people ledger %s unreadable (%s); starting empty",
                           self.people_file, e)
            return []
        if not isinstance(raw, list):
            return []
        return [p for p in raw if isinstance(p, dict) and p.get("name")]

    def _save(self, people: List[Dict[str, Any]]) -> None:
        self.people_file.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the ledger and swap it in: a crash mid-write leaves the old file whole.
        tmp = self.people_file.with_name(self.people_file.name + ".tmp")
        tmp.write_text(json.dumps(people, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.people_file)

    # ------------------------------------------------------------------ api

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """One person's record, matched case-insensitively. None when unknown."""
        key = self._key(self._checked_name(name))
        return next((p for p in self.list() if self._key(p["name"]) == key), None)

    def upsert(self, name: str, relation: Optional[str] = None,
               note: Optional[str] = None) -> Dict[str, Any]:
        """Add a person, or update the one already known (matched case-insensitively).

        A given `relation` replaces the stored one; a given `note` is appended.
        ValueError on a blank name or a blank note.
        """
        display = self._checked_name(name)
        key = self._key(display)
        clean_note = self._checked_note(note) if note is not None else None
        clean_relation = relation.strip() if isinstance(relation, str) and relation.strip() else None

        people = self.list()
        person = next((p for p in people if self._key(p["name"]) == key), None)
        if person is None:
            person = {
                "name": display,
                "relation": clean_relation or "",
                "notes": [clean_note] if clean_note else [],
                "updated_at": self._now(),
            }
            people.append(person)
        else:
            if clean_relation:
                person["relation"] = clean_relation
            if clean_note:
                person.setdefault("notes", []).append(clean_note)
            person["updated_at"] = self._now()
        self._save(people)
        return person

    def add_note(self, name: str, note: str) -> Dict[str, Any]:
        """Append a note to a known person. ValueError when the person is unknown."""
        display = self._checked_name(name)
        clean_note = self._checked_note(note)
        key = self._key(display)
        people = self.list()
        person = next((p for p in people if self._key(p["name"]) == key), None)
        if person is None:
            raise ValueError(f"unknown person {display!r}: add them first with person_add")
        person.setdefault("notes", []).append(clean_note)
        person["updated_at"] = self._now()
        self._save(people)
        return person

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        """People whose name or any note contains `keyword` (case-insensitive).

        A blank keyword matches nothing; never raises.
        """
        if not isinstance(keyword, str) or not keyword.strip():
            return []
        needle = keyword.strip().lower()
        hits = []
        for person in self.list():
            haystack = " ".join([str(person.get("name", ""))] +
                                [str(n) for n in person.get("notes", [])])
            if needle in haystack.lower():
                hits.append(person)
        return hits
