"""Safe git workflow for Boom's autonomous coding.

This module lets the agent check repo state, view diff summaries,
commit safely (blocks secrets + empty commits), and avoid committing straight
to a protected branch (main/master) — before performing any git operation that
rewrites history.

Every git command runs via subprocess (no external libs).
Every function takes `repo` as the path to the directory containing the repo.
When git fails, raises GitOpsError with an English message.

Stdlib only: re, os, subprocess.
"""

import os
import re
import subprocess

# ---------------------------------------------------------------------------
# exception
# ---------------------------------------------------------------------------


class GitOpsError(Exception):
    """Git operation error (invalid repo, git command failed, bad input)."""
    pass


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

_TIMEOUT = 30  # seconds, avoids git hanging when the agent runs in the background

# Valid branch names: lowercase, digits, dash, underscore, slash only.
_BRANCH_RE = re.compile(r"^[a-z0-9][a-z0-9\-_/]*$")

# Unified diff hunk header: @@ -a,b +c,d @@ <function context>
_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@\s*(.*)$")

# Fallback secret-scanning patterns (used when secretscan can't be imported).
# NOTE: these regexes are functional security patterns — kept byte-identical.
_SECRET_PATTERNS = [
    ("aws_secret_key", re.compile(
        r"aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{20,}", re.I)),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("generic_api_key", re.compile(
        r"(?i)(api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}['\"]?")),
    ("password_assign", re.compile(
        r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{4,}['\"]")),
    ("client_secret", re.compile(
        r"(?i)(client_secret|secret[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}['\"]")),
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(
        r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
]


def _git(repo: str, *args: str) -> str:
    """Runs `git <args>` inside the repo, returns stdout.

    Raises GitOpsError when: repo is not a directory, not a git repo, or the
    git command exits with a non-zero code.
    """
    if not os.path.isdir(repo):
        raise GitOpsError("directory does not exist: %s" % repo)
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
        raise GitOpsError("git command not found on this machine")
    except subprocess.TimeoutExpired:
        raise GitOpsError("git command timed out: git %s" % " ".join(args))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        raise GitOpsError("git %s failed: %s" % (" ".join(args), err))
    return proc.stdout


def _staged_paths(repo: str):
    """List of files currently staged (about to be committed)."""
    out = _git(repo, "diff", "--cached", "--name-only", "-z")
    return [p for p in out.split("\x00") if p]


def _read_staged_content(repo: str, path: str) -> str:
    """Read the staged content of a file.

    Prefers reading from the worktree (fast, correct when staged == worktree);
    falls back to reading the staged blob via `git show` if the file was
    deleted from the worktree.
    """
    disk_path = os.path.join(repo, path)
    if os.path.isfile(disk_path):
        try:
            with open(disk_path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read()
        except OSError:
            pass
    # File deleted/renamed away from the worktree: read the staged blob directly.
    blob = _git(repo, "show", ":" + path)
    return blob


def _scan_secrets(repo: str, paths) -> list:
    """Scan secrets in the staged files.

    Tries the secretscan module if importable (prefers the existing tool),
    otherwise falls back to simple built-in regex patterns.
    Returns a list of findings: [{path, pattern, line}].
    """
    findings = []

    # 1) Prefer the external secretscan if available.
    secretscan = None
    for modname in ("secretscan", "autonomous.secretscan"):
        try:
            __import__(modname)
            import sys
            secretscan = sys.modules[modname]
            break
        except ImportError:
            continue
    if secretscan is not None:
        scan_fn = getattr(secretscan, "scan_text", None) \
            or getattr(secretscan, "scan", None)
        if callable(scan_fn):
            for path in paths:
                content = _read_staged_content(repo, path)
                for finding in scan_fn(content) or []:
                    if isinstance(finding, dict):
                        finding = dict(finding)
                        finding.setdefault("path", path)
                    findings.append(finding)
            return findings
        # secretscan exists but has no familiar function -> fall back

    # 2) Fallback: simple built-in regex patterns.
    for path in paths:
        content = _read_staged_content(repo, path)
        for lineno, line in enumerate(content.splitlines(), 1):
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        {"path": path, "pattern": name, "line": lineno})
                    break  # report each line only once, avoid spam
    return findings


def _head_exists(repo: str) -> bool:
    """True if the repo has at least one commit (HEAD is valid)."""
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
        return True
    except GitOpsError:
        return False
def _kind_from_xy(x: str, y: str) -> str:
    """Classify a file from the 2 XY chars of porcelain v1."""
    if x == "?" and y == "?":
        return "untracked"
    codes = x + y
    if "U" in codes:
        return "unmerged"
    if "R" in codes:
        return "renamed"
    if "C" in codes:
        return "copied"
    if "A" in codes:
        return "added"
    if "D" in codes:
        return "deleted"
    if "M" in codes or "T" in codes:
        return "modified"
    return "unknown"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def status(repo: str) -> list:
    """Returns the worktree status as a list of dicts.

    Each dict: {path, staged, unstaged, kind}.
    - staged: True if there are `git add`-ed changes (porcelain X column)
    - unstaged: True if there are un-added changes (Y column), or untracked
    - kind: added | modified | deleted | renamed | copied | untracked
             | unmerged | unknown
    """
    out = _git(repo, "status", "--porcelain=v1", "-z")
    entries = []
    for token in out.split("\x00"):
        if not token or len(token) < 4:
            continue
        xy = token[:2]
        path = token[3:]
        x, y = xy[0], xy[1]
        if x == "?" and y == "?":
            staged, unstaged = False, True
        else:
            staged = x != " "
            unstaged = y != " "
        # Rename lines look like "old -> new": take the new name.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append({
            "path": path,
            "staged": staged,
            "unstaged": unstaged,
            "kind": _kind_from_xy(x, y),
        })
    return entries


def diff_summary(repo: str) -> dict:
    """Summarize the diff against HEAD (staged and unstaged alike).

    Returns:
    {
      "files_changed": int,
      "added": int,        # total lines added
      "deleted": int,       # total lines deleted
      "files": [
        {"path": str, "added": int, "deleted": int,
         "functions": [str]},  # touched functions, parsed from hunk headers
      ],
    }
    Repos with no commits yet: compares the worktree against the index
    (plain `git diff`) instead of HEAD.
    """
    # Per-file added/deleted line counts.
    # Repo without commits: HEAD doesn't exist -> use plain `git diff`
    # (worktree vs index) instead of `git diff HEAD`.
    diff_args = ["diff", "HEAD"] if _head_exists(repo) else ["diff"]
    numstat = _git(repo, *(diff_args + ["--numstat", "-z"]))
    counts = {}
    order = []
    for token in numstat.split("\x00"):
        if not token:
            continue
        parts = token.split("\t")
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        if " -> " in path:  # rename: take the new name
            path = path.split(" -> ", 1)[1].strip("{}")
        try:
            added = int(added_s) if added_s != "-" else 0
            deleted = int(deleted_s) if deleted_s != "-" else 0
        except ValueError:
            continue
        counts[path] = {"added": added, "deleted": deleted}
        if path not in order:
            order.append(path)

    # Touched function names: parse the context after @@ ... @@ hunk headers.
    funcs = {p: [] for p in order}
    current = None
    for line in _git(repo, *(diff_args + ["-U0"])).splitlines():
        if line.startswith("diff --git "):
            m = re.search(r"b/(.+)$", line)
            current = m.group(1) if m else None
            continue
        if current is None:
            continue
        m = _HUNK_RE.match(line)
        if m:
            ctx = m.group(1).strip()
            if ctx and current in funcs and ctx not in funcs[current]:
                funcs[current].append(ctx)

    files = []
    total_added = total_deleted = 0
    for path in order:
        c = counts[path]
        total_added += c["added"]
        total_deleted += c["deleted"]
        files.append({
            "path": path,
            "added": c["added"],
            "deleted": c["deleted"],
            "functions": funcs.get(path, []),
        })
    return {
        "files_changed": len(files),
        "added": total_added,
        "deleted": total_deleted,
        "files": files,
    }


def safe_commit(repo: str, message: str) -> dict:
    """Safe commit: only commits when all conditions hold.

    Conditions:
    1. message is non-empty (after strip)
    2. at least 1 file is staged
    3. no secrets detected in staged content
       (uses secretscan if importable, otherwise simple patterns)

    Returns {"ok": True, "commit_hash": str} or
            {"ok": False, "error": str} (does NOT raise).
    """
    if not message or not message.strip():
        return {"ok": False, "error": "empty commit message, commit refused"}

    staged = _staged_paths(repo)
    if not staged:
        return {"ok": False,
                "error": "nothing staged, commit refused"}

    findings = _scan_secrets(repo, staged)
    if findings:
        first = findings[0]
        detail = "%s (pattern %s, line %s)" % (
            first.get("path", "?"), first.get("pattern", "?"),
            first.get("line", "?"))
        return {"ok": False,
                "error": "secret detected in staged files: %s "
                         "(+%d more finding(s)), commit refused"
                         % (detail, len(findings) - 1)}

    try:
        _git(repo, "commit", "-m", message.strip())
    except GitOpsError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        commit_hash = _git(repo, "rev-parse", "HEAD").strip()
    except GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "commit_hash": commit_hash}


def branch_guard(repo: str, protected=("main", "master")) -> bool:
    """True when on a protected branch -> the agent MUST create a new branch,
    never commit straight to it.

    Raises GitOpsError when the current branch can't be read.
    """
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return branch in protected


def create_branch(repo: str, name: str) -> dict:
    """Create and check out a new branch.

    Name validation: only [a-z0-9-_/], must start with a letter/digit.
    Returns {"ok": True, "branch": name} or {"ok": False, "error": str}.
    """
    if not name or not _BRANCH_RE.match(name):
        return {"ok": False,
                "error": "invalid branch name: %r "
                         "(only a-z 0-9 - _ / allowed)" % (name,)}
    if name.endswith("/") or ".." in name:
        return {"ok": False,
                "error": "invalid branch name: %r" % (name,)}
    try:
        _git(repo, "checkout", "-b", name)
    except GitOpsError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "branch": name}


def last_commit_info(repo: str) -> dict:
    """HEAD commit info: {hash, message, author, date}.

    Raises GitOpsError when the repo has no commits yet.
    """
    try:
        out = _git(repo, "log", "-1",
                   "--format=%H%n%s%n%an%n%ad", "--date=iso")
    except GitOpsError:
        raise GitOpsError("repo has no commits yet")
    lines = out.splitlines()
    if not lines or not lines[0].strip():
        raise GitOpsError("repo has no commits yet")
    return {
        "hash": lines[0].strip(),
        "message": lines[1].strip() if len(lines) > 1 else "",
        "author": lines[2].strip() if len(lines) > 2 else "",
        "date": lines[3].strip() if len(lines) > 3 else "",
    }
