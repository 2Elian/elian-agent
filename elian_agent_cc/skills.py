"""
Skills system - loads and manages SKILL.md files.

Ported from src/skills/loadSkillsDir.ts (1087 lines) and src/skills/mcpSkillBuilders.ts.

Skills are loaded from three tiers:
  1. Managed (policy-enforced)
  2. User (~/.claude/skills/)
  3. Project (.claude/skills/ walking up from cwd)

Conditional skills can activate based on file path patterns.
"""
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


class SkillContext(str, Enum):
    FORK = "fork"  # Execute as sub-agent
    INLINE = "inline"  # Inject into current context


@dataclass
class SkillDefinition:
    """A loaded skill from a SKILL.md file."""
    name: str
    description: str = ""
    version: str = "1.0"
    when_to_use: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    argument_hint: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True
    hooks: dict[str, Any] = field(default_factory=dict)
    context: SkillContext = SkillContext.INLINE
    agent: str = ""
    effort: str = ""
    paths: list[str] = field(default_factory=list)  # Conditional activation patterns
    shell: str = ""
    source: str = ""  # "bundled", path to directory
    prompt_content: str = ""  # The markdown body after frontmatter

    def matches_path(self, file_path: str) -> bool:
        """Check if this skill conditionally activates for a given path."""
        if not self.paths:
            return False
        return any(fnmatch(file_path, p) for p in self.paths)


class SkillLoader:
    """Loads skills from directories, bundled, and MCP sources."""

    def __init__(self, cwd: str | None = None):
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._skills: dict[str, SkillDefinition] = {}
        self._loaded = False
        self._skill_dirs: list[Path] = []

    @property
    def user_skills_dir(self) -> Path:
        return Path.home() / ".claude" / "skills"

    @property
    def project_skills_dirname(self) -> str:
        return ".claude/skills"

    def load_all(self) -> dict[str, SkillDefinition]:
        """Load skills from all sources. Returns deduplicated mapping."""
        if self._loaded:
            return self._skills

        # Tier 3: User skills
        self._load_from_directory(self.user_skills_dir, "user")

        # Tier 4: Project skills (walk up from cwd)
        self._load_project_skills()

        # Tier 5: Bundled skills
        bundled = Path(__file__).parent / "skills_data"
        if bundled.exists():
            self._load_from_directory(bundled, "bundled")

        self._loaded = True
        return self._skills

    def _load_from_directory(self, dir_path: Path, source: str) -> None:
        """Load all SKILL.md files from a directory."""
        if not dir_path.exists():
            return

        self._skill_dirs.append(dir_path)

        for skill_dir in dir_path.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                skill = self._parse_skill_file(skill_file)
                skill.source = source
                # Later tiers override earlier ones
                self._skills[skill.name] = skill
            except Exception:
                continue

    def _load_project_skills(self) -> None:
        """Walk up from cwd looking for .claude/skills/ directories."""
        current = self._cwd.resolve()
        seen: set[str] = set()

        while True:
            skill_dir = current / self.project_skills_dirname
            skill_dir_str = str(skill_dir)
            if skill_dir.exists() and skill_dir_str not in seen:
                seen.add(skill_dir_str)
                self._load_from_directory(skill_dir, "project")

            parent = current.parent
            if parent == current:
                break
            current = parent

    def _parse_skill_file(self, file_path: Path) -> SkillDefinition:
        """Parse a SKILL.md file with YAML frontmatter."""
        try:
            import yaml
        except ImportError:
            yaml = None

        content = file_path.read_text(encoding="utf-8")

        # Parse frontmatter (YAML between --- markers)
        frontmatter: dict[str, Any] = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                if yaml:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                else:
                    # Simple key: value parser fallback
                    for line in parts[1].strip().split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            frontmatter[k.strip()] = v.strip()
                body = parts[2]

        context_raw = frontmatter.get("context", "inline")
        try:
            context = SkillContext(context_raw)
        except ValueError:
            context = SkillContext.INLINE

        return SkillDefinition(
            name=frontmatter.get("name", file_path.parent.name),
            description=frontmatter.get("description", ""),
            version=str(frontmatter.get("version", "1.0")),
            when_to_use=frontmatter.get("when_to_use", ""),
            allowed_tools=frontmatter.get("allowed-tools", []),
            argument_hint=frontmatter.get("argument-hint", ""),
            arguments=frontmatter.get("arguments", []),
            model=frontmatter.get("model", ""),
            disable_model_invocation=frontmatter.get("disable-model-invocation", False),
            user_invocable=frontmatter.get("user-invocable", True),
            hooks=frontmatter.get("hooks", {}),
            context=context,
            agent=frontmatter.get("agent", ""),
            effort=frontmatter.get("effort", ""),
            paths=frontmatter.get("paths", []),
            shell=frontmatter.get("shell", ""),
            prompt_content=body.strip(),
        )

    def get(self, name: str) -> SkillDefinition | None:
        """Get a skill by name."""
        self.load_all()
        return self._skills.get(name)

    def list_user_invocable(self) -> list[SkillDefinition]:
        """List skills that can be invoked by the user."""
        self.load_all()
        return [s for s in self._skills.values() if s.user_invocable]

    def find_for_path(self, file_path: str) -> list[SkillDefinition]:
        """Find skills that should be conditionally activated for a path."""
        self.load_all()
        return [s for s in self._skills.values() if s.matches_path(file_path)]

    def discover_for_paths(self, paths: list[str]) -> list[SkillDefinition]:
        """Find all skills relevant to a set of file paths."""
        discovered = []
        self.load_all()
        for file_path in paths:
            discovered.extend(self.find_for_path(file_path))
        # Deduplicate by name
        seen = set()
        result = []
        for s in discovered:
            if s.name not in seen:
                seen.add(s.name)
                result.append(s)
        return result

    def get_for_prompt(self) -> str:
        """Generate the skills section for the system prompt."""
        skills = self.list_user_invocable()
        if not skills:
            return ""

        lines = ["## Available Skills", ""]
        for s in skills:
            desc = s.description.split("\n")[0][:120]
            hint = f" [{s.argument_hint}]" if s.argument_hint else ""
            lines.append(f"- `/{s.name}{hint}`: {desc}")
        return "\n".join(lines)


# Global skill loader
skill_loader = SkillLoader()
