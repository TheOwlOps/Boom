# -*- coding: utf-8 -*-
"""Boom's deep research package (DeepSearch + DeepThink).

Core modules:
  - ``research.deepsearch``: the ``DeepSearch`` class — multi-hop search,
    entity extraction, follow-up question generation, cross-referencing
    between sources.
  - ``research.deepthink``: the ``DeepThink`` class — analyzes research
    results into a summary, multiple perspectives, and information gaps.
  - ``research.crossref``: the ``cross_reference`` function — compares
    sources, computing consensus/conflict via word overlap (no embeddings yet).

Imports are wrapped in try/except: when a core module isn't on the machine
yet (e.g. only partially deployed), the package still imports and sets that
symbol to ``None``. Callers should check ``is None`` before using it.
"""

__all__ = ["DeepSearch", "DeepThink", "cross_reference"]

DeepSearch = None  # type: ignore[assignment]
DeepThink = None  # type: ignore[assignment]
cross_reference = None  # type: ignore[assignment]

try:  # No crash when a core module isn't deployed yet
    from .deepsearch import DeepSearch  # noqa: F401,E402
except ImportError:
    pass

try:
    from .deepthink import DeepThink  # noqa: F401,E402
except ImportError:
    pass

try:
    from .crossref import cross_reference  # noqa: F401,E402
except ImportError:
    pass


def available() -> dict:
    """Returns each core component's readiness."""
    return {
        "DeepSearch": DeepSearch is not None,
        "DeepThink": DeepThink is not None,
        "cross_reference": cross_reference is not None,
    }
