"""
Plan Mode tools - EnterPlanMode and ExitPlanMode.

Ported from:
  tools/EnterPlanModeTool/EnterPlanModeTool.ts + prompt.ts (171 lines)
  tools/ExitPlanModeTool/

Plan mode flow:
  1. LLM detects complex/ambiguous task
  2. Calls EnterPlanMode -> user approves
  3. LLM explores codebase, uses AskUserQuestion to clarify
  4. LLM calls ExitPlanMode -> presents plan for user approval
  5. User approves -> LLM implements
"""
from dataclasses import dataclass, field
from typing import Any
from tools.base import Tool, ToolResult, tool_registry
from models import ToolUseContext, PermissionResult, PermissionMode


# =============================================================================
# EnterPlanMode - Full prompt from prompt.ts (171 lines)
# =============================================================================

ENTER_PLAN_MODE_PROMPT = """Use this tool proactively when you're about to start a non-trivial implementation task. Getting user sign-off on your approach before writing code prevents wasted effort and ensures alignment. This tool transitions you into plan mode where you can explore the codebase and design an implementation approach for user approval.

## When to Use This Tool

**Prefer using EnterPlanMode** for implementation tasks unless they're simple. Use it when ANY of these conditions apply:

1. **New Feature Implementation**: Adding meaningful new functionality
   - Example: "Add a logout button" - where should it go? What should happen on click?
   - Example: "Add form validation" - what rules? What error messages?

2. **Multiple Valid Approaches**: The task can be solved in several different ways
   - Example: "Add caching to the API" - could use Redis, in-memory, file-based, etc.
   - Example: "Improve performance" - many optimization strategies possible

3. **Code Modifications**: Changes that affect existing behavior or structure
   - Example: "Update the login flow" - what exactly should change?
   - Example: "Refactor this component" - what's the target architecture?

4. **Architectural Decisions**: The task requires choosing between patterns or technologies
   - Example: "Add real-time updates" - WebSockets vs SSE vs polling
   - Example: "Implement state management" - Redux vs Context vs custom solution

5. **Multi-File Changes**: The task will likely touch more than 2-3 files
   - Example: "Refactor the authentication system"
   - Example: "Add a new API endpoint with tests"

6. **Unclear Requirements**: You need to explore before understanding the full scope
   - Example: "Make the app faster" - need to profile and identify bottlenecks
   - Example: "Fix the bug in checkout" - need to investigate root cause

7. **User Preferences Matter**: The implementation could reasonably go multiple ways
   - If you would use AskUserQuestion to clarify the approach, use EnterPlanMode instead
   - Plan mode lets you explore first, then present options with context

## When NOT to Use This Tool

Only skip EnterPlanMode for simple tasks:
- Single-line or few-line fixes (typos, obvious bugs, small tweaks)
- Adding a single function with clear requirements
- Tasks where the user has given very specific, detailed instructions
- Pure research/exploration tasks (use the Agent tool with explore agent instead)

## What Happens in Plan Mode

In plan mode, you'll:
1. Thoroughly explore the codebase using Glob, Grep, and Read tools
2. Understand existing patterns and architecture
3. Design an implementation approach
4. Present your plan to the user for approval
5. Use AskUserQuestion if you need to clarify approaches
6. Exit plan mode with ExitPlanMode when ready to implement

## Examples

### GOOD - Use EnterPlanMode:
User: "Add user authentication to the app"
- Requires architectural decisions (session vs JWT, where to store tokens, middleware structure)

User: "Optimize the database queries"
- Multiple approaches possible, need to profile first, significant impact

User: "Implement dark mode"
- Architectural decision on theme system, affects many components

User: "Add a delete button to the user profile"
- Seems simple but involves: where to place it, confirmation dialog, API call, error handling, state updates

### BAD - Don't use EnterPlanMode:
User: "Fix the typo in the README"
- Straightforward, no planning needed

User: "Add a console.log to debug this function"
- Simple, obvious implementation

User: "What files handle routing?"
- Research task, not implementation planning

## Important Notes

- This tool REQUIRES user approval - they must consent to entering plan mode
- If unsure whether to use it, err on the side of planning - it's better to get alignment upfront than to redo work
- Users appreciate being consulted before significant changes are made to their codebase"""


EXIT_PLAN_MODE_PROMPT = """Exit plan mode when you have finished writing your plan and are ready for user approval.

## How This Tool Works
- You should have already written your plan to the plan file
- This tool signals that you're done planning and ready for the user to review and approve
- The user will see the contents of your plan file when they review it

## When to Use This Tool
Use this tool when the task requires planning the implementation steps of a task that requires writing code. For research tasks where you're gathering information, searching files, reading files or in general trying to understand the codebase - do NOT use this tool.

## Before Using This Tool
Ensure your plan is complete and unambiguous:
- If you have unresolved questions about requirements or approach, use AskUserQuestion first
- Once your plan is finalized, use THIS tool to request approval

**Important:** Do NOT use AskUserQuestion to ask "Is this plan okay?" or "Should I proceed?" - that's exactly what THIS tool does. ExitPlanMode inherently requests user approval of your plan."""


# =============================================================================
# EnterPlanMode Tool
# =============================================================================

class EnterPlanModeTool(Tool):
    name = "EnterPlanMode"
    description = (
        "Requests permission to enter plan mode for complex tasks requiring "
        "exploration and design before implementation. Gets user sign-off on "
        "approach before writing code."
    )

    input_schema = {
        "type": "object",
        "properties": {},  # No parameters needed
    }

    is_read_only = True
    is_concurrency_safe = True

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        # Cannot be used by sub-agents
        if context.agent_id:
            return ToolResult(
                content="EnterPlanMode cannot be used in agent contexts. "
                        "Plan mode is for the main conversation only.",
                is_error=True,
            )

        # Transition to plan mode
        context.tool_permission_context.pre_plan_mode = context.tool_permission_context.mode
        context.tool_permission_context.mode = PermissionMode.PLAN

        return ToolResult(
            content="## Entered Plan Mode\n\n"
                    "You are now in plan mode. Your task:\n\n"
                    "1. **Explore** the codebase thoroughly using Glob, Grep, and Read\n"
                    "2. **Understand** existing patterns and architecture\n"
                    "3. **Design** an implementation approach with trade-offs\n"
                    "4. **Clarify** ambiguities using AskUserQuestion if needed\n"
                    "5. **Present** your plan by writing it to a file using Write\n"
                    "6. **Request approval** by calling ExitPlanMode\n\n"
                    "Rules:\n"
                    "- Do NOT write implementation code — only the plan document\n"
                    "- Use AskUserQuestion for clarification (NOT for plan approval)\n"
                    "- Consider multiple approaches and discuss trade-offs\n"
                    "- The plan must be complete and unambiguous before requesting approval"
        )

    def check_permissions(self, params, context) -> PermissionResult:
        return PermissionResult.ask(
            "Enter plan mode? This will allow exploring the codebase and designing "
            "an implementation approach for your approval before any code is written."
        )

    def to_schema(self) -> dict[str, Any]:
        schema = super().to_schema()
        schema["description"] = f"{schema['description']}\n\n{ENTER_PLAN_MODE_PROMPT}"
        return schema


# =============================================================================
# ExitPlanMode Tool
# =============================================================================

class ExitPlanModeTool(Tool):
    name = "ExitPlanMode"
    description = (
        "Exit plan mode when you have finished writing your plan and are "
        "ready for user approval. The user will review your plan and approve "
        "or request changes before implementation begins."
    )

    input_schema = {
        "type": "object",
        "properties": {},  # No parameters needed
    }

    is_read_only = True
    is_concurrency_safe = True

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        # Restore previous permission mode
        pre_plan = context.tool_permission_context.pre_plan_mode
        if pre_plan:
            context.tool_permission_context.mode = pre_plan
            context.tool_permission_context.pre_plan_mode = None
        else:
            context.tool_permission_context.mode = PermissionMode.DEFAULT

        return ToolResult(
            content="## Exited Plan Mode\n\n"
                    "Your plan is ready for review. The user will now see your plan "
                    "and decide whether to:\n"
                    "- **Approve** — proceed with implementation\n"
                    "- **Request changes** — revise the plan based on feedback\n"
                    "- **Reject** — abandon this approach\n\n"
                    "Wait for user feedback before implementing."
        )

    def check_permissions(self, params, context) -> PermissionResult:
        return PermissionResult.ask(
            "Exit plan mode? Your plan will be presented for user approval."
        )

    def to_schema(self) -> dict[str, Any]:
        schema = super().to_schema()
        schema["description"] = f"{schema['description']}\n\n{EXIT_PLAN_MODE_PROMPT}"
        return schema


# =============================================================================
# Registration
# =============================================================================

tool_registry.register(EnterPlanModeTool())
tool_registry.register(ExitPlanModeTool())
