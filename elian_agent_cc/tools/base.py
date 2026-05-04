"""
Tool system base classes - matches TypeScript Tool.ts architecture.

Every tool implements: call(), checkPermissions(), validateInput(),
to_schema(), description(), isReadOnly(), isConcurrencySafe().
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from models import (
    ToolUseContext, PermissionResult, ValidationResult,
    ToolPermissionContext, PermissionMode,
)


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    tool_name: str = ""
    tool_use_id: str = ""
    new_messages: list = field(default_factory=list)




class Tool(ABC):
    """Abstract tool matching TypeScript Tool interface.
    Each subclass must define: name, description, input_schema, call().
    """
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    is_open_world: bool = True
    is_mcp: bool = False
    max_result_size_chars: int = 30000

    @abstractmethod
    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult: ...

    def check_permissions(self, params: dict[str, Any], context: ToolUseContext) -> PermissionResult:
        return PermissionResult.allow()

    def validate_input(self, params: dict[str, Any], context: ToolUseContext) -> ValidationResult:
        required = self.input_schema.get("required", [])
        for key in required:
            if key not in params:
                return ValidationResult(False, f"Missing required parameter: {key}")
        return ValidationResult(True)

    def is_destructive(self, params: dict[str, Any]) -> bool:
        return not self.is_read_only

    def to_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def truncate_result(self, content: str) -> str:
        if len(content) <= self.max_result_size_chars:
            return content
        half = self.max_result_size_chars // 2
        return content[:half] + f"\n... [truncated {len(content) - self.max_result_size_chars} chars] ...\n" + content[-half:]

    def map_tool_result_to_block(self, content: str, tool_use_id: str) -> dict:
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def find(self, pattern: str) -> Tool | None:
        if pattern in self._tools:
            return self._tools[pattern]
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            for k, v in self._tools.items():
                if k.startswith(prefix):
                    return v
        return None

    def list_all(self) -> list[Tool]:
        return list(self._tools.values())

    def list_schemas(self) -> list[dict]:
        return [t.to_schema() for t in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools


tool_registry = ToolRegistry()
