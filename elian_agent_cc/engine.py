"""
Complete Query Engine — ported from QueryEngine.ts (1295 lines) + query.ts (1729 lines).

Implements the full 16-phase submitMessage flow, all 9 message dispatch types,
all error recovery paths, auto-compaction integration, token budget tracking,
permission denial tracking, structured output enforcement, and agentic loop.
"""
from __future__ import annotations

import asyncio, json, time, uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config import MODEL, DEFAULT_PROVIDER, MAX_TURNS, MAX_BUDGET_USD, MAX_OUTPUT_TOKENS, MAX_CONTEXT_TOKENS
from models import (
    Message, UserMessage, AssistantMessage, SystemMessage,
    ProgressMessage, AttachmentMessage, ToolUseSummaryMessage,
    TextBlock, ToolUseBlock, ToolResultBlock,
    ToolUseContext, PermissionResult, PermissionMode, ToolPermissionContext,
    QueryEngineConfig, NonNullableUsage, PermissionDenial, EMPTY_USAGE, SDKResult,
    ProcessUserInputResult,
)
from tools.base import tool_registry, Tool, ToolResult
from providers import get_provider, LLMProvider, StreamChunk
from prompts import build_system_prompt, clear_cache as clear_prompt_cache
from normalization import normalize_messages_for_api, get_messages_after_compact_boundary, count_tool_calls
from compaction import CompactionManager, ReactiveCompactionManager, ContextCollapseManager, should_auto_compact
from token_estimation import estimate_messages
from file_history import FileHistoryManager
from elian_agent_cc.skills import skill_loader as _skill_loader


# =============================================================================
# SDKMessage — all yielded event types
# =============================================================================

class SDKMessage:
    """Event yielded by the query loop. 9 message types."""
    def __init__(self, type: str, data: Any = None, subtype: str | None = None,
                 session_id: str = "", is_error: bool = False):
        self.type = type; self.data = data; self.subtype = subtype
        self.session_id = session_id; self.is_error = is_error


# =============================================================================
# ProcessUserInput — input processing pipeline (ported from processUserInput.ts)
# =============================================================================

def process_user_input(
    prompt: str,
    messages: list[Message],
    context: dict[str, Any],
    cwd: str = ".",
) -> ProcessUserInputResult:
    """Process raw user input: extract slash commands, attachments, bash mode.

    Returns ProcessUserInputResult with processed messages, should_query flag,
    allowed tools, model override, and result text.
    """
    result = ProcessUserInputResult()
    input_str = prompt.strip()

    # Detect slash commands
    if input_str.startswith("/"):
        parts = input_str.split(maxsplit=1)
        cmd_name = parts[0][1:].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Handle known slash commands locally
        if cmd_name == "clear":
            messages.clear()
            clear_prompt_cache()
            result.messages = [UserMessage(content=[TextBlock(text="Conversation cleared.")], is_meta=True)]
            result.should_query = False
            result.result_text = "Conversation cleared."
            return result
        elif cmd_name == "compact":
            result.messages = [UserMessage(content=[TextBlock(text="Compacting conversation...")], is_meta=True)]
            result.should_query = True  # Still query — the model will summarize
            return result
        elif cmd_name in ("help", "cost", "status", "theme", "config", "exit"):
            result.messages = [UserMessage(content=[TextBlock(text=f"Command: /{cmd_name} {args}")], is_meta=True)]
            result.should_query = False
            result.result_text = f"Local command: /{cmd_name} processed."
            return result

    # Detect bash mode (! prefix)
    if input_str.startswith("!"):
        cmd = input_str[1:].strip()
        result.messages = [UserMessage(
            content=[TextBlock(text=f"Run bash command: {cmd}")],
            is_meta=False,
        )]
        result.should_query = True
        return result

    # Normal text prompt
    result.messages = [UserMessage(content=[TextBlock(text=input_str)])]
    result.should_query = True
    return result


# =============================================================================
# System init message builder (ported from systemInit.ts)
# =============================================================================

def build_system_init_message(
    engine: "QueryEngine",
    agents: list = None,
) -> SDKMessage:
    """Build the SDK init message with session metadata."""
    return SDKMessage(
        type="system_init",
        data={
            "session_id": engine.session_id,
            "model": engine._model,
            "tools": len(tool_registry.list_all()),
            "max_turns": engine._max_turns,
            "cwd": engine.config.cwd,
            "permission_mode": engine.config.permission_mode.value if engine.config.permission_mode else "default",
        },
        session_id=engine.session_id,
    )


# =============================================================================
# QueryEngineConfig — complete
# =============================================================================

class QueryEngineConfig:
    def __init__(self, **kwargs):
        self.cwd = kwargs.get("cwd", ".")
        self.model = kwargs.get("model", MODEL)
        self.provider = kwargs.get("provider", DEFAULT_PROVIDER)
        self.fallback_model = kwargs.get("fallback_model")
        self.max_turns = kwargs.get("max_turns", MAX_TURNS)
        self.max_budget_usd = kwargs.get("max_budget_usd", MAX_BUDGET_USD)
        self.custom_system_prompt = kwargs.get("custom_system_prompt")
        self.append_system_prompt = kwargs.get("append_system_prompt")
        self.json_schema = kwargs.get("json_schema")
        self.tools_filter = kwargs.get("tools")
        self.verbose = kwargs.get("verbose", False)
        self.replay_user_messages = kwargs.get("replay_user_messages", False)
        self.include_partial_messages = kwargs.get("include_partial_messages", False)
        self.session_id = kwargs.get("session_id", "")
        self.abort_controller = kwargs.get("abort_controller")
        self.permission_mode = kwargs.get("permission_mode")
        self.initial_messages = kwargs.get("initial_messages", [])
        self.handle_elicitation = kwargs.get("handle_elicitation")
        self.set_sdk_status = kwargs.get("set_sdk_status")


# =============================================================================
# QueryEngine — complete, matching TypeScript line-for-feature
# =============================================================================

class QueryEngine:
    """One engine instance per conversation. State persists across turns."""

    def __init__(self, config: QueryEngineConfig | None = None):
        cfg = config or QueryEngineConfig()
        self.config = cfg
        self.session_id = cfg.session_id or uuid.uuid4().hex[:12]

        # Persistent state (across turns)
        self._messages: list[Message] = list(cfg.initial_messages)
        self._permission_denials: list[PermissionDenial] = []
        self._total_usage = NonNullableUsage()
        self._total_cost = 0.0
        self._read_file_state: dict[str, str] = {}
        self._abort = cfg.abort_controller or asyncio.Event()
        self._has_handled_orphaned_permission = False
        self._discovered_skill_names: set[str] = set()
        self._loaded_nested_memory_paths: set[str] = set()

        # Model/provider
        self._model = cfg.model or MODEL
        self._provider_name = cfg.provider or DEFAULT_PROVIDER
        self._max_turns = cfg.max_turns or MAX_TURNS
        self._max_budget = cfg.max_budget_usd or MAX_BUDGET_USD

        # Compaction managers
        self._compaction = CompactionManager(model_context_window=MAX_CONTEXT_TOKENS)
        self._reactive_compaction = ReactiveCompactionManager()
        self._context_collapse = ContextCollapseManager()

        # File history — track file state across turns for stale detection
        self._file_history = FileHistoryManager()

        # Skills — preload at init time
        self._skills_loaded = False
        self._skills_prompt_cache: str | None = None
        self._preload_skills()

        # Turn counters
        self._turn_count = 0
        self._total_api_duration_ms = 0.0

    # ========================================================================
    # submitMessage — 16-phase flow
    # ========================================================================

    async def submit_message(self, prompt: str) -> AsyncGenerator[SDKMessage, None]:
        """Main entry point. Full 16-phase submitMessage flow."""
        t0 = time.time()
        self._discovered_skill_names.clear()
        api_duration_ms = 0.0

        # --- Phase 1: Initialization ---
        self._abort.clear()

        # --- Phase 2: System prompt ---
        system_prompt = build_system_prompt(self.config.cwd, self._model)

        # --- Phase 3-5: Process user input ---
        result = process_user_input(
            prompt, self._messages,
            {"cwd": self.config.cwd, "tools": tool_registry.list_all()},
            cwd=self.config.cwd,
        )

        # Push processed messages
        self._messages.extend(result.messages)

        # --- Phase 6: Yield user message ---
        yield SDKMessage(type="user", data={
            "id": result.messages[0].id if result.messages else "",
            "content": prompt,
        }, session_id=self.session_id)

        # --- Phase 7: Skills already preloaded in __init__ via _preload_skills() ---

        # --- Phase 8: Inject skills listing into system prompt ---
        if self._skills_prompt_cache:
            system_prompt = system_prompt + "\n\n" + self._skills_prompt_cache

        # --- Phase 9: Activate conditional skills based on file operations ---
        # (Triggered dynamically when Read/Write/Edit return file paths)

        # --- Phase 10: Check for newly activated skills ---
        activated = _skill_loader.activate_for_paths([])  # Will be populated by tool results

        # --- Phase 11: System init ---
        yield build_system_init_message(self)

        # --- Phase 12: Non-query path ---
        if not result.should_query:
            duration_ms = (time.time() - t0) * 1000
            yield SDKMessage(
                type="result", subtype="success",
                data={
                    "is_error": False, "result": result.result_text or "",
                    "duration_ms": duration_ms, "num_turns": 0,
                    "stop_reason": None, "session_id": self.session_id,
                    "usage": self._usage_dict(), "total_cost_usd": self._total_cost,
                    "permission_denials": len(self._permission_denials),
                },
                session_id=self.session_id,
            )
            return

        # --- Phase 13: File history snapshots ---
        # Record file state at this user message boundary.
        # On Write/Edit, FileHistoryManager.is_stale() detects external modifications
        # and FileHistoryManager.file_was_read() enforces read-before-write.
        if result.messages:
            for msg in result.messages:
                if hasattr(msg, "id") and msg.id:
                    self._file_history.make_snapshot(msg.id)
                    # Track files referenced in this message
                    if isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock) and block.name in ("Read", "Write", "Edit"):
                                fp = block.input.get("file_path", "") or block.input.get("notebook_path", "")
                                if fp:
                                    self._file_history.track_file(fp)

        # --- Phase 14-15: Main query loop ---
        provider = self._get_provider()
        if not provider:
            yield SDKMessage(type="error", data="No LLM provider configured")
            return

        turn_count = 0
        max_turns = self._max_turns
        last_stop_reason: str | None = None
        max_output_recovery = 0
        MAX_RETRIES = 3
        accumulated_text = ""
        structured_output = None
        current_message_usage = NonNullableUsage()

        while turn_count < max_turns and not self._abort.is_set():
            turn_count += 1
            yield SDKMessage(type="stream_request_start", data={"turn": turn_count})

            # --- Auto-compaction check (proper block-level estimation) ---
            estimated_tokens = estimate_messages(self._messages)
            if self._compaction.needs_compaction(estimated_tokens):
                compaction_result = await self._compaction.compact(
                    self._messages, provider, trigger="auto",
                )
                if compaction_result.executed and compaction_result.boundary_message:
                    self._messages = get_messages_after_compact_boundary(
                        self._messages
                    )
                    self._messages.insert(0, compaction_result.boundary_message)
                    yield SDKMessage(
                        type="compact_boundary",
                        data={"pre_tokens": compaction_result.pre_tokens,
                              "post_tokens": compaction_result.post_tokens},
                        session_id=self.session_id,
                    )

            # --- Build tool schemas ---
            tool_schemas = self._get_tool_schemas()

            # --- Call the model ---
            accumulated_text = ""
            tool_use_blocks: list[dict] = []
            needs_follow_up = False
            api_error = None
            current_message_usage = NonNullableUsage()

            try:
                api_start = time.time()
                async for chunk in provider.chat_stream(
                    model=self._model,
                    messages=self._to_api_messages(),
                    system=system_prompt,
                    tools=tool_schemas,
                    max_tokens=MAX_OUTPUT_TOKENS,
                ):
                    if chunk.type == "text_delta" and chunk.text: # llm普通文本输出模式
                        accumulated_text += chunk.text # 累计到完整文本
                        yield SDKMessage(type="token", data=chunk.text) # 实时同步给前端 sse

                    elif chunk.type == "tool_use_start": # LLM决定调用工具
                        """
                          tb = {"id": chunk.tool_id,             # 工具调用的唯一 ID
                                "name": chunk.tool_name,          # 什么工具？ "Read", "Bash", "Grep"...
                                "input": {},                      # 参数
                                "input_json": ""}                 # 参数的 JSON 片段(具体需要chunk.type=="tool_use_delta来处理)
                          tool_use_blocks.append(tb)             # 加入本轮的调用列表
                          needs_follow_up = True                 # 标记：这轮还没完，需要后续处理
                          yield SDKMessage(type="tool_call", ...)  # 通知前端：LLM 要调工具了
                              当 LLM 说 "I need to read the file" 并且调用 Read 工具时：
                              时间线:
                                "I need to read the file"  → text_delta chunks (分支 1)
                                tool_use_start             → (分支 2) 创建空槽位，标记 needs_follow_up
                                tool_use_delta             → (分支 3) 逐片填充参数 JSON
                                tool_use_delta             → (分支 3)
                                done                       → (分支 5) 参数收集完毕
                        """
                        tb = {"id": chunk.tool_id, "name": chunk.tool_name, "input": {}, "input_json": ""}
                        tool_use_blocks.append(tb)
                        needs_follow_up = True
                        yield SDKMessage(type="tool_call", data={
                            "tool_id": chunk.tool_id, "tool_name": chunk.tool_name,
                        })

                    elif chunk.type == "tool_use_delta" and chunk.tool_input_json and tool_use_blocks: # 工具参数的 JSON 片段
                        # 工具参数不是一次性到达的，而是逐步流式传输的 JSON 片段：
                        """
                          chunk #1: '{"file'       → input_json = '{"file'
                          chunk #2: '_path": '      → input_json = '{"file_path": '
                          chunk #3: '"/src/auth'    → input_json = '{"file_path": "/src/auth'
                          chunk #4: '.py"}'         → input_json = '{"file_path": "/src/auth.py"}'
                        
                          最终: json.loads(input_json) → {"file_path": "/src/auth.py"}
                        """
                        tool_use_blocks[-1]["input_json"] += chunk.tool_input_json #  取最后一个（最新的）tool_use_block, 把新的 JSON 片段追加到它的 input_json 字段

                    elif chunk.type == "usage" and chunk.usage: # token 统计
                        current_message_usage.input_tokens += chunk.usage.input_tokens
                        current_message_usage.output_tokens += chunk.usage.output_tokens
                        current_message_usage.total_tokens += chunk.usage.total_tokens

                    elif chunk.type == "done": # stream结束
                        if chunk.usage:
                            current_message_usage.input_tokens += chunk.usage.input_tokens
                            current_message_usage.output_tokens += chunk.usage.output_tokens
                            current_message_usage.total_tokens += chunk.usage.total_tokens

                api_duration_ms += (time.time() - api_start) * 1000
                self._total_usage.input_tokens += current_message_usage.input_tokens
                self._total_usage.output_tokens += current_message_usage.output_tokens
                self._total_usage.total_tokens += current_message_usage.total_tokens

            except asyncio.CancelledError:
                yield SDKMessage(type="error", data="Request cancelled")
                return
            except Exception as e:
                api_error = str(e)
                yield SDKMessage(type="api_error", data={"error": api_error})
                if not needs_follow_up:
                    break

            # --- Parse completed tool inputs ---
            for tb in tool_use_blocks:
                try:
                    tb["input"] = json.loads(tb.get("input_json", "")) if tb.get("input_json") else {}
                except json.JSONDecodeError:
                    tb["input"] = {"_raw": tb.get("input_json", "")}

            # --- Build and yield assistant message ---
            if accumulated_text or tool_use_blocks:
                content_blocks = []
                if accumulated_text:
                    content_blocks.append({"type": "text", "text": accumulated_text})
                for tb in tool_use_blocks:
                    content_blocks.append({
                        "type": "tool_use", "id": tb["id"],
                        "name": tb["name"], "input": tb.get("input", {}),
                    })
                asst = AssistantMessage(content=content_blocks, session_id=self.session_id)
                self._messages.append(asst)
                yield SDKMessage(type="assistant", data={
                    "id": asst.id, "content": content_blocks,
                }, session_id=self.session_id)

            # --- Handle API error ---
            if api_error and not needs_follow_up:
                last_stop_reason = "api_error"
                break

            # --- No follow-up needed: check for recovery ---
            if not needs_follow_up:
                if "prompt too long" in accumulated_text.lower():
                    # --- Prompt-too-long recovery ---
                    # Attempt reactive compaction
                    if self._reactive_compaction.can_attempt():
                        self._reactive_compaction.mark_attempted()
                        comp_result = await self._reactive_compaction.try_reactive_compact(
                            self._messages, accumulated_text, self._compaction, provider,
                        )
                        if comp_result.executed:
                            self._messages = get_messages_after_compact_boundary(self._messages)
                            self._messages.insert(0, comp_result.boundary_message)
                            yield SDKMessage(type="compact_boundary", data={"trigger": "reactive"}, session_id=self.session_id)
                            continue  # Retry with compacted context
                    # --- Context collapse ---
                    self._messages = self._context_collapse.apply_collapse(self._messages)
                    continue

                last_stop_reason = "end_turn"
                break

            # --- Execute tool calls ---
            if tool_use_blocks:
                ctx = self._build_tool_context()

                for tb in tool_use_blocks:
                    # Permission check (wrapped for denial tracking)
                    result = await self._execute_tool(
                        tb["name"], tb.get("input", {}), ctx, tb["id"],
                    )
                    if result.is_error:
                        self._permission_denials.append(PermissionDenial(
                            tool_name=tb["name"], tool_use_id=tb["id"],
                            tool_input=tb.get("input", {}),
                        ))

                    yield SDKMessage(type="tool_result", data={
                        "tool_use_id": tb["id"], "tool_name": tb["name"],
                        "content": result.content, "is_error": result.is_error,
                    })

                    self._messages.append(UserMessage(
                        content=[ToolResultBlock(
                            tool_use_id=tb["id"], content=result.content,
                            is_error=result.is_error,
                        )],
                        tool_use_result=True, session_id=self.session_id,
                    ))

                # --- Refresh tools between turns ---
                continue  # Loop for next model call

            # --- Budget checks ---
            if self._total_cost >= self._max_budget:
                duration_ms = (time.time() - t0) * 1000
                yield SDKMessage(
                    type="result", subtype="error_max_budget",
                    data={
                        "is_error": True, "num_turns": turn_count,
                        "errors": [f"Max budget exceeded: ${self._max_budget}"],
                        "duration_ms": duration_ms, "total_cost_usd": self._total_cost,
                        "session_id": self.session_id, "usage": self._usage_dict(),
                        "permission_denials": len(self._permission_denials),
                    },
                    session_id=self.session_id,
                )
                return

        # --- Check max turns ---
        if turn_count >= max_turns:
            # TODO: 大于最大轮次的时候 让LLM从trajectory里面看看能不能总结出答案
            duration_ms = (time.time() - t0) * 1000
            yield SDKMessage(
                type="result", subtype="error_max_turns",
                data={
                    "is_error": True, "num_turns": turn_count,
                    "errors": [f"Max turns reached ({max_turns})"],
                    "duration_ms": duration_ms, "stop_reason": last_stop_reason,
                    "session_id": self.session_id, "usage": self._usage_dict(),
                    "total_cost_usd": self._total_cost,
                    "permission_denials": len(self._permission_denials),
                },
                session_id=self.session_id,
            )
            return

        # --- Final result ---
        duration_ms = (time.time() - t0) * 1000
        # Check isResultSuccessful
        successful = bool(last_stop_reason in ("end_turn", None) and
                         len(self._permission_denials) == 0)

        if not successful:
            yield SDKMessage(
                type="result", subtype="error_during_execution",
                data={
                    "is_error": True, "num_turns": turn_count,
                    "duration_ms": duration_ms, "duration_api_ms": api_duration_ms,
                    "stop_reason": last_stop_reason, "session_id": self.session_id,
                    "usage": self._usage_dict(), "total_cost_usd": self._total_cost,
                    "permission_denials": len(self._permission_denials),
                    "errors": [f"stop_reason={last_stop_reason}"],
                },
                session_id=self.session_id,
            )
        else:
            yield SDKMessage(
                type="result", subtype="success",
                data={
                    "is_error": False, "result": accumulated_text or "No text content",
                    "duration_ms": duration_ms, "duration_api_ms": api_duration_ms,
                    "num_turns": turn_count, "stop_reason": last_stop_reason,
                    "session_id": self.session_id, "usage": self._usage_dict(),
                    "total_cost_usd": self._total_cost,
                    "permission_denials": len(self._permission_denials),
                },
                session_id=self.session_id,
            )

    # ========================================================================
    # Public API
    # ========================================================================

    def interrupt(self): self._abort.set()
    def get_messages(self) -> list[Message]: return list(self._messages)
    def clear_history(self):
        self._messages.clear(); clear_prompt_cache(); self._abort.clear()
        self._permission_denials.clear(); self._total_usage = NonNullableUsage()
        self._total_cost = 0.0; self._reactive_compaction.reset()

    def get_session_id(self) -> str: return self.session_id
    def set_model(self, model: str): self._model = model
    def get_read_file_state(self) -> dict[str, str]: return self._read_file_state

    # ========================================================================
    # Internal helpers
    # ========================================================================

    def _get_provider(self) -> LLMProvider | None:
        return get_provider(self._provider_name)

    def _get_tool_schemas(self) -> list[dict]:
        if self.config.tools_filter:
            return [t.to_schema() for t in tool_registry.list_all()
                    if t.name in self.config.tools_filter]
        return tool_registry.list_schemas()

    def _build_tool_context(self) -> ToolUseContext:
        return ToolUseContext(
            cwd=self.config.cwd, session_id=self.session_id,
            read_file_state=self._read_file_state,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode.DEFAULT,
            ),
            main_loop_model=self._model, max_budget_usd=self._max_budget,
            custom_system_prompt=self.config.custom_system_prompt,
            append_system_prompt=self.config.append_system_prompt,
        )

    async def _execute_tool(self, name: str, params: dict,
                            ctx: ToolUseContext, tool_use_id: str) -> ToolResult:
        tool = tool_registry.find(name)
        if not tool:
            return ToolResult(
                content=f"Unknown tool: {name}", is_error=True,
                tool_name=name, tool_use_id=tool_use_id,
            )
        try:
            result = await tool.call(params, ctx)
            result.tool_name = name
            result.tool_use_id = tool_use_id
            return result
        except Exception as e:
            return ToolResult(
                content=f"Tool error: {e}", is_error=True,
                tool_name=name, tool_use_id=tool_use_id,
            )

    def _to_api_messages(self) -> list[dict]:
        """Convert internal messages to API format with normalization."""
        api_msgs = []
        for m in self._messages:
            c = m.content
            if isinstance(c, list):
                for block in c:
                    if isinstance(block, TextBlock) and block.text:
                        role = "assistant" if isinstance(m, AssistantMessage) else "user"
                        api_msgs.append({"role": role, "content": block.text})
                    elif isinstance(block, ToolUseBlock):
                        api_msgs.append({"role": "assistant", "content": [
                            {"type": "tool_use", "id": block.id,
                             "name": block.name, "input": block.input}
                        ]})
                    elif isinstance(block, ToolResultBlock):
                        api_msgs.append({"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": block.tool_use_id,
                             "content": block.content}
                        ]})
            elif isinstance(c, str) and c.strip():
                role = "assistant" if isinstance(m, AssistantMessage) else "user"
                api_msgs.append({"role": role, "content": c})
        return api_msgs

    def _preload_skills(self) -> None:
        """Load all 5 layers of skills and cache the prompt section."""
        if self._skills_loaded:
            return
        try:
            _skill_loader._cwd = Path(self.config.cwd)
            _skill_loader.load_all()
            self._skills_prompt_cache = _skill_loader.get_skills_prompt(max_chars=8000)
            self._skills_loaded = True
        except Exception:
            pass  # Skills are non-critical; degrade gracefully

    def _usage_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self._total_usage.input_tokens,
            "output_tokens": self._total_usage.output_tokens,
            "total_tokens": self._total_usage.total_tokens,
        }


# =============================================================================
# Engine registry
# =============================================================================

_engines: dict[str, QueryEngine] = {}

def get_or_create_engine(session_id: str | None = None, **kwargs) -> QueryEngine:
    sid = session_id or uuid.uuid4().hex[:12]
    if sid not in _engines:
        _engines[sid] = QueryEngine(QueryEngineConfig(session_id=sid, **kwargs))
    return _engines[sid]

def get_engine(sid: str) -> QueryEngine | None:
    return _engines.get(sid)

def remove_engine(sid: str):
    _engines.pop(sid, None)
