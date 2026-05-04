"""
Context compaction system - ported from compact.ts (1706 lines) + autoCompact.ts (352 lines).

Two compaction strategies:
  1. Reactive compaction: triggered by prompt-too-long (413) errors
  2. Auto compaction: triggered when context approaches threshold (85%)

Compaction produces a structured summary that replaces older messages,
freeing context window for the next turns.
"""
from dataclasses import dataclass, field
from typing import Any
from models import (
    Message, SystemMessage, UserMessage, TextBlock,
)
from normalization import get_messages_after_compact_boundary, group_messages_by_api_round


# =============================================================================
# Compaction threshold calculation
# =============================================================================

AUTOCOMPACT_BUFFER_TOKENS = 13000
WARNING_THRESHOLD_BUFFER = 20000
ERROR_THRESHOLD_BUFFER = 20000
MAX_CONSECUTIVE_FAILURES = 3


def get_auto_compact_threshold(model_context_window: int = 200000) -> int:
    """Calculate token threshold for auto-compaction (default 85% of usable context)."""
    output_buffer = 32000
    usable = model_context_window - output_buffer
    return int(usable * 0.85)


def should_auto_compact(current_tokens: int, context_window: int = 200000) -> bool:
    """Check if auto-compaction should trigger."""
    threshold = get_auto_compact_threshold(context_window)
    return current_tokens > threshold


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


# =============================================================================
# Compaction summary prompt (9-section structure from compact.ts)
# =============================================================================

COMPACTION_SYSTEM_PROMPT = """You are a conversation summarizer. Create a detailed structured summary.

## Summary Structure

### 1. Primary Request and Intent
The user's overall goal and what they asked for.

### 2. Key Technical Concepts
All important technical concepts, libraries, frameworks, and patterns discussed.

### 3. Files and Code Sections
Every file read, modified, created, or discussed. Include:
- File path, what was done, key changes or findings.

### 4. Errors and Fixes
Every error encountered and how it was resolved.

### 5. Problem Solving
Problem-solving approach. What worked? What didn't?

### 6. All User Messages
All explicit messages/requests from the user.

### 7. Pending Tasks
Tasks started but not completed. Include task IDs.

### 8. Current Work
Current state of work immediately before this summary.

### 9. Optional Next Step
Suggested next action if conversation ended mid-task.

## Formatting
- Be detailed - this summary REPLACES the full conversation
- Use exact file paths
- Preserve critical code snippets
- Do NOT use markdown headers (##) within sections - use bold text instead
- Keep concise but complete"""


# =============================================================================
# Compaction manager
# =============================================================================

@dataclass
class CompactionResult:
    """Result from a compaction operation."""
    boundary_message: SystemMessage | None = None
    summary_messages: list[Message] = field(default_factory=list)
    pre_tokens: int = 0
    post_tokens: int = 0
    messages_summarized: int = 0
    trigger: str = ""  # "manual" or "auto"
    executed: bool = False


class CompactionManager:
    """Manages context compaction for a session."""

    def __init__(self, model_context_window: int = 200000):
        self.context_window = model_context_window
        self._consecutive_failures = 0
        self._total_compactions = 0
        self._last_compaction_tokens = 0

    def needs_compaction(self, current_tokens: int) -> bool:
        """Check if auto-compaction needed, with circuit breaker."""
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            return False
        return should_auto_compact(current_tokens, self.context_window)

    def mark_success(self, pre_tokens: int, post_tokens: int) -> None:
        """Record successful compaction."""
        self._consecutive_failures = 0
        self._total_compactions += 1
        self._last_compaction_tokens = post_tokens

    def mark_failure(self) -> None:
        """Record failed compaction attempt."""
        self._consecutive_failures += 1

    def build_compaction_prompt(self, messages: list[Message]) -> str:
        """Build the compaction prompt from conversation history."""
        parts = [COMPACTION_SYSTEM_PROMPT, "", "## Conversation to Summarize", ""]

        for i, msg in enumerate(messages):
            role = msg.role.upper()
            content = msg.content
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, dict):
                        if block.get('type') == 'text':
                            text_parts.append(str(block.get('text', '')))
                        elif block.get('type') == 'tool_use':
                            text_parts.append(f"[Tool: {block.get('name', '?')}({str(block.get('input', {}))[:200]})]")
                        elif block.get('type') == 'tool_result':
                            text_parts.append(f"[Result({block.get('tool_use_id', '')[:8]}): {str(block.get('content', ''))[:200]}]")
                content = "\n".join(text_parts)
            elif not isinstance(content, str):
                content = str(content)

            if content.strip():
                parts.append(f"[{i}] {role}: {content[:800]}")

        return "\n\n".join(parts)

    async def compact(
        self,
        messages: list[Message],
        provider=None,
        trigger: str = "auto",
    ) -> CompactionResult:
        """Perform compaction using LLM summarization.

        Args:
            messages: Full conversation to summarize
            provider: LLM provider to use (defaults to small model)
            trigger: "manual" (/compact) or "auto" (threshold-based)

        Returns CompactionResult with boundary message and summary.
        """
        pre_tokens = estimate_tokens(
            "\n".join(str(m.content) for m in messages)
        )

        if pre_tokens < get_auto_compact_threshold():
            return CompactionResult(executed=False, trigger=trigger)

        prompt = self.build_compaction_prompt(messages)

        try:
            if provider:
                from providers import get_provider as gp
                p = gp("openai")
                resp = await p.chat(
                    model="mimo-v2-flash",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4000,
                )
                summary = resp.content
            else:
                # No provider available - use simple truncation
                summary = self._fallback_summary(messages)

            post_tokens = estimate_tokens(summary)
            self.mark_success(pre_tokens, post_tokens)

            boundary = SystemMessage(
                subtype="compact_boundary",
                content=f"[Compacted: {len(messages)} messages summarized from ~{pre_tokens} to ~{post_tokens} tokens]",
                compact_metadata={
                    "trigger": trigger,
                    "pre_tokens": pre_tokens,
                    "post_tokens": post_tokens,
                    "messages_summarized": len(messages),
                },
            )

            summary_msg = UserMessage(
                content=[TextBlock(text=f"<summary>\n{summary}\n</summary>")],
                is_meta=True,
            )

            return CompactionResult(
                boundary_message=boundary,
                summary_messages=[boundary, summary_msg],
                pre_tokens=pre_tokens,
                post_tokens=post_tokens,
                messages_summarized=len(messages),
                trigger=trigger,
                executed=True,
            )

        except Exception:
            self.mark_failure()
            return CompactionResult(executed=False, trigger=trigger)

    def _fallback_summary(self, messages: list[Message]) -> str:
        """Simple truncation-based summary when no LLM available."""
        parts = []
        for msg in messages[-20:]:  # Last 20 messages
            role = msg.role
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    b.text for b in content
                    if isinstance(b, TextBlock) and b.text
                )
            if content and isinstance(content, str):
                parts.append(f"[{role}]: {content[:200]}")
        return "\n".join(parts)


# =============================================================================
# Reactive compaction (triggered by 413 prompt-too-long errors)
# =============================================================================

class ReactiveCompactionManager:
    """Handles compaction triggered by API 413 errors.

    Ported from reactive compact logic in query.ts.
    """

    def __init__(self):
        self._attempted = False
        self._recovered = False

    def can_attempt(self) -> bool:
        """Check if reactive compaction hasn't been tried yet this turn."""
        return not self._attempted

    def mark_attempted(self) -> None:
        self._attempted = True

    def mark_recovered(self) -> None:
        self._recovered = True

    def reset(self) -> None:
        self._attempted = False
        self._recovered = False

    async def try_reactive_compact(
        self,
        messages: list[Message],
        error_message: str,
        compaction: CompactionManager,
        provider=None,
    ) -> CompactionResult:
        """Attempt reactive compaction in response to a 413 error."""
        if not self.can_attempt():
            return CompactionResult(executed=False)

        self.mark_attempted()

        # Try more aggressive compaction than normal
        result = await compaction.compact(messages, provider, trigger="reactive")
        if result.executed:
            self.mark_recovered()

        return result


# =============================================================================
# Context collapse (lightweight)
# =============================================================================

class ContextCollapseManager:
    """Lightweight context reduction without LLM summarization.

    Ported from CONTEXT_COLLAPSE feature in query.ts.
    Removes tool result details, keeping only summaries.
    """

    def __init__(self):
        self._applied_count = 0

    def apply_collapse(self, messages: list[Message]) -> list[Message]:
        """Collapse verbose tool results to compact form."""
        result = []
        for msg in messages:
            if isinstance(msg, UserMessage) and msg.tool_use_result:
                if isinstance(msg.content, list):
                    collapsed = []
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            content = block.content
                            if isinstance(content, str) and len(content) > 500:
                                collapsed.append(ToolResultBlock(
                                    tool_use_id=block.tool_use_id,
                                    content=content[:250] + f"\n... [{len(content) - 500} chars collapsed] ...\n" + content[-250:],
                                    is_error=block.is_error,
                                ))
                            else:
                                collapsed.append(block)
                        else:
                            collapsed.append(block)
                    msg.content = collapsed
            result.append(msg)

        self._applied_count += 1
        return result

    @property
    def applied_count(self) -> int:
        return self._applied_count
