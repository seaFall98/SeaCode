"""内置命令注册聚合：11 条命令的 Command 定义与批量注册。"""

from __future__ import annotations

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


# 批量注册 11 条内置命令到注册中心；启动时调用一次。
def register_all_commands(registry: CommandRegistry) -> None:
    for cmd in ALL_COMMANDS:
        registry.register_sync(cmd)
