"""Tool groups for AgentEnvironment — splits the giant `_init_tools` function.

This module splits `AgentEnvironment._init_tools` from
`agent/src/environment.py` (commit bf171b9) into 7 smaller functions by
functional group. Each takes an `env` (an AgentEnvironment instance) and returns
a `dict {tool_name: tool_spec}` — specs preserve 100% of the original
registration logic: same tool names, same schemas, same handlers.

Convention to avoid import cycles (environment.py imports this module):
- ALL handler/tool-module imports are LAZY (inside function bodies),
  no top-level imports besides `typing`.
- Handlers bound to the instance (skill_loader, process_manager, ...) are
  accessed through the `env` argument as closures/lambdas — exactly like the
  original.

Registration order & overrides are preserved from the original:
- In the web group: factories (web_tools -> ... -> tools.web) run first,
  then the 3 legacy tools web_search/web_fetch/web_extract register LAST to
  override.
- In the code group: the tools.code factory runs first, then legacy
  code_review/dep_cycles (section 13) register to override.
- In the security group: the tools.security factory runs first, then legacy
  secret_scan; the connector registry (5g) runs first, then the 5 legacy gmail
  tools (section 12) register to override.
- Plugins (original section 3) live in the code group, right after skill tools —
  matching the original position (plugins override skill tools on name clash).
  Note: in the original, plugins (sec 3) also stood before EVERY later section;
  in this split, groups merged BEFORE the code group (filesystem/terminal/git/web)
  override plugins on name clash — a plugin colliding with a core tool name was
  already pathological; this difference is documented and accepted.

Expected tool count (counted on a real env, commit bf171b9): 159.
"""

from typing import Any, Dict, List


def _schema(properties: Dict[str, Any] = None, required: List[str] = None) -> Dict[str, Any]:
    """JSON schema builder — copied verbatim from environment.py."""
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    }


def _init_filesystem_tools(env) -> Dict[str, Dict[str, Any]]:
    """File group: basic file ops + unified fileops + tools.filesystem package.

    Covers: section 1 (read_file/write_file/patch_file), section 3 (plugins),
    unified tools (edit_file/append_file/file_info), section 14 (fs_*).
    Expected: 13 tools.
    """
    from tools.file_ops import read_file, write_file, patch_file
    from tools.fileops import edit_file, append_file, file_info

    tools: Dict[str, Dict[str, Any]] = {}

    # 1. File ops (original section 1)
    tools["read_file"] = {
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
    tools["write_file"] = {
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
    tools["patch_file"] = {
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

    # 3. Dynamic plugins (original section 3) — kept right after file ops.
    plugin_ctx = env.plugin_registry.scan_and_load()
    for name, tool_meta in plugin_ctx.tools.items():
        tools[name] = tool_meta

    # Unified fileops tools (original, after section 12)
    tools["edit_file"] = {
        "name": "edit_file",
        "func": edit_file,
        "description": "Edit file by exact-match replacement. Creates .bak backup. Fails on 0 or >1 matches.",
        "schema": _schema(
            {"path": {"type": "string", "description": "File path."},
             "old": {"type": "string", "description": "Exact text to replace."},
             "new": {"type": "string", "description": "Replacement text."}},
            ["path", "old", "new"],
        ),
    }
    tools["append_file"] = {
        "name": "append_file",
        "func": append_file,
        "description": "Append text to end of file.",
        "schema": _schema(
            {"path": {"type": "string", "description": "File path."},
             "text": {"type": "string", "description": "Text to append."}},
            ["path", "text"],
        ),
    }
    tools["file_info"] = {
        "name": "file_info",
        "func": file_info,
        "description": "Get file metadata: size, mtime, line count.",
        "schema": _schema(
            {"path": {"type": "string", "description": "File path."}},
            ["path"],
        ),
    }

    # 14. Refactored tools.filesystem package (original section 14, fs_* part).
    #     fs_ prefix to avoid colliding with legacy tools (read_file, ...).
    def _pkg_guard(fn):
        try:
            return fn()
        except Exception as exc:  # tool never raises into the agent loop
            return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        from tools.filesystem import (
            read as _fs_read, write as _fs_write, edit as _fs_edit,
            append as _fs_append, delete as _fs_delete,
            list_dir as _fs_list, info as _fs_info,
        )

        def _reg(name, func, description, properties, required):
            tools[name] = {
                "name": name,
                "func": func,
                "description": description,
                "schema": _schema(properties, required),
            }

        _reg("fs_read",
             lambda path, offset=1, limit=2000: _pkg_guard(
                 lambda: _fs_read(path, int(offset), int(limit))),
             "Read a file with safe path resolution (blocks traversal). Returns numbered lines; binary files are reported, not dumped.",
             {"path": {"type": "string", "description": "File path."},
              "offset": {"type": "integer", "description": "1-based first line. Default 1."},
              "limit": {"type": "integer", "description": "Max lines. Default 2000."}},
             ["path"])
        _reg("fs_write",
             lambda path, content: _pkg_guard(
                 lambda: _fs_write(path, content)),
             "Write complete content to a file, replacing anything already there (safe path resolution).",
             {"path": {"type": "string", "description": "File path."},
              "content": {"type": "string", "description": "Full file content."}},
             ["path", "content"])
        _reg("fs_edit",
             lambda path, old, new: _pkg_guard(
                 lambda: _fs_edit(path, old, new)),
             "Edit a file by exact-match replacement (safe path resolution).",
             {"path": {"type": "string", "description": "File path."},
              "old": {"type": "string", "description": "Exact text to replace."},
              "new": {"type": "string", "description": "Replacement text."}},
             ["path", "old", "new"])
        _reg("fs_append",
             lambda path, text, newline=True: _pkg_guard(
                 lambda: _fs_append(path, text, bool(newline))),
             "Append text to the end of a file (safe path resolution).",
             {"path": {"type": "string", "description": "File path."},
              "text": {"type": "string", "description": "Text to append."},
              "newline": {"type": "boolean", "description": "Add trailing newline. Default true."}},
             ["path", "text"])
        _reg("fs_delete",
             lambda path: _pkg_guard(lambda: _fs_delete(path)),
             "Permanently delete a file (safe path resolution, blocks traversal). No undo.",
             {"path": {"type": "string", "description": "File path to delete."}},
             ["path"])
        _reg("fs_list",
             lambda path=".", recursive=False, limit=200: _pkg_guard(
                 lambda: _fs_list(path or ".", bool(recursive), int(limit))),
             "List directory contents with safe path resolution.",
             {"path": {"type": "string", "description": "Directory path. Default '.'."},
              "recursive": {"type": "boolean", "description": "Recurse into subdirectories. Default false."},
              "limit": {"type": "integer", "description": "Max entries. Default 200."}},
             [])
        _reg("fs_info",
             lambda path: _pkg_guard(lambda: _fs_info(path)),
             "Get file metadata: size, mtime, line count (safe path resolution).",
             {"path": {"type": "string", "description": "File path."}},
             ["path"])
    except Exception as e:
        print(f"[tools] refactored package tools.filesystem unavailable: {e}")

    return tools


def _init_terminal_tools(env) -> Dict[str, Dict[str, Any]]:
    """Terminal group: sandbox IPC, shell, process manager, code execution.

    Covers: section 4 (sandbox_exec), section 6 (process_*), section 10
    (execute_code), unified shell tools, section 14 (shell_forget).
    Expected: 11 tools.
    """
    from tools.code_execution import CodeExecutionKernel
    from tools.shell import run as shell_run, run_bg, job_status, job_kill

    tools: Dict[str, Dict[str, Any]] = {}

    # 4. IPC Sandbox execution (original section 4)
    async def _sandbox_exec(cmd: str, args: List[str] = None):
        return await env.ipc_bridge.execute(cmd, args)

    tools["sandbox_exec"] = {
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

    # 6. Process management (original section 6)
    tools["process_list"] = {
        "name": "process_list",
        "func": env.process_manager.list_processes,
        "description": "List background processes tracked in the process ledger.",
        "schema": _schema(),
    }
    tools["process_poll"] = {
        "name": "process_poll",
        "func": env.process_manager.poll_process,
        "description": "Poll one background process for new stdout/stderr and its status.",
        "schema": _schema(
            {"session_id": {"type": "string", "description": "Process session id from process_list."}},
            ["session_id"],
        ),
    }
    tools["process_start"] = {
        "name": "process_start",
        "func": env.process_manager.start_process,
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
    tools["process_kill"] = {
        "name": "process_kill",
        "func": env.process_manager.kill_process,
        "description": "Terminate a tracked background process and return any output not yet polled.",
        "schema": _schema(
            {"session_id": {"type": "string", "description": "Process session id."}},
            ["session_id"],
        ),
    }

    # 10. Programmatic Code Execution Kernel (original section 10).
    #     The original passed self._tools (a live dict — the kernel sees all
    #     tools once merging finishes because it's the same object). Kept via
    #     env._tools.
    kernel = CodeExecutionKernel(env._tools)
    tools["execute_code"] = {
        "name": "execute_code",
        "func": lambda code: kernel.execute(code),
        "description": "Run Python code in-process with the tool registry exposed. Returns captured stdout.",
        "schema": _schema({"code": {"type": "string", "description": "Python source to execute."}}, ["code"]),
    }

    # Unified shell tools (original, after section 12)
    tools["shell_run"] = {
        "name": "shell_run",
        "func": shell_run,
        "description": "Run shell command with timeout (default 30s). No shell=True, blocks dangerous commands.",
        "schema": _schema(
            {"cmd": {"type": "string", "description": "Command to run."},
             "timeout": {"type": "integer", "description": "Timeout seconds."},
             "cwd": {"type": "string", "description": "Working directory."}},
            ["cmd"],
        ),
    }
    tools["shell_bg"] = {
        "name": "shell_bg",
        "func": run_bg,
        "description": "Run command in background. Returns job_id.",
        "schema": _schema(
            {"cmd": {"type": "string", "description": "Command to run."}},
            ["cmd"],
        ),
    }
    tools["shell_status"] = {
        "name": "shell_status",
        "func": job_status,
        "description": "Check status of background job.",
        "schema": _schema(
            {"job_id": {"type": "string", "description": "Job ID from shell_bg."}},
            ["job_id"],
        ),
    }
    tools["shell_kill"] = {
        "name": "shell_kill",
        "func": job_kill,
        "description": "Kill background job.",
        "schema": _schema(
            {"job_id": {"type": "string", "description": "Job ID from shell_bg."}},
            ["job_id"],
        ),
    }

    # 14. Refactored tools.terminal package (original section 14, shell_forget part)
    def _pkg_guard(fn):
        try:
            return fn()
        except Exception as exc:  # tool never raises into the agent loop
            return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        from tools.terminal.shell import forget as _shell_forget
        tools["shell_forget"] = {
            "name": "shell_forget",
            "func": lambda job_id: _pkg_guard(lambda: _shell_forget(job_id)),
            "description": "Drop a finished background shell job from the registry to free memory.",
            "schema": _schema(
                {"job_id": {"type": "string", "description": "Job id from shell_bg."}},
                ["job_id"],
            ),
        }
    except Exception as e:
        print(f"[tools] refactored package tools.terminal unavailable: {e}")

    return tools


def _init_git_tools(env) -> Dict[str, Dict[str, Any]]:
    """Git group: legacy gitops + tools.git package.

    Covers: section 13 (git_status/git_diff_summary/git_safe_commit/
    git_branch_guard), section 14 (git_diff/git_log).
    Expected: 6 tools.
    """
    from autonomous.gitops import (
        status as git_status_fn,
        diff_summary as git_diff_fn,
        safe_commit as git_commit_fn,
        branch_guard as git_guard_fn,
    )

    tools: Dict[str, Dict[str, Any]] = {}

    # 13. Legacy git (original section 13, git part)
    tools["git_status"] = {
        "name": "git_status",
        "func": lambda repo: git_status_fn(repo or "."),
        "description": "Git status --porcelain parsed: staged/unstaged changes per file.",
        "schema": _schema(
            {"repo": {"type": "string", "description": "Repo path (default cwd)."}},
            [],
        ),
    }
    tools["git_diff_summary"] = {
        "name": "git_diff_summary",
        "func": lambda repo: git_diff_fn(repo or "."),
        "description": "Summarize git diff: files changed, +/- lines, functions touched.",
        "schema": _schema(
            {"repo": {"type": "string", "description": "Repo path (default cwd)."}},
            [],
        ),
    }
    tools["git_safe_commit"] = {
        "name": "git_safe_commit",
        "func": lambda repo, message: git_commit_fn(repo or ".", message),
        "description": "Commit staged files ONLY if message non-empty and no secrets detected in staged content. Refuses on protected branch — use git_branch_guard first.",
        "schema": _schema(
            {"repo": {"type": "string", "description": "Repo path."},
             "message": {"type": "string", "description": "Commit message."}},
            ["message"],
        ),
    }
    tools["git_branch_guard"] = {
        "name": "git_branch_guard",
        "func": lambda repo: {"protected": git_guard_fn(repo or ".")},
        "description": "True if current branch is protected (main/master) — agent must create a feature branch instead of committing directly.",
        "schema": _schema(
            {"repo": {"type": "string", "description": "Repo path."}},
            [],
        ),
    }

    # 14. Refactored tools.git package (original section 14, git_diff/git_log part)
    def _pkg_guard(fn):
        try:
            return fn()
        except Exception as exc:  # tool never raises into the agent loop
            return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        from tools.git.gitops import git_diff as _git_diff, git_log as _git_log
        tools["git_diff"] = {
            "name": "git_diff",
            "func": lambda repo=".", staged=False, stat_only=True, max_chars=20000: _pkg_guard(
                lambda: _git_diff(repo or ".", bool(staged), bool(stat_only), int(max_chars))),
            "description": "Show git diff: per-file stats plus optional raw diff text (capped). staged=true diffs the index.",
            "schema": _schema(
                {"repo": {"type": "string", "description": "Repo path. Default '.'."},
                 "staged": {"type": "boolean", "description": "Diff staged changes. Default false."},
                 "stat_only": {"type": "boolean", "description": "Only per-file stats, no raw diff. Default true."},
                 "max_chars": {"type": "integer", "description": "Cap raw diff length. Default 20000."}},
                []),
        }
        tools["git_log"] = {
            "name": "git_log",
            "func": lambda repo=".", limit=10: _pkg_guard(
                lambda: _git_log(repo or ".", int(limit))),
            "description": "Show recent commit history: hash, author, date, subject.",
            "schema": _schema(
                {"repo": {"type": "string", "description": "Repo path. Default '.'."},
                 "limit": {"type": "integer", "description": "Max commits. Default 10."}},
                []),
        }
    except Exception as e:
        print(f"[tools] refactored package tools.git unavailable: {e}")

    return tools


def _init_web_tools(env) -> Dict[str, Dict[str, Any]]:
    """Web group: search/fetch/extract, scraper, browser, deepsearch/deepthink.

    Covers: factories 5e (web_tools, browser_tool, deepsearch, deepthink,
    scraper, tools.web — in the original tuple order) then the 3 legacy tools
    web_search/web_fetch/web_extract register LAST to override the factories.
    Expected: 25 tools (unique).
    """
    from importlib import import_module
    from tools.web import web_search, web_fetch, web_extract

    tools: Dict[str, Dict[str, Any]] = {}

    # 5e. Web factories — in the original tuple order (web_tools first,
    #     tools.web later so tools.web wins on name clashes).
    for module_name, use_root_dir in (
        ("tools.web_tools", False),
        ("tools.browser_tool", False),
        ("research.deepsearch", False),
        ("research.deepthink", False),
        ("tools.scraper", False),
        ("tools.web", False),
    ):
        try:
            factory = getattr(import_module(module_name), "build_tools", None)
            if factory is not None:
                tools.update(factory(env.root_dir if use_root_dir else env))
        except Exception as e:
            print(f"[tools] {module_name} unavailable: {e}")

    # Legacy unified web tools (original, after section 12) — override factories.
    tools["web_search"] = {
        "name": "web_search",
        "func": web_search,
        "description": "Search the web via DuckDuckGo HTML. Returns list of {title, url, snippet}.",
        "schema": _schema(
            {"query": {"type": "string", "description": "Search query."},
             "max_results": {"type": "integer", "description": "Max results (default 10)."}},
            ["query"],
        ),
    }
    tools["web_fetch"] = {
        "name": "web_fetch",
        "func": web_fetch,
        "description": "Fetch a URL and return text content. Has timeout, retry, circuit breaker.",
        "schema": _schema(
            {"url": {"type": "string", "description": "URL to fetch."},
             "timeout": {"type": "integer", "description": "Timeout seconds (default 15)."}},
            ["url"],
        ),
    }
    tools["web_extract"] = {
        "name": "web_extract",
        "func": web_extract,
        "description": "Extract main text content from a URL, removing nav/footer/scripts.",
        "schema": _schema(
            {"url": {"type": "string", "description": "URL to extract."},
             "selector": {"type": "string", "description": "Optional CSS selector."}},
            ["url"],
        ),
    }

    return tools


def _init_code_tools(env) -> Dict[str, Dict[str, Any]]:
    """Code group: skills, subagents, rewind, AST, discovery, review/depgraph.

    Covers: section 2 (skills), sections 5b/5c/5d (delegate/optimize/rewind),
    factories 5e (research_tool, tools.code — before section 13 so legacy
    code_review/dep_cycles override), section 9 (ast_inspect),
    section 11 (search_tools/list_toolsets), section 13 (code_review/
    dep_cycles/dep_impact).
    Expected: 15 tools (+ dynamic plugins, 0 in a clean env).
    """
    from importlib import import_module
    from tools.ast_tool import ASTInspector
    from tools.tool_discovery import search_tools, list_toolsets
    from autonomous.codereview import CodeReviewer
    from autonomous.depgraph import build_graph, find_cycles, impact_analysis

    tools: Dict[str, Dict[str, Any]] = {}

    # 2. Skill ops (original section 2)
    tools["list_skills"] = {
        "name": "list_skills",
        "func": lambda: env.skill_loader.list_skills(),
        "description": "List available Agent Skills with their names and descriptions.",
        "schema": _schema(),
    }
    tools["view_skill"] = {
        "name": "view_skill",
        "func": lambda name, file_path=None: env.skill_loader.view_skill(name, file_path),
        "description": "Load the full documentation of a skill by name, optionally a specific file inside it.",
        "schema": _schema(
            {
                "name": {"type": "string", "description": "Skill name."},
                "file_path": {"type": "string", "description": "Optional file inside the skill directory."},
            },
            ["name"],
        ),
    }
    tools["install_skill"] = {
        "name": "install_skill",
        "func": lambda skill_name: env.skills_hub.install_skill(skill_name),
        "description": "Install a skill by name from the skills hub.",
        "schema": _schema(
            {"skill_name": {"type": "string", "description": "Name of the skill to install."}},
            ["skill_name"],
        ),
    }

    # 5b. Subagents (original section 5b)
    async def _delegate(
        tasks: List[Dict[str, Any]] = None,
        fail_fast: bool = False,
        workflow: str = "",
        goal: str = "",
    ):
        return await env.delegation.delegate_task(
            tasks, fail_fast=fail_fast, workflow=workflow, goal=goal
        )

    tools["delegate_task"] = {
        "name": "delegate_task",
        "func": _delegate,
        "description": (
            "Run subtasks as separate subagents, each with its own transcript, and return "
            "every subagent's final answer. Subtasks without depends_on run in parallel; a "
            "subtask that names depends_on receives those subagents' results and waits for "
            "them. Roles: "
            + ", ".join(env.delegation.swarm.role_names())
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
                                "enum": env.delegation.swarm.role_names(),
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

    # 5c. Workflow self-improvement (original section 5c)
    async def _optimize(goal: str, candidates: List[Dict[str, Any]] = None, max_candidates: int = 3):
        return await env.delegation.optimize_workflow(goal, candidates, max_candidates)

    tools["optimize_workflow"] = {
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
                                            "enum": env.delegation.swarm.role_names(),
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

    # 5d. Rewind (original section 5d)
    async def _rewind(dry_run: bool = False):
        result = env.checkpoints.rewind(env.checkpoints.session_id, dry_run=dry_run)
        if not dry_run and (result["restored"] or result["deleted"]):
            # The transcript still reads as if those edits happened. Put the rewind in it, so
            # the next turn sees what the files actually are now.
            env.state_db.append_message(
                env.checkpoints.session_id, "system",
                f"rewind: {len(result['restored'])} file(s) restored, "
                f"{len(result['deleted'])} file(s) this session created were removed",
            )
            env.state_db.record_timeline_event(env.checkpoints.session_id, "rewind", result)
        return result

    tools["rewind"] = {
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

    # 5e. Code factories — run BEFORE section 13 so legacy overrides win on name
    #     clashes (code_review, dep_cycles). In the original tuple order.
    for module_name, use_root_dir in (
        ("tools.research_tool", False),
        ("tools.code", False),
    ):
        try:
            factory = getattr(import_module(module_name), "build_tools", None)
            if factory is not None:
                tools.update(factory(env.root_dir if use_root_dir else env))
        except Exception as e:
            print(f"[tools] {module_name} unavailable: {e}")

    # 9. AST code inspection (original section 9)
    tools["ast_inspect"] = {
        "name": "ast_inspect",
        "func": lambda file_path: ASTInspector.inspect_file(file_path),
        "description": "Parse a Python file and list its imports, classes and functions without executing it.",
        "schema": _schema({"file_path": {"type": "string", "description": "Path to a Python file."}}, ["file_path"]),
    }

    # 11. Runtime tool discovery (original section 11)
    tools["search_tools"] = {
        "name": "search_tools",
        "func": search_tools,
        "description": "Find tools by keyword at runtime. Searches name, description, and toolset; returns matching tools with their schemas. Use this instead of guessing tool names.",
        "schema": _schema(
            {
                "keyword": {"type": "string", "description": "Keyword(s) to search for, e.g. 'gmail', 'file', 'send'."},
                "limit": {"type": "integer", "description": "Max tools to return. Default 10."},
            },
            ["keyword"],
        ),
    }
    tools["list_toolsets"] = {
        "name": "list_toolsets",
        "func": list_toolsets,
        "description": "List all registered toolsets and the tool names in each.",
        "schema": _schema({}, []),
    }

    # 13. Legacy coding tools (original section 13, code part) — override tools.code factory
    _reviewer = CodeReviewer()
    tools["code_review"] = {
        "name": "code_review",
        "func": lambda path: _reviewer.to_markdown(_reviewer.review_file(path)),
        "description": "Static code review of a Python file: complexity, dead code, bare except, TODOs. Returns a Markdown report.",
        "schema": _schema(
            {"path": {"type": "string", "description": "Path to Python file."}},
            ["path"],
        ),
    }
    tools["dep_cycles"] = {
        "name": "dep_cycles",
        "func": lambda root: find_cycles(build_graph(root)),
        "description": "Find circular imports under a directory. Returns list of cycles.",
        "schema": _schema(
            {"root": {"type": "string", "description": "Root directory to scan."}},
            ["root"],
        ),
    }
    tools["dep_impact"] = {
        "name": "dep_impact",
        "func": lambda root, files: impact_analysis(build_graph(root), files if isinstance(files, list) else [files]),
        "description": "Which modules are affected when given files change (reverse dependency BFS).",
        "schema": _schema(
            {"root": {"type": "string", "description": "Root directory."},
             "files": {"type": "string", "description": "Changed file(s), comma-separated or single path."}},
            ["root", "files"],
        ),
    }

    return tools


def _init_security_tools(env) -> Dict[str, Dict[str, Any]]:
    """Security group: authz/audit, connectors, gmail, secret scan, MCP.

    Covers: factories 5e (authz.tools, audit.tools, tools.security — in the
    original tuple order), 5g (connector registry), section 12 (5 legacy gmail
    tools, overriding connectors on name clash), section 13 (legacy
    secret_scan, overriding the tools.security factory), 9b (MCP registry).
    Expected: 45 tools (+ dynamic MCP, 0 in a clean env).
    """
    from importlib import import_module
    from security.secretscan import scan_file as secret_scan_file, scan_dir as secret_scan_dir
    from tools import gmail_tool as _gmail_tool

    tools: Dict[str, Dict[str, Any]] = {}

    # 5e. Security factories — in the original tuple order (authz, audit, security).
    #     The factory's secret_scan is overridden by the legacy section 13 below.
    for module_name, use_root_dir in (
        ("authz.tools", False),
        ("audit.tools", True),
        ("tools.security", False),
    ):
        try:
            factory = getattr(import_module(module_name), "build_tools", None)
            if factory is not None:
                tools.update(factory(env.root_dir if use_root_dir else env))
        except Exception as e:
            print(f"[tools] {module_name} unavailable: {e}")

    # 5g. Service connectors (original section 5g) — merged via connector registry.
    try:
        from connectors.registry import load_default_registry
        registry = load_default_registry()
        tools.update(registry.build_tools())
        tools.update(registry.build_status_tool())
    except Exception as e:
        print(f"[tools] connectors unavailable: {e}")

    # 12. Legacy Gmail (original section 12) — overrides connectors on name clash
    #     (gmail_search/gmail_read/gmail_send). gmail_send is NOT a safe tool:
    #     the approval gate always prompts before sending.
    tools["gmail_search"] = {
        "name": "gmail_search",
        "func": _gmail_tool.gmail_search,
        "description": "Search Gmail messages. Returns id/snippet list, no bodies.",
        "schema": _schema(
            {
                "query": {"type": "string", "description": "Gmail search query, e.g. 'from:boss subject:report'."},
                "max_results": {"type": "integer", "description": "Max messages. Default 10."},
            },
            ["query"],
        ),
    }
    tools["gmail_read"] = {
        "name": "gmail_read",
        "func": _gmail_tool.gmail_read,
        "description": "Read one Gmail message: headers, snippet, and plain-text body.",
        "schema": _schema(
            {"message_id": {"type": "string", "description": "Gmail message id from gmail_search."}},
            ["message_id"],
        ),
    }
    tools["gmail_draft"] = {
        "name": "gmail_draft",
        "func": _gmail_tool.gmail_draft,
        "description": "Create a LOCAL email draft (nothing sent, no Gmail API call). Returns a draft_id plus preview. Pass the draft_id to gmail_send to send (requires approval).",
        "schema": _schema(
            {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Email subject."},
                "body": {"type": "string", "description": "Email body text."},
            },
            ["to", "body"],
        ),
    }
    tools["gmail_send"] = {
        "name": "gmail_send",
        "func": _gmail_tool.gmail_send,
        "description": "Send a draft created by gmail_draft. DANGER: actually sends email. Always requires approval.",
        "schema": _schema(
            {"draft_id": {"type": "string", "description": "Draft id returned by gmail_draft."}},
            ["draft_id"],
        ),
    }
    tools["gmail_discard"] = {
        "name": "gmail_discard",
        "func": _gmail_tool.gmail_discard,
        "description": "Discard a local draft without sending.",
        "schema": _schema(
            {"draft_id": {"type": "string", "description": "Draft id returned by gmail_draft."}},
            ["draft_id"],
        ),
    }

    # 13. Legacy secret_scan (original section 13) — overrides tools.security factory
    tools["secret_scan"] = {
        "name": "secret_scan",
        "func": lambda target, recursive=False: secret_scan_dir(target) if recursive else secret_scan_file(target),
        "description": "Scan file or directory for leaked secrets (API keys, tokens, private keys). Previews are masked.",
        "schema": _schema(
            {"target": {"type": "string", "description": "File or directory path."},
             "recursive": {"type": "boolean", "description": "Scan directory recursively."}},
            ["target"],
        ),
    }

    # 9b. MCP servers in mcp.json (original section 9b). register_mcp_tools
    #     writes directly into env._tools, so snapshot-diff to collect them.
    try:
        from mcp.registry import register_mcp_tools
        before = set(env.get_tools())
        register_mcp_tools(env, env.root_dir)
        for name in env.get_tools():
            if name not in before:
                tools[name] = env.get_tools()[name]
    except Exception as e:
        print(f"[mcp] discovery failed, continuing without MCP tools: {e}")

    return tools


def _init_db_tools(env) -> Dict[str, Dict[str, Any]]:
    """Data group: memory, session search, db, cron, triggers, learning, etc.

    Covers: section 8 (memory/session_search), remaining factories 5e (db,
    cronjob_tool, triggers, learning, goals, digest, people, tasks, verify,
    ocr_tool, artifacts, broadcast — in the original tuple order).
    Expected: 44 tools.
    """
    from importlib import import_module

    tools: Dict[str, Dict[str, Any]] = {}

    # 8. Curated Memory & Session Search (original section 8)
    tools["memory"] = {
        "name": "memory",
        "func": lambda target, operations: env.curated_memory.execute_operations(target, operations),
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
    tools["session_search"] = {
        "name": "session_search",
        "func": lambda **kwargs: env.session_search.search(**kwargs),
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

    # 5e. Data factories — in the original tuple order.
    for module_name, use_root_dir in (
        ("tools.cronjob_tool", False),
        ("artifacts.tools", False),
        ("goals.tools", False),
        ("digest.tools", False),
        ("people.tools", True),
        ("tasks.tools", True),
        ("verify.tools", True),
        ("learning.tools", False),
        ("triggers.tools", False),
        ("tools.ocr_tool", False),
        ("gateway.broadcast", False),
        ("tools.db", False),
    ):
        try:
            factory = getattr(import_module(module_name), "build_tools", None)
            if factory is not None:
                tools.update(factory(env.root_dir if use_root_dir else env))
        except Exception as e:
            print(f"[tools] {module_name} unavailable: {e}")

    return tools


# Merge order — matches the section order of the original _init_tools.
_GROUP_FUNCS = (
    _init_filesystem_tools,
    _init_terminal_tools,
    _init_git_tools,
    _init_web_tools,
    _init_code_tools,
    _init_security_tools,
    _init_db_tools,
)


def init_all_tool_groups(env) -> Dict[str, Dict[str, Any]]:
    """Call all 7 group functions and merge them into one registry.

    Later groups override earlier ones on name clash — but by design, groups
    don't collide (every intentional override lives INSIDE a group), so this
    merge reproduces the original `_init_tools` registry exactly.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for group_fn in _GROUP_FUNCS:
        merged.update(group_fn(env))
    return merged
