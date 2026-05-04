"""
LLM Provider abstraction layer. Supports Anthropic + OpenAI-compatible APIs.
Streaming with full event types: text_delta, tool_use_start/delta, thinking_delta, done.
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
import json, os, re
import aiohttp
from config import BASE_URL, API_KEY, MODEL


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

@dataclass
class LLMResponse:
    content: str = ""
    model: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    stop_reason: str = "end_turn"
    tool_calls: list[dict] = field(default_factory=list)

@dataclass
class StreamChunk:
    type: str  # text_delta, tool_use_start, tool_use_delta, thinking_delta, usage, done
    text: str | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    tool_input_json: str | None = None
    usage: LLMUsage | None = None


class LLMProvider(ABC):
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    async def chat(self, model: str, messages: list[dict], system: str | None = None,
                   tools: list[dict] | None = None, max_tokens: int = 4096) -> LLMResponse: ...

    @abstractmethod
    async def chat_stream(self, model: str, messages: list[dict], system: str | None = None,
                          tools: list[dict] | None = None, max_tokens: int = 4096) -> AsyncGenerator[StreamChunk, None]: ...


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible API provider (works with OpenAI, DeepSeek, Groq, Ollama, MiMo, etc.)"""

    @property
    def provider_name(self) -> str:
        return "openai"

    async def chat(self, model, messages, system=None, tools=None, max_tokens=4096, temperature=None, top_p=None) -> LLMResponse:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            msgs.append({"role": m.get("role", "user"), "content": str(c)})

        body = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }} for t in tools]

        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base_url}/chat/completions", json=body, headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }, ssl=False) as r:
                data = await r.json()

        if r.status != 200:
            return LLMResponse(content=f"API Error {r.status}: {data}", stop_reason="error")

        choice = data["choices"][0]
        msg = choice.get("message", {})
        u = data.get("usage", {})
        content = msg.get("content", "") or ""
        # Handle reasoning models (mimo-v2.5-pro etc.) that put output in reasoning_content
        if not content:
            content = msg.get("reasoning_content", "") or ""
        tool_calls = []
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                try: args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError: args = {}
                tool_calls.append({"id": tc["id"], "name": tc["function"]["name"], "input": args})

        return LLMResponse(content=content, model=data.get("model", model),
                          usage=LLMUsage(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                                         total_tokens=u.get("total_tokens", 0)),
                          tool_calls=tool_calls, stop_reason=choice.get("finish_reason", "stop"))

    async def chat_stream(self, model, messages, system=None, tools=None, max_tokens=4096, temperature=None, top_p=None) -> AsyncGenerator[StreamChunk, None]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            msgs.append({"role": m.get("role", "user"), "content": str(c)})

        body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "stream": True}
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }} for t in tools]

        cur_id, cur_name, parts = None, None, []
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{self.base_url}/chat/completions", json=body, headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }, ssl=False) as r:
                if r.status != 200:
                    text = await r.text()
                    yield StreamChunk(type="done", usage=LLMUsage())
                    return

                async for line in r.content:
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text.startswith("data: "): continue
                    d = text[6:]
                    if d == "[DONE]": break
                    try: ev = json.loads(d)
                    except json.JSONDecodeError: continue

                    choices = ev.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    # Handle both regular content and reasoning_content (for reasoning models)
                    text_content = delta.get("content", "") or delta.get("reasoning_content", "") or ""
                    if text_content:
                        yield StreamChunk(type="text_delta", text=text_content)

                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tcd in delta["tool_calls"]:
                            if "id" in tcd and tcd["id"]: cur_id = tcd["id"]; parts = []
                            if "function" in tcd:
                                if "name" in tcd["function"]:
                                    cur_name = tcd["function"]["name"]
                                    yield StreamChunk(type="tool_use_start", tool_id=cur_id, tool_name=cur_name)
                                if "arguments" in tcd["function"]:
                                    parts.append(tcd["function"]["arguments"])
                                    yield StreamChunk(type="tool_use_delta", tool_id=cur_id, tool_input_json=tcd["function"]["arguments"])

                    if ev.get("usage"):
                        u = ev["usage"]
                        yield StreamChunk(type="usage", usage=LLMUsage(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0), total_tokens=u.get("total_tokens", 0)))

                yield StreamChunk(type="done")


def get_provider(name: str = "openai") -> LLMProvider:
    if name == "anthropic":
        return OpenAIProvider(API_KEY or os.environ.get("ANTHROPIC_API_KEY", ""), BASE_URL)
    return OpenAIProvider(API_KEY, BASE_URL)
