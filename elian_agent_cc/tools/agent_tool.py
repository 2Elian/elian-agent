"""
AgentTool — LLM-callable tool that spawns sub-agents.

Ported from tools/AgentTool/AgentTool.tsx + runAgent.ts.
Wires into agents.py's AgentManager, AgentRegistry, and fork mechanism.
"""
import asyncio
from typing import Any
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext, PermissionResult


AGENT_TOOL_PROMPT = """Launch a new agent to handle complex, multi-step tasks.
Each agent type has specific capabilities and tools available to it.

Available agent types:
- Explore: Fast read-only search agent for locating code. Use for finding files, searching for symbols, or "where is X defined" questions. (10 turns max)
- Plan: Software architect for designing implementation plans. Returns step-by-step plans. (15 turns max)
- general-purpose: Full-capability agent for researching, searching, and executing multi-step tasks. (25 turns max)
- verification: Reviews code changes and validates correctness. Returns PASS/FAIL/PARTIAL verdict. (20 turns max, background)
- claude-code-guide: Answers questions about Claude Code features and usage. (10 turns max)

Usage notes:
- Always include a short (3-5 word) description of the task
- Launch multiple agents in a single message when tasks are independent
- Use Explore for searching across the codebase; Plan for designing approaches
- Use run_in_background for long-running tasks (they notify via task-notification)
- Trust but verify: an agent's summary describes intent, not necessarily what it did"""


class AgentTool(Tool):
    """Spawn sub-agents. The primary tool for parallel work and context isolation."""

    name = "Agent"
    description = f"""Launch a sub-agent to handle complex multi-step tasks. Spawns independent agents with isolated context.
{AGENT_TOOL_PROMPT}"""

    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "A short (3-5 word) description of the task"},
            "prompt": {"type": "string", "description": "The full task description for the agent to perform"},
            "subagent_type": {
                "type": "string",
                "description": "Type of agent: Explore (search), Plan (design), general-purpose (execute)",
                "enum": ["Explore", "Plan", "general-purpose", "verification", "claude-code-guide"],
            },
            "model": {"type": "string", "enum": ["sonnet", "opus", "haiku"], "description": "Optional model override"},
            "run_in_background": {"type": "boolean", "default": False, "description": "Run async, results via task-notification"},
        },
        "required": ["description", "prompt"],
    }

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        from agents import agent_manager, agent_registry

        description = params.get("description", "untitled")
        prompt_text = params.get("prompt", "")
        agent_type_str = params.get("subagent_type", "general-purpose")
        run_async = params.get("run_in_background", False)
        model_override = params.get("model")

        # 1. Find the agent definition
        definition = agent_registry.get(agent_type_str)
        if not definition:
            definition = agent_registry.match_for_task(description)
        if not definition:
            return ToolResult(content=f"No agent found for: {agent_type_str}", is_error=True)

        # 2. Apply model override if specified
        if model_override:
            definition = __import__('copy').deepcopy(definition)
            definition.model = model_override

        # 3. Spawn the agent
        ctx = agent_manager.spawn(
            definition=definition,
            cwd=context.cwd,
            parent_session_id=context.session_id,
            is_async=run_async,
            prompt_messages=[],
        )

        # 4. Run the agent (sync or async)
        if run_async:
            # Fire-and-forget background execution
            asyncio.create_task(agent_manager.run_agent_async(ctx, prompt_text))
            return ToolResult(
                content=f"Agent started in background.\n"
                        f"  Agent ID: {ctx.agent_id}\n"
                        f"  Type: {definition.agent_type}\n"
                        f"  Task: {description}\n"
                        f"  Max turns: {ctx.max_turns}\n\n"
                        f"Use TaskOutput with task_id='{ctx.agent_id}' to check results."
            )
        else:
            # Synchronous execution — block until done
            result_text = await agent_manager.run_agent(ctx, prompt_text)
            return ToolResult(
                content=f"[Agent {ctx.agent_id} result]\n\n{result_text}",
            )

    def to_schema(self) -> dict[str, Any]:
        schema = super().to_schema()
        # Inject the full agent type descriptions
        from agents import BUILT_IN_AGENTS
        agent_descs = []
        for name, agent_def in BUILT_IN_AGENTS.items():
            agent_descs.append(f"- **{name}**: {agent_def.when_to_use[:120]}")
        schema["description"] = schema["description"] + "\n\n" + "\n".join(agent_descs)
        return schema


# =============================================================================
# SkillTool — already registered in agent_comms.py but using old pattern.
# Let's ensure the Skill tool properly invokes skills.
# =============================================================================

tool_registry.register(AgentTool())
