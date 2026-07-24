"""Bash 工具：执行 shell 命令并返回合并输出。"""

from __future__ import annotations

import asyncio
import re
import shlex
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    # 仅用于类型注解；运行时由 app.py 在装配时注入实例，避免循环导入。
    from seacode.sandbox import Sandbox, SandboxConfig

# 超时上限（秒），用户传入值会被截断到此值。
MAX_TIMEOUT: int = 600

# 特殊命令的退出码阈值：低于阈值的非零退出码不视为错误。
# 例如 grep 返回 1 仅表示无匹配，不是执行出错。
_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2,
    "egrep": 2,
    "fgrep": 2,
    "rg": 2,
    "diff": 2,
    "find": 2,
    "test": 2,
    "[": 2,
}

# 特殊命令非零退出码的可读提示，帮助模型理解退出码语义。
_EXIT_CODE_HINTS: dict[str, str] = {
    "grep": "no matches found",
    "egrep": "no matches found",
    "fgrep": "no matches found",
    "rg": "no matches found",
    "diff": "files differ",
    "find": "some directories were inaccessible",
    "test": "condition is false",
    "[": "condition is false",
}


def _extract_last_command_name(command: str) -> str | None:
    """从命令字符串中提取最后一个管道段的基础命令名。

    管道中最后一个命令决定整体退出码，因此只看最后一段。
    例如 "cat file | grep pattern" → "grep"。
    """
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None

    # 跳过形如 VAR=VALUE 的环境变量赋值前缀与 sudo/env 等包装命令。
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        tokens = last_segment.split()

    for token in tokens:
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # 取 basename，去掉路径前缀（如 /usr/bin/grep → grep）。
        return token.rsplit("/", maxsplit=1)[-1]

    return None


def _interpret_exit_code(command: str, exit_code: int) -> bool:
    """根据命令语义判断退出码是否代表真正的错误。"""
    if exit_code == 0:
        return False

    cmd_name = _extract_last_command_name(command)
    if cmd_name and cmd_name in _COMMAND_ERROR_THRESHOLDS:
        return exit_code >= _COMMAND_ERROR_THRESHOLDS[cmd_name]

    return True


def _exit_code_hint(command: str, exit_code: int) -> str:
    """为非零退出码生成可读提示，附加特殊命令的语义说明。"""
    cmd_name = _extract_last_command_name(command)
    hint = _EXIT_CODE_HINTS.get(cmd_name, "") if cmd_name else ""
    if hint:
        return f"Exit code {exit_code} ({hint})"
    return f"Exit code {exit_code}"


class Params(BaseModel):
    """Bash 参数模型。"""

    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


class Bash(Tool):
    """执行 shell 命令，合并 stdout 与 stderr，超时与非零退出码语义明确。"""

    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = ToolCategory.SYSTEM

    # 工作目录，为 None 时使用当前进程的工作目录。
    work_dir: str | None = None

    # OS 级沙箱实例和配置（由 app.py 在装配时注入，为 None 时不启用沙箱）。
    sandbox: Sandbox | None = None
    sandbox_config: SandboxConfig | None = None

    async def execute(self, params: Params) -> ToolResult:  # type: ignore[override]
        timeout = min(params.timeout, MAX_TIMEOUT)

        # 三条件守卫：sandbox / sandbox_config / available 任一为 None/False 时直接执行原命令。
        actual_command = params.command
        if self.sandbox and self.sandbox_config and self.sandbox.available():
            actual_command = self.sandbox.wrap(params.command, self.sandbox_config)

        try:
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # 合并 stderr 到 stdout
                cwd=self.work_dir,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                content=f"Error: command timed out after {timeout}s", is_error=True
            )
        except Exception as e:
            return ToolResult(content=f"Error executing command: {e}", is_error=True)

        # 合并流输出；非零退出码追加提示但 is_error 始终为 False，
        # 只有超时和异常才设置 is_error=True。
        output = stdout.decode(errors="replace") if stdout else ""
        exit_code = proc.returncode or 0
        if exit_code != 0:
            hint = _exit_code_hint(params.command, exit_code)
            if output:
                output = f"{output.rstrip()}\n\n{hint}"
            else:
                output = hint

        if not output:
            output = "(no output)"

        return ToolResult(content=output, is_error=False)
