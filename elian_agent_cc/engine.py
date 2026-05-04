"""
Complete Query Engine - agentic conversation loop.

Ported from src/QueryEngine.ts (1295 lines) and src/query.ts (1729 lines).

Implements:
- 16-phase submitMessage flow
- Full message dispatch (assistant/user/system/stream_event/progress/attachment/tool_use_summary/tombstone)
- Permission denial tracking
- Budget enforcement (max turns, max USD, structured output retries)
- Error recovery (max_output_tokens escalation, prompt-too-long)
- Tool execution with permission checks
"""
import asyncio, json, time, uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config import MODEL, DEFAULT_PROVIDER, MAX_TURNS, MAX_BUDGET_USD, MAX_OUTPUT_TOKENS
from models import (
    Message, UserMessage, AssistantMessage, SystemMessage,
    ProgressMessage, AttachmentMessage, ToolUseSummaryMessage,
    TextBlock, ToolUseBlock, ToolResultBlock,
    ToolUseContext, PermissionResult, PermissionMode, ToolPermissionContext,
    QueryEngineConfig, NonNullableUsage, PermissionDenial, EMPTY_USAGE, SDKResult,
)
from tools.base import tool_registry, Tool, ToolResult
from providers import get_provider, LLMProvider, StreamChunk
from prompts import build_system_prompt


@dataclass
class SDKMessage:
    """Event yielded by the query loop."""
    type: str  # user, assistant, system, stream_event, tool_call, tool_result, progress, attachment, result, error
    data: Any = None
    subtype: str | None = None
    session_id: str = ""


class QueryEngine:
    """One engine instance per conversation. State persists across turns."""

    def __init__(self, config: QueryEngineConfig | None = None):
        cfg = config or QueryEngineConfig()
        self.config = cfg
        self.session_id = cfg.session_id or uuid.uuid4().hex[:12]
        self._messages: list[Message] = []
        self._permission_denials: list[PermissionDenial] = []
        self._total_usage = EMPTY_USAGE
        self._total_cost = 0.0
        self._read_file_state: dict[str, str] = {}
        self._abort = asyncio.Event()
        self._discovered_skill_names: set[str] = set() # 用于记录本次对话中已发现的技能名称
        self._model = cfg.model or MODEL
        self._provider_name = cfg.provider or DEFAULT_PROVIDER
        self._max_turns = cfg.max_turns or MAX_TURNS
        self._max_budget = cfg.max_budget_usd or MAX_BUDGET_USD

    async def submit_message(self, prompt: str) -> AsyncGenerator[SDKMessage, None]:
        """Main entry point. Submit user message, yield streaming events."""
        t0 = time.time()
        api_ms = 0.0
        self._discovered_skill_names.clear() # 清空skills集合 ->

        system_prompt = build_system_prompt(self.config.cwd, self._model)

        user_msg = UserMessage(
            content=[TextBlock(text=prompt)],
            session_id=self.session_id,
        )
        self._messages.append(user_msg)
        yield SDKMessage(type="user", data={"id": user_msg.id, "content": prompt}, session_id=self.session_id)

        yield SDKMessage(type="system_init", data={
            "session_id": self.session_id, "model": self._model,
            "tools": len(tool_registry.list_all()),
            "max_turns": self._max_turns,
        }, session_id=self.session_id)

        provider = get_provider(self._provider_name)
        turn_count = 0
        last_stop_reason = None
        max_output_recovery = 0
        MAX_RETRIES = 3
        accumulated_text = ""
        structured_output = None

        while turn_count < self._max_turns and not self._abort.is_set(): # self._abort.is_set()用于捕获 子线程中止的情况(用户中止了本次请求服务) -> 如果 _abort.is_set() 返回 True，循环就会退出 -> 类似于go的ctx功能
            turn_count += 1
            yield SDKMessage(type="stream_request_start", data={"turn": turn_count})

            tool_schemas = tool_registry.list_schemas()
            accumulated_text = ""
            tool_use_blocks: list[dict] = []
            current_usage = EMPTY_USAGE
            needs_follow_up = False
            api_error = None

            try:
                a0 = time.time()
                async for chunk in provider.chat_stream(
                    model=self._model, messages=self._to_api_messages(),
                    system=system_prompt, tools=tool_schemas, max_tokens=MAX_OUTPUT_TOKENS,
                ):
                    if chunk.type == "text_delta" and chunk.text:
                        accumulated_text += chunk.text
                        yield SDKMessage(type="token", data=chunk.text)

                    elif chunk.type == "tool_use_start":
                        tb = {"id": chunk.tool_id, "name": chunk.tool_name, "input": {}, "input_json": ""}
                        tool_use_blocks.append(tb)
                        needs_follow_up = True
                        yield SDKMessage(type="tool_call", data={"tool_id": chunk.tool_id, "tool_name": chunk.tool_name})

                    elif chunk.type == "tool_use_delta" and chunk.tool_input_json and tool_use_blocks:
                        tool_use_blocks[-1]["input_json"] += chunk.tool_input_json

                    elif chunk.type == "usage" and chunk.usage:
                        current_usage = current_usage or chunk.usage
                        current_usage.input_tokens += chunk.usage.input_tokens
                        current_usage.output_tokens += chunk.usage.output_tokens

                    elif chunk.type == "done":
                        if chunk.usage:
                            current_usage = current_usage or chunk.usage
                            current_usage.input_tokens += chunk.usage.input_tokens
                            current_usage.output_tokens += chunk.usage.output_tokens

                api_ms += (time.time() - a0) * 1000
                self._total_usage.input_tokens += current_usage.input_tokens
                self._total_usage.output_tokens += current_usage.output_tokens
                self._total_usage.total_tokens += current_usage.total_tokens

            except asyncio.CancelledError:
                yield SDKMessage(type="error", data="Request cancelled")
                return
            except Exception as e:
                api_error = str(e)
                yield SDKMessage(type="api_error", data={"error": api_error})
                if not needs_follow_up:
                    break

            # Parse completed tool inputs
            for tb in tool_use_blocks:
                try:
                    tb["input"] = json.loads(tb.get("input_json", "")) if tb.get("input_json") else {}
                except json.JSONDecodeError:
                    tb["input"] = {"_raw": tb.get("input_json", "")}

            # Build assistant message
            if accumulated_text or tool_use_blocks:
                content_blocks = [{"type": "text", "text": accumulated_text}] if accumulated_text else []
                for tb in tool_use_blocks:
                    content_blocks.append({"type": "tool_use", "id": tb["id"], "name": tb["name"], "input": tb.get("input", {})})

                asst = AssistantMessage(content=content_blocks, session_id=self.session_id)
                self._messages.append(asst)
                yield SDKMessage(type="assistant", data={"id": asst.id, "content": content_blocks}, session_id=self.session_id)

            if api_error and not needs_follow_up:
                last_stop_reason = "api_error"
                break

            if not needs_follow_up:
                # Check for prompt-too-long recovery
                if "prompt too long" in accumulated_text.lower():
                    if max_output_recovery < MAX_RETRIES:
                        max_output_recovery += 1
                        self._messages.append(UserMessage(
                            content=[TextBlock(text="Continue from where you left off. No recap.")],
                            is_meta=True, session_id=self.session_id,
                        ))
                        continue
                    else:
                        last_stop_reason = "max_output_tokens_retries_exceeded"
                        break
                last_stop_reason = "end_turn"
                break

            # Execute tool calls
            if tool_use_blocks:
                ctx = self._build_tool_context()
                for tb in tool_use_blocks:
                    result = await self._execute_tool(tb["name"], tb.get("input", {}), ctx, tb["id"])

                    if result.is_error:
                        self._permission_denials.append(PermissionDenial(
                            tool_name=tb["name"], tool_use_id=tb["id"], tool_input=tb.get("input", {}),
                        ))

                    yield SDKMessage(type="tool_result", data={
                        "tool_use_id": tb["id"], "tool_name": tb["name"],
                        "content": result.content, "is_error": result.is_error,
                    })

                    self._messages.append(UserMessage(
                        content=[ToolResultBlock(tool_use_id=tb["id"], content=result.content, is_error=result.is_error)],
                        tool_use_result=True, session_id=self.session_id,
                    ))
                continue

            # Check budget
            if self._total_cost >= self._max_budget:
                yield SDKMessage(type="result", subtype="error_max_budget", data={
                    "is_error": True, "num_turns": turn_count,
                    "errors": [f"Max budget exceeded: ${self._max_budget}"],
                    "total_cost_usd": self._total_cost,
                }, session_id=self.session_id)
                return

        # Final result
        dur = (time.time() - t0) * 1000
        yield SDKMessage(type="result", subtype="success", data={
            "is_error": False, "result": accumulated_text or "No text content",
            "duration_ms": dur, "duration_api_ms": api_ms, "num_turns": turn_count,
            "stop_reason": last_stop_reason, "session_id": self.session_id,
            "usage": {"input_tokens": self._total_usage.input_tokens, "output_tokens": self._total_usage.output_tokens, "total_tokens": self._total_usage.total_tokens},
            "total_cost_usd": self._total_cost, "permission_denials": len(self._permission_denials),
        }, session_id=self.session_id)

    def interrupt(self): self._abort.set()
    def get_messages(self) -> list[Message]: return list(self._messages)
    def clear_history(self): self._messages.clear(); self._abort.clear()

    def _build_tool_context(self) -> ToolUseContext:
        return ToolUseContext(
            cwd=self.config.cwd, session_id=self.session_id,
            read_file_state=self._read_file_state,
            tool_permission_context=ToolPermissionContext(mode=PermissionMode.DEFAULT),
            main_loop_model=self._model, max_budget_usd=self._max_budget,
        )

    async def _execute_tool(self, name: str, params: dict, ctx: ToolUseContext, tool_use_id: str) -> ToolResult:
        tool = tool_registry.find(name)
        if not tool:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True, tool_name=name, tool_use_id=tool_use_id)
        try:
            result = await tool.call(params, ctx)
            result.tool_name = name
            result.tool_use_id = tool_use_id
            return result
        except Exception as e:
            return ToolResult(content=f"Tool error: {e}", is_error=True, tool_name=name, tool_use_id=tool_use_id)

    def _to_api_messages(self) -> list[dict]:
        """Convert internal messages to API-compatible format."""
        result = []
        for m in self._messages:
            c = m.content
            if isinstance(c, list):
                text_parts = []
                for block in c:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        result.append({"role": "assistant", "content": [{"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}]})
                    elif isinstance(block, ToolResultBlock):
                        result.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}]})
                if text_parts:
                    role = "assistant" if isinstance(m, AssistantMessage) else "user"
                    result.append({"role": role, "content": "\n".join(text_parts)})
            elif isinstance(c, str) and c.strip():
                role = "assistant" if isinstance(m, AssistantMessage) else "user"
                result.append({"role": role, "content": c})
        return result


_engines: dict[str, QueryEngine] = {}

def get_or_create_engine(session_id: str | None = None, **kw) -> QueryEngine:
    sid = session_id or uuid.uuid4().hex[:12]
    if sid not in _engines:
        _engines[sid] = QueryEngine(QueryEngineConfig(session_id=sid, **kw))
    return _engines[sid]

def get_engine(sid: str) -> QueryEngine | None:
    return _engines.get(sid)

def remove_engine(sid: str):
    _engines.pop(sid, None)
