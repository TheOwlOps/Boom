"""People ledger: who the user knows, and what is known about them.

This is the relationship side of memory. The generic key-value memory
(`memory.store.MemoryStore`) keeps facts and lessons; this package keeps *people*:
a name-keyed ledger of relations and notes at `memories/people.json`, plus the
`person_*` tools that let the agent read and update it.

Import root is `agent/src`, so: `from people.store import PeopleStore`.
"""

from people.store import PEOPLE_FILE, PeopleStore
from people.tools import build_tools

__all__ = ["PEOPLE_FILE", "PeopleStore", "build_tools"]
