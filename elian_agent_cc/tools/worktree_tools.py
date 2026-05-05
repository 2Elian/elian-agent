"""Worktree isolation tools: EnterWorktree, ExitWorktree."""
import subprocess, uuid
from pathlib import Path
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext


class EnterWorktreeTool(Tool):
    name = "EnterWorktree"
    description = """Create an isolated git worktree. The agent works in a separate copy of the repo.
Changes are only visible to the agent. On exit, changes can be kept or discarded."""
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Optional name for the worktree branch"},
            "path": {"type": "string", "description": "Path to existing worktree to enter (instead of creating)"},
        },
    }
    is_read_only = False

    async def call(self, params, ctx):
        worktree_name = params.get("name") or f"agent-worktree-{uuid.uuid4().hex[:8]}"
        worktree_path = f"/tmp/claude-worktrees/{worktree_name}"

        try:
            r = subprocess.run(
                ["git", "worktree", "add", worktree_path, "-b", worktree_name],
                cwd=ctx.cwd, capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return ToolResult(content=f"Worktree created at {worktree_path}\nBranch: {worktree_name}")
            # Try detached
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", worktree_path, "HEAD"],
                cwd=ctx.cwd, capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return ToolResult(content=f"Worktree created (detached) at {worktree_path}")
            return ToolResult(content=f"Failed: {r.stderr}", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class ExitWorktreeTool(Tool):
    name = "ExitWorktree"
    description = """Exit the current worktree. action: 'keep' to preserve, 'remove' to delete."""
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["keep", "remove"], "description": "keep or remove the worktree"},
            "discard_changes": {"type": "boolean", "default": False, "description": "Force remove with uncommitted changes"},
        },
        "required": ["action"],
    }
    is_read_only = False

    async def call(self, params, ctx):
        action = params.get("action", "remove")
        discard = params.get("discard_changes", False)
        if action == "keep":
            return ToolResult(content="Worktree kept. Branch and files preserved.")
        try:
            flag = ["--force"] if discard else []
            r = subprocess.run(
                ["git", "worktree", "remove", ctx.cwd, *flag],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return ToolResult(content="Worktree removed.")
            return ToolResult(content=f"Remove failed: {r.stderr}\nUse discard_changes=true to force.", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


tool_registry.register(EnterWorktreeTool())
tool_registry.register(ExitWorktreeTool())
