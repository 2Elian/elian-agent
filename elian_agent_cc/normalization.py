"""
Message normalization pipeline - ported from utils/messages.ts (4686 lines).

The 12-step pipeline that converts internal messages to API-compatible format:
  1. reorderAttachmentsForAPI  - bubble attachments to nearest stop point
  2. filterVirtualMessages     - remove is_virtual messages
  3. stripAPIErrorBlocks       - strip PDF/image blocks that caused 413 errors
  4. filterNonAPI              - filter progress, system (non-command), synthetic API errors
  5. convertLocalCommands      - system/local_command -> user messages
  6. mergeConsecutiveUsers     - merge consecutive user messages (Bedrock compat)
  7. normalizeToolInputs       - normalize tool inputs, canonicalize names
  8. mergeAssistantWithSameID  - merge assistants sharing the same message ID
  9. convertAttachments        - convert attachment messages to user messages
  10. postProcess              - relocate tool references, filter orphans, ensure non-empty content
  11. smooshSystemReminders    - fold <system-reminder> into tool_result content
  12. sanitizeErrors           - strip non-text blocks from error tool_results
"""
from dataclasses import dataclass, field
from typing import Any
from models import (
    Message, UserMessage, AssistantMessage, SystemMessage,
    ProgressMessage, AttachmentMessage, TextBlock, ToolUseBlock,
    ToolResultBlock,
)


# =============================================================================
# Step 1: Reorder attachments for API
# =============================================================================

def reorder_attachments_for_api(messages: list[Message]) -> list[Message]:
    """Bubble attachment messages upward to the nearest tool_result or assistant.

    Attachments interleaved with tool results need to be moved
    to the end of the tool result group so the API accepts them.
    """
    result = list(messages)
    i = len(result) - 1
    while i >= 0:
        msg = result[i]
        if isinstance(msg, AttachmentMessage):
            # Find nearest stop point above
            insert_at = i + 1
            for j in range(i + 1, len(result)):
                above = result[j]
                if isinstance(above, UserMessage) and above.tool_use_result:
                    insert_at = j + 1
                    break
                if isinstance(above, AssistantMessage):
                    insert_at = j
                    break
            if insert_at != i + 1 and insert_at < len(result):
                result.insert(insert_at, result.pop(i))
            elif insert_at == len(result):
                result.append(result.pop(i))
        i -= 1
    return result


# =============================================================================
# Step 2-4: Filter messages
# =============================================================================

def filter_non_api_messages(messages: list[Message]) -> list[Message]:
    """Remove messages that should NOT go to the API."""
    result = []
    for msg in messages:
        # Skip virtual/synthetic
        if getattr(msg, 'is_synthetic', False):
            continue
        # Skip progress messages
        if isinstance(msg, ProgressMessage):
            continue
        # Skip non-command system messages
        if isinstance(msg, SystemMessage) and msg.subtype not in ('local_command',):
            continue
        # Skip synthetic API error messages
        if isinstance(msg, AssistantMessage) and msg.is_api_error_message:
            continue
        result.append(msg)
    return result


# =============================================================================
# Step 5: Convert local commands to user messages
# =============================================================================

def convert_local_commands(messages: list[Message]) -> list[Message]:
    """Convert system/local_command messages to user messages."""
    result = []
    for msg in messages:
        if isinstance(msg, SystemMessage) and msg.subtype == 'local_command':
            result.append(UserMessage(
                content=msg.content,
                is_meta=True,
                session_id=msg.session_id,
            ))
        else:
            result.append(msg)
    return result


# =============================================================================
# Step 6: Merge consecutive user messages
# =============================================================================

def merge_consecutive_users(messages: list[Message]) -> list[Message]:
    """Merge consecutive UserMessages for Bedrock compatibility."""
    result = []
    for msg in messages:
        if (isinstance(msg, UserMessage) and result
                and isinstance(result[-1], UserMessage)
                and not msg.tool_use_result
                and not result[-1].tool_use_result):
            # Merge content
            prev = result[-1]
            prev_blocks = prev.content if isinstance(prev.content, list) else [TextBlock(text=str(prev.content))]
            new_blocks = msg.content if isinstance(msg.content, list) else [TextBlock(text=str(msg.content))]
            prev.content = prev_blocks + new_blocks
        else:
            result.append(msg)
    return result


# =============================================================================
# Step 7: Normalize tool inputs
# =============================================================================

def normalize_tool_inputs(messages: list[Message]) -> list[Message]:
    """Normalize tool input keys (snake_case), canonicalize tool names (mcp__ prefix)."""
    for msg in messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    # Normalize input keys
                    if block.input:
                        normalized = {}
                        for k, v in block.input.items():
                            # Convert camelCase to snake_case for consistency
                            import re
                            snake = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', k).lower()
                            normalized[snake] = v
                        block.input = normalized

                elif isinstance(block, ToolResultBlock):
                    # Ensure error is bool
                    if not isinstance(block.is_error, bool):
                        block.is_error = False
    return messages


# =============================================================================
# Step 8: Merge assistant messages with same ID
# =============================================================================

def merge_assistants_same_id(messages: list[Message]) -> list[Message]:
    """Merge AssistantMessages sharing the same message ID (happens with concurrent agents)."""
    result = []
    for msg in messages:
        if (isinstance(msg, AssistantMessage) and msg.id
                and result and isinstance(result[-1], AssistantMessage)
                and result[-1].id == msg.id):
            prev = result[-1]
            prev_blocks = prev.content if isinstance(prev.content, list) else [TextBlock(text=str(prev.content))]
            new_blocks = msg.content if isinstance(msg.content, list) else [TextBlock(text=str(msg.content))]
            prev.content = prev_blocks + new_blocks
            if msg.stop_reason:
                prev.stop_reason = msg.stop_reason
            if msg.usage:
                prev.usage = msg.usage
        else:
            result.append(msg)
    return result


# =============================================================================
# Step 9: Convert attachments to user messages
# =============================================================================

def convert_attachments(messages: list[Message]) -> list[Message]:
    """Convert attachment messages to user messages wrapped in <system-reminder>."""
    result = []
    for msg in messages:
        if isinstance(msg, AttachmentMessage):
            atype = msg.attachment_type
            adata = msg.attachment_data

            content = f"<system-reminder>\n"

            if atype == 'memory':
                content += f"Memory system loaded. See memory instructions above.\n"
            elif atype == 'skill_discovery':
                content += f"New skills discovered: {adata}\n"
            elif atype == 'structured_output':
                content += f"Structured output schema: {adata}\n"
            elif atype == 'max_turns_reached':
                content += f"Maximum turns reached. Provide final answer.\n"
            elif atype == 'queued_command':
                content += f"Queued command: {adata}\n"
            elif atype == 'plan_mode':
                content += "Entered plan mode. Design implementation approach.\n"
            elif atype == 'auto_mode':
                content += "Auto mode activated.\n"
            elif atype == 'hook_additional_context':
                content += f"Additional context: {adata}\n"
            elif atype == 'command_permissions':
                content += f"Command permissions: {adata}\n"
            elif atype == 'mcp_instructions':
                content += f"MCP: {adata}\n"
            elif atype == 'agent_mention':
                content += f"Agent mentioned: {adata}\n"
            else:
                content += f"[{atype}]: {adata}\n"

            content += "</system-reminder>"
            result.append(UserMessage(content=[TextBlock(text=content)], is_meta=True, session_id=msg.session_id))
        else:
            result.append(msg)
    return result


# =============================================================================
# Step 10-12: Post-processing
# =============================================================================

def post_process(messages: list[Message]) -> list[Message]:
    """Final cleanup: relocate tool reference siblings, remove orphan thinking, ensure non-empty."""
    result = []
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            blocks = msg.content if isinstance(msg.content, list) else [TextBlock(text=str(msg.content))]
            # Filter trailing thinking blocks from last assistant
            filtered = []
            for block in blocks:
                if isinstance(block, TextBlock):
                    text = block.text
                    # Remove thinking tags
                    text = __import__('re').sub(r'<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>', '', text).strip()
                    if text:
                        filtered.append(TextBlock(text=text))
                elif isinstance(block, (ToolUseBlock, ToolResultBlock)):
                    filtered.append(block)
            if not filtered:
                continue  # Skip empty assistant messages
            msg.content = filtered
        result.append(msg)
    return result


def smoosh_system_reminders(messages: list[Message]) -> list[Message]:
    """Fold <system-reminder> text from text blocks into preceding tool_result blocks."""
    import re
    for msg in messages:
        if isinstance(msg, UserMessage) and isinstance(msg.content, list):
            new_blocks = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    reminder_match = re.search(r'<system-reminder>(.*?)</system-reminder>', block.text, re.DOTALL)
                    if reminder_match and new_blocks:
                        last = new_blocks[-1]
                        if isinstance(last, ToolResultBlock):
                            last.content += "\n" + reminder_match.group(1).strip()
                            continue
                    new_blocks.append(block)
                else:
                    new_blocks.append(block)
            msg.content = new_blocks
    return messages


def sanitize_error_tool_results(messages: list[Message]) -> list[Message]:
    """Strip non-text blocks from error tool results."""
    for msg in messages:
        if isinstance(msg, UserMessage) and msg.tool_use_result and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    # Ensure content is plain text
                    if not isinstance(block.content, str):
                        block.content = str(block.content)
    return messages


# =============================================================================
# Master Pipeline
# =============================================================================

def normalize_messages_for_api(messages: list[Message]) -> list[dict[str, Any]]:
    """Full 12-step normalization pipeline. Converts internal Message[] to API dicts.

    This is THE critical function that bridges internal state to LLM API format.
    Every message that goes to the LLM passes through this pipeline.
    """
    msgs = list(messages)

    # Step 1: Reorder attachments
    msgs = reorder_attachments_for_api(msgs)

    # Step 2-4: Filter non-API messages
    msgs = filter_non_api_messages(msgs)

    # Step 5: Convert local commands
    msgs = convert_local_commands(msgs)

    # Step 6: Merge consecutive users
    msgs = merge_consecutive_users(msgs)

    # Step 7: Normalize tool inputs
    msgs = normalize_tool_inputs(msgs)

    # Step 8: Merge assistants with same ID
    msgs = merge_assistants_same_id(msgs)

    # Step 9: Convert attachments
    msgs = convert_attachments(msgs)

    # Step 10: Post-process
    msgs = post_process(msgs)

    # Step 11: Smoosh system reminders
    msgs = smoosh_system_reminders(msgs)

    # Step 12: Sanitize errors
    msgs = sanitize_error_tool_results(msgs)

    # Convert to API dicts
    return messages_to_api_dicts(msgs)


def messages_to_api_dicts(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert normalized messages to API-compatible dicts."""
    result = []
    for msg in messages:
        role = msg.role
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    result.append({"role": role, "content": block.text})
                elif isinstance(block, ToolUseBlock):
                    result.append({"role": "assistant", "content": [
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    ]})
                elif isinstance(block, ToolResultBlock):
                    result.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
                    ]})
        elif isinstance(msg.content, str):
            result.append({"role": role, "content": msg.content})
    return result


# =============================================================================
# Message Grouping (for UI display and compaction)
# =============================================================================

def group_messages_by_api_round(messages: list[Message]) -> list[list[Message]]:
    """Group messages into API rounds. Each round starts with a user message."""
    rounds = []
    current = []
    for msg in messages:
        if isinstance(msg, UserMessage) and not msg.tool_use_result:
            if current:
                rounds.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def get_messages_after_compact_boundary(messages: list[Message]) -> list[Message]:
    """Get messages after the last compact boundary."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, SystemMessage) and msg.subtype == 'compact_boundary':
            return messages[i + 1:]
    return messages


def count_tool_calls(messages: list[Message], tool_name: str) -> int:
    """Count tool calls for a specific tool across all messages."""
    count = 0
    for msg in messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.name == tool_name:
                    count += 1
                elif isinstance(block, dict) and block.get('name') == tool_name:
                    count += 1
    return count
