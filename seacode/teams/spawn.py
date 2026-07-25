# spawn 命令构造：shell 引用与 teammate worker CLI 拼接。
"""teams 子包的 spawn 命令构造工具。"""

from __future__ import annotations

import sys


# 对字符串做 POSIX shell 引用；不含特殊字符时原样返回，否则包单引号并转义内部单引号。
def shell_quote(s: str) -> str:
    if not s:
        return "''"
    # 仅字母数字与 _ - . / 视为安全字符，无需引用。
    if all(c.isalnum() or c in "_-./" for c in s):
        return s
    # 包单引号；内部单引号用 '"'"' 转义。
    return "'" + s.replace("'", "'\"'\"'") + "'"


# 构造 teammate worker 启动 CLI：cd <workdir> && <python> -m seacode
# --teammate --team-name <t> --agent-name <n>。
def build_teammate_cli(team_name: str, member_name: str, workdir: str) -> str:
    python = sys.executable or "python"
    cd_part = f"cd {shell_quote(workdir)}"
    cmd_part = (
        f"{shell_quote(python)} -m seacode --teammate "
        f"--team-name {shell_quote(team_name)} "
        f"--agent-name {shell_quote(member_name)}"
    )
    return f"{cd_part} && {cmd_part}"
