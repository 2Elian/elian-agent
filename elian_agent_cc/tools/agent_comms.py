"""
Agent communication tools: SendMessage.

Ported from:
  - tools/SendMessageTool/SendMessageTool.ts
  - Channel-based inter-agent communication

Supports:
  - Direct messages to specific agents
  - Broadcast to all agents (*)
  - Structured messages (shutdown_request, plan_approval_response)
"""
import asyncio
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext
from typing import Any


class SendMessageTool(Tool):
    name = "SendMessage"
    description = """Send a message to a running agent. Use to continue conversations with async agents.

Communication patterns:
- Send to a specific agent by name or ID
- Broadcast to all agents with '*'
- Receive task_notification responses

The agent will see your message as its next user input and continue its work.
This allows iterative collaboration with background agents."""

    input_schema = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Agent name, agent ID, or '*' for broadcast",
            },
            "message": {
                "type": "string",
                "description": "The message to send to the agent",
            },
            "message_type": {
                "type": "string",
                "enum": ["direct", "shutdown_request", "plan_approval_response"],
                "description": "Type of message",
                "default": "direct",
            },
        },
        "required": ["to", "message"],
    }
    is_read_only = True

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        from agents import agent_manager

        target = params["to"]
        message = params["message"]
        msg_type = params.get("message_type", "direct")

        # Handle broadcast
        if target == "*":
            running = agent_manager.get_running()
            if not running:
                return ToolResult(content="No running agents to broadcast to.")

            for agent_ctx in running:
                agent_ctx.messages.append({
                    "role": "user",
                    "content": f"[Broadcast from {context.agent_id or 'parent'}]: {message}",
                })

            return ToolResult(
                content=f"Broadcast sent to {len(running)} agents: "
                + ", ".join(a.agent_id for a in running)
            )

        # Find target agent
        target_ctx = agent_manager.get_agent(target)
        if not target_ctx:
            target_ctx = agent_manager.find_by_name(target)
        if not target_ctx:
            return ToolResult(
                content=f"Agent not found: {target}. Available agents: "
                + ", ".join(a.agent_id for a in agent_manager.get_running()),
                is_error=True,
            )

        # Send structured message based on type
        if msg_type == "shutdown_request":
            message = f"[SHUTDOWN_REQUEST from {context.agent_id or 'parent'}]: {message}"
        elif msg_type == "plan_approval_response":
            message = f"[PLAN_APPROVED from {context.agent_id or 'parent'}]: {message}"

        target_ctx.messages.append({"role": "user", "content": message})

        return ToolResult(
            content=f"Message sent to {target_ctx.agent_id} "
            f"(type={msg_type}, len={len(message)} chars)"
        )


class TaskOutputTool(Tool):
    """Extended version: get output from a running/completed agent."""

    name = "TaskOutput"
    description = """Retrieve output from a running or completed background task/agent."""
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task/agent ID to get output from"},
            "block": {"type": "boolean", "default": True, "description": "Wait for completion"},
            "timeout": {"type": "integer", "default": 30000, "description": "Max wait ms"},
        },
        "required": ["task_id"],
    }
    is_read_only = True

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        from agents import agent_manager

        task_id = params["task_id"]
        block = params.get("block", True)
        timeout_ms = params.get("timeout", 30000)

        # Check running agents
        ctx = agent_manager.get_agent(task_id)
        if ctx and ctx.is_complete:
            return ToolResult(content=ctx.result or "Agent completed with no text output")

        if ctx and block:
            # Wait for completion
            elapsed = 0
            while not ctx.is_complete and elapsed < timeout_ms / 1000:
                await asyncio.sleep(1)
                elapsed += 1

            if ctx.is_complete:
                return ToolResult(content=ctx.result or "Agent completed")
            return ToolResult(content=f"Agent still running after {timeout_ms}ms. Use SendMessage to interact.")

        # Check completed agents
        for completed in agent_manager.get_completed():
            if completed.agent_id == task_id:
                return ToolResult(content=completed.result or "Agent completed")

        # Check notifications
        for notif in agent_manager.consume_notifications():
            if notif["task_id"] == task_id:
                status = notif["status"]
                result = notif["result"]
                return ToolResult(
                    content=f"<task-notification>\n"
                            f"<task-id>{task_id}</task-id>\n"
                            f"<status>{status}</status>\n"
                            f"<summary>{notif['summary']}</summary>\n"
                            f"<result>{result}</result>\n"
                            f"</task-notification>"
                )

        return ToolResult(content=f"No task found with ID: {task_id}", is_error=True)


class TaskStopTool(Tool):
    """Kill a running agent/task."""

    name = "TaskStop"
    description = """Stops a running background task or agent by its ID."""
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task/agent ID to stop"},
        },
        "required": ["task_id"],
    }

    async def call(self, params: dict[str, Any], context: ToolUseContext) -> ToolResult:
        from agents import agent_manager

        task_id = params["task_id"]
        agent_manager.kill(task_id)
        return ToolResult(content=f"Task {task_id} stopped")


tool_registry.register(SendMessageTool())
tool_registry.register(TaskOutputTool())
tool_registry.register(TaskStopTool())
