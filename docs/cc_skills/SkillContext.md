  1.2 SkillContext — "这个技能怎么执行？"

  class SkillContext(str, Enum):
      INLINE = "inline"  # 注入当前对话 — LLM 直接看到技能内容
      FORK = "fork"      # 启动子 Agent — 独立上下文 + 独立 token 预算

  INLINE 模式的数据流：

  LLM 调用 Skill(skill="update-config", args="")
          ↓
  skill_loader.get_skill_prompt("update-config", "")
    → 返回 ("# Configure Claude Code\n\n## Permissions\n...", {})
          ↓
  技能内容作为 attachment 消息注入当前对话
          ↓
  LLM 在当前上下文中看到并处理技能指令

  FORK 模式的数据流：

  LLM 调用 Skill(skill="simplify", args="login-bug")
          ↓
  skill_loader.get_skill_prompt("simplify", "login-bug")
    → 返回 (skill_body, {"agent": "general-purpose"})  ← 注意返回了 modifier
          ↓
  engine 读取 modifier，启动子 Agent
    → agent_manager.spawn(GENERAL_PURPOSE_AGENT)
    → agent_manager.run_agent(ctx, skill_body)
    → 子 Agent 有独立的 token 预算（不消耗主对话的上下文窗口）
    → 子 Agent 执行完毕后返回结果
          ↓
  结果作为 tool_result 返回给主 LLM

  为什么需要两种模式？

  ┌──────────┬──────────────────────┬────────────────────────────┐
  │          │        INLINE        │            FORK            │
  ├──────────┼──────────────────────┼────────────────────────────┤
  │ 上下文   │ 共享主对话           │ 隔离的独立上下文           │
  ├──────────┼──────────────────────┼────────────────────────────┤
  │ Token    │ 消耗主对话 token     │ 独立 token 预算            │
  ├──────────┼──────────────────────┼────────────────────────────┤
  │ 适用场景 │ 短指令（配置、帮助） │ 长任务（代码审查、批处理） │
  ├──────────┼──────────────────────┼────────────────────────────┤
  │ example  │ update-config        │ simplify, batch, verify    │
  └──────────┴──────────────────────┴────────────────────────────┘