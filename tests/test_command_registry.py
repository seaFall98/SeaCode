"""命令注册中心测试：覆盖类型定义、命令查找、冲突检测与并发注册。"""

from __future__ import annotations

import asyncio

import pytest

from seacode.commands.registry import (
    Command,
    CommandContext,
    CommandRegistry,
    CommandType,
)
from seacode.permissions import PermissionMode


# 空实现 handler，仅用于构造 Command 实例，不执行任何业务逻辑。
async def _noop_handler(ctx: CommandContext) -> None:
    del ctx


# 不继承 UIController 但实现其全部六个方法的鸭子类型，验证 Protocol 不强制继承。
class _DuckUI:
    def add_system_message(self, text: str) -> None:
        del text

    def send_user_message(self, text: str) -> None:
        del text

    def set_plan_mode(self, enabled: bool) -> None:
        del enabled

    def set_permission_mode(self, mode: PermissionMode) -> None:
        del mode

    def get_token_count(self) -> tuple[int, int]:
        return (0, 0)

    def refresh_status(self) -> None:
        return None


# 构造最小 Command 实例，便于各测试复用并保持可读性。
def _make_command(
    name: str = "help",
    description: str = "显示帮助",
    type: CommandType = CommandType.LOCAL,
    aliases: list[str] | None = None,
    usage: str = "",
    arg_prompt: str = "",
    hidden: bool = False,
) -> Command:
    return Command(
        name=name,
        description=description,
        type=type,
        handler=_noop_handler,
        aliases=aliases if aliases is not None else [],
        usage=usage,
        arg_prompt=arg_prompt,
        hidden=hidden,
    )


# 验证 CommandType 三个成员到字符串的映射保持稳定。
# 通过逐成员断言 value 字段，避免依赖枚举顺序或隐式转换。
def test_command_type_values() -> None:
    assert CommandType.LOCAL.value == "local"
    assert CommandType.LOCAL_UI.value == "local_ui"
    assert CommandType.PROMPT.value == "prompt"


# 验证 CommandType 继承 StrEnum，成员本身即字符串可直接比较。
# 测试设计为直接与字符串字面量比较，确认 StrEnum 的核心行为。
def test_command_type_str_comparison() -> None:
    assert CommandType.LOCAL == "local"
    assert CommandType.LOCAL_UI == "local_ui"
    assert CommandType.PROMPT == "prompt"


# 验证 Command 的可选字段在不传入时使用默认值。
# 测试覆盖 aliases、usage、arg_prompt、hidden 四个有默认值的字段。
def test_command_defaults() -> None:
    cmd = Command(
        name="x",
        description="d",
        type=CommandType.LOCAL,
        handler=_noop_handler,
    )
    assert cmd.aliases == []
    assert cmd.usage == ""
    assert cmd.arg_prompt == ""
    assert cmd.hidden is False


# 验证 CommandContext 构造后原样持有传入字段，不做额外转换。
# 测试使用可标识对象作为入参，通过 is 断言确认引用一致性。
def test_command_context_fields() -> None:
    ui = _DuckUI()
    agent = object()
    conversation = object()
    session = object()
    session_manager = object()
    memory_manager = object()
    config = object()
    ctx = CommandContext(
        args="hello",
        agent=agent,
        conversation=conversation,
        session=session,
        session_manager=session_manager,
        memory_manager=memory_manager,
        ui=ui,
        config=config,
    )
    assert ctx.args == "hello"
    assert ctx.agent is agent
    assert ctx.conversation is conversation
    assert ctx.session is session
    assert ctx.session_manager is session_manager
    assert ctx.memory_manager is memory_manager
    assert ctx.ui is ui
    assert ctx.config is config


# 验证 UIController 作为 Protocol 不要求显式继承即可注入 CommandContext。
# 测试通过 _DuckUI 鸭子类型实例构造 CommandContext，运行时不抛异常。
def test_ui_controller_protocol_duck_typing() -> None:
    ui = _DuckUI()
    ctx = CommandContext(
        args="",
        agent=None,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config=None,
    )
    assert ctx.ui is ui


# 验证 register_sync 注册主名后 find 能按主名返回该 Command。
# 测试设计为单命令注册后立即查找，断言返回同一对象。
def test_register_sync_main_name() -> None:
    registry = CommandRegistry()
    cmd = _make_command(name="help")
    registry.register_sync(cmd)
    assert registry.find("help") is cmd


# 验证 register_sync 注册带别名后 find 能按别名返回该 Command。
# 测试通过别名查找确认别名映射建立成功。
def test_register_sync_alias() -> None:
    registry = CommandRegistry()
    cmd = _make_command(name="help", aliases=["h"])
    registry.register_sync(cmd)
    assert registry.find("h") is cmd


# 验证 register_sync 在主名与已注册主名重复时抛 ValueError。
# 测试使用 pytest.raises 匹配错误信息，确认冲突被检测。
def test_register_sync_duplicate_main_name() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help"))
    with pytest.raises(ValueError, match="命令名冲突"):
        registry.register_sync(_make_command(name="help"))


# 验证 register_sync 在新命令名与已有别名冲突时抛 ValueError。
# 测试先注册带别名的命令，再用该别名作为新命令主名注册。
def test_register_sync_main_name_conflicts_with_existing_alias() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help", aliases=["h"]))
    with pytest.raises(ValueError, match="命令名与已有别名冲突"):
        registry.register_sync(_make_command(name="h"))


# 验证 register_sync 在新别名与已有命令主名冲突时抛 ValueError。
# 测试先注册主名 help，再用 help 作为另一命令的别名注册。
def test_register_sync_alias_conflicts_with_existing_main_name() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help"))
    with pytest.raises(ValueError, match="别名与已有命令名冲突"):
        registry.register_sync(_make_command(name="other", aliases=["help"]))


# 验证 register_sync 在新别名与已有别名冲突时抛 ValueError。
# 测试先注册带别名 h 的命令，再用 h 作为另一命令的别名注册。
def test_register_sync_alias_conflicts_with_existing_alias() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help", aliases=["h"]))
    with pytest.raises(ValueError, match="别名冲突"):
        registry.register_sync(_make_command(name="other", aliases=["h"]))


# 验证 find 的三种行为：主名命中、别名命中、未命中返回 None。
# 测试在单次注册后覆盖三条路径，确认查找逻辑完整。
def test_find_behaviors() -> None:
    registry = CommandRegistry()
    cmd = _make_command(name="help", aliases=["h"])
    registry.register_sync(cmd)
    assert registry.find("help") is cmd
    assert registry.find("h") is cmd
    assert registry.find("nonexistent") is None


# 验证 list_commands 默认过滤 hidden 命令。
# 测试注册一个可见命令与一个隐藏命令，断言仅可见命令出现。
def test_list_commands_filters_hidden() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="visible"))
    registry.register_sync(_make_command(name="secret", hidden=True))
    names = [c.name for c in registry.list_commands()]
    assert names == ["visible"]


# 验证 list_commands 按 name 字段升序排序返回。
# 测试以乱序注册三个命令，断言返回顺序为字母序。
def test_list_commands_sorted_by_name() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="zebra"))
    registry.register_sync(_make_command(name="alpha"))
    registry.register_sync(_make_command(name="mike"))
    names = [c.name for c in registry.list_commands()]
    assert names == ["alpha", "mike", "zebra"]


# 验证 async register 在加锁下并发注册多个不同命令时不抛错且全部入库。
# 测试通过 asyncio.gather 触发并发，确认 Lock 串行化注册过程。
async def test_async_register_concurrent_safety() -> None:
    registry = CommandRegistry()
    commands = [_make_command(name=f"cmd{i:02d}") for i in range(20)]
    await asyncio.gather(*(registry.register(c) for c in commands))
    for cmd in commands:
        assert registry.find(cmd.name) is cmd
