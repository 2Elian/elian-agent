"""File tools: Read, Write, Edit, Glob, Grep."""
from pathlib import Path
from tools.base import Tool, ToolResult, tool_registry
from models import ToolUseContext


class FileReadTool(Tool):
    name = "Read"
    description = "Reads a file. Also reads PDF, images, Jupyter notebooks. Use offset/limit for large files."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute file path"},
            "offset": {"type": "integer", "description": "Start line (0-indexed)"},
            "limit": {"type": "integer", "description": "Max lines to read"},
        },
        "required": ["file_path"],
    }
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, params, context):
        path = Path(params["file_path"])
        if not path.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            o = params.get("offset") or 0
            lim = params.get("limit")
            if lim: lines = lines[o:o + lim]
            elif o: lines = lines[o:]
            formatted = [f"{o+i+1}\t{l.rstrip()}" for i, l in enumerate(lines)]
            content = "\n".join(formatted) or "(empty file)"
            if hasattr(context, 'read_file_state'):
                context.read_file_state[str(path)] = content
            return ToolResult(content=content)
        except Exception as e:
            return ToolResult(content=f"Read error: {e}", is_error=True)


class FileWriteTool(Tool):
    name = "Write"
    description = "Writes a file. Overwrites if exists. Prefer Edit for modifications."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["file_path", "content"],
    }

    async def call(self, params, context):
        try:
            p = Path(params["file_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(params["content"], encoding="utf-8")
            return ToolResult(content=f"File written: {params['file_path']}")
        except Exception as e:
            return ToolResult(content=f"Write error: {e}", is_error=True)


class FileEditTool(Tool):
    name = "Edit"
    description = "Exact string replacement in files. Use replace_all to replace all occurrences."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File to modify"},
            "old_string": {"type": "string", "description": "Text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    async def call(self, params, context):
        path = Path(params["file_path"])
        if not path.exists():
            return ToolResult(content=f"File not found: {path}", is_error=True)
        old, new = params["old_string"], params["new_string"]
        replace_all = params.get("replace_all", False)
        if old == new:
            return ToolResult(content="old_string and new_string are identical", is_error=True)
        try:
            content = path.read_text(encoding="utf-8")
            count = content.count(old)
            if count == 0:
                return ToolResult(content="old_string not found", is_error=True)
            if not replace_all and count > 1:
                return ToolResult(content=f"Found {count} occurrences. Use more context or replace_all.", is_error=True)
            nc = content.replace(old, new) if replace_all else content.replace(old, new, 1)
            path.write_text(nc, encoding="utf-8")
            return ToolResult(content=f"File edited: {params['file_path']}")
        except Exception as e:
            return ToolResult(content=f"Edit error: {e}", is_error=True)


class GlobTool(Tool):
    name = "Glob"
    description = "Fast file pattern matching. Supports **/*.js, src/**/*.ts etc."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Search directory"},
        },
        "required": ["pattern"],
    }
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, params, context):
        import glob as g
        try:
            results = g.glob(params["pattern"], root_dir=params.get("path", "."), recursive=True)
            return ToolResult(content="\n".join(sorted(results)[:200]) or "No matches")
        except Exception as e:
            return ToolResult(content=f"Glob error: {e}", is_error=True)


class GrepTool(Tool):
    name = "Grep"
    description = "Search code with regex. Supports glob filtering and output modes."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory to search"},
            "glob": {"type": "string", "description": 'File filter, e.g. "*.py"'},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "default": "files_with_matches"},
            "-i": {"type": "boolean", "description": "Case insensitive"},
            "head_limit": {"type": "integer", "default": 50},
        },
        "required": ["pattern"],
    }
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, params, context):
        import re, fnmatch
        try:
            compiled = re.compile(params["pattern"], re.IGNORECASE if params.get("-i") else 0)
        except re.error as e:
            return ToolResult(content=f"Invalid regex: {e}", is_error=True)

        search_root = Path(params.get("path", "."))
        if not search_root.exists():
            return ToolResult(content=f"Path not found: {search_root}", is_error=True)
        glob_filter = params.get("glob")
        output_mode = params.get("output_mode", "files_with_matches")
        head_limit = params.get("head_limit", 50)
        results = []
        files = list(search_root.rglob("*"))[:500] if search_root.is_dir() else [search_root]
        for f in files:
            if not f.is_file(): continue
            if glob_filter and not fnmatch.fnmatch(f.name, glob_filter): continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines()):
                    if compiled.search(line):
                        if output_mode == "files_with_matches":
                            if str(f) not in results: results.append(str(f)); break
                        else:
                            results.append(f"{f}:{i+1}: {line}")
            except Exception: continue
        if output_mode == "count": return ToolResult(content=f"{len(results)} matches")
        if head_limit and len(results) > head_limit: results = results[:head_limit]; results.append(f"... ({head_limit} shown)")
        return ToolResult(content="\n".join(results) if results else "No matches")


for cls in [FileReadTool, FileWriteTool, FileEditTool, GlobTool, GrepTool]:
    tool_registry.register(cls())
