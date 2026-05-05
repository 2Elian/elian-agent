"""Task management tools: Create, Get, List, Update.

Ported from: TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool, TodoWriteTool
"""
from dataclasses import dataclass, field
from typing import Any
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext


@dataclass
class TaskItem:
    id: str; subject: str; description: str = ""
    status: str = "pending"; owner: str = ""
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)


class TaskStore:
    """In-memory task store shared across all task tools."""
    _tasks: dict[str, TaskItem] = {}

    @classmethod
    def create(cls, subject: str, description: str) -> TaskItem:
        import uuid
        tid = f"task-{uuid.uuid4().hex[:6]}"
        t = TaskItem(id=tid, subject=subject, description=description)
        cls._tasks[tid] = t
        return t

    @classmethod
    def get(cls, tid: str) -> TaskItem | None:
        return cls._tasks.get(tid)

    @classmethod
    def list_all(cls) -> list[TaskItem]:
        return list(cls._tasks.values())

    @classmethod
    def update(cls, tid: str, **kw) -> TaskItem | None:
        t = cls._tasks.get(tid)
        if t:
            for k, v in kw.items():
                if hasattr(t, k): setattr(t, k, v)
        return t

    @classmethod
    def delete(cls, tid: str) -> None:
        cls._tasks.pop(tid, None)


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = "Create a structured task to track progress on complex multi-step work."
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Brief actionable title"},
            "description": {"type": "string", "description": "What needs to be done"},
            "activeForm": {"type": "string", "description": "Present continuous form for spinner"},
        },
        "required": ["subject", "description"],
    }
    is_read_only = False
    async def call(self, params, ctx):
        t = TaskStore.create(params["subject"], params["description"])
        return ToolResult(content=f"Task created: [{t.id}] {t.subject}")

class TaskGetTool(Tool):
    name = "TaskGet"
    description = "Retrieve a task by ID with full details and dependencies."
    input_schema = {
        "type": "object",
        "properties": {"taskId": {"type": "string", "description": "Task ID"}},
        "required": ["taskId"],
    }
    is_read_only = True
    async def call(self, params, ctx):
        t = TaskStore.get(params["taskId"])
        if not t: return ToolResult(content=f"Task not found: {params['taskId']}", is_error=True)
        return ToolResult(content=f"Task [{t.id}]: {t.subject}\nStatus: {t.status}\nDescription: {t.description}\nBlocks: {t.blocks}\nBlocked by: {t.blocked_by}")

class TaskListTool(Tool):
    name = "TaskList"
    description = "List all tasks in the task list."
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True
    async def call(self, params, ctx):
        tasks = TaskStore.list_all()
        if not tasks: return ToolResult(content="No tasks.")
        lines = []
        for t in tasks:
            icon = {"pending": " ", "in_progress": ">", "completed": "x", "deleted": "-"}.get(t.status, "?")
            lines.append(f"  [{icon}] [{t.id}] {t.subject}")
        return ToolResult(content="Tasks:\n" + "\n".join(lines))

class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = "Update task status or details. Status: pending→in_progress→completed. Use 'deleted' to remove."
    input_schema = {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
            "subject": {"type": "string"},
            "addBlocks": {"type": "array", "items": {"type": "string"}},
            "addBlockedBy": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["taskId"],
    }
    async def call(self, params, ctx):
        t = TaskStore.update(params["taskId"], **{k: v for k, v in params.items() if k not in ("taskId", "addBlocks", "addBlockedBy")})
        if not t: return ToolResult(content=f"Task not found: {params['taskId']}", is_error=True)
        if "addBlocks" in params:
            t.blocks.extend(params["addBlocks"])
        if "addBlockedBy" in params:
            t.blocked_by.extend(params["addBlockedBy"])
        status = params.get("status", t.status)
        return ToolResult(content=f"Task [{t.id}] → {status}")

class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = "Create a session todo list."
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending","in_progress","completed"]}},
                "required": ["id", "content", "status"],
            }},
        },
        "required": ["todos"],
    }
    async def call(self, params, ctx):
        todos = params.get("todos", [])[:20]
        lines = ["## Todo"]
        for t in todos:
            icon = {"pending":" ","in_progress":"▸","completed":"✓"}.get(t.get("status","pending")," ")
            lines.append(f"- [{icon}] {t['content']}")
        return ToolResult(content="\n".join(lines))


for cls in [TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool, TodoWriteTool]:
    tool_registry.register(cls())
