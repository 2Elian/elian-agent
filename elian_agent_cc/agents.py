"""
Complete Agent System — sub-agent spawning, lifecycle, communication, isolation.

Ported from:
  - tools/AgentTool/ (20 files): runAgent.ts, forkSubagent.ts, loadAgentsDir.ts
  - tools/AgentTool/built-in/*.ts (6 built-in agents)
  - tools/SendMessageTool/ — inter-agent communication
  - tools/TeamCreateTool/, TeamDeleteTool/ — team management
  - coordinator/coordinatorMode.ts — coordinator-worker pattern

Agent types (7):
  - explore:           Read-only code search
  - plan:              Architecture design, no implementation
  - general-purpose:   Full-capability research + execution
  - fork:              Inherits parent context, runs in background
  - verification:      Verifies agent output (PASS/FAIL/PARTIAL)
  - claude-code-guide: Answers Claude Code usage questions
  - statusline-setup:  Terminal PS1 configuration
"""
import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable


# =============================================================================
# Enums and constants
# =============================================================================

class AgentType(str, Enum):
    EXPLORE = "Explore"
    PLAN = "Plan"
    GENERAL_PURPOSE = "general-purpose"
    FORK = "fork"
    VERIFICATION = "verification"
    CLAUDE_CODE_GUIDE = "claude-code-guide"
    STATUSLINE_SETUP = "statusline-setup"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"
    DONT_ASK = "dontAsk"
    BUBBLE = "bubble"  # Permission prompts bubble to parent terminal


class IsolationMode(str, Enum):
    NONE = "none"
    WORKTREE = "worktree"


class AgentSource(str, Enum):
    BUILTIN = "built-in"
    USER = "userSettings"
    PROJECT = "projectSettings"
    POLICY = "policySettings"
    FLAG = "flagSettings"
    PLUGIN = "plugin"


class MemoryScope(str, Enum):
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


# Priority order for agent resolution (higher index = higher priority)
SOURCE_PRIORITY = {
    AgentSource.BUILTIN: 0,
    AgentSource.PLUGIN: 1,
    AgentSource.USER: 2,
    AgentSource.PROJECT: 3,
    AgentSource.FLAG: 4,
    AgentSource.POLICY: 5,
}

# Tools disallowed for ALL sub-agents (dangerous/meta tools)
ALL_AGENT_DISALLOWED_TOOLS = {
    "agent",          # Prevent recursive agent spawning
    "task_stop",      # Don't let sub-agents kill tasks
    "cron_create",    # Scheduling is parent-only
    "cron_delete",
}

# Additional disallowed tools for custom (non-builtin) agents
CUSTOM_AGENT_DISALLOWED_TOOLS = {
    "plan_mode",      # Plan mode is main-thread only
    "exit_plan_mode",
}

# Tools allowed for async background agents (strict allowlist)
ASYNC_AGENT_ALLOWED_TOOLS = {
    "Read", "Glob", "Grep", "Write", "Edit", "Bash",
    "WebFetch", "WebSearch", "task", "todo",
}

# Agent color palette (8 colors)
AGENT_COLORS = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"]

# Fork boilerplate tag (prevents recursive forking)
FORK_BOILERPLATE_TAG = "fork-boilerplate"
FORK_PLACEHOLDER_RESULT = "Fork started — processing in background"


# =============================================================================
# AgentDefinition — ported from loadAgentsDir.ts
# =============================================================================

@dataclass
class AgentDefinition:
    """Complete agent definition. Matches BaseAgentDefinition from TypeScript."""
    agent_type: str
    when_to_use: str
    description: str = ""
    tools: list[str] = field(default_factory=lambda: ["*"])
    disallowed_tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str | dict] = field(default_factory=list)
    hooks: dict[str, Any] = field(default_factory=dict)
    color: str = "blue"
    model: str = "inherit"
    effort: str = "medium"
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    max_turns: int = 25
    source: AgentSource = AgentSource.BUILTIN
    base_dir: str = ""
    background: bool = False
    initial_prompt: str = ""
    memory: MemoryScope | None = None
    isolation: IsolationMode = IsolationMode.NONE
    omit_claude_md: bool = False
    critical_reminder: str = ""
    required_mcp_servers: list[str] = field(default_factory=list)
    system_prompt: str = ""

    @property
    def allows_all_tools(self) -> bool:
        return "*" in self.tools

    @property
    def is_read_only(self) -> bool:
        return self.agent_type in (AgentType.EXPLORE.value, AgentType.PLAN.value)

    @property
    def is_builtin(self) -> bool:
        return self.source == AgentSource.BUILTIN

    @property
    def is_admin_trusted(self) -> bool:
        return self.source in (AgentSource.BUILTIN, AgentSource.PLUGIN, AgentSource.POLICY)


# =============================================================================
# Built-in agent definitions (ported from built-in/*.ts)
# =============================================================================

GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type=AgentType.GENERAL_PURPOSE.value,
    when_to_use="General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries, use this agent.",
    description="Full-capability research and execution agent",
    tools=["*"],
    color="blue",
    max_turns=25,
    system_prompt="""You are an agent for Claude Code, Anthropic's official CLI for Claude. Complete the task fully — don't gold-plate, but don't leave it half-done.

Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture
- Investigating complex questions that require exploring many files
- Performing multi-step research tasks

Guidelines:
- For file searches: search broadly when you don't know where something lives
- For analysis: Start broad and narrow down. Use multiple search strategies
- Be thorough: Check multiple locations, consider different naming conventions
- NEVER create files unless absolutely necessary. ALWAYS prefer editing existing files
- NEVER proactively create documentation files (*.md) or README files

When you complete the task, respond with a concise report covering what was done and any key findings.""",
)

EXPLORE_AGENT = AgentDefinition(
    agent_type=AgentType.EXPLORE.value,
    when_to_use="Fast read-only search agent for locating code. Use it to find files by pattern (eg. 'src/components/**/*.tsx'), grep for symbols or keywords (eg. 'API endpoints'), or answer 'where is X defined / which files reference Y'.",
    description="Read-only code search agent. Fast, no edits.",
    tools=["Read", "Glob", "Grep"],
    disallowed_tools=["Write", "Edit", "Bash", "agent", "NotebookEdit", "ExitPlanMode"],
    color="green",
    model="haiku",
    max_turns=10,
    omit_claude_md=True,
    system_prompt="""You are a code search and exploration agent. Your role is to find and read code.

Use Read, Glob, and Grep tools to search the codebase. Do NOT edit or write any files.
Return clear, organized results. Be thorough but concise. Cite file paths precisely.""",
)

PLAN_AGENT = AgentDefinition(
    agent_type=AgentType.PLAN.value,
    when_to_use="Software architect agent for designing implementation plans. Use this when you need to plan the implementation strategy for a task. Returns step-by-step plans, identifies critical files, and considers architectural trade-offs.",
    description="Architecture planning agent. Designs implementation approaches.",
    tools=["Read", "Glob", "Grep", "Write"],
    disallowed_tools=["Edit", "Bash", "agent", "NotebookEdit"],
    color="purple",
    permission_mode=PermissionMode.PLAN,
    max_turns=15,
    omit_claude_md=True,
    system_prompt="""You are a software architecture planning agent. Your role is to design implementation approaches.

1. Read and analyze code to understand the current state
2. Design implementation plans with specific steps
3. Identify critical files and dependencies
4. Consider edge cases and trade-offs

Output format:
## Plan
- Step-by-step implementation steps
### Critical Files for Implementation
- List each critical file with its path
## Trade-offs Considered
- Discuss alternatives considered""",
)

VERIFICATION_AGENT = AgentDefinition(
    agent_type=AgentType.VERIFICATION.value,
    when_to_use="Verification agent that reviews code changes and validates correctness.",
    description="Reviews and verifies code changes. Returns PASS/FAIL/PARTIAL verdict.",
    tools=["*"],
    disallowed_tools=["agent", "NotebookEdit", "ExitPlanMode"],
    color="red",
    model="inherit",
    background=True,
    max_turns=20,
    critical_reminder="You MUST output VERDICT: PASS, FAIL, or PARTIAL at the end of your response.",
    system_prompt="""You are a verification agent. Your task is to review code changes and validate correctness.

Testing strategy:
- Check for logical errors, edge cases, and security issues
- Verify the changes match the intended behavior
- Look for regressions in related code

Output format:
## Analysis
[Your detailed analysis]

## VERDICT: PASS / FAIL / PARTIAL
[Final verdict with reasoning]""",
)

CLAUDE_CODE_GUIDE_AGENT = AgentDefinition(
    agent_type=AgentType.CLAUDE_CODE_GUIDE.value,
    when_to_use="Use when the user asks questions about Claude Code features, hooks, slash commands, MCP servers, settings, IDE integrations, or keyboard shortcuts.",
    description="Answers questions about Claude Code CLI usage and features.",
    tools=["Read", "Glob", "Grep"],
    disallowed_tools=["Write", "Edit", "Bash", "agent"],
    color="cyan",
    model="haiku",
    permission_mode=PermissionMode.DONT_ASK,
    max_turns=10,
    system_prompt="""You are a Claude Code usage expert. Answer questions about Claude Code features.

Topics you cover:
- CLI features, slash commands, hooks
- MCP server configuration
- Settings and customization
- IDE integrations and keyboard shortcuts
- Agent/Skill/Plugin systems

Be concise and accurate. Reference the official documentation when possible.""",
)

STATUSLINE_SETUP_AGENT = AgentDefinition(
    agent_type=AgentType.STATUSLINE_SETUP.value,
    when_to_use="Use to configure the user's Claude Code status line setting.",
    description="Configures terminal status line display.",
    tools=["Read", "Edit"],
    color="orange",
    model="sonnet",
    max_turns=10,
    system_prompt="""You are a terminal configuration agent. Help the user set up their status line.

You can read and edit shell configuration files (like .bashrc, .zshrc) to add status line integration.""",
)

FORK_AGENT_DEF = AgentDefinition(
    agent_type=AgentType.FORK.value,
    when_to_use="Implicit fork — inherits full conversation context. Not selectable via subagent_type; triggered by omitting subagent_type.",
    description="Fork sub-agent inheriting parent context. Runs in background.",
    tools=["*"],
    color="pink",
    model="inherit",
    permission_mode=PermissionMode.BUBBLE,
    max_turns=200,
    system_prompt="",  # Fork inherits parent's rendered system prompt
)

BUILT_IN_AGENTS: dict[str, AgentDefinition] = {
    AgentType.GENERAL_PURPOSE.value: GENERAL_PURPOSE_AGENT,
    AgentType.EXPLORE.value: EXPLORE_AGENT,
    AgentType.PLAN.value: PLAN_AGENT,
    AgentType.VERIFICATION.value: VERIFICATION_AGENT,
    AgentType.CLAUDE_CODE_GUIDE.value: CLAUDE_CODE_GUIDE_AGENT,
    AgentType.STATUSLINE_SETUP.value: STATUSLINE_SETUP_AGENT,
}


# =============================================================================
# AgentDefinition loading from .claude/agents/*.md
# =============================================================================

def load_agent_from_md(file_path: Path) -> AgentDefinition | None:
    """Parse a .claude/agents/*.md file with YAML frontmatter.

    Format:
    ---
    agentType: my-agent
    whenToUse: Use when doing X
    tools: [Read, Grep, Glob]
    model: inherit
    permissionMode: acceptEdits
    maxTurns: 15
    color: yellow
    background: false
    ---
    (System prompt body)
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        import yaml
        frontmatter = yaml.safe_load(parts[1]) or {}
    except ImportError:
        frontmatter = {}
        for line in parts[1].strip().split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                frontmatter[k.strip()] = v.strip().strip("[]").replace('"', "").replace("'", "")

    body = parts[2].strip()

    tools_raw = frontmatter.get("tools", ["*"]) if "tools" in frontmatter else ["*"]
    if isinstance(tools_raw, str):
        tools_raw = [t.strip() for t in tools_raw.strip("[]").split(",") if t.strip()]

    disallowed_raw = frontmatter.get("disallowedTools", [])
    if isinstance(disallowed_raw, str):
        disallowed_raw = [t.strip() for t in disallowed_raw.strip("[]").split(",") if t.strip()]

    color = frontmatter.get("color", "blue")
    if color not in AGENT_COLORS:
        color = "blue"

    perm_mode = frontmatter.get("permissionMode", "default")
    try:
        perm_mode_enum = PermissionMode(perm_mode)
    except ValueError:
        perm_mode_enum = PermissionMode.DEFAULT

    isolation_raw = frontmatter.get("isolation", "none")
    try:
        isolation = IsolationMode(isolation_raw)
    except ValueError:
        isolation = IsolationMode.NONE

    memory_raw = frontmatter.get("memory")
    memory = None
    if memory_raw:
        try:
            memory = MemoryScope(memory_raw)
        except ValueError:
            pass

    return AgentDefinition(
        agent_type=frontmatter.get("agentType", file_path.stem),
        when_to_use=frontmatter.get("whenToUse", ""),
        description=frontmatter.get("description", ""),
        tools=tools_raw,
        disallowed_tools=disallowed_raw,
        skills=frontmatter.get("skills", []),
        mcp_servers=frontmatter.get("mcpServers", []),
        hooks=frontmatter.get("hooks", {}),
        color=color,
        model=frontmatter.get("model", "inherit"),
        effort=frontmatter.get("effort", "medium"),
        permission_mode=perm_mode_enum,
        max_turns=frontmatter.get("maxTurns", 25),
        source=AgentSource.PROJECT,
        background=frontmatter.get("background", False),
        initial_prompt=frontmatter.get("initialPrompt", ""),
        memory=memory,
        isolation=isolation,
        omit_claude_md=frontmatter.get("omitClaudeMd", False),
        system_prompt=body,
    )


def load_agents_from_dir(dir_path: Path) -> list[AgentDefinition]:
    """Load all agent definitions from a directory."""
    if not dir_path.exists():
        return []
    agents = []
    for md_file in dir_path.glob("*.md"):
        agent = load_agent_from_md(md_file)
        if agent:
            agent.base_dir = str(dir_path)
            agents.append(agent)
    return agents


def discover_agent_dirs(cwd: str = ".") -> list[Path]:
    """Walk up from cwd to find .claude/agents/ directories."""
    current = Path(cwd).resolve()
    dirs = []
    seen = set()
    while True:
        agents_dir = current / ".claude" / "agents"
        if agents_dir.exists() and str(agents_dir) not in seen:
            seen.add(str(agents_dir))
            dirs.append(agents_dir)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return dirs


# =============================================================================
# Agent Registry — loads, resolves, and manages all agents
# =============================================================================

@dataclass
class AgentRegistry:
    """Central registry for agent definitions with priority resolution."""
    builtin: dict[str, AgentDefinition] = field(default_factory=dict)
    user: dict[str, AgentDefinition] = field(default_factory=dict)
    project: dict[str, AgentDefinition] = field(default_factory=dict)
    policy: dict[str, AgentDefinition] = field(default_factory=dict)
    plugin: dict[str, AgentDefinition] = field(default_factory=dict)

    def load_all(self, cwd: str = ".") -> list[AgentDefinition]:
        """Load agents from all sources, resolve by priority."""
        self.builtin = dict(BUILT_IN_AGENTS)

        # Load project agents (walk up from cwd)
        for agents_dir in discover_agent_dirs(cwd):
            for agent in load_agents_from_dir(agents_dir):
                agent.source = AgentSource.PROJECT
                self.project[agent.agent_type] = agent

        # Load user agents
        user_dir = Path.home() / ".claude" / "agents"
        for agent in load_agents_from_dir(user_dir):
            agent.source = AgentSource.USER
            self.user[agent.agent_type] = agent

        # Resolve: higher priority overrides lower
        return self.resolve_active()

    def resolve_active(self) -> list[AgentDefinition]:
        """Resolve active agents by source priority (higher wins)."""
        resolved: dict[str, AgentDefinition] = {}

        for source, agents in [
            (AgentSource.BUILTIN, self.builtin),
            (AgentSource.PLUGIN, self.plugin),
            (AgentSource.USER, self.user),
            (AgentSource.PROJECT, self.project),
            (AgentSource.POLICY, self.policy),
        ]:
            for name, agent in agents.items():
                if name in resolved:
                    existing_priority = SOURCE_PRIORITY.get(resolved[name].source, 0)
                    new_priority = SOURCE_PRIORITY.get(source, 0)
                    if new_priority < existing_priority:
                        continue
                resolved[name] = agent

        return list(resolved.values())

    def get(self, agent_type: str) -> AgentDefinition | None:
        """Get agent by type string."""
        try:
            at = AgentType(agent_type)
            return BUILT_IN_AGENTS.get(at.value)
        except ValueError:
            pass
        all_agents = {**self.builtin, **self.user, **self.project, **self.policy}
        return all_agents.get(agent_type)

    def match_for_task(self, task_description: str) -> AgentDefinition:
        """Select the best agent for a task based on keywords."""
        task_lower = task_description.lower()

        explore_keywords = ["find", "search", "locate", "where is", "grep", "glob", "which file"]
        plan_keywords = ["plan", "design", "architecture", "approach", "how to implement", "refactor"]
        verify_keywords = ["verify", "validate", "check", "review", "test"]
        guide_keywords = ["how do i", "what is", "claude code", "slash command", "keybinding"]

        if any(kw in task_lower for kw in explore_keywords):
            return EXPLORE_AGENT
        if any(kw in task_lower for kw in plan_keywords):
            return PLAN_AGENT
        if any(kw in task_lower for kw in verify_keywords):
            return VERIFICATION_AGENT
        if any(kw in task_lower for kw in guide_keywords):
            return CLAUDE_CODE_GUIDE_AGENT
        return GENERAL_PURPOSE_AGENT


# =============================================================================
# Agent Context — runtime state for a running agent
# =============================================================================

@dataclass
class AgentContext:
    """Runtime state for a spawned agent instance. Matches TS AgentContext."""
    agent_id: str
    definition: AgentDefinition
    cwd: str
    session_id: str
    parent_session_id: str = ""
    max_turns: int = 25
    turn_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    is_complete: bool = False
    is_async: bool = False
    result: str = ""
    errors: list[str] = field(default_factory=list)
    worktree_path: str | None = None
    worktree_branch: str | None = None
    messages: list[dict] = field(default_factory=list)
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    mcp_connections: list[Any] = field(default_factory=list)
    output_file: str = ""


# =============================================================================
# Agent Manager — full lifecycle
# =============================================================================

class AgentManager:
    """Manages agent lifecycle: spawn, run, communicate, cleanup.

    Ported from runAgent.ts + agentToolUtils.ts + forkSubagent.ts.
    """

    def __init__(self):
        self._running: dict[str, AgentContext] = {}
        self._completed: list[AgentContext] = []
        self._pending_notifications: list[dict] = []
        self._name_registry: dict[str, str] = {}  # name → agentId
        self._agent_colors: dict[str, str] = {}

    # ========================================================================
    # Agent resolution
    # ========================================================================

    def resolve_tools(
        self,
        definition: AgentDefinition,
        parent_tools: list[str] | None = None,
        use_exact_tools: bool = False,
    ) -> list[str]:
        """Resolve tool allowlist for an agent.

        Ported from resolveAgentTools() in agentToolUtils.ts.
        """
        # Get available tool names
        from tools.base import tool_registry
        all_names = [t.name for t in tool_registry.list_all()]

        if use_exact_tools and parent_tools:
            base = list(parent_tools)
        elif definition.allows_all_tools:
            base = list(all_names)
        else:
            base = [t for t in definition.tools if t in all_names]

        # Apply disallowed tools
        disallowed = set(ALL_AGENT_DISALLOWED_TOOLS)
        for dt in definition.disallowed_tools:
            disallowed.add(dt)
        if not definition.is_builtin:
            for dt in CUSTOM_AGENT_DISALLOWED_TOOLS:
                disallowed.add(dt)

        result = [t for t in base if t not in disallowed]
        return result

    # ========================================================================
    # Spawn
    # ========================================================================

    def spawn(
        self,
        definition: AgentDefinition,
        cwd: str = ".",
        parent_session_id: str = "",
        is_async: bool = False,
        prompt_messages: list[dict] | None = None,
    ) -> AgentContext:
        """Spawn a new agent instance."""
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"

        ctx = AgentContext(
            agent_id=agent_id,
            definition=definition,
            cwd=cwd,
            session_id=agent_id,
            parent_session_id=parent_session_id,
            max_turns=definition.max_turns,
            is_async=is_async,
            messages=prompt_messages or [],
        )

        # Register name
        self._name_registry[definition.agent_type] = agent_id
        self._agent_colors[agent_id] = definition.color

        self._running[agent_id] = ctx
        return ctx

    # ========================================================================
    # Run — synchronous
    # ========================================================================

    async def run_agent(
        self,
        ctx: AgentContext,
        prompt: str,
    ) -> str:
        """Run an agent synchronously. Returns final text result.

        Ported from runAgent() in runAgent.ts.
        """
        from providers import get_provider
        from config import MODEL, DEFAULT_PROVIDER
        from models import ToolUseContext, ToolPermissionContext

        # Build system prompt
        system_prompt = ctx.definition.system_prompt
        if not system_prompt:
            system_prompt = self._build_default_prompt(ctx.definition)

        # Resolve tools
        resolved = self.resolve_tools(ctx.definition)
        tool_schemas = []
        from tools.base import tool_registry
        for name in resolved:
            t = tool_registry.get(name)
            if t:
                tool_schemas.append(t.to_schema())

        # Build context
        tool_ctx = ToolUseContext(
            cwd=ctx.cwd,
            session_id=ctx.session_id,
            agent_id=ctx.agent_id,
            agent_type=ctx.definition.agent_type,
            tool_permission_context=ToolPermissionContext(
                mode=PermissionMode(ctx.definition.permission_mode.value),
                should_avoid_permission_prompts=ctx.is_async,
            ),
            main_loop_model=ctx.definition.model if ctx.definition.model != "inherit" else MODEL,
        )

        # Build messages
        messages = list(ctx.messages)
        if prompt:
            messages.append({"role": "user", "content": prompt})

        provider = get_provider(DEFAULT_PROVIDER)
        result_text = ""
        last_stop_reason = ""

        try:
            for turn in range(ctx.max_turns):
                if ctx.abort.is_set():
                    break

                try:
                    resp = await provider.chat(
                        model=tool_ctx.main_loop_model,
                        messages=messages,
                        system=system_prompt,
                        tools=tool_schemas,
                        max_tokens=4096,
                    )
                except Exception as e:
                    ctx.errors.append(f"API error at turn {turn}: {e}")
                    break

                result_text = resp.content
                ctx.total_tokens += resp.usage.total_tokens
                ctx.turn_count = turn + 1

                if not resp.tool_calls:
                    last_stop_reason = resp.stop_reason
                    break

                # Add assistant response
                messages.append({"role": "assistant", "content": resp.content})

                # Execute tool calls
                for tc in resp.tool_calls:
                    tool_result = await self._execute_tool(tc, tool_ctx)
                    messages.append({
                        "role": "user",
                        "content": f"Tool result for {tc['name']}:\n{tool_result.content}",
                    })

            ctx.is_complete = True

        except Exception as e:
            ctx.errors.append(str(e))
            result_text = f"Agent error: {e}"

        finally:
            ctx.result = result_text
            self._running.pop(ctx.agent_id, None)
            self._completed.append(ctx)

            if ctx.worktree_path:
                await self._cleanup_worktree(ctx)

        return result_text

    # ========================================================================
    # Run — asynchronous (background, with task-notification)
    # ========================================================================

    async def run_agent_async(self, ctx: AgentContext, prompt: str) -> None:
        """Run agent in background. Results delivered via task-notification.

        Ported from runAsyncAgentLifecycle() in agentToolUtils.ts.
        """
        ctx.is_async = True

        try:
            result = await self.run_agent(ctx, prompt)

            # Build task-notification
            notification = self._build_task_notification(ctx)
            self._pending_notifications.append(notification)

        except Exception as e:
            ctx.errors.append(str(e))
            notification = self._build_task_notification(ctx, failed=True)
            self._pending_notifications.append(notification)

    # ========================================================================
    # Fork mechanism
    # ========================================================================

    async def fork_agent(
        self,
        parent_messages: list[dict],
        parent_system_prompt: str,
        tool_names: list[str],
        cwd: str = ".",
        parent_session_id: str = "",
    ) -> AgentContext:
        """Fork a sub-agent inheriting the parent's full conversation context.

        Ported from forkSubagent.ts buildForkedMessages().
        """
        definition = FORK_AGENT_DEF

        # Build forked messages: keep parent context, add fork boilerplate
        fork_messages = list(parent_messages)
        fork_messages.insert(0, {
            "role": "user",
            "content": f"<{FORK_BOILERPLATE_TAG}>Fork from parent session</{FORK_BOILERPLATE_TAG}>\n\n"
                       f"This is a background fork. Continue the parent's work independently."
        })

        # Detect recursive fork
        if self._is_fork_child(parent_messages):
            return None  # type: ignore

        ctx = self.spawn(
            definition=FORK_AGENT_DEF,
            cwd=cwd,
            parent_session_id=parent_session_id,
            is_async=True,
            prompt_messages=fork_messages,
        )

        ctx.definition.system_prompt = parent_system_prompt  # Use parent's rendered prompt
        return ctx

    def _is_fork_child(self, messages: list[dict]) -> bool:
        """Check if messages contain fork boilerplate (prevent recursive fork)."""
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str) and FORK_BOILERPLATE_TAG in content:
                return True
        return False

    # ========================================================================
    # Worktree isolation
    # ========================================================================

    async def create_worktree(self, ctx: AgentContext) -> str | None:
        """Create a git worktree for agent isolation.

        Returns the worktree path, or None if creation failed.
        """
        import subprocess
        try:
            branch = f"agent/{ctx.agent_id}"
            worktree_path = f"/tmp/claude-worktrees/{ctx.agent_id}"

            result = subprocess.run(
                ["git", "worktree", "add", worktree_path, "-b", branch],
                cwd=ctx.cwd, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                # Try without creating a new branch
                result = subprocess.run(
                    ["git", "worktree", "add", "--detach", worktree_path, "HEAD"],
                    cwd=ctx.cwd, capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    return None

            ctx.worktree_path = worktree_path
            ctx.worktree_branch = branch
            ctx.cwd = worktree_path
            return worktree_path
        except Exception:
            return None

    async def _cleanup_worktree(self, ctx: AgentContext) -> None:
        """Remove worktree after agent completes."""
        if not ctx.worktree_path:
            return
        import subprocess
        try:
            # Check for changes
            has_changes = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                cwd=ctx.worktree_path, capture_output=True, text=True, timeout=10,
            )
            if has_changes.returncode == 0 and has_changes.stdout.strip():
                # Has changes — don't delete, report to user
                pass
            else:
                # No changes — clean up
                subprocess.run(
                    ["git", "worktree", "remove", ctx.worktree_path, "--force"],
                    cwd=ctx.cwd, capture_output=True, timeout=10,
                )
                if ctx.worktree_branch:
                    subprocess.run(
                        ["git", "branch", "-D", ctx.worktree_branch],
                        cwd=ctx.cwd, capture_output=True, timeout=10,
                    )
        except Exception:
            pass

    # ========================================================================
    # Task notification system
    # ========================================================================

    def _build_task_notification(self, ctx: AgentContext, failed: bool = False) -> dict:
        """Build task-notification XML message.

        Ported from enqueueAgentNotification() in agentToolUtils.ts.
        """
        status = "failed" if failed else ("completed" if ctx.is_complete else "running")
        summary = ctx.result[:200] if ctx.result else (ctx.errors[0][:200] if ctx.errors else "")
        return {
            "type": "task_notification",
            "task_id": ctx.agent_id,
            "status": status,
            "summary": summary,
            "agent_type": ctx.definition.agent_type,
            "usage": {
                "tokens": ctx.total_tokens,
                "cost": ctx.total_cost,
            },
            "result": ctx.result,
            "errors": ctx.errors,
            "worktree_path": ctx.worktree_path,
            "worktree_branch": ctx.worktree_branch,
        }

    def consume_notifications(self) -> list[dict]:
        """Consume pending task notifications."""
        notifications = list(self._pending_notifications)
        self._pending_notifications.clear()
        return notifications

    # ========================================================================
    # Helpers
    # ========================================================================

    async def _execute_tool(self, tool_call: dict, context: Any):
        from tools.base import tool_registry, ToolResult
        tool = tool_registry.find(tool_call.get("name", ""))
        if tool:
            try:
                return await tool.call(tool_call.get("input", {}), context)
            except Exception as e:
                return ToolResult(content=str(e), is_error=True)
        return ToolResult(content=f"Unknown tool: {tool_call.get('name')}", is_error=True)

    def _build_default_prompt(self, definition: AgentDefinition) -> str:
        """Build a default system prompt for an agent type."""
        prompts = {
            AgentType.EXPLORE.value: "You are a code search agent. Use Read, Glob, and Grep. Do not edit files. Return clear results.",
            AgentType.PLAN.value: "You are an architecture planning agent. Design implementation approaches. Output structured plans.",
            AgentType.GENERAL_PURPOSE.value: "You are a general-purpose agent. Research, analyze, execute. Be thorough and systematic.",
            AgentType.FORK.value: "You are a fork sub-agent. Execute your task independently. Return results concisely.",
            AgentType.VERIFICATION.value: "You are a verification agent. Review code changes. Output VERDICT: PASS/FAIL/PARTIAL.",
            AgentType.CLAUDE_CODE_GUIDE.value: "You are a Claude Code expert. Answer usage questions concisely and accurately.",
            AgentType.STATUSLINE_SETUP.value: "You are a terminal config agent. Help set up status line integration.",
        }
        return prompts.get(definition.agent_type, prompts[AgentType.GENERAL_PURPOSE.value])

    # ========================================================================
    # Management
    # ========================================================================

    def get_running(self) -> list[AgentContext]:
        return list(self._running.values())

    def get_completed(self) -> list[AgentContext]:
        return list(self._completed)

    def get_agent(self, agent_id: str) -> AgentContext | None:
        return self._running.get(agent_id)

    def find_by_name(self, name: str) -> AgentContext | None:
        agent_id = self._name_registry.get(name)
        if agent_id:
            return self._running.get(agent_id)
        return None

    def kill(self, agent_id: str) -> None:
        ctx = self._running.get(agent_id)
        if ctx:
            ctx.abort.set()
            self._running.pop(agent_id, None)


# Global instances
agent_manager = AgentManager()
agent_registry = AgentRegistry()
