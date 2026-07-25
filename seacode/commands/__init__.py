"""SeaCode 本地命令框架：注册中心、解析器、补全弹窗与自定义命令加载。"""

from .completion import CompletionPopup, Selected
from .loader import load_user_commands, register_user_commands
from .parser import complete, parse_command
from .registry import (
    Command,
    CommandContext,
    CommandRegistry,
    CommandType,
    UIController,
)

__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "CommandType",
    "CompletionPopup",
    "Selected",
    "UIController",
    "complete",
    "load_user_commands",
    "parse_command",
    "register_user_commands",
]
