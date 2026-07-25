"""命令解析器与补全测试：覆盖斜杠命令识别、参数切分与前缀匹配补全。"""

from __future__ import annotations

from seacode.commands.parser import MAX_COMPLETION_ITEMS, complete, parse_command
from seacode.commands.registry import Command, CommandContext, CommandRegistry, CommandType


# 空实现 handler，仅用于构造 Command 实例，不执行任何业务逻辑。
async def _noop_handler(ctx: CommandContext) -> None:
    del ctx


# 构造最小 Command 实例，便于补全测试复用并保持可读性。
def _make_command(
    name: str,
    description: str = "",
    aliases: list[str] | None = None,
    hidden: bool = False,
) -> Command:
    return Command(
        name=name,
        description=description,
        type=CommandType.LOCAL,
        handler=_noop_handler,
        aliases=aliases if aliases is not None else [],
        hidden=hidden,
    )


# 验证 parse_command 对非斜杠开头的输入返回非命令三元组。
# 测试设计为传入普通文本，断言 is_command 为 False 且名称与参数为空。
def test_parse_command_non_slash_returns_false() -> None:
    assert parse_command("hello") == ("", "", False)


# 验证 parse_command 对仅含斜杠的输入识别为命令但名称与参数均为空。
# 测试覆盖空 body 分支，确认不抛错且 is_command 为 True。
def test_parse_command_slash_only() -> None:
    assert parse_command("/") == ("", "", True)


# 验证 parse_command 对无参数命令返回空 args。
# 测试设计为传入 /help，断言名称与 is_command 正确且 args 为空串。
def test_parse_command_no_args() -> None:
    assert parse_command("/help") == ("help", "", True)


# 验证 parse_command 切分命令名与参数部分。
# 测试传入中文参数，确认参数原样保留不变形。
def test_parse_command_with_args() -> None:
    assert parse_command("/review 关注并发") == ("review", "关注并发", True)


# 验证 parse_command 将命令名小写化。
# 测试传入混合大小写命令名，断言返回的 name 为小写。
def test_parse_command_lowercases_name() -> None:
    assert parse_command("/Help") == ("help", "", True)


# 验证 parse_command 对命令名后接单空格与参数的切分。
# 测试传入 /session list，断言名称与参数分别正确。
def test_parse_command_name_and_space_arg() -> None:
    assert parse_command("/session list") == ("session", "list", True)


# 验证 parse_command 对多空格分隔的参数折叠为单段 args。
# 测试传入三个空格分隔，断言 args 中不再包含多余空格。
def test_parse_command_multiple_spaces_in_args() -> None:
    assert parse_command("/plan   分析目录") == ("plan", "分析目录", True)


# 验证 complete 对非斜杠前缀返回空列表。
# 测试设计为传入不以 / 开头的前缀，断言结果为空。
def test_complete_non_slash_prefix() -> None:
    registry = CommandRegistry()
    assert complete(registry, "h") == []


# 验证 complete 在唯一匹配时返回 display 文本与补全值。
# 测试注册单条命令，断言返回的元组与预期完全一致。
def test_complete_single_match() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help", description="显示帮助"))
    result = complete(registry, "/h")
    assert result == [("/help - 显示帮助", "/help ")]


# 验证 complete 在多匹配时返回多条结果。
# 测试注册两条以 h 开头的命令，断言结果数量为 2 且包含两者。
def test_complete_multiple_matches() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help", description="显示帮助"))
    registry.register_sync(_make_command(name="history", description="显示历史"))
    result = complete(registry, "/h")
    assert len(result) == 2
    displays = [r[0] for r in result]
    assert "/help - 显示帮助" in displays
    assert "/history - 显示历史" in displays


# 验证 complete 通过别名前缀匹配命令时返回该命令一条。
# 测试主名不以 h 开头，仅别名命中前缀，确认仍能匹配。
def test_complete_alias_match() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="exit", aliases=["h"], description="退出"))
    result = complete(registry, "/h")
    assert result == [("/exit - 退出", "/exit ")]


# 验证 complete 主名与别名同时命中前缀时按命令名去重为一条。
# 测试主名 help 与别名 h 都以 h 开头，断言结果仅一条。
def test_complete_main_and_alias_dedup() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help", aliases=["h"], description="显示帮助"))
    result = complete(registry, "/h")
    assert len(result) == 1
    assert result[0] == ("/help - 显示帮助", "/help ")


# 验证 complete 结果按命令名升序排序。
# 测试以乱序注册两条命令，断言返回顺序为字母序。
def test_complete_sorted_by_name() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="history", description="h2"))
    registry.register_sync(_make_command(name="help", description="h1"))
    result = complete(registry, "/h")
    values = [r[1] for r in result]
    assert values == ["/help ", "/history "]


# 验证 complete 最多返回 MAX_COMPLETION_ITEMS 条。
# 测试注册超过上限的命令数量，断言结果长度被截断为上限值。
def test_complete_max_items() -> None:
    registry = CommandRegistry()
    for i in range(MAX_COMPLETION_ITEMS + 4):
        registry.register_sync(_make_command(name=f"c{i:02d}", description="d"))
    result = complete(registry, "/c")
    assert len(result) == MAX_COMPLETION_ITEMS


# 验证 complete 在无匹配时返回空列表。
# 测试注册命令后传入不匹配的前缀，断言结果为空。
def test_complete_no_match() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="help", description="显示帮助"))
    assert complete(registry, "/z") == []


# 验证 complete 过滤 hidden 命令。
# 测试注册 hidden 命令并以其名前缀查询，断言结果为空列表。
def test_complete_filters_hidden() -> None:
    registry = CommandRegistry()
    registry.register_sync(_make_command(name="hidden_cmd", hidden=True))
    assert complete(registry, "/hidden") == []
