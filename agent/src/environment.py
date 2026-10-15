import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List

# gateway/ is a peer of agent/ at the repo root, so it is importable only when the root is on the
# path. Evals and tools load this module with agent/src alone, so put the root there once.
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from extensibility.skill_loader import SkillLoader
from extensibility.plugin_registry import PluginRegistry
from extensibility.skills_hub import SkillsHub
from memory.store import MemoryStore
from memory.state_db import StateDB
from memory.curated_memory import CuratedMemory
from memory.reflexion import ReflexionEngine
from memory.checkpoints import CheckpointStore
from tools.bridge import IpcBridgeClient
from tools.file_ops import read_file, write_file, patch_file
from tools.browser_tool import BrowserTool
from tools.process_manager import ProcessManager
from tools.delegation import DelegationManager
from tools.code_execution import CodeExecutionKernel
from tools.session_search import SessionSearchTool
from tools.ast_tool import ASTInspector
from security.guardrail import SecurityGuardrail
from security.approval import ApprovalGate
from loop.context_compressor import ContextCompressor
from loop.prompt_builder import SystemPromptBuilder
from loop.token_accounting import TokenAccountant
from gateway.delivery_ledger import DeliveryLedger

def _schema(properties: Dict[str, Any] = None, required: List[str] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    }

class AgentEnvironment:
    def __init__(self, root_dir: Path = None, db_path: Path = None, ipc_port: int = 51234, workdir: Path = None, yolo: bool = False):
        self.root_dir = Path(root_dir or workdir or Path.cwd())
        self.skills_dir = self.root_dir / "skills"
        self.plugins_dir = self.root_dir / "plugins"
        self.db_path = db_path or (self.root_dir / "state.db")

        self.skill_loader = SkillLoader(self.skills_dir)
        self.skills_hub = SkillsHub(self.skills_dir)
        self.plugin_registry = PluginRegistry(self.plugins_dir)
        self.memory_store = MemoryStore(self.db_path)
        self.state_db = StateDB(self.db_path)
        self.curated_memory = CuratedMemory(self.root_dir / "memories")
        self.delivery_ledger = DeliveryLedger(self.db_path)
        self.session_search = SessionSearchTool(self.state_db)

        self.reflexion = ReflexionEngine(self.memory_store, self.skills_dir)
        self.ipc_bridge = IpcBridgeClient(port=ipc_port)
        self.guardrail = SecurityGuardrail(self.root_dir)
        self.approval_gate = ApprovalGate(yolo=yolo)
        self.compressor = ContextCompressor(self.root_dir / "scratch")
        self.delegation = DelegationManager()
        self.prompt_builder = SystemPromptBuilder(self.root_dir)
        self.accountant = TokenAccountant()
        self.token_accountant = self.accountant
        self.process_manager = ProcessManager(self.root_dir / "processes.json")
        self.browser = BrowserTool()
        # Pre-write file snapshots; the loop calls this, rewind undoes a session's edits.
        self.checkpoints = CheckpointStore(self.root_dir)

        self._tools: Dict[str, Dict[str, Any]] = {}
        # Live MCP server clients, keyed by server name; empty unless mcp.json declares one.
        self.mcp_clients: Dict[str, Any] = {}
        self._init_tools()

    def _init_tools(self):
        # 1. File ops
        self._tools["read_file"] = {
            "name": "read_file",
            "func": read_file,
            "description": "Read a file from disk. Returns numbered lines. Use offset/limit to page through large files.",
            "schema": _schema(
                {
                    "path": {"type": "string", "description": "Path to the file to read."},
                    "offset": {"type": "integer", "description": "1-based first line to return. Default 1."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return. Default 2000."},
                },
                ["path"],
            ),
        }
        self._tools["write_file"] = {
            "name": "write_file",
            "func": write_file,
            "description": "Write complete content to a file, replacing anything already there.",
            "schema": _schema(
                {
                    "path": {"type": "string", "description": "Path to the file to write."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                ["path", "content"],
            ),
        }
        self._tools["patch_file"] = {
            "name": "patch_file",
            "func": patch_file,
            "description": "Replace an exact substring inside a file. Prefer this over write_file for small edits.",
            "schema": _schema(
                {
                    "path": {"type": "string", "description": "Path to the file to edit."},
                    "old_string": {"type": "string", "description": "Exact text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
                },
                ["path", "old_string", "new_string"],
            ),
        }

        # 2. Skill ops
        self._tools["list_skills"] = {
            "name": "list_skills",
            "func": lambda: self.skill_loader.list_skills(),
            "description": "List available Agent Skills with their names and descriptions.",
            "schema": _schema(),
        }
        self._tools["view_skill"] = {
            "name": "view_skill",
            "func": lambda name, file_path=None: self.skill_loader.view_skill(name, file_path),
            "description": "Load the full documentation of a skill by name, optionally a specific file inside it.",
            "schema": _schema(
                {
                    "name": {"type": "string", "description": "Skill name."},
                    "file_path": {"type": "string", "description": "Optional file inside the skill directory."},
                },
                ["name"],
            ),
        }
        self._tools["install_skill"] = {
            "name": "install_skill",
            "func": lambda skill_name: self.skills_hub.install_skill(skill_name),
            "description": "Install a skill by name from the skills hub.",
            "schema": _schema(
                {"skill_name": {"type": "string", "description": "Name of the skill to install."}},
                ["skill_name"],
            ),
        }

        # 3. Dynamic plugins
        plugin_ctx = self.plugin_registry.scan_and_load()
        for name, tool_meta in plugin_ctx.tools.items():
            self._tools[name] = tool_meta

        # 4. IPC Sandbox execution
        async def _sandbox_exec(cmd: str, args: List[str] = None):
            return await self.ipc_bridge.execute(cmd, args)

        self._tools["sandbox_exec"] = {
            "name": "sandbox_exec",
            "func": _sandbox_exec,
            "description": "Run a command in the Rust sandbox core over IPC and return its stdout and exit code.",
            "schema": _schema(
                {
                    "cmd": {"type": "string", "description": "Executable to run."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Argument list."},
                },
                ["cmd"],
            ),
        }

        # 5. Web and browser tools are deliberately NOT registered:
        #    web_search/web_extract return synthetic example.com rows (tools/web_tools.py:6-26);
        #    BrowserTool keeps a string, it makes no HTTP request (tools/browser_tool.py:10-34).
        #    Register them again only once each does the work it advertises.

        # 5b. Subagents. Each subtask runs a real child ReAct turn, ordered by the DAG and
        #     shaped by its role; this needs a loop to delegate from, so an unbound manager
        #     reports an error rather than a fake result.
        async def _delegate(
            tasks: List[Dict[str, Any]] = None,
            fail_fast: bool = False,
            workflow: str = "",
            goal: str = "",
        ):
            return await self.delegation.delegate_task(
                tasks, fail_fast=fail_fast, workflow=workflow, goal=goal
            )

        self._tools["delegate_task"] = {
            "name": "delegate_task",
            "func": _delegate,
            "description": (
                "Run subtasks as separate subagents, each with its own transcript, and return "
                "every subagent's final answer. Subtasks without depends_on run in parallel; a "
                "subtask that names depends_on receives those subagents' results and waits for "
                "them. Roles: "
                + ", ".join(self.delegation.swarm.role_names())
                + "."
            ),
            "schema": _schema(
                {
                    "tasks": {
                        "type": "array",
                        "description": "Subtasks, run in dependency order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_id": {
                                    "type": "string",
                                    "description": "Id other subtasks refer to; defaults to subagent-<n>.",
                                },
                                "goal": {"type": "string", "description": "What the subagent must accomplish."},
                                "role": {
                                    "type": "string",
                                    "enum": self.delegation.swarm.role_names(),
                                    "description": "Subagent role: sets its instructions and its toolset.",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "task_ids this subtask waits for; their results are passed in.",
                                },
                                "context": {"type": "string", "description": "Background the subagent needs."},
                                "contract": {
                                    "type": "string",
                                    "description": "What this subtask owes back; overrides the role's contract.",
                                },
                                "verify": {
                                    "type": "boolean",
                                    "description": (
                                        "Have adversarial critics try to falsify this subtask's result and "
                                        "make it revise. Costs extra subagent turns. Default false."
                                    ),
                                },
                            },
                            "required": ["goal"],
                        },
                    },
                    "fail_fast": {
                        "type": "boolean",
                        "description": "Stop starting subtasks once one fails. Default false.",
                    },
                    "workflow": {
                        "type": "string",
                        "description": (
                            "Replay a kept workflow by name instead of sending tasks; pass the goal too."
                        ),
                    },
                    "goal": {"type": "string", "description": "The goal, when replaying a workflow."},
                },
                [],
            ),
        }

        # 5c. Workflow self-improvement (MASS's optimizer leg). Runs candidate divisions of
        #     labour, has critics score them, and keeps the best in memories/workflows.json.
        async def _optimize(goal: str, candidates: List[Dict[str, Any]] = None, max_candidates: int = 3):
            return await self.delegation.optimize_workflow(goal, candidates, max_candidates)

        self._tools["optimize_workflow"] = {
            "name": "optimize_workflow",
            "func": _optimize,
            "description": (
                "Search for a better multi-agent workflow for a recurring kind of goal. Each "
                "candidate is run for real and scored by adversarial critics; the best one beats "
                "the standing champion and is kept in memories/workflows.json for delegate_task "
                "workflow=<name> to replay."
            ),
            "schema": _schema(
                {
                    "goal": {"type": "string", "description": "The kind of goal the workflow should solve."},
                    "candidates": {
                        "type": "array",
                        "description": "Workflows to try; omit to have the optimizer propose them.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Short workflow name."},
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "role": {
                                                "type": "string",
                                                "enum": self.delegation.swarm.role_names(),
                                            },
                                            "goal": {"type": "string"},
                                            "contract": {"type": "string"},
                                        },
                                        "required": ["role"],
                                    },
                                },
                            },
                            "required": ["name", "steps"],
                        },
                    },
                    "max_candidates": {"type": "integer", "description": "Cap on candidates tried. Default 3."},
                },
                ["goal"],
            ),
        }

        # 5d. Rewind. The loop snapshots a file the first time this session writes it, so this
        #     undoes the session's edits — it is not a step-back one write at a time.
        async def _rewind(dry_run: bool = False):
            result = self.checkpoints.rewind(self.checkpoints.session_id, dry_run=dry_run)
            if not dry_run and (result["restored"] or result["deleted"]):
                # The transcript still reads as if those edits happened. Put the rewind in it, so
                # the next turn sees what the files actually are now.
                self.state_db.append_message(
                    self.checkpoints.session_id, "system",
                    f"rewind: {len(result['restored'])} file(s) restored, "
                    f"{len(result['deleted'])} file(s) this session created were removed",
                )
                self.state_db.record_timeline_event(self.checkpoints.session_id, "rewind", result)
            return result

        self._tools["rewind"] = {
            "name": "rewind",
            "func": _rewind,
            "description": (
                "Undo the file edits this session made: restore every file it changed to what "
                "it was before the session, and delete the files it created. Use dry_run to "
                "see the list first. Only files inside the project root are covered, and only "
                "write_file/patch_file edits — a command run through a shell is not. A real "
                "rewind is recorded in the transcript, so later turns are not misled."
            ),
            "schema": _schema(
                {
                    "dry_run": {
                        "type": "boolean",
                        "description": "List what would change without changing anything. Default false.",
                    },
                },
                [],
            ),
        }

        # 5e. Optional tool modules (web, cron, browser, artifacts, goals,
        #     people, digest, research, tasks, verify). Each module builds its
        #     own tool dicts, so this is the only place that has to know about
        #     them. Imported lazily and guarded: a subsystem that cannot load
        #     must not take the whole environment down. people.tools, tasks.tools
        #     and verify.tools take root_dir instead of the env; everything
        #     else takes the env.
        for module_name, use_root_dir in (
            ("tools.web_tools", False),
            ("tools.cronjob_tool", False),
            ("tools.browser_tool", False),
            ("artifacts.tools", False),
            ("goals.tools", False),
            ("digest.tools", False),
            ("tools.research_tool", False),
            ("tools.scraper", False),
            ("people.tools", True),
            ("tasks.tools", True),
            ("verify.tools", True),
        ):
            try:
                factory = getattr(import_module(module_name), "build_tools", None)
                if factory is not None:
                    self._tools.update(
                        factory(self.root_dir if use_root_dir else self))
            except Exception as e:
                print(f"[tools] {module_name} unavailable: {e}")

        # 5g. Service connectors (Gmail, Calendar, Drive, generic HTTP API).
        #     Merged through the connector registry; a missing credential only
        #     marks that connector disconnected, it never breaks the env.
        #     connector_status lets the agent report which connectors are live.
        try:
            from connectors.registry import load_default_registry
            registry = load_default_registry()
            self._tools.update(registry.build_tools())
            self._tools.update(registry.build_status_tool())
        except Exception as e:
            print(f"[tools] connectors unavailable: {e}")

        # 6. Process management
        self._tools["process_list"] = {
            "name": "process_list",
            "func": self.process_manager.list_processes,
            "description": "List background processes tracked in the process ledger.",
            "schema": _schema(),
        }
        self._tools["process_poll"] = {
            "name": "process_poll",
            "func": self.process_manager.poll_process,
            "description": "Poll one background process for new stdout/stderr and its status.",
            "schema": _schema(
                {"session_id": {"type": "string", "description": "Process session id from process_list."}},
                ["session_id"],
            ),
        }
        self._tools["process_start"] = {
            "name": "process_start",
            "func": self.process_manager.start_process,
            "description": (
                "Start a long-running background process and track it. Returns a session_id "
                "to pass to process_poll; stdout/stderr are buffered until polled."
            ),
            "schema": _schema(
                {
                    "session_id": {"type": "string", "description": "Id to track this process under."},
                    "cmd": {"type": "array", "items": {"type": "string"}, "description": "Command and arguments."},
                    "cwd": {"type": "string", "description": "Working directory. Default: the agent's."},
                },
                ["session_id", "cmd"],
            ),
        }
        self._tools["process_kill"] = {
            "name": "process_kill",
            "func": self.process_manager.kill_process,
            "description": "Terminate a tracked background process and return any output not yet polled.",
            "schema": _schema(
                {"session_id": {"type": "string", "description": "Process session id."}},
                ["session_id"],
            ),
        }

        # 8. Curated Memory & Session Search
        self._tools["memory"] = {
            "name": "memory",
            "func": lambda target, operations: self.curated_memory.execute_operations(target, operations),
            "description": "Read-modify-write the durable memory notes shown in the system prompt.",
            "schema": _schema(
                {
                    "target": {"type": "string", "enum": ["memory", "user"], "description": "Which store to edit."},
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                                "content": {"type": "string", "description": "New entry text."},
                                "old_text": {"type": "string", "description": "Substring identifying the entry to change."},
                            },
                            "required": ["action"],
                        },
                        "description": "Batch of operations, applied atomically.",
                    },
                },
                ["target", "operations"],
            ),
        }
        self._tools["session_search"] = {
            "name": "session_search",
            "func": lambda **kwargs: self.session_search.search(**kwargs),
            "description": "Full-text search across past conversation sessions stored in SQLite, or list a session's recorded events with events=true.",
            "schema": _schema(
                {
                    "query": {"type": "string", "description": "Full-text query."},
                    "session_id": {"type": "string", "description": "Restrict to one session."},
                    "around_message_id": {"type": "integer", "description": "Scroll around this message id."},
                    "events": {"type": "boolean", "description": "Return the session's event log instead of its messages."},
                    "limit": {"type": "integer", "description": "Maximum messages to return. Default 10."},
                },
            ),
        }

        # 9. AST code inspection
        self._tools["ast_inspect"] = {
            "name": "ast_inspect",
            "func": lambda file_path: ASTInspector.inspect_file(file_path),
            "description": "Parse a Python file and list its imports, classes and functions without executing it.",
            "schema": _schema({"file_path": {"type": "string", "description": "Path to a Python file."}}, ["file_path"]),
        }

        # 9b. MCP servers declared in mcp.json. Registered before the code kernel so the
        #     model can also reach them from execute_code. A broken server is reported,
        #     not fatal: the rest of the harness must still come up.
        try:
            from mcp.registry import register_mcp_tools

            register_mcp_tools(self, self.root_dir)
        except Exception as e:
            print(f"[mcp] discovery failed, continuing without MCP tools: {e}")

        # 10. Programmatic Code Execution Kernel
        kernel = CodeExecutionKernel(self._tools)
        self._tools["execute_code"] = {
            "name": "execute_code",
            "func": lambda code: kernel.execute(code),
            "description": "Run Python code in-process with the tool registry exposed. Returns captured stdout.",
            "schema": _schema({"code": {"type": "string", "description": "Python source to execute."}}, ["code"]),
        }

    def get_tools(self) -> Dict[str, Dict[str, Any]]:
        return self._tools

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        """OpenAI-compatible function definitions for every registered tool."""
        schemas = []
        for name, meta in self._tools.items():
            declared = meta.get("schema") or {}
            # Plugins may declare either a bare JSON Schema or a {"parameters": ...} wrapper.
            parameters = declared.get("parameters", declared) if isinstance(declared, dict) else {}
            schemas.append({
                "name": name,
                "description": meta.get("description") or f"Tool '{name}'.",
                "parameters": parameters or _schema(),
            })
        return schemas
