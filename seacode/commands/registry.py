"""命令注册中心：类型定义、执行上下文与集中注册。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


# 命令类型：LOCAL 直接输出、LOCAL_UI 附带 UI 状态操作、PROMPT 构造提示词发给 LLM。
# 继承 StrEnum 使成员既是字符串又支持字符串比较，便于序列化与分发层判断。
class CommandType(StrEnum):
    LOCAL = "local"
    LOCAL_UI = "local_ui"
    PROMPT = "prompt"


# UI 协议：handler 通过这五个方法操作 TUI，不直接依赖 SeaCodeApp 实现。
class UIController(Protocol):
    def add_system_message(self, text: str) -> None: ...
    def send_user_message(self, text: str) -> None: ...
    def set_plan_mode(self, enabled: bool) -> None: ...
    def get_token_count(self) -> tuple[int, int]: ...
    def refresh_status(self) -> None: ...


# handler 签名：接收上下文，无返回值，统一 async 以覆盖异步操作。
CommandHandler = Callable[["CommandContext"], Awaitable[None]]


# 命令执行上下文：注入业务对象与 UI 状态回调，让 handler 可测试且不依赖 TUI 实现。
@dataclass
class CommandContext:
    args: str
    agent: Any
    conversation: Any
    session: Any
    session_manager: Any
    memory_manager: Any
    ui: UIController
    config: Any


# 命令定义：name 为主键，aliases 为别名列表，hidden 控制是否在 /help 中列出。
@dataclass
class Command:
    name: str
    description: str
    type: CommandType
    handler: CommandHandler
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    arg_prompt: str = ""
    hidden: bool = False


# 注册中心：集中注册、按名/别名查找、列举可见命令。
# async register 与 asyncio.Lock 为后续 Skill 动态注册预留；本步只用 register_sync。
class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._alias_map: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # 异步注册：加锁保护，防止 Skill 后台动态注册时的并发冲突。
    async def register(self, command: Command) -> None:
        async with self._lock:
            self._register_inner(command)

    # 同步注册：启动时批量注册内置命令用。
    def register_sync(self, command: Command) -> None:
        self._register_inner(command)

    # 实际注册逻辑：检查命令名与别名的双向冲突。
    def _register_inner(self, command: Command) -> None:
        if command.name in self._commands:
            raise ValueError(f"命令名冲突: {command.name}")
        if command.name in self._alias_map:
            raise ValueError(f"命令名与已有别名冲突: {command.name}")
        for alias in command.aliases:
            if alias in self._commands:
                raise ValueError(f"别名与已有命令名冲突: {alias}")
            if alias in self._alias_map:
                raise ValueError(f"别名冲突: {alias}")
        self._commands[command.name] = command
        for alias in command.aliases:
            self._alias_map[alias] = command.name

    # 查找命令：先查主名，再查别名映射。
    def find(self, name: str) -> Command | None:
        if name in self._commands:
            return self._commands[name]
        target = self._alias_map.get(name)
        if target is not None:
            return self._commands.get(target)
        return None

    # 列举可见命令：过滤 hidden 命令，按 name 排序保证补全顺序稳定。
    def list_commands(self) -> list[Command]:
        return sorted(
            (cmd for cmd in self._commands.values() if not cmd.hidden),
            key=lambda c: c.name,
        )
