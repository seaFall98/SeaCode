"""内置命令注册聚合：11 条命令的 Command 定义与批量注册。"""

from __future__ import annotations

import os

from seacode.commands.handlers.clear import CLEAR_COMMAND
from seacode.commands.handlers.compact import COMPACT_COMMAND
from seacode.commands.handlers.help import HELP_COMMAND
from seacode.commands.handlers.mcp import MCP_COMMAND
from seacode.commands.handlers.memory import MEMORY_COMMAND
from seacode.commands.handlers.permission import PERMISSION_COMMAND
from seacode.commands.handlers.plan import PLAN_COMMAND
from seacode.commands.handlers.review import REVIEW_COMMAND
from seacode.commands.handlers.sandbox import SANDBOX_COMMAND
from seacode.commands.handlers.session import SESSION_COMMAND
from seacode.commands.handlers.status import STATUS_COMMAND
from seacode.commands.loader import register_user_commands
from seacode.commands.registry import Command, CommandRegistry

# 全部内置命令：顺序仅作可读性，注册中心按 name 排序后用于 /help 与补全。
ALL_COMMANDS: list[Command] = [
    HELP_COMMAND,
    STATUS_COMMAND,
    CLEAR_COMMAND,
    COMPACT_COMMAND,
    PLAN_COMMAND,
    SESSION_COMMAND,
    MEMORY_COMMAND,
    PERMISSION_COMMAND,
    REVIEW_COMMAND,
    MCP_COMMAND,
    SANDBOX_COMMAND,
]


# 批量注册内置命令并加载用户自定义命令；启动时调用一次。
# work_dir 为空时回退到当前工作目录，用于定位 <work_dir>/.seacode/commands/。
def register_all_commands(
    registry: CommandRegistry, work_dir: str | None = None
) -> None:
    for cmd in ALL_COMMANDS:
        registry.register_sync(cmd)
    # 内置命令注册后再加载用户命令；同名/别名冲突时内置命令优先。
    register_user_commands(registry, work_dir or os.getcwd())
