  1.4 SkillDefinition — 技能的完整"身份证"

```python
  @dataclass
  class SkillDefinition:
      # ====== 基本信息 ======
      name: str                 # "simplify" — 唯一标识符
      description: str          # "Review changed code..." — 一句话描述
      when_to_use: str          # 详细触发条件说明
      version: str = "1.0"

      # ====== 调用方式 ======
      argument_hint: str        # "<file-or-query>" — 参数提示
      arguments: list[str]      # ["arg1", "arg2"] — 命名参数
      user_invocable: bool      # 用户可以用 /simplify 调用吗？
      disable_model_invocation: bool  # 禁止 LLM 自动调用？

      # ====== 执行配置 ======
      context: SkillContext     # INLINE 还是 FORK？
      agent: str                # fork 时用哪个 Agent 类型？
      model: str                # 模型覆盖
      effort: str               # thinking 强度
      allowed_tools: list[str]  # 允许使用的工具列表
      shell: str                # "bash" 或 "powershell"

      # ====== 条件激活 ======
      paths: list[str]          # ["*.py", "src/**/*.ts"] — 匹配文件路径

      # ====== 来源 ======
      source: SkillSource       # bundled / user / project / mcp / managed
      skill_root: str           # 技能文件所在目录

      # ====== 核心内容 ======
      prompt_body: str          # SKILL.md 的 markdown 正文 — 注入 LLM 的内容
      files: dict[str, str]     # 内置技能的引用文件
      hooks: dict[str, list[SkillHook]]  # 生命周期钩子
      prompt_fn: Callable       # 动态 prompt 生成器（内置技能用）
```

  让我用 YAML frontmatter 到 SkillDefinition 的映射来说明每个字段的来源：

  # SKILL.md frontmatter                  → SkillDefinition 字段
  ---                                     # ==========
  name: simplify                          # → name
  description: Review changed code        # → description
  when_to_use: When you need...           # → when_to_use
  version: "1.0"                          # → version
  argument-hint: "<file-or-query>"        # → argument_hint
  arguments: [arg1, arg2]                 # → arguments
  user-invocable: true                    # → user_invocable
  disable-model-invocation: false         # → disable_model_invocation
  context: fork                           # → context = SkillContext.FORK
  agent: general-purpose                  # → agent
  model: haiku                            # → model
  effort: high                            # → effort
  allowed-tools: [Read, Grep, Glob]       # → allowed_tools
  shell: bash                             # → shell
  paths: ["*.py", "*.ts"]                 # → paths
  hooks:                                  # → hooks
    PostToolUse:
      - matcher: "Write|Edit"
        hooks: [{type: command, command: "black $FILE"}]
  ---                                     # ==========
  # Code Simplifier                       # → prompt_body (frontmatter 之后的所有内容)

  ## Phase 1: Identify Changes...
  ## Phase 2: Parallel Review...

  ---
  数据流全景

  SKILL.md 文件
      │
      ▼
  _parse_skill_file(path)  ─── 解析 YAML frontmatter + markdown body
      │
      ▼
  SkillDefinition 对象
      │
      ▼
  _register(skill)  ─── 按优先级插入 self._skills[name]
      │
      ├─ skill.paths 存在 → 加入 _conditional_skills
      ├─ skill.hooks 存在 → 加入 _hooks_registry
      └─ skill.source > existing.source → 覆盖
      │
      ▼
  engine._preload_skills()
      │
      ▼
  get_skills_prompt()  ─── 生成 "## Available Skills" 部分
      │
      ▼
  engine.submit_message()  ─── 注入系统提示词
      │
      ▼
  LLM 看到技能列表 → 调用 Skill(skill="simplify", args="...")
      │
      ▼
  get_skill_prompt("simplify", "args")
      ├─ substitute_args(body, args)      # $ARGUMENTS → 实际参数
      ├─ execute_shell_blocks(body)       # !`cmd` → 实际输出
      └─ 返回 (final_body, modifier)
      │
      ▼
  modifier.context === FORK ?
      ├─ YES → agent_manager.run_agent(skill_body)
      └─ NO  → 注入为 attachment 消息