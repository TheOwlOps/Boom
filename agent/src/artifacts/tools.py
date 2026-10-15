"""Tool dicts for the environment: artifact_markdown, artifact_html, artifact_csv,
artifact_json. Each one builds a finished document under <root>/artifacts/ via
DocumentBuilder; failures come back as {"error": ...}, never a faked path."""

from typing import Any, Dict, List, Optional

from artifacts.documents import DocumentBuilder


def _schema(properties: Optional[Dict[str, Any]] = None,
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def build_tools(env) -> Dict[str, Dict[str, Any]]:
    """Four artifact tools, in the same {"name","func","description","schema"} shape as
    the cron/browser modules. Without a root_dir on the environment the tools report
    the missing directory instead of writing somewhere surprising."""
    root_dir = getattr(env, "root_dir", None)
    builder = DocumentBuilder(root_dir) if root_dir is not None else None

    def _need_builder() -> Optional[Dict[str, Any]]:
        if builder is None:
            return {"error": "environment has no root_dir: nowhere to keep artifacts"}
        return None

    def _markdown(title: str, body_md: str, filename: Optional[str] = None,
                  overwrite: bool = False) -> Dict[str, Any]:
        err = _need_builder()
        return err if err else builder.build_markdown(title, body_md, filename, overwrite)

    def _html(title: str, body_html: str, filename: Optional[str] = None,
              overwrite: bool = False) -> Dict[str, Any]:
        err = _need_builder()
        return err if err else builder.build_html(title, body_html, filename, overwrite)

    def _csv(filename: str, rows: List[Dict[str, Any]],
             overwrite: bool = False) -> Dict[str, Any]:
        err = _need_builder()
        return err if err else builder.build_csv(filename, rows, overwrite)

    def _json(filename: str, data: Any, overwrite: bool = False) -> Dict[str, Any]:
        err = _need_builder()
        return err if err else builder.build_json(filename, data, overwrite)

    return {
        "artifact_markdown": {
            "name": "artifact_markdown",
            "func": _markdown,
            "description": (
                "Build a Markdown report file under artifacts/ (e.g. a summary or briefing). "
                "Returns the file path and byte size. A bare filename is auto-derived from "
                "the title when omitted."
            ),
            "schema": _schema(
                {
                    "title": {"type": "string", "description": "Report title, used as the # header."},
                    "body_md": {"type": "string", "description": "Markdown body of the report."},
                    "filename": {"type": "string", "description": "Optional bare filename, e.g. 'report.md'."},
                    "overwrite": {"type": "boolean", "description": "Replace the file if it exists."},
                },
                ["title", "body_md"],
            ),
        },
        "artifact_html": {
            "name": "artifact_html",
            "func": _html,
            "description": (
                "Build a complete standalone HTML page under artifacts/. The title is escaped; "
                "the body is the supplied HTML fragment. Returns the file path and byte size."
            ),
            "schema": _schema(
                {
                    "title": {"type": "string", "description": "Page title (goes in <title>, escaped)."},
                    "body_html": {"type": "string", "description": "HTML fragment for the page body."},
                    "filename": {"type": "string", "description": "Optional bare filename, e.g. 'page.html'."},
                    "overwrite": {"type": "boolean", "description": "Replace the file if it exists."},
                },
                ["title", "body_html"],
            ),
        },
        "artifact_csv": {
            "name": "artifact_csv",
            "func": _csv,
            "description": (
                "Build a CSV table under artifacts/ from a list of dicts. The header is the "
                "ordered union of the row keys; missing cells are empty. "
                "Returns the file path and byte size."
            ),
            "schema": _schema(
                {
                    "filename": {"type": "string", "description": "Bare filename, e.g. 'data.csv'."},
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Table rows as a list of dicts.",
                    },
                    "overwrite": {"type": "boolean", "description": "Replace the file if it exists."},
                },
                ["filename", "rows"],
            ),
        },
        "artifact_json": {
            "name": "artifact_json",
            "func": _json,
            "description": (
                "Build a pretty-printed JSON file under artifacts/ from any JSON-serializable "
                "data. Returns the file path and byte size."
            ),
            "schema": _schema(
                {
                    "filename": {"type": "string", "description": "Bare filename, e.g. 'data.json'."},
                    "data": {"description": "JSON-serializable data to write."},
                    "overwrite": {"type": "boolean", "description": "Replace the file if it exists."},
                },
                ["filename", "data"],
            ),
        },
    }
