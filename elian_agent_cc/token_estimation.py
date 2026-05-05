"""
Token estimation — ported from services/tokenEstimation.ts (496 lines).

Three estimation tiers:
  1. API-based counting (countTokens API) — exact, used when available
  2. Rough estimation by content block — chars/4 (chars/2 for JSON)
  3. Per-message iteration — walks content blocks for accurate estimate

Image blocks: 2000 tokens (conservative, matches microcompact IMAGE_MAX_TOKEN_SIZE)
Tool use blocks: name + json(input)
Tool result blocks: recursively estimate content
"""
from typing import Any
from models import (
    Message, UserMessage, AssistantMessage, SystemMessage,
    TextBlock, ToolUseBlock, ToolResultBlock, ImageBlock,
)
import json


# Bytes-per-token ratio by file extension (tokenEstimation.ts line 215-223)
BYTES_PER_TOKEN: dict[str, float] = {
    "json": 2.0, "jsonl": 2.0, "jsonc": 2.0,
}
DEFAULT_BYTES_PER_TOKEN = 4.0
IMAGE_TOKEN_ESTIMATE = 2000  # Conservative estimate for images (2000px / 750)


def rough_token_estimate(content: str, file_extension: str = "") -> int:
    """Rough estimate: chars / bytes_per_token. Default 4 chars/token."""
    bpt = BYTES_PER_TOKEN.get(file_extension.lstrip(".").lower(), DEFAULT_BYTES_PER_TOKEN)
    return max(1, round(len(content) / bpt))


def estimate_block(block: Any) -> int:
    """Estimate tokens for a single content block.
    Ported from roughTokenCountEstimationForBlock() lines 391-435.
    """
    if isinstance(block, str):
        return rough_token_estimate(block)

    block_type = getattr(block, "type", "") or block.get("type", "")

    if block_type == "text":
        text = getattr(block, "text", "") or block.get("text", "")
        return rough_token_estimate(text)

    if block_type in ("image", "document"):
        # Conservative: 2000 tokens (2000x2000px / 750, rounded up)
        return IMAGE_TOKEN_ESTIMATE

    if block_type == "tool_result":
        content = getattr(block, "content", None)
        if content == "":
            raise ValueError("ToolResultBlock.content is empty string, which is not allowed, 请找一下tool call的逻辑 看看为什么content没写进来")
        if content is None:
            if isinstance(block, dict):
                content = block.get("content", "")
            else:
                raise TypeError("block has no 'content' attribute and is not a dict, 请找一下tool call的逻辑 看看为什么content没写进来")

        return estimate_content(content)

    if block_type == "tool_use":
        name = getattr(block, "name", "") or block.get("name", "")
        inp = getattr(block, "input", {}) or block.get("input", {})
        try:
            input_str = json.dumps(inp, ensure_ascii=False)
        except (TypeError, ValueError):
            input_str = str(inp)
        return rough_token_estimate(name + input_str)

    if block_type == "thinking":
        text = getattr(block, "thinking", "") or block.get("thinking", "")
        return rough_token_estimate(text)

    if block_type == "redacted_thinking":
        text = getattr(block, "data", "") or block.get("data", "")
        return rough_token_estimate(text)

    # server_tool_use, web_search_tool_result, mcp_tool_use, etc.
    if isinstance(block, dict):
        try:
            return rough_token_estimate(json.dumps(block, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return rough_token_estimate(str(block))


def estimate_content(content: Any) -> int:
    """Estimate tokens for content (string or list of blocks).
    Ported from roughTokenCountEstimationForContent() lines 371-389.
    """
    if not content:
        return 0
    if isinstance(content, str):
        return rough_token_estimate(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            total += estimate_block(block)
        return total
    return 0


def estimate_message(msg: Any) -> int:
    """Estimate tokens for a single message.
    Ported from roughTokenCountEstimationForMessage() lines 341-369.
    """
    msg_type = getattr(msg, "type", "")
    content = getattr(msg, "content", None)

    if msg_type in ("assistant", "user") and content:
        return estimate_content(content)

    # System messages
    if isinstance(msg, SystemMessage) and content:
        return estimate_content(content)

    # Fallback
    if content:
        return estimate_content(content)
    return 0


def estimate_messages(messages: list[Any]) -> int:
    """Estimate total tokens for a list of messages.
    Ported from roughTokenCountEstimationForMessages() lines 327-339.
    """
    total = 0
    for msg in messages:
        total += estimate_message(msg)
    # Add system prompt estimate (~3000 chars average)
    total += rough_token_estimate(" " * 3000)  # ~750 tokens for system prompt
    return total
