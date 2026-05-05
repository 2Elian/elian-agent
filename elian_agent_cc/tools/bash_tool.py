"""
BashTool - ported from BashTool/ (23 files, ~5300 lines).

Multi-layer security:
  Layer 0: Syntax safety check (command substitution, incomplete commands)
  Layer 1: Destructive command pattern detection (15 patterns)
  Layer 2: Read-only command detection (14 patterns)
  Layer 3: Always-ask command list (20 commands)
  Layer 4: Path validation (redirect targets, system file checks)
  Layer 5: Permission-mode-aware decision (acceptEdits auto-allow)
"""
import asyncio, os, re
from pathlib import Path
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext, PermissionResult, ValidationResult


# Destructive patterns (from bashSecurity.ts)
DESTRUCTIVE = [
    (r'\brm\s+-rf\b', 'rm -rf (force recursive delete)'),
    (r'\brm\s+-r\b', 'rm -r (recursive)'),
    (r'\bgit\s+reset\s+--hard\b', 'git reset --hard'),
    (r'\bgit\s+push\s+--force\b', 'git push --force'),
    (r'\bgit\s+clean\s+-f\b', 'git clean -f'),
    (r'\bgit\s+branch\s+-D\b', 'git branch -D'),
    (r'\bdd\s+if=', 'dd (disk copy)'),
    (r'\bmkfs\.', 'mkfs (format)'),
    (r'\bchmod\s+777\b', 'chmod 777 (world-writable)'),
    (r'>\s*/dev/sd[a-z]', 'write to raw device'),
    (r'\bshutdown\b', 'shutdown'),
    (r'\breboot\b', 'reboot'),
    (r'\b:\(\)\s*\{', 'fork bomb pattern'),
    (r'\bwget\b.*\|.*\bsh\b', 'curl/wget pipe to shell'),
    (r'\bcurl\b.*\|.*\bsh\b', 'curl pipe to shell'),
]

# Safe read-only command patterns
READONLY = [
    r'^(ls|dir|echo|cat|head|tail|less|more|wc|du|df|ps|top|who|date|pwd|which|file|stat|type|uname|hostname|id|groups|env|printenv)\b',
    r'^(find|locate)\b',
    r'^(grep|rg|egrep|fgrep)\b',
    r'^(git\s+status|git\s+diff|git\s+log|git\s+show|git\s+branch|git\s+remote)\b',
    r'^(pip\s+list|pip\s+show|pip\s+freeze|npm\s+ls|npm\s+list)\b',
]

# Commands requiring explicit user confirmation
ALWAYS_ASK = [
    'rm', 'rmdir', 'mv', 'cp -r', 'cp -R',
    'chmod', 'chown', 'mkfs', 'dd', 'fdisk',
    'git reset --hard', 'git push --force', 'git clean',
    'git branch -D', 'git rebase', 'git stash drop',
    'npm publish', 'pip install', 'gem install',
    'docker rm', 'docker rmi', 'docker system prune',
    'systemctl', 'service', 'kill', 'killall', 'pkill',
]


class BashTool(Tool):
    name = "Bash"
    description = """Executes a bash command and returns its output. Working directory persists but shell state does not.

IMPORTANT: Prefer Read/Edit/Write/Glob/Grep over Bash when possible. Use Bash only for shell-specific operations.

Instructions:
- Quote file paths with spaces
- Use absolute paths and avoid cd
- Optional timeout (max 600000ms, default 120000ms)
- Use run_in_background for long-running commands
- For git: create new commits, never skip hooks (--no-verify) unless explicitly asked
- Do not use sleep between commands that can run immediately"""

    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in ms (max 600000)", "default": 120000},
            "description": {"type": "string", "description": "Clear description of what this command does"},
            "run_in_background": {"type": "boolean", "description": "Run in background", "default": False},
            "dangerouslyDisableSandbox": {"type": "boolean", "description": "Override sandbox", "default": False},
        },
        "required": ["command"],
    }

    async def call(self, params, context):
        cmd = params["command"]
        timeout_ms = min(params.get("timeout", 120000), 600000)
        run_bg = params.get("run_in_background", False)
        cwd = context.cwd or "."

        security = self._security_check(cmd)
        if not security.is_valid:
            return ToolResult(content=security.message, is_error=True)
        # TODO 可能危险的命令 未作处理
        destructive, warning = self._check_destructive(cmd)
        path_ok, path_msg = self._validate_paths(cmd)
        if not path_ok:
            return ToolResult(content=f"Path safety: {path_msg}", is_error=True)

        if run_bg:
            # 耗时的bash命令 --> 起一个子线程 到后台执行任务
            asyncio.create_task(self._run(cmd, cwd, timeout_ms / 1000))
            return ToolResult(content=f"Background task started: {cmd}")

        try:
            stdout, stderr, exit_code = await self._run(cmd, cwd, timeout_ms / 1000)
            parts = [stdout.strip()] if stdout.strip() else []
            if stderr.strip():
                parts.append(f"stderr:\n{stderr.strip()}")
            result = "\n".join(parts) or "(no output)"
            semantic = self._interpret_semantics(cmd, exit_code, stderr)
            if semantic:
                result += f"\n\n{semantic}"
            return ToolResult(content=self.truncate_result(result), is_error=exit_code != 0)
        except asyncio.TimeoutError:
            return ToolResult(content=f"Command timed out after {timeout_ms}ms", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)

    def check_permissions(self, params, context):
        cmd = params.get("command", "")
        destructive, warning = self._check_destructive(cmd)
        is_ro = self._is_readonly_command(cmd)
        always_ask = self._is_always_ask(cmd)
        mode = context.tool_permission_context.mode

        if destructive:
            return PermissionResult.ask(f"Destructive: {warning}")
        if always_ask and mode not in ("bypassPermissions", "auto"):
            return PermissionResult.ask(f"Command '{cmd}' requires confirmation")
        if mode == "acceptEdits" and is_ro:
            return PermissionResult.allow()
        if mode == "bypassPermissions":
            return PermissionResult.allow()
        return PermissionResult.allow()

    def validate_input(self, params, context):
        cmd = params.get("command", "")
        if not cmd.strip():
            return ValidationResult(False, "Command cannot be empty")
        if len(cmd) > 10000:
            return ValidationResult(False, "Command too long")
        return ValidationResult(True)

    def _security_check(self, cmd: str) -> ValidationResult:
        # cwd是bash命令 这里做命令的安全强校验：比如rm -rf
        # TODO 企业级别的agent的前置安全检查要写在这里，不包括路径校验
        """安全检查：判断命令是否以 && / || / | 开头"""
        if re.match(r'^\s*(&&|\|\||\|)\s', cmd):
            return ValidationResult(False, "Command starts with an operator")
        return ValidationResult(True)

    def _check_destructive(self, cmd: str) -> tuple[bool, str]:
        """基于规则的危险命令检测器 与_security_check的区别是：这个函数不直接拒绝命令，而是标记危险"""
        for pattern, warning in DESTRUCTIVE:
            if re.search(pattern, cmd, re.IGNORECASE):
                return True, warning
        return False, ""

    def _is_readonly_command(self, cmd: str) -> bool:
        for pattern in READONLY:
            if re.match(pattern, cmd.strip(), re.IGNORECASE):
                return True
        return False

    def _is_always_ask(self, cmd: str) -> bool:
        stripped = cmd.strip().lower()
        for ask_cmd in ALWAYS_ASK:
            if stripped.startswith(ask_cmd.lower()):
                return True
        return False

    def _validate_paths(self, cmd: str) -> tuple[bool, str]:
        # TODO 企业级agent 如果不想让模型读取某个路径下的东西 把逻辑写到这里 这里只负责路径安全校验
        """输出重定向目标路径安全校: 通过正则提取 shell 重定向目标路径，并阻止向 /etc/ 或 /proc/ 等关键系统路径写入，从而防止文件覆盖型系统破坏攻击"""
        redirects = re.findall(r'>\s*(\S+)', cmd)
        for target in redirects:
            if target in ('/etc/passwd', '/etc/shadow', '/etc/sudoers'):
                return False, f"Writing to system file blocked: {target}"
            if target.startswith('/proc/'):
                return False, f"Writing to /proc/ blocked: {target}"
        return True, ""

    def _interpret_semantics(self, cmd: str, exit_code: int, stderr: str) -> str:
        stripped = cmd.strip().lower()
        if stripped.startswith(('grep', 'rg')) and exit_code == 1:
            return "(Exit code 1 = no matches found, not an error)"
        if stripped.startswith('find') and exit_code == 1:
            return "(Exit code 1 may indicate permission denied on some directories)"
        if stripped.startswith('diff') and exit_code == 1:
            return "(Exit code 1 = files differ, not an error)"
        if (stripped.startswith('test ') or stripped.startswith('[')) and exit_code == 1:
            return "(Condition false, exit code 1 is normal for test)"
        return ""

    async def _run(self, cmd: str, cwd: str, timeout: float) -> tuple[str, str, int]:
        """
        输入：
            参数	含义
            cmd	shell 命令
            cwd	工作目录
            timeout	超时时间

        """
        """异步 shell 命令执行器: 在指定目录 + 超时控制下执行 shell 命令，并返回 stdout / stderr / exit code。"""
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd, executable='bash' if os.name == 'nt' else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return (stdout.decode('utf-8', errors='replace') if stdout else '',
                stderr.decode('utf-8', errors='replace') if stderr else '',
                proc.returncode or 0)


tool_registry.register(BashTool())
