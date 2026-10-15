"""Interactive Terminal CLI and Daemon Runner for Boom Agent Harness."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add root directory to path
root_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "agent" / "src"))

from agent.src.config import BoomConfig
from agent.src.environment import AgentEnvironment
from agent.src.loop.react import ReActLoop
from agent.src.providers.llm import LLMProvider
from agent.src.security.system_one_gate import SystemOneGate
from gateway.daemon import GatewayDaemon

BANNER = r"""
  ██████╗  ██████╗  ██████╗ ███╗   ███╗
  ██╔══██╗██╔═══██╗██╔═══██╗████╗ ████║
  ██████╔╝██║   ██║██║   ██║██╔████╔██║
  ██╔══██╗██║   ██║██║   ██║██║╚██╔╝██║
  ██████╔╝╚██████╔╝╚██████╔╝██║ ╚═╝ ██║
  ╚═════╝  ╚═════╝  ╚═════╝ ╚═╝     ╚═╝
  Autonomous Agent Harness (Parity Edition)
"""

class BoomCLI:
    def __init__(self, config: BoomConfig):
        self.config = config
        self.env = AgentEnvironment(workdir=Path(config.workdir), yolo=config.yolo)
        self.llm = LLMProvider(
            base_url=config.primary_provider.base_url,
            api_key=config.primary_provider.api_key,
            model=config.primary_provider.model,
            timeout=config.primary_provider.timeout
        )
        self.session_id = f"session_{int(time.time())}"
        self.loop = ReActLoop(
            self.env,
            self.llm,
            accountant=self.env.token_accountant,
            session_id=self.session_id,
        )
        # cron_run needs a loop to run a scheduled prompt through; without it the tool refuses.
        self.env.loop = self.loop
        self._attach_evaluator()

    def _attach_evaluator(self):
        """Run the adversarial critics on a second provider when one is configured.

        MASS's own caveat is that solver, evaluator and optimizer share one model and so
        share its blind spots. A different model is the only defence available here, so it
        is opt-in: BOOM_EVALUATOR_BASE_URL / _API_KEY / _MODEL.
        """
        base_url = os.environ.get("BOOM_EVALUATOR_BASE_URL")
        model = os.environ.get("BOOM_EVALUATOR_MODEL")
        if not base_url or not model:
            return
        evaluator = LLMProvider(
            base_url=base_url,
            api_key=os.environ.get("BOOM_EVALUATOR_API_KEY", ""),
            model=model,
            timeout=self.config.primary_provider.timeout,
        )
        self.env.delegation.evaluator_llm = evaluator
        self.env.delegation.optimizer_llm = evaluator
        print(f"Evaluator model: {model} (separate from {self.config.primary_provider.model})")
    def print_banner(self):
        print(BANNER)
        print(f"Model: {self.config.primary_provider.model} | Mode: {'YOLO (Auto-approve)' if self.config.yolo else 'Interactive Approval'}")
        print(f"Session: {self.session_id} | Working Directory: {Path(self.config.workdir).resolve()}")
        print("Type your message or /help for commands. Press Ctrl+C or type /exit to quit.\n" + "-" * 70)

    async def handle_slash_command(self, cmd_text: str) -> bool:
        """Handle slash command. Returns True if handled, False if regular turn."""
        parts = cmd_text.strip().split()
        cmd = parts[0].lower()

        if cmd in ("/exit", "/quit"):
            print("\nShutting down Boom CLI. Goodbye!")
            sys.exit(0)
        elif cmd == "/new":
            self.session_id = f"session_{int(time.time())}"
            self.loop.reset()
            self.loop.session_id = self.session_id
            print(f"[OK] Started new session: {self.session_id}")
            return True
        elif cmd == "/help":
            print("\nAvailable Slash Commands:")
            print("  /new              - Start a fresh session")
            print("  /sessions         - List recent sessions from SQLite WAL")
            print("  /model <name>     - Switch active LLM model")
            print("  /skills           - List all available Agent Skills")
            print("  /tools            - List all registered tools and toolsets")
            print("  /status           - Display token metrics, cost & active background tasks")
            print("  /exit             - Quit the CLI\n")
            return True
        elif cmd == "/skills":
            skills = self.env.skill_loader.list_skills()
            print(f"\nAvailable Skills ({len(skills)}):")
            for s in skills:
                print(f"  - {s['name']}: {s['description']}")
            print()
            return True
        elif cmd == "/tools":
            tools = self.env.get_tools()
            print(f"\nRegistered Tools ({len(tools)}):")
            for name, meta in tools.items():
                print(f"  - {name}: {meta.get('description', '')}")
            print()
            return True
        elif cmd == "/status":
            usage = self.env.token_accountant.get_usage(self.session_id)
            cost = self.env.token_accountant.calculate_cost(self.session_id, self.llm.model)
            print(f"\nStatus Report:")
            print(f"  Session ID: {self.session_id}")
            print(f"  History turns: {len(self.loop.history)}")
            print(
                f"  Tokens: in {usage.input_tokens} / out {usage.output_tokens}"
                f" / reasoning {usage.reasoning_tokens} / cache read {usage.cache_read_tokens}"
            )
            print(f"  Session Cost: ${cost:.4f}")
            print(f"  Model: {self.llm.model}")
            return True
        elif cmd == "/model" and len(parts) > 1:
            self.llm.model = parts[1]
            print(f"[OK] Model switched to: {self.llm.model}")
            return True

        return False

    async def start_repl(self):
        self.print_banner()
        while True:
            try:
                user_input = input("\n[user] > ").strip()
                if not user_input:
                    continue

                # Handle slash commands
                if user_input.startswith("/"):
                    if await self.handle_slash_command(user_input):
                        continue

                # Owner-Only Guard Check
                route, meta = SystemOneGate.evaluate(user_input, user_identity="Ryan")
                if route == "owner_guard_blocked":
                    print(f"\n[agent] {meta['response']}")
                    continue
                elif route == "blocked":
                    print(f"\n[security] {meta['reason']}")
                    continue

                # Run turn
                t0 = time.time()
                print("\n[thinking] analyzing request...")
                try:
                    result = await self.loop.step(user_input)
                except Exception as e:
                    print(f"\n[error] {type(e).__name__}: {e}")
                    continue
                dt = time.time() - t0

                print(f"\n[agent] {result.get('action', 'Response:')}")
                if "observation" in result:
                    print(f"\n[output]\n{result['observation']}")
                if result.get("steps"):
                    print(f"\n[tools: {result['steps']} call(s)]")

                print(f"\n[latency: {dt:.2f}s]")

            except (KeyboardInterrupt, EOFError):
                print("\n\nSession terminated by user.")
                break

    async def _serve_cron(self, deliver=None):
        """Run scheduled prompts for as long as the daemon lives.

        Only the daemon hosts this: the interactive REPL blocks on input, so a scheduler
        there would never fire. A failing job is logged by the runner, not fatal here.
        `deliver`, when given, is an async (channel, target, text) -> bool hook that pushes
        a successful job's result to its deliver_to target (see tools.cronjob_tool).
        """
        from tools.cronjob_tool import JOBS_FILE, CronRunner, CronStore

        try:
            store = CronStore(self.env.root_dir / "memories" / JOBS_FILE)
            jobs = store.list()
            if jobs:
                print(f"Cron: {len(jobs)} scheduled job(s) from memories/{JOBS_FILE}")
            await CronRunner(store, get_loop=lambda: self.loop, deliver=deliver).serve()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[cron] scheduler stopped: {type(e).__name__}: {e}")

    async def run_daemon(self):
        """Run as autonomous multi-channel gateway daemon."""
        print(BANNER)
        print("Starting Boom Autonomous Gateway Daemon...")

        async def _gateway_runner(text: str, channel: str, sender: str):
            res = await self.loop.step(text)
            return res.get("action") or res.get("observation") or "Done."

        daemon = GatewayDaemon(
            runner_fn=_gateway_runner,
            delivery_ledger=self.env.delivery_ledger,
            root_dir=self.env.root_dir,  # the turn queue is persisted under memories/
        )
        print(f"Telegram Listener: {'ENABLED' if daemon.tg_token else 'DISABLED (No token)'}")
        print(f"Discord Listener: {'ENABLED' if daemon.dc_token else 'DISABLED (No token)'}")
        print(
            "Ingress Webhooks: http://"
            f"{os.getenv('BOOM_INGRESS_HOST', '127.0.0.1')}:{os.getenv('BOOM_INGRESS_PORT', '8788')}"
            "/webhook/<channel>"
        )
        print("Ready and listening for incoming messages.")

        async def _cron_deliver(channel: str, target: str, text: str) -> bool:
            """Push a cron job's result to its deliver_to target via the channel adapters."""
            try:
                return await daemon.send_to(channel, target, text)
            except Exception as e:
                print(f"[cron] delivery to {channel}:{target} failed: "
                      f"{type(e).__name__}: {e}", flush=True)
                return False

        cron_task = asyncio.create_task(self._serve_cron(deliver=_cron_deliver))
        try:
            await daemon.start()
        finally:
            cron_task.cancel()

    # ---------------------------------------------------------------- rpc worker

    def run_rpc_worker(self, out):
        """Serve turns for the Go edge; `out` is where the protocol lines go.

        The caller has already pointed sys.stdout at stderr: tools print freely (progress,
        checkpoints, browser logs), and one stray print on stdout would land between two protocol
        lines and break the reader on the other side.
        """
        asyncio.run(self.serve_rpc(out))

    async def serve_rpc(self, out):
        """One JSON message per line in, one reply per line out, one turn at a time.

        The framing is the one `proto/rpc.json` and `crates/core/src/ipc.rs` already define:
        `{"id", "method", "params"}` in, `{"id", "result"}` or `{"id", "error"}` out. A single
        turn runs at a time on purpose: the live context is instance state, so two turns in one
        process would interleave into one transcript. The edge starts as many of these processes
        as it wants turns at once.
        """
        event_loop = asyncio.get_running_loop()
        while True:
            line = await event_loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break  # the edge closed the pipe
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError as e:
                self._reply(out, 0, error=f"Invalid JSON: {e}")
                continue
            if not isinstance(request, dict):
                self._reply(out, 0, error="request must be a JSON object")
                continue
            rid = request.get("id") or 0
            method = str(request.get("method") or "")
            params = request.get("params") or {}
            if method == "ping":
                self._reply(out, rid, result={
                    "status": "pong", "model": getattr(self.llm, "model", "default")})
            elif method == "turn":
                await self._rpc_turn(out, rid, params)
            else:
                self._reply(out, rid, error=f"Unknown method: {method}")

    async def _rpc_turn(self, out, rid, params):
        text = str(params.get("text") or "")
        if not text:
            self._reply(out, rid, error="turn needs a text")
            return
        self.loop.load_session(str(params.get("session_id") or self.session_id),
                               channel=str(params.get("channel") or ""))
        try:
            res = await self.loop.step(text)
        except Exception as e:
            # The edge logs this and answers the channel; the traceback stays on stderr, never
            # inside a protocol line.
            print(f"[rpc] turn failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            self._reply(out, rid, error=f"{type(e).__name__}: {e}")
            return
        self._reply(out, rid, result={
            "action": res.get("action") or res.get("observation") or "Done.",
            "success": bool(res.get("success")),
            "steps": int(res.get("steps") or 0),
            "error": res.get("error"),
        })

    @staticmethod
    def _reply(out, rid, result=None, error=None):
        message = {"id": rid}
        if error is not None:
            message["error"] = str(error)
        else:
            message["result"] = result if result is not None else {}
        print(json.dumps(message), file=out, flush=True)

def main():
    # The banner and arbitrary model output are not cp1252-safe on a default Windows console.
    # The protocol lines carry whatever a platform sent, which is not codepage-safe either.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Boom Agent Harness")
    parser.add_argument("--yolo", "-y", action="store_true", help="Auto-approve mutating tool actions without prompting")
    parser.add_argument("--model", "-m", type=str, help="LLM model name")
    parser.add_argument("--gateway", "-g", action="store_true", help="Run as multi-channel gateway daemon")
    parser.add_argument("--serve-rpc", action="store_true",
                        help="Serve turns for the Go edge: line JSON-RPC on stdin/stdout")
    args = parser.parse_args()

    protocol_out = sys.stdout
    if args.serve_rpc:
        # Setup prints too (MCP registration, provider selection), so the split happens before
        # anything is constructed: stderr keeps the log, stdout carries protocol lines only.
        sys.stdout = sys.stderr

    config = BoomConfig.load()
    if args.yolo:
        config.yolo = True
    if args.model:
        config.primary_provider.model = args.model

    cli = BoomCLI(config)

    # Retention is opt-in: it deletes history, so nothing is dropped until the operator asks
    # for it with BOOM_STATE_RETENTION_DAYS. The daemon is restarted far more often than a REPL,
    # but doing it here covers both and costs one call.
    retention = float(os.getenv("BOOM_STATE_RETENTION_DAYS", "0") or 0)
    if retention > 0:
        print(f"State: pruned history older than {retention:g} days: "
              f"{cli.env.state_db.prune(retention)}")

    if args.serve_rpc:
        cli.run_rpc_worker(protocol_out)
    elif args.gateway:
        asyncio.run(cli.run_daemon())
    else:
        asyncio.run(cli.start_repl())

if __name__ == "__main__":
    main()
