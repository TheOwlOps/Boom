# BUGFIX.md — Boom full-pipeline integration test (commit bf171b9)

Test run: `python3 test_integration.py` → **39/39 PASS, 0 FAIL**.
Repo NOT modified (read-only at /tmp/boom_git).

## Conclusion: NO real bugs found

During testing, 2 suspects were investigated thoroughly and concluded
to be **not repo bugs**. Details below to avoid re-doing the work.

---

## 1. Suspect: `ModuleNotFoundError: No module named 'pydantic'` when importing environment

- **Symptom:** `from environment import AgentEnvironment` crashed at
  `from extensibility.plugin_registry import PluginRegistry`
  (file `agent/src/extensibility/plugin_registry.py`, line 5:
  `from pydantic import BaseModel`) because the test machine had no pydantic installed.
- **Conclusion: NOT a repo bug.** `pydantic>=2.0.0` is declared in the right places:
  - `agent/pyproject.toml` (dependencies)
  - `devbox.json` (`python312Packages.pydantic`)
- **Handling during the test:** transiently installed on the test machine:
  `pip install --break-system-packages pydantic` (pydantic 2.14.1).
  Repo untouched.
- **Suggestion (optional):** if you want to run tests outside devbox without manual installs,
  consider `pip install -e agent/` or a requirements file in README/CI.
  No code fix needed.

## 2. Suspect: `search_tools('file')` returned 0 results

- **Symptom (first run):** the test FAILED because `search_tools("file")` returned
  0 results when the test read the `"results"` key.
- **Conclusion: the bug was in the TEST, not the repo.**
  - The real API returns the `"tools"` key (file
    `agent/src/tools/tool_discovery.py`, function `search_tools`), not `"results"`. The test was fixed to use the right key.
  - `search_tools("read_file")` correctly returns 2 hits, `read_file` ranked #1
    (exact-name match scores higher). The generic keyword `"file"` has 35 hits,
    and `read_file` fell out of the top-10 due to the default limit — intended
    ranking behavior, not a bug.

## 3. Registry scan (bonus)

Scanned all 159 tools in `env._tools` after `AgentEnvironment.__init__`:
- 0 entries missing `name`/`func`/`description`/`schema` keys
- 0 non-callable `func`
- 0 empty descriptions

→ Clean registry, no broken entries found.

## 4. Notes (not crash bugs, but worth knowing)

- File `agent/src/tools/registry.py` line 100:
  `for name, meta in (env or AgentEnvironment()).get_tools().items():`
  When `search_tools` is called without an `env`, the registry builds its own
  `AgentEnvironment()` with `root_dir` defaulting to the current directory (cwd),
  and that initialization **writes `state.db` and the `skills/`, `plugins/`…
  directories into the cwd as a side effect** (observed when running the probe from
  an output directory). No crash, no wrong data, but an implicit side effect —
  for a clean run, pass an explicit `root_dir` or point at a temp directory
  when building the environment for the registry.
