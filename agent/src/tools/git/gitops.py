"""tools.git.gitops — safe git tools for Boom.

Wraps agent/src/autonomous/gitops.py into 5 tool functions:
git_status / git_diff / git_log / git_safe_commit / git_branch_guard.

- Every error (GitOpsError, git exit != 0, invalid repo) is caught and
  returned as {"ok": False, "error": ...} instead of raising.
- git_safe_commit scans secrets with agent/src/security/secretscan.py
  (scan_diff over the staged diff) BEFORE committing — a detected secret
  aborts the commit.

Stdlib + pydantic only.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

_TIMEOUT = 30  # seconds, avoids git hanging when the agent runs in the background


# ---------------------------------------------------------------------------
# load the original module (works whether repo root or agent/src is on sys.path)
# ---------------------------------------------------------------------------

def _import(name_primary: str, name_fallback: str, label: str):
    try:
        module = __import__(name_primary, fromlist=["*"])
        return module
    except ImportError:
        pass
    try:
        return __import__(name_fallback, fromlist=["*"])
    except ImportError as exc:
        raise ImportError(
            f"could not load module {label}: both '{name_primary}' and "
            f"'{name_fallback}' failed ({exc})")


def _gitops():
    return _import("agent.src.autonomous.gitops", "autonomous.gitops", "gitops")


def _secretscan():
    return _import("agent.src.security.secretscan", "security.secretscan",
                   "secretscan")


class LogOptions(BaseModel):
    """Validated parameters for git_log()."""

    limit: int = Field(default=10, ge=1, le=100,
                       description="max commits, 1..100")


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _check_repo(repo: str) -> Optional[str]:
    """Validate repo: must be a non-empty string pointing at an existing dir.

    Returns None if valid, otherwise an error string.
    """
    if not isinstance(repo, str) or not repo.strip():
        return "repo must be a non-empty path string"
    if not os.path.isdir(repo):
        return f"repo is not an existing directory: {repo}"
    return None


def _git(repo: str, *args: str) -> Tuple[bool, str]:
    """Run `git <args>` inside the repo. Returns (ok, stdout_or_error).

    Never raises — errors are wrapped in the tuple.
    """
    try:
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "git command not found on this machine"
    except subprocess.TimeoutExpired:
        return False, f"git command timed out: git {' '.join(args)}"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        return False, f"git {' '.join(args)} failed: {err}"
    return True, proc.stdout


def _scan_staged_secrets(repo: str) -> List[Dict[str, Any]]:
    """Scan secrets on the staged diff via secretscan.scan_diff.

    Returns a list of findings (empty = clean). Each finding has
    {path, line, type, preview}.
    """
    secretscan = _secretscan()
    ok, diff_text = _git(repo, "diff", "--cached", "-U0")
    if not ok:
        return []  # couldn't read the diff -> gitops handles it in the next step
    try:
        findings = secretscan.scan_diff(diff_text) or []
    except Exception:
        return []
    # Attach each finding's file path for clearer error reporting.
    ok, names = _git(repo, "diff", "--cached", "--name-only", "-z")
    paths = [p for p in names.split("\x00") if p] if ok else []
    for f in findings:
        if isinstance(f, dict):
            f.setdefault("path", paths[0] if len(paths) == 1 else "?")
    return findings


# ---------------------------------------------------------------------------
# 5 tool functions
# ---------------------------------------------------------------------------

def git_status(repo: str = ".") -> Dict[str, Any]:
    """Show worktree status (which files are staged/unstaged, change type).

    Description:
        Equivalent to `git status --porcelain`, returned in structured form.
    Args:
        repo: path to the directory containing the git repo.
    Returns:
        {"ok": True, "files": [{"path","staged","unstaged","kind"}], "counts": {...}}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    gitops = _gitops()
    try:
        files = gitops.status(repo)
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # defensive: no unexpected exception escapes
        return {"ok": False, "error": f"unexpected error: {exc}"}
    counts: Dict[str, int] = {}
    for f in files:
        counts[f.get("kind", "unknown")] = counts.get(f.get("kind", "unknown"), 0) + 1
    return {"ok": True, "files": files, "counts": counts}


def git_diff(repo: str = ".", staged: bool = False,
             stat_only: bool = True, max_chars: int = 20000) -> Dict[str, Any]:
    """Show diff: stats summary + (optional) raw diff text.

    Description:
        summary: files changed, total added/removed lines, touched functions
        (parsed from hunk headers).
        text: unified diff content (capped at max_chars), None when
        stat_only=True.
    Args:
        repo: repo path.
        staged: True to only show `git add`-ed changes (--cached).
        stat_only: True returns only the summary, no text.
        max_chars: text length cap (1..200000).
    Returns:
        {"ok": True, "summary": {...}, "text": str|None}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    if not isinstance(max_chars, int) or not 1 <= max_chars <= 200000:
        return {"ok": False, "error": "max_chars must be within 1..200000"}
    gitops = _gitops()
    try:
        summary = gitops.diff_summary(repo)
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"unexpected error: {exc}"}
    text = None
    if not stat_only:
        args = ["diff", "--cached"] if staged else ["diff", "HEAD"]
        ok, out = _git(repo, *args)
        if not ok:
            return {"ok": False, "error": out}
        text = out[:max_chars]
        if len(out) > max_chars:
            text += f"\n...[truncated at {max_chars} chars]..."
    return {"ok": True, "summary": summary, "text": text}


def git_log(repo: str = ".", limit: int = 10) -> Dict[str, Any]:
    """Show recent commit history.

    Description:
        Returns the *limit* newest commits: hash, author, date, message.
    Args:
        repo: repo path.
        limit: number of commits (1..100).
    Returns:
        {"ok": True, "commits": [{"hash","author","date","message"}]}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    try:
        opts = LogOptions(limit=limit)
    except ValidationError as exc:
        return {"ok": False, "error": f"invalid limit: {exc.errors()[0]['msg']}"}
    ok, out = _git(repo, "log", f"-n{opts.limit}",
                   "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=iso")
    if not ok:
        return {"ok": False, "error": out}
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        commits.append({"hash": parts[0], "author": parts[1],
                        "date": parts[2], "message": parts[3]})
    return {"ok": True, "commits": commits}


def git_safe_commit(repo: str, message: str) -> Dict[str, Any]:
    """Safe commit: blocks empty messages, empty commits, and secrets.

    Description:
        1. Empty message -> refused.
        2. Scans secrets on the staged diff via secretscan.scan_diff;
           any secret found -> refused (with location).
        3. Calls gitops.safe_commit (re-checks: anything staged,
           fallback secret scan) then commits.
    Args:
        repo: repo path.
        message: commit message.
    Returns:
        {"ok": True, "commit_hash": str}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    if not isinstance(message, str) or not message.strip():
        return {"ok": False, "error": "empty commit message, commit refused"}
    findings = _scan_staged_secrets(repo)
    if findings:
        first = findings[0] if isinstance(findings[0], dict) else {}
        detail = "%s (type %s, line %s)" % (
            first.get("path", "?"), first.get("type", "?"), first.get("line", "?"))
        return {"ok": False,
                "error": "secret detected in staged files: %s "
                         "(+%d more finding(s)), commit refused"
                         % (detail, len(findings) - 1)}
    gitops = _gitops()
    try:
        result = gitops.safe_commit(repo, message)
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"unexpected error: {exc}"}
    # gitops.safe_commit already returns an {"ok": ...} dict — pass it through.
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": f"unexpected result from gitops: {result!r}"}


def git_branch_guard(repo: str = ".",
                     protected: Tuple[str, ...] = ("main", "master")) -> Dict[str, Any]:
    """Check whether the current branch is a protected one.

    Description:
        The agent must NOT commit directly to a protected branch (main/master) —
        it must create a new branch instead. This tool tells the agent where
        it is.
    Args:
        repo: repo path.
        protected: tuple of protected branch names.
    Returns:
        {"ok": True, "branch": str, "protected": bool, "warning": str|None}
        or {"ok": False, "error": str}.
    Raises:
        Never raises — all errors are returned as dicts.
    """
    err = _check_repo(repo)
    if err:
        return {"ok": False, "error": err}
    if not isinstance(protected, (tuple, list)) or not protected:
        return {"ok": False, "error": "protected must be a non-empty tuple/list"}
    gitops = _gitops()
    try:
        on_protected = gitops.branch_guard(repo, tuple(protected))
    except gitops.GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"unexpected error: {exc}"}
    ok, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch_name = branch.strip() if ok else "?"
    warning = None
    if on_protected:
        warning = (f"on protected branch '{branch_name}': "
                   "do not commit directly, create a new branch")
    return {"ok": True, "branch": branch_name, "protected": on_protected,
            "warning": warning}


__all__ = ["git_status", "git_diff", "git_log", "git_safe_commit",
           "git_branch_guard"]
