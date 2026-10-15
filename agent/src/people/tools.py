"""person_* tools: the agent's read/write interface to the people ledger."""

from typing import Any, Dict, List, Optional

from people.store import PeopleStore, _people_path


def _schema(properties: Dict[str, Any] = None, required: List[str] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def build_tools(root_dir) -> Dict[str, Dict[str, Any]]:
    """Tool dicts for the environment: person_add, person_note, person_get,
    person_list, person_search.

    The ledger lives at <root_dir>/memories/people.json. Tool failures are
    reported as {"error": ...}, never raised.
    """
    store = PeopleStore(_people_path(root_dir)) if root_dir is not None else None

    def _need_store() -> Optional[Dict[str, Any]]:
        if store is None:
            return {"error": "environment has no root_dir: nowhere to keep people"}
        return None

    def _add(name: str, relation: Optional[str] = None,
             note: Optional[str] = None) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            return {"person": store.upsert(name, relation=relation, note=note)}
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the people ledger: {e}"}

    def _note(name: str, note: str) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            return {"person": store.add_note(name, note)}
        except ValueError as e:
            return {"error": str(e)}
        except OSError as e:
            return {"error": f"could not write the people ledger: {e}"}

    def _get(name: str) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        try:
            person = store.get(name)
        except ValueError as e:
            return {"error": str(e)}
        if person is None:
            return {"error": f"unknown person {name.strip()!r}"}
        return {"person": person}

    def _list() -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        people = store.list()
        return {"people": people, "total": len(people)}

    def _search(keyword: str) -> Dict[str, Any]:
        err = _need_store()
        if err:
            return err
        hits = store.search(keyword)
        return {"people": hits, "total": len(hits)}

    return {
        "person_add": {
            "name": "person_add",
            "func": _add,
            "description": (
                "Remember a person: their name (required), relation to the user "
                "(free text, e.g. 'bạn', 'đồng nghiệp', 'gia đình'), and an optional "
                "first note. When the name is already known (case-insensitive) the "
                "entry is updated instead: a new relation replaces the old one and "
                "a note is appended."
            ),
            "schema": _schema(
                {
                    "name": {"type": "string", "description": "Person's name."},
                    "relation": {"type": "string",
                                 "description": "Relation to the user, free text."},
                    "note": {"type": "string",
                             "description": "Optional first note about them."},
                },
                ["name"],
            ),
        },
        "person_note": {
            "name": "person_note",
            "func": _note,
            "description": (
                "Append a note to a person already in the ledger (matched "
                "case-insensitively). Returns an error when the person is unknown; "
                "add them first with person_add."
            ),
            "schema": _schema(
                {
                    "name": {"type": "string", "description": "Person's name."},
                    "note": {"type": "string", "description": "Note text to append."},
                },
                ["name", "note"],
            ),
        },
        "person_get": {
            "name": "person_get",
            "func": _get,
            "description": "Get one person's record by name (case-insensitive).",
            "schema": _schema(
                {"name": {"type": "string", "description": "Person's name."}},
                ["name"],
            ),
        },
        "person_list": {
            "name": "person_list",
            "func": _list,
            "description": "List everyone in the people ledger, in the order added.",
            "schema": _schema(),
        },
        "person_search": {
            "name": "person_search",
            "func": _search,
            "description": (
                "Search people by keyword across names and notes (case-insensitive). "
                "Use before asking the user about someone they may have mentioned."
            ),
            "schema": _schema(
                {"keyword": {"type": "string", "description": "Keyword to search for."}},
                ["keyword"],
            ),
        },
    }

