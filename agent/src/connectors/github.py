"""GitHub connector for the Boom agent: issues, PRs, file reads, code search.

Credential: a personal access token in ``BOOM_GITHUB_TOKEN``. This module never
performs the OAuth flow itself — the operator creates a token (classic with
``repo`` scope, or fine-grained with contents/issues/pull-requests read access)
and exports it before starting the agent::

    export BOOM_GITHUB_TOKEN=<redacted>

Only stdlib + httpx. Every tool returns ``{"error": ...}`` on failure and
never raises to the caller; a success is only reported when the GitHub API
actually confirmed it.
"""

import base64
import binascii
import contextlib
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import httpx

from connectors.base import Connector, _schema
from connectors.credentials import get_token_for

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
# Cap decoded file bodies so one huge file cannot flood the agent context.
MAX_FILE_CHARS = 200_000


# ------------------------------------------------------------------ helpers


def _labels(item: Dict[str, Any]) -> List[str]:
    out = []
    for label in item.get("labels") or []:
        if isinstance(label, dict):
            out.append(str(label.get("name", "")))
        else:
            out.append(str(label))
    return [name for name in out if name]


def _api_error(resp: httpx.Response) -> str:
    """Best-effort human message out of a GitHub API error body."""
    try:
        body = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return f"GitHub HTTP {resp.status_code}" + (f": {text[:200]}" if text else "")
    message = body.get("message") if isinstance(body, dict) else None
    if message:
        return f"GitHub HTTP {resp.status_code}: {message}"
    return f"GitHub HTTP {resp.status_code}: unexpected response"


def _decode_content(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Decode a /contents file payload. Returns {"text", "truncated"} or {"error"}."""
    if isinstance(payload, list):
        return {"error": "path is a directory, not a file"}
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        return {"error": "unexpected /contents response shape"}
    raw = "".join(payload["content"].split())
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return {"error": "could not base64-decode the file content"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": "file is not UTF-8 text (binary files are not supported)"}
    truncated = len(text) > MAX_FILE_CHARS
    if truncated:
        text = text[:MAX_FILE_CHARS]
    return {"text": text, "truncated": truncated}


class GithubConnector(Connector):
    """GitHub REST API over httpx. `client` is injectable so tests never touch the network."""

    name = "github"

    def __init__(
        self,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        api_base: str = API_BASE,
        timeout: float = 30.0,
    ) -> None:
        # token=None means "read BOOM_GITHUB_TOKEN lazily at call time",
        # so an env change after import is still picked up. A non-None value
        # is an explicit override (used by tests with a fake transport).
        self._token_override = token
        self._injected_client = client
        self._api_base = (api_base or API_BASE).rstrip("/")
        self._timeout = timeout

    # -- config ------------------------------------------------------

    def _token(self) -> str:
        if self._token_override is not None:
            return self._token_override.strip()
        return get_token_for("github", "token")

    def status(self) -> Dict[str, Any]:
        if self._token():
            return {
                "connected": True,
                "detail": "BOOM_GITHUB_TOKEN is set; GitHub API ready.",
            }
        return {
            "connected": False,
            "detail": (
                "BOOM_GITHUB_TOKEN is not set: export a personal access token "
                "(classic scope `repo`, or fine-grained with contents/issues "
                "read access) to enable GitHub."
            ),
        }

    # -- HTTP --------------------------------------------------------

    @contextlib.contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        """Yield a client; an injected one is never closed by us."""
        if self._injected_client is not None:
            yield self._injected_client
        else:
            with httpx.Client(timeout=self._timeout) as client:
                yield client

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One GitHub API call. Parsed JSON on success, {"error": ...} otherwise."""
        token = self._token()
        if not token:
            return {
                "error": (
                    "BOOM_GITHUB_TOKEN is not set: export a personal access token "
                    "to use GitHub."
                )
            }
        url = f"{self._api_base}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        try:
            with self._session() as client:
                resp = client.request(method, url, params=params, headers=headers)
        except httpx.RequestError as exc:
            return {"error": f"GitHub request failed: {type(exc).__name__}: {exc}"}
        if resp.status_code >= 400:
            return {"error": _api_error(resp)}
        try:
            data = resp.json()
        except ValueError:
            return {"error": f"GitHub returned non-JSON (HTTP {resp.status_code})."}
        return data

    # -- tools -------------------------------------------------------

    def github_list_issues(
        self, owner: str, repo: str, state: str = "open"
    ) -> Dict[str, Any]:
        """List issues (pull requests are filtered out — use github_list_prs)."""
        owner, repo = (owner or "").strip(), (repo or "").strip()
        if not owner or not repo:
            return {"error": "github_list_issues needs owner and repo."}
        state = (state or "open").strip().lower()
        if state not in ("open", "closed", "all"):
            return {"error": "state must be one of: open, closed, all."}
        data = self._request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/issues",
            params={"state": state, "per_page": 30},
        )
        if "error" in data or not isinstance(data, list):
            return data if "error" in data else {"error": "unexpected issues response shape"}
        issues = [
            {
                "number": item.get("number"),
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "labels": _labels(item),
                "created_at": item.get("created_at", ""),
            }
            for item in data
            if isinstance(item, dict) and "pull_request" not in item
        ]
        return {"issues": issues, "total": len(issues)}

    def github_get_issue(
        self, owner: str, repo: str, number: int
    ) -> Dict[str, Any]:
        """One issue with its first comments."""
        owner, repo = (owner or "").strip(), (repo or "").strip()
        try:
            number = int(number)
        except (TypeError, ValueError):
            return {"error": "github_get_issue needs a numeric issue number."}
        if not owner or not repo or number <= 0:
            return {"error": "github_get_issue needs owner, repo and a positive number."}
        data = self._request("GET", f"/repos/{quote(owner)}/{quote(repo)}/issues/{number}")
        if "error" in data or not isinstance(data, dict):
            return data if "error" in data else {"error": "unexpected issue response shape"}
        comments_data = self._request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/issues/{number}/comments",
            params={"per_page": 5, "sort": "created", "direction": "asc"},
        )
        comments: List[Dict[str, Any]] = []
        if isinstance(comments_data, list):
            for comment in comments_data:
                if not isinstance(comment, dict):
                    continue
                author = comment.get("user") or {}
                comments.append({
                    "author": author.get("login", "") if isinstance(author, dict) else "",
                    "body": (comment.get("body") or "")[:2000],
                    "created_at": comment.get("created_at", ""),
                })
        return {
            "number": data.get("number"),
            "title": data.get("title", ""),
            "state": data.get("state", ""),
            "labels": _labels(data),
            "body": (data.get("body") or "")[:8000],
            "created_at": data.get("created_at", ""),
            "comments": comments,
        }

    def github_list_prs(
        self, owner: str, repo: str, state: str = "open"
    ) -> Dict[str, Any]:
        """List pull requests."""
        owner, repo = (owner or "").strip(), (repo or "").strip()
        if not owner or not repo:
            return {"error": "github_list_prs needs owner and repo."}
        state = (state or "open").strip().lower()
        if state not in ("open", "closed", "all"):
            return {"error": "state must be one of: open, closed, all."}
        data = self._request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/pulls",
            params={"state": state, "per_page": 30},
        )
        if "error" in data or not isinstance(data, list):
            return data if "error" in data else {"error": "unexpected pulls response shape"}
        prs = [
            {
                "number": item.get("number"),
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "draft": bool(item.get("draft")),
                "labels": _labels(item),
                "created_at": item.get("created_at", ""),
            }
            for item in data
            if isinstance(item, dict)
        ]
        return {"pull_requests": prs, "total": len(prs)}

    def github_read_file(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> Dict[str, Any]:
        """Read a text file's content at a ref (branch/tag/commit)."""
        owner, repo, path = (owner or "").strip(), (repo or "").strip(), (path or "").strip()
        if not owner or not repo:
            return {"error": "github_read_file needs owner and repo."}
        if not path or path.startswith("/") or ".." in path.split("/"):
            return {"error": "github_read_file needs a repo-relative file path."}
        ref = (ref or "main").strip()
        data = self._request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/contents/{quote(path)}",
            params={"ref": ref},
        )
        if "error" in data:
            return data
        decoded = _decode_content(data)
        if "error" in decoded:
            return decoded
        return {
            "path": path,
            "ref": ref,
            "size": data.get("size", 0),
            "sha": data.get("sha", ""),
            "content": decoded["text"],
            "truncated": decoded["truncated"],
        }

    def github_search_code(self, query: str) -> Dict[str, Any]:
        """Code search across repositories the token can access."""
        query = (query or "").strip()
        if not query:
            return {"error": "github_search_code needs a query string."}
        data = self._request(
            "GET", "/search/code", params={"q": query, "per_page": 10}
        )
        if "error" in data or not isinstance(data, dict):
            return data if "error" in data else {"error": "unexpected search response shape"}
        items = []
        for item in data.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            repo_info = item.get("repository") or {}
            items.append({
                "path": item.get("path", ""),
                "repository": repo_info.get("full_name", "") if isinstance(repo_info, dict) else "",
            })
        return {"items": items, "total_count": data.get("total_count", len(items))}

    # -- contract ----------------------------------------------------

    def build_tools(self, env: Any = None) -> Dict[str, Dict[str, Any]]:
        _ = env  # accepted for environment.py's factory(self) convention; unused.
        return {
            "github_list_issues": {
                "name": "github_list_issues",
                "func": self.github_list_issues,
                "description": (
                    "List issues of a repo (pull requests are excluded). "
                    "Returns number/title/state/labels per issue."
                ),
                "schema": _schema(
                    {
                        "owner": {"type": "string", "description": "Repo owner or org."},
                        "repo": {"type": "string", "description": "Repository name."},
                        "state": {
                            "type": "string",
                            "description": "open (default), closed or all.",
                        },
                    },
                    ["owner", "repo"],
                ),
            },
            "github_get_issue": {
                "name": "github_get_issue",
                "func": self.github_get_issue,
                "description": (
                    "Read one issue with its body and first comments."
                ),
                "schema": _schema(
                    {
                        "owner": {"type": "string", "description": "Repo owner or org."},
                        "repo": {"type": "string", "description": "Repository name."},
                        "number": {"type": "integer", "description": "Issue number."},
                    },
                    ["owner", "repo", "number"],
                ),
            },
            "github_list_prs": {
                "name": "github_list_prs",
                "func": self.github_list_prs,
                "description": (
                    "List pull requests of a repo. Returns number/title/state/"
                    "draft/labels per PR."
                ),
                "schema": _schema(
                    {
                        "owner": {"type": "string", "description": "Repo owner or org."},
                        "repo": {"type": "string", "description": "Repository name."},
                        "state": {
                            "type": "string",
                            "description": "open (default), closed or all.",
                        },
                    },
                    ["owner", "repo"],
                ),
            },
            "github_read_file": {
                "name": "github_read_file",
                "func": self.github_read_file,
                "description": (
                    "Read a text file from a repo at a ref (branch/tag/commit, "
                    "default main). Content is base64-decoded; large files are "
                    "truncated with a flag."
                ),
                "schema": _schema(
                    {
                        "owner": {"type": "string", "description": "Repo owner or org."},
                        "repo": {"type": "string", "description": "Repository name."},
                        "path": {"type": "string", "description": "Repo-relative file path."},
                        "ref": {
                            "type": "string",
                            "description": "Branch, tag or commit SHA (default main).",
                        },
                    },
                    ["owner", "repo", "path"],
                ),
            },
            "github_search_code": {
                "name": "github_search_code",
                "func": self.github_search_code,
                "description": (
                    "Search code across repositories the token can access. "
                    "Returns path + repository per hit."
                ),
                "schema": _schema(
                    {
                        "query": {
                            "type": "string",
                            "description": "Search query, e.g. 'BOOM_GITHUB_TOKEN repo:TheOwlOps/Boom'.",
                        },
                    },
                    ["query"],
                ),
            },
        }


def build_tools(env: Any = None) -> Dict[str, Dict[str, Any]]:
    """Module-level factory for environment.py's tool-module convention."""
    return GithubConnector().build_tools(env)


# The connectors registry (connectors.registry.load_default_registry) picks up
# each connector module through this instance.
CONNECTOR = GithubConnector()
