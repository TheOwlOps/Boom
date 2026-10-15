"""Runtime tool discovery for the Boom agent.

The model no longer needs every tool schema stuffed into its prompt. Instead it
calls ``search_tools(keyword)`` to find the right tool at runtime, reads the
returned schema, then calls the tool by name.

Search is over tool name, description, and toolset. Matching is
case-insensitive substring on each word of the keyword query.
"""

from typing import Any, Dict, List


def _get_registry():
    # Lazy import: this module is imported by environment.py during tool
    # registration, and tools.registry imports environment lazily too.
    from tools.registry import registry
    return registry


def search_tools(keyword: str, limit: int = 10) -> Dict[str, Any]:
    """Find tools by keyword. Returns name, description, toolset, and schema."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return {"error": "keyword must be a non-empty string."}
    limit = max(1, min(int(limit or 10), 25))

    registry = _get_registry()
    words = keyword.split()
    hits: List[Dict[str, Any]] = []

    for name in registry.get_all_tool_names():
        entry = registry.get_tool(name)
        if entry is None:
            continue
        haystack = f"{entry.name} {entry.description} {entry.toolset}".lower()
        score = sum(1 for w in words if w in haystack)
        if score == 0:
            continue
        # Exact name match outranks description match.
        if keyword == entry.name.lower():
            score += 10
        elif entry.name.lower().startswith(keyword):
            score += 5
        hits.append({
            "name": entry.name,
            "description": entry.description,
            "toolset": entry.toolset,
            "schema": entry.schema or {},
            "_score": score,
        })

    hits.sort(key=lambda h: (-h["_score"], h["name"]))
    for h in hits:
        del h["_score"]
    return {
        "keyword": keyword,
        "count": len(hits),
        "tools": hits[:limit],
    }


def list_toolsets() -> Dict[str, Any]:
    """List all registered toolsets and the tool names in each."""
    registry = _get_registry()
    return {
        toolset: registry.get_tools_in_toolset(toolset)
        for toolset in registry.get_registered_toolset_names()
    }
