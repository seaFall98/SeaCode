"""危险命令检测：维护黑名单与安全命令白名单，供权限检查器调用。"""

from __future__ import annotations

import re

# 危险命令黑名单：8 条预编译正则，命中即硬拦截，不可被任何模式绕过。
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/\s*$"), "递归强制删除根目录"),
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"curl\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
]

# 安全命令白名单：51 条只读命令；命中后自动放行，无需触发 HITL。
_SAFE_COMMANDS: frozenset[str] = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "head", "tail", "wc",
    "find", "which", "whereis", "whoami", "hostname", "uname",
    "date", "cal", "uptime", "df", "du", "free", "env", "printenv",
    "file", "stat", "readlink", "realpath", "basename", "dirname",
    "sort", "uniq", "tr", "cut", "awk", "sed", "grep", "egrep", "fgrep",
    "diff", "comm", "tee", "xargs", "true", "false", "test",
    "git status", "git log", "git diff", "git show", "git branch",
    "git tag", "git remote", "git rev-parse", "git ls-files",
    "git blame", "git stash list", "go version", "go env",
    "node -v", "npm -v", "npx", "python --version", "pip list",
    "cargo --version", "rustc --version", "java -version", "java --version",
})


def is_safe_command(command: str) -> bool:
    """检查命令是否为白名单只读命令；含元字符或非白名单命令返回 False。"""
    trimmed = command.strip()
    if not trimmed:
        return False
    # 含管道、重定向、命令串联等元字符的复合命令不视为安全。
    for ch in ("|", ";", "&&", ">", "$(", "`"):
        if ch in trimmed:
            return False
    for safe in _SAFE_COMMANDS:
        if trimmed == safe or trimmed.startswith(safe + " "):
            return True
    return False


class DangerousCommandDetector:
    """危险命令检测器；支持注入额外模式用于扩展黑名单。"""

    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None) -> None:
        self._patterns: list[tuple[re.Pattern[str], str]] = list(_DANGEROUS_PATTERNS)
        if extra_patterns:
            for regex_str, reason in extra_patterns:
                self._patterns.append((re.compile(regex_str), reason))

    # 返回 (是否命中, 命中原因)；未命中时 reason 为空字符串。
    def detect(self, command: str) -> tuple[bool, str]:
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""
