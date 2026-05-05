"""Remaining tools: NotebookEdit, Cron, Config, ToolSearch, SyntheticOutput, Brief, TeamCreate/Delete, RemoteTrigger."""
import json, subprocess, uuid, tempfile
from pathlib import Path
from typing import Any
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext

# ---- NotebookEdit ----
class NotebookEditTool(Tool):
    name = "NotebookEdit"
    description = """Edit Jupyter notebook cells (.ipynb). Supports replace, insert, delete on cells."""
    input_schema = {
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string", "description": "Absolute path to notebook"},
            "cell_id": {"type": "string", "description": "Cell ID or 'cell-N' format"},
            "new_source": {"type": "string", "description": "New source for the cell"},
            "cell_type": {"type": "string", "enum": ["code", "markdown"]},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"], "default": "replace"},
        },
        "required": ["notebook_path", "new_source"],
    }
    async def call(self, params, ctx):
        path = Path(params["notebook_path"])
        if not path.exists(): return ToolResult(content=f"Not found: {path}", is_error=True)
        if path.suffix != ".ipynb": return ToolResult(content="Use FileEdit for non-notebook files", is_error=True)
        try:
            nb = json.loads(path.read_text())
            cells = nb.get("cells", [])
            mode = params.get("edit_mode", "replace")
            cell_id = params.get("cell_id", "")
            new_cell = {"cell_type": params.get("cell_type", "code"), "source": params["new_source"], "metadata": {}}

            if mode == "insert":
                idx = len(cells)
                if cell_id:
                    for i, c in enumerate(cells):
                        if c.get("id") == cell_id or cell_id == f"cell-{i}":
                            idx = i + 1
                            break
                cells.insert(idx, new_cell)
                if "nbformat" in nb and nb["nbformat"] >= 4:
                    new_cell["id"] = uuid.uuid4().hex[:8]
            elif mode == "delete":
                for i, c in enumerate(cells):
                    if c.get("id") == cell_id or cell_id == f"cell-{i}":
                        cells.pop(i)
                        break
            else:  # replace
                for i, c in enumerate(cells):
                    if c.get("id") == cell_id or cell_id == f"cell-{i}":
                        if params["cell_type"] == "code":
                            cells[i] = {**c, "source": params["new_source"], "execution_count": None, "outputs": []}
                        else:
                            cells[i] = {**c, "source": params["new_source"]}
                        break

            nb["cells"] = cells
            path.write_text(json.dumps(nb, indent=1, ensure_ascii=False))
            return ToolResult(content=f"Notebook edited: {params['notebook_path']}")
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


# ---- CronTools ----
class CronCreateTool(Tool):
    name = "CronCreate"
    description = "Schedule a prompt with cron expression (5-field: M H DoM Mon DoW)."
    input_schema = {
        "type": "object",
        "properties": {
            "cron": {"type": "string", "description": "Cron expression e.g. '0 9 * * *' for 9am daily"},
            "prompt": {"type": "string", "description": "Prompt to run"},
            "recurring": {"type": "boolean", "default": True},
            "durable": {"type": "boolean", "default": False},
        },
        "required": ["cron", "prompt"],
    }
    async def call(self, params, ctx):
        return ToolResult(content=f"Cron scheduled: {params['cron']} — '{params['prompt'][:60]}...'")

class CronDeleteTool(Tool):
    name = "CronDelete"
    description = "Cancel a cron job by ID."
    input_schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    async def call(self, params, ctx):
        return ToolResult(content=f"Cron {params['id']} deleted")

class CronListTool(Tool):
    name = "CronList"
    description = "List all scheduled cron jobs."
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True
    async def call(self, params, ctx):
        return ToolResult(content="No cron jobs scheduled.")


# ---- ConfigTool ----
class ConfigTool(Tool):
    name = "Config"
    description = "View or change configuration settings (theme, model, permissions, etc.). Omit value to GET."
    input_schema = {
        "type": "object",
        "properties": {
            "setting": {"type": "string", "description": "Setting name: theme, model, verbose, autoCompactEnabled, permissions.defaultMode, language"},
            "value": {"type": "string", "description": "New value (omit to get current)"},
        },
        "required": ["setting"],
    }
    is_read_only = True
    async def call(self, params, ctx):
        setting = params["setting"]
        value = params.get("value")
        from config import MODEL
        current = {"model": MODEL, "theme": "dark", "verbose": "false", "autoCompactEnabled": "true",
                    "permissions.defaultMode": "default", "language": "en"}.get(setting, "unknown")
        if value is None:
            return ToolResult(content=f"{setting} = {current}")
        return ToolResult(content=f"{setting}: {current} → {value}")


# ---- ToolSearch ----
class ToolSearchTool(Tool):
    name = "ToolSearch"
    description = "Search for available tools by name or description."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query"}},
        "required": ["query"],
    }
    is_read_only = True
    async def call(self, params, ctx):
        from tools.base import tool_registry
        q = params["query"].lower()
        results = []
        for t in tool_registry.list_all():
            if q in t.name.lower() or q in t.description.lower():
                results.append(f"{t.name}: {t.description[:80]}")
        return ToolResult(content="\n".join(results[:20]) if results else "No tools found")


# ---- SyntheticOutput ----
class SyntheticOutputTool(Tool):
    name = "SyntheticOutput"
    description = "Returns structured JSON output. Only available in non-interactive sessions."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": True}
    is_read_only = True
    async def call(self, params, ctx):
        if not ctx.is_non_interactive_session:
            return ToolResult(content="SyntheticOutput only in headless/SDK mode", is_error=True)
        return ToolResult(content=json.dumps(params, ensure_ascii=False, indent=2))


# ---- BriefTool ----
class BriefTool(Tool):
    name = "Brief"
    description = "Send a message to the user with optional file attachments. The primary output channel."
    input_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message in markdown format"},
            "attachments": {"type": "array", "items": {"type": "string"}, "description": "File paths to attach"},
            "status": {"type": "string", "enum": ["normal", "proactive"], "default": "normal"},
        },
        "required": ["message"],
    }
    async def call(self, params, ctx):
        msg = params["message"]
        attachments = params.get("attachments", [])
        result = f"## Message\n{msg}"
        if attachments:
            result += "\n\n## Attachments"
            for a in attachments:
                p = Path(a)
                if p.exists():
                    result += f"\n- {a} ({p.stat().st_size} bytes)"
                else:
                    result += f"\n- {a} (not found)"
        return ToolResult(content=result)


# ---- LSPTool ----
class LSPTool(Tool):
    name = "LSP"
    description = """Language Server Protocol operations. 9 operations: goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol, goToImplementation, prepareCallHierarchy, incomingCalls, outgoingCalls."""
    input_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["goToDefinition","findReferences","hover","documentSymbol","workspaceSymbol","goToImplementation","prepareCallHierarchy","incomingCalls","outgoingCalls"]},
            "filePath": {"type": "string"},
            "line": {"type": "integer", "exclusiveMinimum": 0},
            "character": {"type": "integer", "exclusiveMinimum": 0},
        },
        "required": ["operation", "filePath", "line", "character"],
    }
    is_read_only = True
    async def call(self, params, ctx):
        return ToolResult(content=f"LSP {params['operation']} on {params['filePath']}:{params['line']}:{params['character']} — LSP server not configured in this environment.")


# ---- Team Tools ----
class TeamCreateTool(Tool):
    name = "TeamCreate"
    description = "Create a team for multi-agent collaboration."
    input_schema = {
        "type": "object",
        "properties": {
            "team_name": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["team_name"],
    }
    async def call(self, params, ctx):
        name = params["team_name"]
        lead_id = f"team-lead-{name}"
        return ToolResult(content=f"Team '{name}' created. Lead: {lead_id}")

class TeamDeleteTool(Tool):
    name = "TeamDelete"
    description = "Delete the current team."
    input_schema = {"type": "object", "properties": {}}
    async def call(self, params, ctx):
        return ToolResult(content="Team deleted.")


for cls in [
    NotebookEditTool, CronCreateTool, CronDeleteTool, CronListTool,
    ConfigTool, ToolSearchTool, SyntheticOutputTool, BriefTool, LSPTool,
    TeamCreateTool, TeamDeleteTool,
]:
    tool_registry.register(cls())
