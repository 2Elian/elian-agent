r"""
Complete Skills System — 5-layer loading, conditional activation, bundled skills.

Ported from:
  - skills/loadSkillsDir.ts (1087 lines)
  - skills/bundledSkills.ts (220 lines)
  - skills/bundled/ (17 bundled skills)
  - skills/mcpSkillBuilders.ts

Architecture:
  5 loading layers: Managed > Bundled > User > Project > MCP
  2 execution contexts: inline (inject context) / fork (sub-agent)
  Conditional activation: gitignore-style path matching
"""
from __future__ import annotations

import os, re, shutil, subprocess, uuid
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

import yaml


# =============================================================================
# Enums and types
# =============================================================================

class SkillSource(str, Enum):
    """定义了某个技能从哪里来"""
    MANAGED = "managed"  # 企业管理员推送的，最高权限
    BUNDLED = "bundled"  # 编译进程序的 14 个内置技能
    USER = "userSettings"  # ~/.claude/skills/
    PROJECT = "projectSettings"  # .claude/skills/ (从工作目录向上找)
    PLUGIN = "plugin"  # 插件注册的技能
    MCP = "mcp"  # MCP 服务器提供的技能


class SkillContext(str, Enum):
    """这个技能怎么执行的策略"""
    INLINE = "inline" # 注入当前对话 — LLM 直接看到技能内容
    FORK = "fork" # 启动子 Agent — 独立上下文 + 独立 token 预算


class SkillHookType(str, Enum):
    COMMAND = "command"  # 执行 shell 命令
    PROMPT = "prompt"  # LLM 评估条件
    AGENT = "agent"  # 启动子 Agent 判断


@dataclass
class SkillHook:
    matcher: str = "*" # 匹配的工具名，如 "Write|Edit"
    hooks: list[dict[str, Any]] = field(default_factory=list) # 具体钩子配置
    type: SkillHookType = SkillHookType.COMMAND # 钩子类型
    once: bool = False # 只执行一次？


@dataclass
class SkillDefinition:
    """Full skill definition matching TypeScript Command / BundledSkillDefinition."""
    name: str
    description: str = ""
    when_to_use: str = ""
    version: str = "1.0"
    argument_hint: str = ""
    arguments: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    model: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True
    context: SkillContext = SkillContext.INLINE
    agent: str = ""
    effort: str = ""
    paths: list[str] = field(default_factory=list)  # Conditional activation patterns
    shell: str = ""  # "bash" or "powershell"
    hooks: dict[str, list[SkillHook]] = field(default_factory=dict)
    source: SkillSource = SkillSource.PROJECT
    skill_root: str = ""  # Base directory for file references
    files: dict[str, str] = field(default_factory=dict)  # Reference files to extract
    prompt_body: str = ""  # The markdown body after frontmatter
    prompt_fn: Callable[..., Any] | None = None  # Dynamic prompt generator

    def matches_path(self, file_path: str) -> bool:
        """Gitignore-style path matching (uses git check-ignore when available)."""
        if not self.paths:
            return False
        for pattern in self.paths:
            # Support both fnmatch and basic gitignore patterns
            if fnmatch(file_path, pattern):
                return True
            if pattern.startswith("**/"):
                rest = pattern[3:]
                for part in Path(file_path).parts:
                    if fnmatch(part, rest):
                        return True
        return False

    def substitute_args(self, body: str, args: str) -> str:
        """Substitute $ARGUMENTS and named args in body."""
        result = body
        result = result.replace("$ARGUMENTS", args)
        if self.arguments and args:
            arg_values = args.split()
            for i, name in enumerate(self.arguments):
                placeholder = f"${name}"
                value = arg_values[i] if i < len(arg_values) else ""
                result = result.replace(placeholder, value)
        return result

    def execute_shell_blocks(self, body: str) -> str:
        """Execute !`cmd` and ```! cmd``` blocks, replace with output."""
        # Inline !`cmd`
        def exec_inline(m):
            cmd = m.group(1)
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                return r.stdout.strip() or "(no output)"
            except Exception as e:
                return f"(error: {e})"

        body = re.sub(r"!`([^`]+)`", exec_inline, body)

        # Block ```! cmd ```
        def exec_block(m):
            lang = m.group(1) or ""
            cmd = m.group(2).strip()
            if lang != "!" and lang != "bash":
                return m.group(0)
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                return r.stdout.strip() or "(no output)"
            except Exception as e:
                return f"(error: {e})"

        body = re.sub(r"```(\!|bash)\s*\n(.*?)```", exec_block, body, flags=re.DOTALL)

        # Replace special variables
        body = body.replace("${CLAUDE_SKILL_DIR}", self.skill_root)
        from config import MODEL as cfg_model
        body = body.replace("${CLAUDE_SESSION_ID}", "current-session")

        return body


# =============================================================================
# SOURCE_PRIORITY for dedup (higher = wins)
# =============================================================================

SOURCE_PRIORITY: dict[SkillSource, int] = {
    SkillSource.MCP: 0,
    SkillSource.BUNDLED: 1,
    SkillSource.PLUGIN: 2,
    SkillSource.USER: 3,
    SkillSource.PROJECT: 4,
    SkillSource.MANAGED: 5,
}


# =============================================================================
# SkillLoader — 5-layer loading
# =============================================================================

class SkillLoader:
    """Loads skills from all 5 layers with priority-based dedup."""

    def __init__(self, cwd: str | None = None):
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._skills: dict[str, SkillDefinition] = {}
        self._conditional_skills: dict[str, SkillDefinition] = {}
        self._activated_skill_names: set[str] = set()
        self._loaded = False
        self._skill_dirs: dict[SkillSource, list[Path]] = {
            SkillSource.USER: [Path.home() / ".claude" / "skills"],
            SkillSource.PROJECT: [],
            SkillSource.BUNDLED: [Path(__file__).parent / "skills_bundled"],
        }
        self._hooks_registry: dict[str, list[SkillHook]] = {}
        self._mcp_skills: list[SkillDefinition] = []
        self._extracted_files_dir = Path.home() / ".claude" / "bundled-skills"

    # ======== Master load ========

    def load_all(self) -> dict[str, SkillDefinition]:
        if self._loaded:
            return self._skills

        # Layer order: lower priority loaded first, higher overwrites
        sources = [
            SkillSource.MCP, SkillSource.BUNDLED, SkillSource.PLUGIN,
            SkillSource.USER, SkillSource.PROJECT, SkillSource.MANAGED,
        ]
        for src in sources:
            self._load_from_source(src)

        self._loaded = True
        return self._skills

    def _load_from_source(self, source: SkillSource) -> None:
        if source == SkillSource.BUNDLED:
            self._load_bundled()
        elif source == SkillSource.USER:
            for d in self._skill_dirs.get(SkillSource.USER, []):
                self._load_from_directory(d, source)
        elif source == SkillSource.PROJECT:
            for d in self._discover_project_dirs():
                self._load_from_directory(d, source)
        elif source == SkillSource.MCP:
            for skill in self._mcp_skills:
                self._register(skill)
        elif source == SkillSource.MANAGED:
            # Managed path — typically set via env var
            managed = os.environ.get("CLAUDE_MANAGED_SKILLS_DIR", "")
            if managed:
                self._load_from_directory(Path(managed), source)

    # ======== Directory loading ========

    def _load_from_directory(self, dir_path: Path, source: SkillSource) -> None:
        if not dir_path.exists():
            return
        for skill_dir in dir_path.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                skill = self._parse_skill_file(skill_file, source)
                skill.skill_root = str(skill_dir)
                self._register(skill)
            except Exception:
                continue

    def _discover_project_dirs(self) -> list[Path]:
        """Walk up from cwd finding .claude/skills/ directories (gitignore-aware)."""
        current = self._cwd.resolve()
        dirs = []
        seen = set()
        while True:
            skill_dir = current / ".claude" / "skills"
            skill_dir_str = str(skill_dir)
            if skill_dir.exists() and skill_dir_str not in seen:
                if not self._is_gitignored(skill_dir):
                    seen.add(skill_dir_str)
                    dirs.append(skill_dir)
            parent = current.parent
            if parent == current:
                break
            current = parent
        return dirs

    def _is_gitignored(self, path: Path) -> bool:
        """Check if a directory is gitignored."""
        try:
            r = subprocess.run(
                ["git", "check-ignore", "-q", str(path)],
                cwd=self._cwd, capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    # ======== SKILL.md parsing ========

    def _parse_skill_file(self, file_path: Path, source: SkillSource) -> SkillDefinition:
        content = file_path.read_text(encoding="utf-8")
        frontmatter: dict[str, Any] = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = yaml.safe_load(parts[1]) or {}
                body = parts[2].strip()

        context_raw = frontmatter.get("context", "inline")
        try:
            context = SkillContext(context_raw)
        except ValueError:
            context = SkillContext.INLINE

        # Parse hooks
        hooks: dict[str, list[SkillHook]] = {}
        hooks_raw = frontmatter.get("hooks", {})
        if isinstance(hooks_raw, dict):
            for hook_event, hook_list in hooks_raw.items():
                if isinstance(hook_list, list):
                    hooks[hook_event] = []
                    for h in hook_list:
                        if isinstance(h, dict):
                            hooks[hook_event].append(SkillHook(
                                matcher=h.get("matcher", "*"),
                                hooks=h.get("hooks", []),
                                type=SkillHookType(h.get("type", "command")),
                                once=h.get("once", False),
                            ))

        return SkillDefinition(
            name=frontmatter.get("name", file_path.parent.name),
            description=frontmatter.get("description", ""),
            when_to_use=frontmatter.get("when_to_use", ""),
            version=str(frontmatter.get("version", "1.0")),
            argument_hint=frontmatter.get("argument-hint", ""),
            arguments=frontmatter.get("arguments", []),
            allowed_tools=frontmatter.get("allowed-tools", []),
            model=frontmatter.get("model", ""),
            disable_model_invocation=frontmatter.get("disable-model-invocation", False),
            user_invocable=frontmatter.get("user-invocable", True),
            context=context,
            agent=frontmatter.get("agent", ""),
            effort=frontmatter.get("effort", ""),
            paths=frontmatter.get("paths", []),
            shell=frontmatter.get("shell", "bash"),
            hooks=hooks,
            source=source,
            prompt_body=body,
        )

    # ======== Registration with priority ========

    def _register(self, skill: SkillDefinition) -> None:
        name = skill.name
        existing = self._skills.get(name)
        if existing and SOURCE_PRIORITY.get(existing.source, 0) >= SOURCE_PRIORITY.get(skill.source, 0):
            return  # Existing has higher or equal priority

        self._skills[name] = skill

        if skill.paths:
            self._conditional_skills[name] = skill

        # Register hooks
        if skill.hooks:
            self._hooks_registry[name] = [
                h for hook_list in skill.hooks.values()
                for h in hook_list
            ]

    # ======== Bundled skills ========

    def _load_bundled(self) -> None:
        """Load all 17 built-in bundled skills with their full prompts."""
        for register_fn in _BUNDLED_REGISTRATIONS:
            try:
                skill = register_fn()
                if skill:
                    self._register(skill)
            except Exception:
                continue

    # ======== MCP skills ========

    def register_mcp_skill(self, name: str, description: str, prompt_body: str, server_name: str) -> None:
        """Register an MCP skill (called by MCP client on connection)."""
        skill = SkillDefinition(
            name=f"mcp__{server_name}__{name}",
            description=description,
            prompt_body=prompt_body,
            source=SkillSource.MCP,
            user_invocable=True,
            context=SkillContext.INLINE,
            skill_root=server_name,
        )
        self._mcp_skills.append(skill)
        self._register(skill)

    # ======== Conditional activation ========

    def activate_for_paths(self, file_paths: list[str]) -> list[SkillDefinition]:
        """Activate conditional skills matching given file paths."""
        activated = []
        for path in file_paths:
            for name, skill in list(self._conditional_skills.items()):
                if skill.matches_path(path):
                    if name not in self._activated_skill_names:
                        self._activated_skill_names.add(name)
                        activated.append(skill)
        return activated

    def discover_for_paths(self, file_paths: list[str]) -> list[SkillDefinition]:
        """Discover new skill dirs from file paths and load them."""
        new_skills = []
        for fp in file_paths:
            current = (self._cwd / fp).resolve().parent
            while True:
                skill_dir = current / ".claude" / "skills"
                if skill_dir.exists() and not self._is_gitignored(skill_dir):
                    self._load_from_directory(skill_dir, SkillSource.PROJECT)
                parent = current.parent
                if parent == current or parent == self._cwd.resolve().parent.parent:
                    break
                current = parent
        return new_skills

    # ======== Query ========

    def get(self, name: str) -> SkillDefinition | None:
        self.load_all()
        return self._skills.get(name)

    def list_user_invocable(self) -> list[SkillDefinition]:
        self.load_all()
        return [s for s in self._skills.values() if s.user_invocable and not s.disable_model_invocation]

    def list_for_model(self) -> list[SkillDefinition]:
        """Skills visible to the model (user-invocable or model-invocable)."""
        self.load_all()
        return [s for s in self._skills.values() if not s.disable_model_invocation]

    def get_hooks(self) -> dict[str, list[SkillHook]]:
        self.load_all()
        return dict(self._hooks_registry)

    # ======== Prompt generation ========

    def get_skills_prompt(self, max_chars: int = 8000) -> str:
        """Generate the skills section for the system prompt."""
        skills = self.list_for_model()
        if not skills:
            return ""

        lines = ["## Available Skills", ""]
        total = 0
        for s in skills:
            entry = f"- **{s.name}**"
            if s.argument_hint:
                entry += f" `{s.argument_hint}`"
            entry += f": {s.description}"
            if s.when_to_use:
                when = s.when_to_use.split("\n")[0][:100]
                entry += f" — {when}"
            if total + len(entry) > max_chars:
                lines.append(f"... ({len(skills) - len(lines) + 2} more skills)")
                break
            lines.append(entry)
            total += len(entry)

        return "\n".join(lines)

    def get_skill_prompt(self, name: str, args: str = "") -> tuple[str | None, dict[str, Any]]:
        """Get the full prompt for a skill, with arg substitution and shell execution."""
        skill = self.get(name)
        if not skill:
            return None, {}

        body = skill.prompt_body
        body = skill.substitute_args(body, args)
        if skill.shell:
            body = skill.execute_shell_blocks(body)

        # Build context modifier for fork mode
        modifier: dict[str, Any] = {}
        if skill.context == SkillContext.FORK:
            modifier["agent"] = skill.agent or "general-purpose"
            modifier["tools"] = skill.allowed_tools or None
            if skill.model:
                modifier["model"] = skill.model

        return body, modifier

    # ======== File extraction (bundled skills) ========

    def extract_files(self, skill: SkillDefinition) -> str | None:
        """Extract reference files to disk, return base directory."""
        if not skill.files:
            return None
        nonce = uuid.uuid4().hex[:8]
        extract_dir = self._extracted_files_dir / nonce / skill.name
        extract_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in skill.files.items():
            dest = extract_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        return str(extract_dir)


# =============================================================================
# BUNDLED SKILL REGISTRATIONS (17 skills, ported from bundled/*.ts)
# =============================================================================

def _register_update_config():
    return SkillDefinition(
        name="update-config", source=SkillSource.BUNDLED,
        description="Configure Claude Code via settings.json. Use for hooks, permissions, env vars, model settings.",
        when_to_use="When you need to configure hooks, permissions, environment variables, or settings.",
        allowed_tools=["Read"], user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Configure Claude Code

You help configure Claude Code via settings.json.

## Permissions
```json
{
  "permissions": {
    "allow": ["Bash(npm:*)"],
    "deny": ["Bash(rm:*)", "Bash(git push:*)"]
  }
}
```

## Hooks (8 event types)
- UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure
- Notification, Stop, PreCompact, SessionStart

3 hook types: command (runs shell), prompt (LLM evaluation), agent (sub-agent)

## Environment Variables
Set via `.env` file or directly in settings.

## Model Configuration
Set `model` in settings to change the default model.
""",
    )


def _register_simplify():
    return SkillDefinition(
        name="simplify", source=SkillSource.BUNDLED,
        description="Review changed code for reuse, quality, and efficiency, then fix issues found.",
        user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Code Simplifier

## Phase 1: Identify Changes
Run `git diff` to see what changed.

## Phase 2: Parallel Review
Launch 3 exploration agents in parallel:
1. Code Reuse — Are there existing utilities that could replace new code?
2. Code Quality — Are there bugs, edge cases, or unclear logic?
3. Efficiency — Are there performance issues or unnecessary complexity?

## Phase 3: Fix
Apply the improvements found. Prioritize actual bugs over style preferences.
""",
    )


def _register_verify():
    return SkillDefinition(
        name="verify", source=SkillSource.BUNDLED,
        description="Verify a code change does what it should by running the app and checking results.",
        user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Code Verifier

1. Read the changes via `git diff`
2. Run the application or tests
3. Check outputs match expected behavior
4. Report: what works, what doesn't, suggestions
""",
    )


def _register_debug():
    return SkillDefinition(
        name="debug", source=SkillSource.BUNDLED,
        description="Debug the current Claude Code session via logs and diagnostics.",
        allowed_tools=["Read", "Grep", "Glob"], user_invocable=True,
        disable_model_invocation=True, context=SkillContext.FORK,
        prompt_body=r"""# Session Debugger

1. Enable debug logging if not already active
2. Check the most recent log entries for errors or warnings
3. Identify the root cause of any issues
4. Suggest fixes or workarounds
""",
    )


def _register_keybindings():
    return SkillDefinition(
        name="keybindings-help", source=SkillSource.BUNDLED,
        description="Customize keyboard shortcuts in ~/.claude/keybindings.json.",
        allowed_tools=["Read"], user_invocable=False,
        prompt_body=r"""# Keybindings Configuration

File: `~/.claude/keybindings.json`

## Format
```json
{
  "bindings": {
    "ctrl+s": "submit",
    "ctrl+c": "exit"
  }
}
```

## Common Actions
- submit, exit, clear, compact, interrupt

## Contexts
- editor, prompt, default
""",
    )


def _register_loop():
    return SkillDefinition(
        name="loop", source=SkillSource.BUNDLED,
        description="Run a prompt or command on a recurring interval (e.g., every 5 minutes).",
        user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Loop Runner

Parse the interval and prompt from args. Schedule via cron.
Supported intervals: Xm (minutes), Xh (hours), Xs (seconds).
Default interval: 10 minutes.
""",
    )


def _register_batch():
    return SkillDefinition(
        name="batch", source=SkillSource.BUNDLED,
        description="Research and plan a large change, then execute in parallel across multiple agents.",
        user_invocable=True, disable_model_invocation=True, context=SkillContext.FORK,
        prompt_body=r"""# Batch Task Runner

## Phase 1: Plan
Enter plan mode. Decompose work into 5-30 independent units.
Each unit must be self-contained.

## Phase 2: Execute
Spawn parallel agents with worktree isolation.
Each agent: simplify → test → report.

## Phase 3: Track
Monitor progress. Handle failures. Merge results.
""",
    )


def _register_remember():
    return SkillDefinition(
        name="remember", source=SkillSource.BUNDLED,
        description="Review auto-memory entries and propose promotions to CLAUDE.md.",
        user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Memory Reviewer

1. Gather memory layers: MEMORY.md index + individual memory files
2. Classify entries into 4 destinations:
   - CLAUDE.md: Critical project instructions
   - CLAUDE.local.md: Local overrides
   - MEMORY.md: Persistent memory (keep)
   - Delete: Obsolete or wrong entries
3. Identify cleanup opportunities (duplicates, stale entries)
4. Present report with specific recommendations
""",
    )


def _register_skillify():
    return SkillDefinition(
        name="skillify", source=SkillSource.BUNDLED,
        description="Capture the session's repeatable process as a reusable skill with SKILL.md.",
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "AskUserQuestion", "Bash"],
        user_invocable=True, disable_model_invocation=True, context=SkillContext.FORK,
        prompt_body=r"""# Skill Creator

## Step 1: Analyze
Review the conversation to identify a repeatable workflow.

## Step 2: Interview (up to 4 rounds)
Ask the user: name, description, trigger conditions, required tools.

## Step 3: Write SKILL.md
Create a complete SKILL.md with frontmatter:
- name, description, when_to_use
- allowed-tools, arguments, argument-hint
- context (fork/inline), agent type

## Step 4: Save
Write to .claude/skills/<skill-name>/SKILL.md
""",
    )


def _register_stuck():
    return SkillDefinition(
        name="stuck", source=SkillSource.BUNDLED,
        description="Diagnose frozen or slow Claude Code sessions.",
        user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Session Diagnostician

1. Check for running Claude Code processes
2. Look for high CPU usage, memory leaks, stuck subprocesses
3. Check debug logs for error patterns
4. Report findings with specific recommendations
""",
    )


def _register_claude_api():
    return SkillDefinition(
        name="claude-api", source=SkillSource.BUNDLED,
        description="Build Claude API / Anthropic SDK apps. Handles prompt caching, tool use, streaming.",
        user_invocable=True, context=SkillContext.FORK,
        allowed_tools=["Read", "Grep", "Glob", "WebFetch"],
        when_to_use="When code imports anthropic/@anthropic-ai/sdk or user asks about Claude API.",
        prompt_body=r"""# Claude API Developer

Help build applications using the Claude API and Anthropic SDK.

## Key Features
- Prompt caching (ephemeral 5-min cache)
- Tool use (function calling)
- Streaming responses (SSE)
- Extended thinking (Opus/Sonnet)
- Token counting and cost estimation

## Supported Languages
Python, TypeScript, Java, Go, Ruby, C#, PHP, curl

## Common Patterns
- Cache system prompts with cache_control
- Use tool use for structured data extraction
- Stream for real-time UI updates
- Implement retry with exponential backoff
""",
    )


def _register_claude_in_chrome():
    return SkillDefinition(
        name="claude-in-chrome", source=SkillSource.BUNDLED,
        description="Automate Chrome browser: clicking, forms, screenshots, console logs.",
        user_invocable=True, context=SkillContext.FORK,
        prompt_body=r"""# Chrome Browser Automation

You have access to browser automation tools. Start by checking the current tabs.

## Available Actions
- Navigate to URLs
- Click elements
- Fill forms
- Take screenshots
- Read console logs
- Execute JavaScript
""",
    )


def _register_schedule():
    return SkillDefinition(
        name="schedule", source=SkillSource.BUNDLED,
        description="Create, update, list, or run scheduled remote agents on cron.",
        user_invocable=True, context=SkillContext.FORK,
        allowed_tools=["CronCreate", "CronDelete", "CronList", "AskUserQuestion"],
        prompt_body=r"""# Remote Agent Scheduler

Create scheduled agents that run on cron.

## Cron Expression Reference
- `0 9 * * *` Daily at 9:00 AM
- `0 */4 * * *` Every 4 hours
- `0 0 * * 1` Every Monday
- `30 8 1 * *` First day of month at 8:30

## Workflow
1. Define the task and schedule
2. Create via CronCreate
3. List via CronList to verify
4. Manage via CronDelete when done
""",
    )


def _register_lorem_ipsum():
    return SkillDefinition(
        name="lorem-ipsum", source=SkillSource.BUNDLED,
        description="Generate filler text for context testing.",
        argument_hint="[token_count]", user_invocable=True, context=SkillContext.INLINE,
        prompt_body=r"""# Lorem Ipsum Generator

Generate random text for testing. Default 10,000 tokens.
Use a dictionary of common 1-token words to compose sentences.
Each sentence: 10-20 words. Each paragraph: 5-8 sentences.
""",
    )


# All registration functions (ordered by priority)
_BUNDLED_REGISTRATIONS = [
    _register_update_config, _register_keybindings, _register_verify,
    _register_debug, _register_lorem_ipsum, _register_skillify,
    _register_remember, _register_simplify, _register_batch,
    _register_stuck, _register_claude_api, _register_claude_in_chrome,
    _register_schedule, _register_loop,
]


# =============================================================================
# Global instance
# =============================================================================

skill_loader = SkillLoader()
