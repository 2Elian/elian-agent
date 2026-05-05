❯ _register_xxx函数都是干嘛的？

● 这是 14 个内置技能的注册函数。每个函数的作用就是返回一个预配置好的 SkillDefinition 对象。

  _BUNDLED_REGISTRATIONS 列表在加载时逐个调用它们：

  _BUNDLED_REGISTRATIONS = [
      _register_update_config,   # 编号 #1
      _register_keybindings,     # 编号 #2
      ...
      _register_loop,            # 编号 #14
  ]

  # 加载时:
  def _load_bundled(self):
      for register_fn in _BUNDLED_REGISTRATIONS:
          skill = register_fn()      # 调用注册函数 → 返回 SkillDefinition
          self._register(skill)      # 插入 self._skills[name]

  ---
  逐一看每个函数：

  _register_update_config() — #1

  def _register_update_config():
      return SkillDefinition(
          name="update-config",
          source=SkillSource.BUNDLED,
          description="Configure Claude Code via settings.json...",
          when_to_use="When you need to configure hooks, permissions...",
          allowed_tools=["Read"],          # 只需要读文件
          user_invocable=True,
          context=SkillContext.FORK,       # 在单独的 Agent 中运行
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

  Hooks (8 event types)

  - UserPromptSubmit, PreToolUse, PostToolUse, ...
  ...
  """,
    )

  **作用：** 当 LLM 或用户调用 `/update-config`，这个技能告诉 LLM 如何修改 `settings.json` —
  添加权限、配置钩子、设置环境变量。

  ---

  ### `_register_simplify()` — #2

  ```python
  def _register_simplify():
      return SkillDefinition(
          name="simplify",
          source=SkillSource.BUNDLED,
          description="Review changed code for reuse, quality, and efficiency...",
          context=SkillContext.FORK,       # 在单独 Agent 中运行（不占主对话 token）
          prompt_body=r"""# Code Simplifier

  ## Phase 1: Identify Changes
  Run `git diff` to see what changed.

  ## Phase 2: Parallel Review
  Launch 3 exploration agents in parallel:
  1. Code Reuse — Are there existing utilities that could replace new code?
  2. Code Quality — Are there bugs, edge cases, or unclear logic?
  3. Efficiency — Are there performance issues or unnecessary complexity?

  ## Phase 3: Fix
  Apply the improvements found.
  """,
      )

  作用： 当 LLM 调 /simplify，启动一个子 Agent，子 Agent 并行审查代码的复用性、质量、效率，然后修复问题。这个技能有
  3 阶段工作流。

  ---
  _register_verify() — #3

  def _register_verify():
      return SkillDefinition(
          name="verify",
          context=SkillContext.FORK,
          prompt_body=r"""# Code Verifier
  1. Read the changes via `git diff`
  2. Run the application or tests
  3. Check outputs match expected behavior
  4. Report: what works, what doesn't, suggestions
  """,
      )

  作用： 验证代码变更 — 运行测试、检查结果、报告问题。

  ---
  _register_debug() — #4

  def _register_debug():
      return SkillDefinition(
          name="debug",
          allowed_tools=["Read", "Grep", "Glob"],  # 只读
          disable_model_invocation=True,  # LLM 不能自动调用，必须用户主动
          context=SkillContext.FORK,
          prompt_body=r"""# Session Debugger
  1. Enable debug logging if not already active
  2. Check the most recent log entries for errors
  3. Identify the root cause of any issues
  """,
      )

  作用： 当会话出问题时，用户主动调 /debug 来诊断。注意 disable_model_invocation=True — 防止 LLM 自己乱调。

  ---
  _register_keybindings() — #5

  def _register_keybindings():
      return SkillDefinition(
          name="keybindings-help",
          user_invocable=False,  # 用户不能调 /keybindings-help
          allowed_tools=["Read"],
          prompt_body=r"""# Keybindings Configuration
  File: `~/.claude/keybindings.json`
  ...
  """,
      )

  作用： 只对 LLM 可见（user_invocable=False），LLM 在回答快捷键问题时自动参考。用户不能手动调用。

  ---
  _register_loop() — #6

  def _register_loop():
      return SkillDefinition(
          name="loop",
          context=SkillContext.FORK,
          prompt_body=r"""# Loop Runner
  Parse the interval and prompt from args. Schedule via cron.
  Supported intervals: Xm (minutes), Xh (hours), Xs (seconds).
  Default interval: 10 minutes.
  """,
      )

  作用： /loop 5m /test → 每 5 分钟运行一次 /test 命令。

  ---
  _register_batch() — #7

  def _register_batch():
      return SkillDefinition(
          name="batch",
          disable_model_invocation=True,  # 太危险，不允许 LLM 自动调
          context=SkillContext.FORK,
          prompt_body=r"""# Batch Task Runner
  ## Phase 1: Plan
  Enter plan mode. Decompose work into 5-30 independent units.
  ## Phase 2: Execute
  Spawn parallel agents with worktree isolation.
  ## Phase 3: Track
  Monitor progress. Handle failures. Merge results.
  """,
      )

  作用： 把大任务分解成 5-30 个并行子任务，每个在独立的 git worktree 中运行。disable_model_invocation=True 防止 LLM
  随便启动几十个 Agent。

  ---
  _register_remember() — #8

  def _register_remember():
      return SkillDefinition(
          name="remember",
          context=SkillContext.FORK,
          prompt_body=r"""# Memory Reviewer
  1. Gather memory layers: MEMORY.md index + individual memory files
  2. Classify entries into 4 destinations:
     - CLAUDE.md: Critical project instructions
     - CLAUDE.local.md: Local overrides
     - MEMORY.md: Persistent memory (keep)
     - Delete: Obsolete or wrong entries
  3. Present report with specific recommendations
  """,
      )

  作用： 审查自动记忆，把重要的提升到 CLAUDE.md，把过期的删除。

  ---
  _register_skillify() — #9

  def _register_skillify():
      return SkillDefinition(
          name="skillify",
          allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "AskUserQuestion", "Bash"],
          disable_model_invocation=True,
          context=SkillContext.FORK,
          prompt_body=r"""# Skill Creator
  ## Step 1: Analyze — Review conversation to find repeatable workflow
  ## Step 2: Interview (up to 4 rounds) — Ask user: name, triggers, tools
  ## Step 3: Write SKILL.md — Create complete SKILL.md with frontmatter
  ## Step 4: Save — Write to .claude/skills/<name>/SKILL.md
  """,
      )

  作用： 从当前会话中提取可重复的工作流，和用户对话后生成新的 Skill。

  ---
  _register_stuck() — #10

  def _register_stuck():
      return SkillDefinition(
          name="stuck",
          context=SkillContext.FORK,
          prompt_body=r"""# Session Diagnostician
  1. Check for running Claude Code processes
  2. Look for high CPU usage, memory leaks, stuck subprocesses
  3. Check debug logs for error patterns
  """,
      )

  作用： 会话冻结时诊断问题 — 查进程、查 CPU、查日志。

  ---
  _register_claude_api() — #11

  def _register_claude_api():
      return SkillDefinition(
          name="claude-api",
          allowed_tools=["Read", "Grep", "Glob", "WebFetch"],
          context=SkillContext.FORK,
          prompt_body=r"""# Claude API Developer
  ## Key Features
  - Prompt caching (ephemeral 5-min cache)
  - Tool use (function calling)
  - Streaming responses (SSE)
  - Extended thinking
  ...
  ## Supported Languages
  Python, TypeScript, Java, Go, Ruby, C#, PHP, curl
  """,
      )

  作用： 帮助 LLM 编写 Claude API 调用代码 — 缓存、工具使用、流式传输。

  ---
  _register_claude_in_chrome() — #12

  def _register_claude_in_chrome():
      return SkillDefinition(
          name="claude-in-chrome",
          context=SkillContext.FORK,
          prompt_body=r"""# Chrome Browser Automation
  ## Available Actions
  - Navigate to URLs
  - Click elements
  - Fill forms
  - Take screenshots
  ...
  """,
      )

  作用： Chrome 浏览器自动化。

  ---
  _register_schedule() — #13

  def _register_schedule():
      return SkillDefinition(
          name="schedule",
          allowed_tools=["CronCreate", "CronDelete", "CronList", "AskUserQuestion"],
          context=SkillContext.FORK,
          prompt_body=r"""# Remote Agent Scheduler
  ## Cron Expression Reference
  - `0 9 * * *` Daily at 9:00 AM
  - `0 */4 * * *` Every 4 hours
  ...
  """,
      )

  作用： 定时任务调度。

  ---
  _register_lorem_ipsum() — #14

  def _register_lorem_ipsum():
      return SkillDefinition(
          name="lorem-ipsum",
          argument_hint="[token_count]",
          context=SkillContext.INLINE,    # 注入对话即可，不需要子 Agent
          prompt_body=r"""# Lorem Ipsum Generator
  Generate random text for testing. Default 10,000 tokens.
  """,
      )

  作用： 生成长文本用于测试上下文窗口。

  ---
  总结：14 个技能的属性一览

  技能名              执行模式    用户可调   模型禁用  工具限制
  ────────────────────────────────────────────────────────────
  update-config        FORK        ✓          ✗         Read
  keybindings-help     INLINE      ✗          ✗         Read
  verify               FORK        ✓          ✗         无
  debug                FORK        ✓          ✓(自动)    Read/Grep/Glob
  lorem-ipsum          INLINE      ✓          ✗         无
  skillify             FORK        ✓          ✓(自动)    7个
  remember             FORK        ✓          ✗         无
  simplify             FORK        ✓          ✗         无
  batch                FORK        ✓          ✓(自动)    无
  stuck                FORK        ✓          ✗         无
  claude-api           FORK        ✓          ✗         4个
  claude-in-chrome     FORK        ✓          ✗         无
  schedule             FORK        ✓          ✗         4个
  loop                 FORK        ✓          ✗         无