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
from tools.tool_discovery import search_tools, list_toolsets
from tools.web import web_search, web_fetch, web_extract
from tools.fileops import edit_file, append_file, file_info
from tools.shell import run as shell_run, run_bg, job_status, job_kill
from tools import gmail_tool as _gmail_tool
from security.guardrail import SecurityGuardrail
from security.approval import ApprovalGate
from security.ratelimit import UserRateLimiter
from autonomous.codereview import CodeReviewer
from autonomous.depgraph import build_graph, find_cycles, impact_analysis
from autonomous.gitops import status as git_status_fn, diff_summary as git_diff_fn, safe_commit as git_commit_fn, branch_guard as git_guard_fn
from security.secretscan import scan_file as secret_scan_file, scan_dir as secret_scan_dir
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
        # Overnight hardening: per-user rate limiter (token bucket).
        # Gateway/loop calls self.user_limiter.allow_or_raise("channel:user_id") before dangerous tools.
        self.user_limiter = UserRateLimiter()
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
        # Refactor 2026-10-16: split into 7 groups in agent/src/tool_groups.py.
        # Merge order preserves the original's override semantics (~800 lines).
        from tool_groups import (
            _init_filesystem_tools,
            _init_terminal_tools,
            _init_git_tools,
            _init_web_tools,
            _init_code_tools,
            _init_security_tools,
            _init_db_tools,
        )
        for _group_fn in (
            _init_filesystem_tools,
            _init_terminal_tools,
            _init_git_tools,
            _init_web_tools,
            _init_code_tools,
            _init_security_tools,
            _init_db_tools,
        ):
            self._tools.update(_group_fn(self))

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
