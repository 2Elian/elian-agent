  Skill Hook — 从声明到执行

  第一步：在 SKILL.md 中声明钩子

  # SKILL.md
  ---
  name: auto-formatter
  description: Auto-format code on save
  hooks:
    PostToolUse:                          # ← 钩子事件：工具使用之后
      - matcher: "Write|Edit"             # ← 匹配规则：只拦截 Write 或 Edit
        hooks:                            # ← 钩子列表
          - type: command                 # ← 钩子类型：执行 Shell 命令
            command: "black $FILE"        # ← 实际命令
          - type: command
            command: "isort $FILE"
        once: false                       # ← 每次都执行

    PreToolUse:                           # ← 另一个事件：工具使用之前
      - matcher: "Bash(rm:*)"
        hooks:
          - type: prompt                  # ← 钩子类型：让 LLM 判断是否可以
            prompt: "Is it safe to delete this file? Consider git history."
        once: false
  ---

  这个例子做了两件事：
  1. 每次 Write/Edit 之后自动运行 black 和 isort 格式化代码
  2. 每次 Bash rm 之前让 LLM 评估是否安全

  第二步：解析为 Python 数据结构

  # skills.py 中的解析逻辑
  hooks_raw = frontmatter.get("hooks", {})
  # hooks_raw = {
  #     "PostToolUse": [
  #         {"matcher": "Write|Edit",
  #          "hooks": [{"type": "command", "command": "black $FILE"},
  #                    {"type": "command", "command": "isort $FILE"}],
  #          "once": False}
  #     ],
  #     "PreToolUse": [
  #         {"matcher": "Bash(rm:*)",
  #          "hooks": [{"type": "prompt", "prompt": "Is it safe to delete..."}],
  #          "once": False}
  #     ]
  # }

  hooks: dict[str, list[SkillHook]] = {}
  for hook_event, hook_list in hooks_raw.items():   # hook_event = "PostToolUse" / "PreToolUse"
      hooks[hook_event] = []
      for h in hook_list:
          hooks[hook_event].append(SkillHook(
              matcher=h["matcher"],      # "Write|Edit"
              hooks=h["hooks"],          # [{"type": "command", "command": "black $FILE"}, ...]
              type=SkillHookType(h.get("type", "command")),
              once=h.get("once", False),
          ))

  结果存储在 Skill Definition 中：

  skill.hooks = {
      "PostToolUse": [
          SkillHook(
              matcher="Write|Edit",
              hooks=[
                  {"type": "command", "command": "black $FILE"},
                  {"type": "command", "command": "isort $FILE"},
              ],
              type=SkillHookType.COMMAND,
              once=False,
          ),
      ],
      "PreToolUse": [
          SkillHook(
              matcher="Bash(rm:*)",
              hooks=[{"type": "prompt", "prompt": "Is it safe to delete this file?"}],
              type=SkillHookType.PROMPT,
              once=False,
          ),
      ],
  }

  第三步：当技能被调用时，注册钩子到全局系统

  # skills.py 的 _register() 方法中
  def _register(self, skill):
      if skill.hooks:
          self._hooks_registry[skill.name] = [
              h for hook_list in skill.hooks.values()  # 遍历所有事件
              for h in hook_list                        # 每个事件下的所有 SkillHook
          ]

  第四步：钩子执行的具体流程

  当 LLM 调用工具时，engine 在执行前后检查钩子：

  LLM 调用 Write(file_path="auth.py", content="def login(): ...")
          ↓
  === PreToolUse 阶段 ===
  hooks_manager.find_matching("auth.py", "Write")
    → 遍历所有已注册 SkillHook
    → "auto-formatter" 技能有 PreToolUse matcher="Bash(rm:*)"
    → "Write" 不匹配 "Bash(rm:*)" → 跳过
          ↓
  engine._execute_tool("Write", params, ctx)
    → 实际写入文件
          ↓
  === PostToolUse 阶段 ===
  hooks_manager.find_matching("auth.py", "Write")
    → 遍历所有已注册 SkillHook
    → "auto-formatter" 技能有 PostToolUse matcher="Write|Edit"
    → "Write" 匹配 "Write|Edit" ✓
    → 执行 hooks[0]: {"type": "command", "command": "black $FILE"}
        → $FILE 替换为 "auth.py"
        → subprocess.run("black auth.py", shell=True)
    → 执行 hooks[1]: {"type": "command", "command": "isort $FILE"}
        → $FILE 替换为 "auth.py"
        → subprocess.run("isort auth.py", shell=True)

  三种钩子类型的区别

  class SkillHookType(str, Enum):
      COMMAND = "command"  # 直接执行 Shell 命令，同步返回
      PROMPT = "prompt"    # 把 prompt 文本发给 LLM，让 LLM 判断 yes/no
      AGENT = "agent"      # 启动一个子 Agent 来评估

  COMMAND — 最简单，直接 subprocess.run()：

  # SKILL.md:
  # hooks:
  #   PostToolUse:
  #     - matcher: "Write|Edit"
  #       hooks:
  #         - type: command
  #           command: "prettier --write $FILE"

  # 效果：每次 Write/Edit 后，自动运行 prettier

  PROMPT — 让 LLM 来评估是否允许：

  # SKILL.md:
  # hooks:
  #   PreToolUse:
  #     - matcher: "Bash"
  #       hooks:
  #         - type: prompt
  #           prompt: "This command will run: $COMMAND. Is it safe? Consider:
  #                    - Does it modify files outside the project?
  #                    - Could it delete important data?
  #                    Reply YES or NO."

  # 效果：每次 Bash 之前，LLM 先评估安全风险

  AGENT — 启动子 Agent 来评估：

  # SKILL.md:
  # hooks:
  #   PreToolUse:
  #     - matcher: "Write"
  #       hooks:
  #         - type: agent
  #           agent: verification
  #           prompt: "Review this file change: $FILE was modified with content: $CONTENT.
  #                    Is this a safe and correct change?"

  # 效果：每次 Write 之前，启动 verification Agent 审查变更

  8 种钩子事件

  ┌────────────────────┬──────────────────┬──────────────────────┐
  │        事件        │     触发时机     │       典型用途       │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ PreToolUse         │ 工具调用之前     │ 安全检查、LLM 审核   │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ PostToolUse        │ 工具调用之后     │ 自动格式化、代码检查 │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ PostToolUseFailure │ 工具调用失败后   │ 错误处理、通知       │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ UserPromptSubmit   │ 用户提交消息之前 │ 输入验证、上下文注入 │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ Notification       │ 发送通知时       │ 自定义通知格式       │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ Stop               │ 会话停止时       │ 清理、保存状态       │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ PreCompact         │ 上下文压缩之前   │ 保护重要内容         │
  ├────────────────────┼──────────────────┼──────────────────────┤
  │ SessionStart       │ 会话启动时       │ 初始化环境           │
  └────────────────────┴──────────────────┴──────────────────────┘

  完整示例：看一个真实的钩子如何工作

  # .claude/skills/my-skill/SKILL.md
  ---
  name: my-skill
  hooks:
    PostToolUse:
      - matcher: "Write"
        hooks:
          - type: command
            command: "echo 'File $FILE was written at $(date)' >> /tmp/changes.log"
        once: false
  ---

  当 LLM 调用 Write(file_path="test.py", content="print('hello')")：

  1. engine._execute_tool("Write", {"file_path": "test.py", ...}, ctx)
     ↓
  2. 检查 PreToolUse hooks → 无匹配的 → 继续
     ↓
  3. tool.call(params, ctx) → 写入文件成功
     ↓
  4. 检查 PostToolUse hooks:
     → 找到 my-skill 的 PostToolUse
     → matcher="Write" 匹配当前工具名 "Write" ✓
     → 执行 hooks[0]:
         command = "echo 'File $FILE was written at $(date)' >> /tmp/changes.log"
         $FILE → "test.py" (从 tool params 中提取)
         subprocess.run(command, shell=True)
     → 返回成功
     ↓
  5. 日志追加: /tmp/changes.log 多了一行:
     File test.py was written at Mon May 4 2026 22:00:00