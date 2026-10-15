"""Google Drive connector for Boom.

Real Google Drive REST API v3 client (https://www.googleapis.com/drive/v3).
Credential is a Bearer access token from BOOM_GOOGLE_DRIVE_ACCESS_TOKEN —
this module never performs an OAuth flow and never prompts for secrets.

Every tool returns {"error": ...} on failure: nothing raises to the caller,
nothing reports a success it did not observe.
"""

import json
import os
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from connectors.base import Connector, _schema

API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
TOKEN_ENV = "BOOM_GOOGLE_DRIVE_ACCESS_TOKEN"
_LIST_FIELDS = "files(id,name,mimeType,modifiedTime),nextPageToken"
_UPLOAD_FIELDS = "id,name,mimeType,size"
# A turn's context is finite: cap inline text, say so, keep the byte size.
_MAX_TEXT_CHARS = 200_000


class _DriveAPIError(Exception):
    """Internal only: raised for HTTP/API failures, converted to {"error": ...}."""


def _escape_query_value(value: str) -> str:
    """Escape a string for embedding in a Drive `q` single-quoted literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _api_error_message(resp: httpx.Response) -> str:
    """Best-effort human message from a failed Drive response."""
    try:
        payload = resp.json()
        message = payload.get("error", {}).get("message", "")
        if message:
            return f"drive api {resp.status_code}: {message}"
    except Exception:
        pass
    text = (resp.text or "").strip().replace("\n", " ")[:300]
    return f"drive api {resp.status_code}: {text or resp.reason_phrase}"


class GoogleDriveConnector(Connector):
    """Google Drive. connected=True only when a token is configured."""

    name = "google_drive"

    def __init__(
        self,
        token: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 30.0,
    ):
        # `token` is an explicit override (tests); otherwise the env var wins.
        self._token = token
        self._transport = transport
        self._timeout = timeout

    @property
    def token(self) -> str:
        return (self._token if self._token is not None
                else os.environ.get(TOKEN_ENV, "")).strip()

    def status(self) -> Dict[str, Any]:
        if self.token:
            return {"connected": True,
                    "detail": f"access token present ({TOKEN_ENV})"}
        return {"connected": False,
                "detail": f"missing env {TOKEN_ENV}"}

    # ------------------------------------------------------------------ http

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET a Drive API path, returning parsed JSON. Raises _DriveAPIError."""
        self._require_token()
        try:
            with self._client() as client:
                resp = client.get(API_BASE + path, params=params or {})
        except httpx.HTTPError as e:
            raise _DriveAPIError(f"drive request failed: {type(e).__name__}: {e}")
        if resp.status_code >= 400:
            raise _DriveAPIError(_api_error_message(resp))
        try:
            return resp.json()
        except Exception as e:
            raise _DriveAPIError(f"drive returned non-JSON: {e}")

    def _require_token(self) -> None:
        if not self.token:
            raise _DriveAPIError(
                f"google drive not connected: set {TOKEN_ENV}")

    @staticmethod
    def _file_dict(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "mimeType": item.get("mimeType"),
            "modifiedTime": item.get("modifiedTime"),
        }

    # ------------------------------------------------------------------ tools

    def build_tools(self) -> Dict[str, Dict[str, Any]]:
        connector = self

        def drive_list(query: str = "trashed=false",
                       page_size: int = 20) -> Dict[str, Any]:
            """List Drive files: id, name, mimeType, modifiedTime (newest first)."""
            try:
                page_size = max(1, min(int(page_size or 20), 100))
                payload = connector._get("/files", params={
                    "q": (query or "trashed=false").strip() or "trashed=false",
                    "pageSize": page_size,
                    "fields": _LIST_FIELDS,
                    "orderBy": "modifiedTime desc",
                })
                files = [connector._file_dict(f)
                         for f in payload.get("files", [])]
                out: Dict[str, Any] = {"files": files, "count": len(files)}
                if payload.get("nextPageToken"):
                    out["next_page_token"] = payload["nextPageToken"]
                return out
            except _DriveAPIError as e:
                return {"error": str(e)}
            except (ValueError, TypeError) as e:
                return {"error": f"drive_list: bad argument: {e}"}

        def drive_search(name: str) -> Dict[str, Any]:
            """Find Drive files whose name contains `name` (not trashed)."""
            try:
                needle = (name or "").strip()
                if not needle:
                    return {"error": "drive_search needs a name"}
                q = (f"name contains '{_escape_query_value(needle)}' "
                     f"and trashed=false")
                payload = connector._get("/files", params={
                    "q": q,
                    "pageSize": 20,
                    "fields": _LIST_FIELDS,
                    "orderBy": "modifiedTime desc",
                })
                files = [connector._file_dict(f)
                         for f in payload.get("files", [])]
                return {"files": files, "count": len(files)}
            except _DriveAPIError as e:
                return {"error": str(e)}

        def drive_download(file_id: str) -> Dict[str, Any]:
            """Download a file's bytes. Text is inlined (capped); binary
            returns size only. Google Workspace native files (Docs/Sheets)
            need export and report an honest error."""
            try:
                fid = (file_id or "").strip()
                if not fid:
                    return {"error": "drive_download needs a file_id"}
                connector._require_token()
                try:
                    with connector._client() as client:
                        resp = client.get(
                            f"{API_BASE}/files/{quote(fid, safe='')}",
                            params={"alt": "media"})
                except httpx.HTTPError as e:
                    raise _DriveAPIError(
                        f"drive request failed: {type(e).__name__}: {e}")
                if resp.status_code >= 400:
                    raise _DriveAPIError(_api_error_message(resp))
                data = resp.content
                size = len(data)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    return {"file_id": fid, "size": size,
                            "text": None, "binary": True,
                            "note": "content is not utf-8 text"}
                truncated = len(text) > _MAX_TEXT_CHARS
                return {"file_id": fid, "size": size,
                        "text": text[:_MAX_TEXT_CHARS],
                        "truncated": truncated, "binary": False}
            except _DriveAPIError as e:
                return {"error": str(e)}

        def drive_upload(name: str, content: str,
                         mime_type: str = "text/plain",
                         folder_id: Optional[str] = None) -> Dict[str, Any]:
            """Upload content as a new Drive file (multipart upload).
            `content` is text; `folder_id` puts it in that folder."""
            try:
                fname = (name or "").strip()
                if not fname:
                    return {"error": "drive_upload needs a name"}
                connector._require_token()
                mtype = (mime_type or "text/plain").strip() or "text/plain"
                if isinstance(content, (bytes, bytearray)):
                    body = bytes(content)
                else:
                    body = str(content if content is not None else "").encode("utf-8")
                metadata: Dict[str, Any] = {"name": fname, "mimeType": mtype}
                if (folder_id or "").strip():
                    metadata["parents"] = [folder_id.strip()]
                boundary = uuid.uuid4().hex
                head_meta = (f"--{boundary}\r\n"
                             f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                             ).encode("ascii")
                head_media = (f"\r\n--{boundary}\r\n"
                              f"Content-Type: {mtype}\r\n\r\n").encode("ascii")
                tail = f"\r\n--{boundary}--\r\n".encode("ascii")
                payload = (head_meta + json.dumps(metadata).encode("utf-8")
                           + head_media + body + tail)
                try:
                    with connector._client() as client:
                        resp = client.post(
                            f"{UPLOAD_BASE}/files",
                            params={"uploadType": "multipart",
                                    "fields": _UPLOAD_FIELDS},
                            content=payload,
                            headers={"Content-Type":
                                     f'multipart/related; boundary="{boundary}"'})
                except httpx.HTTPError as e:
                    raise _DriveAPIError(
                        f"drive request failed: {type(e).__name__}: {e}")
                if resp.status_code >= 400:
                    raise _DriveAPIError(_api_error_message(resp))
                try:
                    created = resp.json()
                except Exception as e:
                    raise _DriveAPIError(f"drive returned non-JSON: {e}")
                return {"id": created.get("id"), "name": created.get("name"),
                        "mimeType": created.get("mimeType"),
                        "size": created.get("size")}
            except _DriveAPIError as e:
                return {"error": str(e)}

        return {
            "drive_list": {
                "name": "drive_list",
                "func": drive_list,
                "description": ("List Google Drive files (id, name, mimeType, "
                                "modifiedTime, newest first)."),
                "schema": _schema(
                    {
                        "query": {"type": "string",
                                  "description": "Drive search query, default 'trashed=false'."},
                        "page_size": {"type": "integer",
                                      "description": "Max files to return (1-100, default 20)."},
                    },
                    [],
                ),
            },
            "drive_search": {
                "name": "drive_search",
                "func": drive_search,
                "description": "Find Drive files whose name contains the given text.",
                "schema": _schema(
                    {"name": {"type": "string",
                              "description": "Text to match inside file names."}},
                    ["name"],
                ),
            },
            "drive_download": {
                "name": "drive_download",
                "func": drive_download,
                "description": ("Download a Drive file's content. Text is inlined "
                                "(capped at 200k chars); binary returns size only."),
                "schema": _schema(
                    {"file_id": {"type": "string",
                                 "description": "Drive file id from drive_list/drive_search."}},
                    ["file_id"],
                ),
            },
            "drive_upload": {
                "name": "drive_upload",
                "func": drive_upload,
                "description": "Upload text content as a new Google Drive file.",
                "schema": _schema(
                    {
                        "name": {"type": "string",
                                 "description": "File name on Drive."},
                        "content": {"type": "string",
                                    "description": "Text content to upload."},
                        "mime_type": {"type": "string",
                                      "description": "MIME type, default 'text/plain'."},
                        "folder_id": {"type": "string",
                                      "description": "Optional Drive folder id to upload into."},
                    },
                    ["name", "content"],
                ),
            },
        }


# Module-level instance picked up by connectors.registry.load_default_registry().
CONNECTOR = GoogleDriveConnector()
