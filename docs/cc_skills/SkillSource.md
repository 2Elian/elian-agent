
  1.1 SkillSource — "这个技能从哪来？"

  class SkillSource(str, Enum):
      MANAGED = "managed"       # 企业管理员推送的，最高权限
      BUNDLED = "bundled"       # 编译进程序的 14 个内置技能
      USER = "userSettings"     # ~/.claude/skills/
      PROJECT = "projectSettings"  # .claude/skills/ (从工作目录向上找)
      PLUGIN = "plugin"         # 插件注册的技能
      MCP = "mcp"               # MCP 服务器提供的技能

  为什么用 str, Enum 而不是普通 Enum？

  # 因为需要和 YAML frontmatter 里的字符串直接比较
  frontmatter_source = "bundled"           # 从 SKILL.md 读出来的
  if SkillSource(frontmatter_source) == SkillSource.BUNDLED:  # 可以比较
      ...

  # 也可以用 .value 做字符串比较
  skill.source == "bundled"  # True, 因为继承了 str

  优先级表 — 数字越大越优先：

  SOURCE_PRIORITY: dict[SkillSource, int] = {
      SkillSource.MCP: 0,       # 最低 — MCP 技能可以被任何来源覆盖
      SkillSource.BUNDLED: 1,   # 内置技能
      SkillSource.PLUGIN: 2,    # 插件覆盖内置
      SkillSource.USER: 3,      # 用户覆盖插件
      SkillSource.PROJECT: 4,   # 项目覆盖用户
      SkillSource.MANAGED: 5,   # 最高 — 管理员说了算
  }

  这个优先级表驱动了 _register() 的去重逻辑。举个例子：如果你在 ~/.claude/skills/simplify/SKILL.md 里自定义了一个
  simplify（USER=3），它会覆盖内置的
  simplify（BUNDLED=1）。但如果管理员推送了一个（MANAGED=5），管理员的版本会覆盖你的。