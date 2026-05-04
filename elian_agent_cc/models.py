"""
Claude Code Python Backend - Message & Core Data Models

Ported from TypeScript source. These types mirror the internal message system
that drives the entire agentic conversation loop.

All types match the TypeScript definitions in:
  - src/types/message.js (auto-generated, re-exported by Tool.ts)
  - src/Tool.ts (ToolUseContext, PermissionResult, ToolPermissionContext)
  - src/QueryEngine.ts (QueryEngineConfig, SDKMessage variants)
  - src/utils/processUserInput/processUserInput.ts (ProcessUserInputContext)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Literal
import asyncio
import uuid


# =============================================================================
# Content Block Types (matching Anthropic SDK content blocks)
# =============================================================================

@dataclass
class TextBlock:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass
class ToolUseBlock:
    type: Literal["tool_use"] = "tool_use"
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = ""
    content: str = ""
    is_error: bool = False


@dataclass
class ImageBlock:
    type: Literal["image"] = "image"
    source: dict[str, Any] = field(default_factory=dict)


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | ImageBlock | dict[str, Any]  # type: ignore[misc]


# =============================================================================
# Message Types (matching TypeScript Message union)
# =============================================================================

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass(kw_only=True)
class Message:
    """Base message - all messages have these fields."""
    role: str = ""  # "user" | "assistant" | "system"
    content: str | list[ContentBlock] = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    type: str = ""  # Message discriminator

    # Metadata
    uuid: str = field(default_factory=lambda: uuid.uuid4().hex)
    is_meta: bool = False
    is_synthetic: bool = False
    is_replay: bool = False
    session_id: str = ""
    parent_tool_use_id: str | None = None


@dataclass
class UserMessage(Message):
    """User input message."""
    role: str = "user"
    type: str = "user"
    is_compact_summary: bool = False
    tool_use_result: bool = False  # True when this is actually tool results


@dataclass
class AssistantMessage(Message):
    """Model response message."""
    role: str = "assistant"
    type: str = "assistant"
    stop_reason: str | None = None
    is_api_error_message: bool = False
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class SystemMessage(Message):
    """System event message with subtypes."""
    role: str = "system"
    type: str = "system"
    subtype: str = ""  # "compact_boundary", "api_error", "local_command", "init", etc.
    compact_metadata: dict[str, Any] | None = None
    retry_attempt: int = 0
    max_retries: int = 0
    retry_delay_ms: int = 0
    error_status: int | None = None
    error: str | None = None


@dataclass
class ProgressMessage(Message):
    """Tool execution progress."""
    role: str = "system"
    type: str = "progress"
    progress_type: str = ""  # "bash_progress", "agent_tool_progress", etc.
    progress_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachmentMessage(Message):
    """Attachment message (memory, skill discovery, etc.)."""
    role: str = "system"
    type: str = "attachment"
    attachment_type: str = ""  # "memory", "skill_discovery", "structured_output", etc.
    attachment_data: Any = None


@dataclass
class ToolUseSummaryMessage(Message):
    """Summary of tool usage for the next turn."""
    role: str = "system"
    type: str = "tool_use_summary"
    summary: str = ""
    preceding_tool_use_ids: list[str] = field(default_factory=list)


@dataclass
class StreamEvent:
    """Low-level streaming event from the API."""
    type: str  # "message_start", "content_block_start", "content_block_delta", "message_delta", "message_stop"
    event: dict[str, Any] = field(default_factory=dict)


@dataclass
class TombstoneMessage(Message):
    """Control signal to remove a message from the conversation."""
    role: str = "system"
    type: str = "tombstone"
    target_uuid: str = ""


# Full discriminated union type
AnyMessage = UserMessage | AssistantMessage | SystemMessage | ProgressMessage | AttachmentMessage | ToolUseSummaryMessage | TombstoneMessage  # type: ignore[misc]


# =============================================================================
# Tool Permission Types (matching src/types/permissions.ts)
# =============================================================================

class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"
    AUTO = "auto"
    DONT_ASK = "dontAsk"


@dataclass(frozen=True)
class PermissionResult:
    """Result of a permission check."""
    behavior: Literal["allow", "deny", "ask"]
    message: str = ""
    updated_input: dict[str, Any] | None = None
    updated_permission_mode: PermissionMode | None = None

    @staticmethod
    def allow() -> "PermissionResult":
        return PermissionResult(behavior="allow")

    @staticmethod
    def deny(reason: str) -> "PermissionResult":
        return PermissionResult(behavior="deny", message=reason)

    @staticmethod
    def ask(reason: str = "") -> "PermissionResult":
        return PermissionResult(behavior="ask", message=reason)


@dataclass
class ToolPermissionRulesBySource:
    """Permission rules organized by source."""
    command: dict[str, list[str]] = field(default_factory=dict)
    settings: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ToolPermissionContext:
    """Full permission context for tool execution. Matches TypeScript ToolPermissionContext."""
    mode: PermissionMode = PermissionMode.DEFAULT
    additional_working_directories: set[str] = field(default_factory=set)
    always_allow_rules: dict[str, list[str]] = field(default_factory=dict)
    always_deny_rules: dict[str, list[str]] = field(default_factory=dict)
    always_ask_rules: dict[str, list[str]] = field(default_factory=dict)
    is_bypass_permissions_mode_available: bool = False
    is_auto_mode_available: bool = False
    stripped_dangerous_rules: dict[str, list[str]] | None = None
    should_avoid_permission_prompts: bool = False
    await_automated_checks_before_dialog: bool = False
    pre_plan_mode: PermissionMode | None = None


# =============================================================================
# Tool Use Context (matching src/Tool.ts ToolUseContext - 30+ fields)
# =============================================================================

@dataclass
class ToolUseContext:
    """Complete execution context passed to every tool. 30+ fields matching TypeScript."""
    # Core
    cwd: str = "."
    session_id: str = ""
    agent_id: str = ""
    agent_type: str = ""

    # Abort
    abort_controller: asyncio.Event | None = None

    # File state
    read_file_state: dict[str, str] = field(default_factory=dict)

    # Permission
    tool_permission_context: ToolPermissionContext = field(default_factory=ToolPermissionContext)

    # State get/set
    _app_state: dict[str, Any] = field(default_factory=dict)

    def get_app_state(self) -> dict[str, Any]:
        return self._app_state

    def set_app_state(self, updater: Callable[[dict], dict]) -> None:
        self._app_state = updater(self._app_state)

    # Tool JSX (replaced by notifications in headless mode)
    set_tool_jsx: Callable[[dict | None], None] | None = None

    # Notifications
    add_notification: Callable[[dict[str, Any]], None] | None = None

    # System message
    append_system_message: Callable[[str, str], None] | None = None

    # OS notification
    send_os_notification: Callable[[str, str], None] | None = None

    # Nested memory tracking
    nested_memory_attachment_triggers: set[str] = field(default_factory=set)
    loaded_nested_memory_paths: set[str] = field(default_factory=set)

    # Skill discovery
    dynamic_skill_dir_triggers: set[str] = field(default_factory=set)
    discovered_skill_names: set[str] = field(default_factory=set)

    # In-progress tool tracking
    in_progress_tool_use_ids: set[str] = field(default_factory=set)
    has_interruptible_tool_in_progress: bool = False

    # Response tracking
    response_length: int = 0

    # Options (matches ToolUseContext.options)
    commands: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    debug: bool = False
    verbose: bool = False
    main_loop_model: str = ""
    thinking_config: dict[str, Any] = field(default_factory=dict)
    mcp_clients: list[Any] = field(default_factory=list)
    mcp_resources: dict[str, list[Any]] = field(default_factory=dict)
    is_non_interactive_session: bool = False
    agent_definitions: dict[str, Any] = field(default_factory=dict)
    max_budget_usd: float | None = None
    custom_system_prompt: str | None = None
    append_system_prompt: str | None = None
    query_source: str = ""
    theme: str = "dark"

    # Hooks
    can_use_tool: Callable[..., Any] | None = None
    require_can_use_tool: bool = False
    request_prompt: Callable[..., Any] | None = None

    # File history
    update_file_history_state: Callable[[Any], Any] | None = None
    update_attribution_state: Callable[[Any], Any] | None = None

    # Additional
    user_modified: bool = False
    set_in_progress_tool_use_ids: Callable[[Callable[[set[str]], set[str]]], None] | None = None
    set_response_length: Callable[[Callable[[int], int]], None] | None = None
    set_stream_mode: Callable[[str], None] | None = None
    set_has_interruptible_tool_in_progress: Callable[[bool], None] | None = None
    open_message_selector: Callable[[], None] | None = None
    set_conversation_id: Callable[[str], None] | None = None


@dataclass
class ValidationResult:
    is_valid: bool
    message: str = ""
    error_code: int = 0


# =============================================================================
# Query Engine Types
# =============================================================================

@dataclass
class NonNullableUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0


EMPTY_USAGE = NonNullableUsage()


@dataclass
class PermissionDenial:
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any]


@dataclass
class QueryEngineConfig:
    """Full QueryEngine configuration (matching TypeScript QueryEngineConfig)."""
    cwd: str = "."
    model: str = "claude-sonnet-4-6"
    provider: str = "anthropic"
    fallback_model: str | None = None
    max_turns: int = 50
    max_budget_usd: float | None = None
    task_budget: dict[str, int] | None = None
    custom_system_prompt: str | None = None
    append_system_prompt: str | None = None
    json_schema: dict[str, Any] | None = None
    tools: list[str] | None = None
    verbose: bool = False
    replay_user_messages: bool = False
    include_partial_messages: bool = False
    session_id: str = ""
    abort_controller: asyncio.Event | None = None


# =============================================================================
# Process User Input Types (matching processUserInput.ts)
# =============================================================================

@dataclass
class ProcessUserInputResult:
    """Result from processUserInput() - matches TypeScript return type."""
    messages: list[AnyMessage] = field(default_factory=list)
    should_query: bool = True
    allowed_tools: dict[str, list[str]] | None = None
    model: str | None = None
    result_text: str | None = None
    next_input: str | None = None
    submit_next_input: bool = False
    effort: str | None = None


# =============================================================================
# Stream Event Types (SDKMessage variants yielded by QueryEngine)
# =============================================================================

@dataclass
class SDKResult:
    """Final result yielded by QueryEngine.submitMessage()."""
    type: str = "result"
    subtype: str = "success"  # "success" | "error_max_turns" | "error_max_budget" | "error_during_execution" | "error_max_structured_output_retries"
    is_error: bool = False
    duration_ms: float = 0.0
    duration_api_ms: float = 0.0
    num_turns: int = 0
    result: str = ""
    stop_reason: str | None = None
    session_id: str = ""
    total_cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    model_usage: dict[str, int] = field(default_factory=dict)
    permission_denials: list[PermissionDenial] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    uuid: str = field(default_factory=lambda: uuid.uuid4().hex)
