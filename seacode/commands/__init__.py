"""SeaCode 本地命令框架：注册中心、解析器与补全弹窗。"""

from .completion import CompletionPopup, Selected
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
    "parse_command",
]
