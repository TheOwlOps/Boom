from pathlib import Path
from typing import Dict, Any
try:
    from agent.src.tools.fuzzy_patch import fuzzy_find_and_replace
except ModuleNotFoundError:
    from tools.fuzzy_patch import fuzzy_find_and_replace

def read_file(path: str, offset: int = 1, limit: int = 2000) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {"error": f"File '{path}' does not exist", "lines": []}

    lines = file_path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    start_idx = max(0, offset - 1)
    end_idx = min(total, start_idx + limit)
    selected = lines[start_idx:end_idx]

    formatted = "\n".join(f"{start_idx + i + 1}|{line}" for i, line in enumerate(selected))
    return {
        "content": formatted,
        "total_lines": total,
        "offset": offset,
        "limit": limit
    }

def write_file(path: str, content: str) -> Dict[str, Any]:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {"path": str(file_path), "bytes_written": len(content.encode("utf-8")), "success": True}

def patch_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {"error": f"File '{path}' does not exist", "success": False}

    content = file_path.read_text(encoding="utf-8")
    updated, match_count, strategy, err = fuzzy_find_and_replace(content, old_string, new_string, replace_all)
    if err:
        return {"error": err, "matches": match_count, "success": False}

    file_path.write_text(updated, encoding="utf-8")
    return {
        "path": str(file_path),
        "patched": True,
        "matches": match_count,
        "strategy": strategy,
        "success": True
    }

def search_files(pattern: str, path: str = ".", target: str = "content", limit: int = 50) -> Dict[str, Any]:
    import re
    root = Path(path)
    if not root.exists():
        return {"error": f"Path '{path}' not found", "matches": []}

    matches = []
    if target == "files":
        for p in root.rglob("*"):
            if re.search(pattern, p.name):
                matches.append(str(p))
                if len(matches) >= limit:
                    break
        return {"target": "files", "matches": matches, "total": len(matches)}
    else:
        # Search content
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".rs", ".ts", ".tsx", ".json", ".toml", ".md", ".txt"):
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                    for line_no, line in enumerate(text.splitlines(), start=1):
                        if re.search(pattern, line):
                            matches.append({"file": str(p), "line": line_no, "text": line})
                            if len(matches) >= limit:
                                break
                except Exception:
                    continue
            if len(matches) >= limit:
                break
        return {"target": "content", "matches": matches, "total": len(matches)}
