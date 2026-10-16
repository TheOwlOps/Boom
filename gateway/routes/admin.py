"""The "admin" route group — internal introspection (skills, DAG).

Origin: ``gateway/api.py`` (the ``do_GET`` branch for ``/api/skills`` and
``/api/dag/status``).

Note: the gateway currently has NO ``/admin/*`` or ``/config`` namespace
on the HTTP API. The two existing introspection routes are grouped into
the admin module because they serve agent administration/monitoring, not
the chat/tools/channels flows.

Backward-compat: keep ``/api/skills`` and ``/api/dag/status`` (GET).
"""

# Each entry: (HTTP method, path pattern, handler name in api.py).
ROUTES = [
    # Scan the repo's real skills/ directory: {"skills":[{name,description}],
    # "count": n}. Descriptions come from the first line of SKILL.md.
    ("GET", "/api/skills", "handle_skills"),
    # BoomAPI.dag_status(): {"nodes": [...]} — today the Brain does not
    # persist last-run, so [] is the honest answer; if a future Brain
    # exposes last_result, surface it per the contract.
    ("GET", "/api/dag/status", "handle_dag_status"),
]


def register(router):
    """Attach every route of this module to the router (duck-typed)."""
    for method, path, handler in ROUTES:
        router.add(method, path, handler, __name__.rsplit(".", 1)[-1])
