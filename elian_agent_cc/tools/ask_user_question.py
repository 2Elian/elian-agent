"""
AskUserQuestion tool - let LLM ask users multiple-choice questions.

Ported from:
  tools/AskUserQuestionTool/AskUserQuestionTool.tsx
  tools/AskUserQuestionTool/prompt.ts

LLM can ask 1-4 questions, each with 2-4 options.
Supports: multi-select, recommended options, preview content, free-form "Other".
"""
from dataclasses import dataclass, field
from typing import Any, Literal
from tools.base import Tool, ToolResult, tool_registry
from models import ToolUseContext, PermissionResult


# =============================================================================
# Schema types
# =============================================================================

@dataclass
class QuestionOption:
    label: str           # 1-5 words, displayed to user
    description: str     # What this option means
    preview: str | None = None  # Optional preview content (markdown/code)


@dataclass
class Question:
    question: str        # The question text, ending with "?"
    header: str          # Short label, max 12 chars. e.g. "Auth method"
    options: list[QuestionOption]  # 2-4 options
    multi_select: bool = False  # Allow multiple answers


@dataclass
class AskUserQuestionOutput:
    questions: list[Question]
    answers: dict[str, str]        # question_text -> answer
    annotations: dict[str, dict[str, str]] | None = None


# =============================================================================
# Prompt content (ported from prompt.ts)
# =============================================================================

ASK_USER_QUESTION_PROMPT = """Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label

Plan mode note: In plan mode, use this tool to clarify requirements or choose between approaches BEFORE finalizing your plan. Do NOT use this tool to ask "Is my plan ready?" or "Should I proceed?" - use ExitPlanMode for plan approval. IMPORTANT: Do not reference "the plan" in your questions (e.g., "Do you have feedback about the plan?", "Does the plan look good?") because the user cannot see the plan in the UI until you call ExitPlanMode. If you need plan approval, use ExitPlanMode instead."""


PREVIEW_FEATURE_PROMPT = """Preview feature:
Use the optional `preview` field on options when presenting concrete artifacts that users need to visually compare:
- ASCII mockups of UI layouts or components
- Code snippets showing different implementations
- Diagram variations
- Configuration examples

Preview content is rendered as markdown in a monospace box. Multi-line text with newlines is supported. When any option has a preview, the UI switches to a side-by-side layout with a vertical option list on the left and preview on the right. Do not use previews for simple preference questions where labels and descriptions suffice. Note: previews are only supported for single-select questions (not multiSelect)."""


# =============================================================================
# Tool Implementation
# =============================================================================

class AskUserQuestionTool(Tool):
    name = "AskUserQuestion"
    description = (
        "Asks the user multiple choice questions to gather information, "
        "clarify ambiguity, understand preferences, make decisions or offer choices."
    )

    input_schema = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to ask the user (1-4 questions)",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": 'The complete question to ask the user. Should be clear, specific, and end with a question mark. Example: "Which library should we use for date formatting?" If multiSelect is true, phrase it accordingly, e.g. "Which features do you want to enable?"',
                        },
                        "header": {
                            "type": "string",
                            "description": 'Very short label displayed as a chip/tag (max 12 chars). Examples: "Auth method", "Library", "Approach".',
                            "maxLength": 12,
                        },
                        "options": {
                            "type": "array",
                            "description": "The available choices for this question. Must have 2-4 options. Each option should be a distinct, mutually exclusive choice (unless multiSelect is enabled). There should be no 'Other' option, that will be provided automatically.",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "The display text for this option that the user will see and select. Should be concise (1-5 words) and clearly describe the choice.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Explanation of what this option means or what will happen if chosen. Useful for providing context about trade-offs or implications.",
                                    },
                                    "preview": {
                                        "type": "string",
                                        "description": "Optional preview content rendered when this option is focused. Use for mockups, code snippets, or visual comparisons that help users compare options.",
                                    },
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "Set to true to allow the user to select multiple options instead of just one. Use when choices are not mutually exclusive.",
                            "default": False,
                        },
                    },
                    "required": ["question", "header", "options", "multiSelect"],
                },
            },
            "answers": {
                "type": "object",
                "description": "User answers collected by the permission component",
                "additionalProperties": {"type": "string"},
            },
            "annotations": {
                "type": "object",
                "description": "Optional per-question annotations from the user (e.g., notes on preview selections). Keyed by question text.",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "preview": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
        },
        "required": ["questions"],
    }

    is_read_only = True

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        questions_data = params.get("questions", [])
        answers = params.get("answers", {})

        # If answers are provided, process them
        if answers:
            return self._process_answers(questions_data, answers)

        # Otherwise, format the questions for the user
        return self._format_questions(questions_data)

    def _format_questions(self, questions_data: list[dict]) -> ToolResult:
        """Format questions for display to user. The permission UI will render them."""
        lines = []
        lines.append(f"## Questions ({len(questions_data)})\n")

        for i, q in enumerate(questions_data):
            question = q.get("question", "")
            header = q.get("header", "")
            options = q.get("options", [])
            multi = q.get("multiSelect", False)

            lines.append(f"### Q{i+1}: {header}")
            lines.append(f"**{question}**")
            if multi:
                lines.append("_(Select all that apply)_")
            lines.append("")

            for j, opt in enumerate(options):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                preview = opt.get("preview")

                if "+ (Recommended)" in label or "(Recommended)" in label:
                    lines.append(f"{j+1}. **{label}** (Recommended) — {desc}")
                else:
                    lines.append(f"{j+1}. **{label}** — {desc}")

                if preview:
                    lines.append(f"   ```\n   {preview}\n   ```")

            lines.append(f"{len(options) + 1}. **Other** — Provide your own answer")
            lines.append("")

        return ToolResult(content="\n".join(lines))

    def _process_answers(self, questions_data: list[dict], answers: dict[str, str]) -> ToolResult:
        """Process user answers and return structured result."""
        lines = ["## User Answers\n"]

        for q in questions_data:
            question = q.get("question", "")
            answer = answers.get(question, "(No answer)")

            lines.append(f"**Q: {question}**")
            lines.append(f"**A: {answer}**")
            lines.append("")

        lines.append("\n_Continue based on these answers._")
        return ToolResult(content="\n".join(lines))

    def to_schema(self) -> dict[str, Any]:
        """Override to add the full prompt as description extension."""
        schema = super().to_schema()
        schema["description"] = f"{schema['description']}\n\n{ASK_USER_QUESTION_PROMPT}\n\n{PREVIEW_FEATURE_PROMPT}"
        return schema


tool_registry.register(AskUserQuestionTool())
