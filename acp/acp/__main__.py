"""Run the Boom ACP server:  python -m acp   (from the repo root).

Builds the real Boom agent (BoomCLI -> ReActLoop) and serves ACP requests on
stdin/stdout. Stdout carries protocol lines only; every stray print -- agent
construction logs, MCP registration, model output -- goes to stderr, exactly
like BoomCLI.serve_rpc does for the Go edge.

If the Boom agent cannot be constructed (missing deps, no provider key, ...),
the server still starts but answers session/prompt with an honest JSON-RPC
error instead of crashing or fabricating replies.
"""

import sys
from pathlib import Path

# Repo root on the path: acp/ lives at the root, agent code under agent/src/.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "agent" / "src"))

from .server import AcpServer


def _build_turn_fn():
    """Return an async (session_id, text) -> str wired to the real Boom loop.

    Seam (same one BoomCLI.serve_rpc uses): loop.load_session(...) then
    loop.step(text); the reply is action/observation, never invented.
    Raises on any construction failure so the caller can report it clearly.
    """
    from agent.src.cli import BoomCLI
    from agent.src.config import BoomConfig

    config = BoomConfig.load()
    cli = BoomCLI(config)

    async def turn(session_id, text):
        cli.loop.load_session(session_id, channel="acp")
        res = await cli.loop.step(text)
        return res.get("action") or res.get("observation") or "Done."

    return turn


def main():
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # protocol lines own the real stdout

    turn_fn = None
    try:
        turn_fn = _build_turn_fn()
        print("[acp] Boom agent attached.", file=sys.stderr, flush=True)
    except Exception as exc:  # clear error, no blind crash
        print(f"[acp] Boom agent unavailable "
              f"({type(exc).__name__}: {exc}); serving with honest errors.",
              file=sys.stderr, flush=True)

    AcpServer(turn_fn=turn_fn, out=protocol_out).serve()


if __name__ == "__main__":
    main()
