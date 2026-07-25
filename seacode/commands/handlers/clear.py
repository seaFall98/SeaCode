"""/clear 命令：清空当前对话并创建新会话。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /clear：清屏、创建新会话、重置 Agent 历史游标，避免旧上下文污染新会话。
async def handle_clear(ctx: CommandContext) -> None:
    ctx.config["clear_chat"]()
    if ctx.session_manager is not None:
        new_session = ctx.session_manager.create()
        ctx.config["set_session"](new_session)
    # 重置 Agent 的 history_cursor，避免新会话重复写前缀。
    agent = ctx.agent
    if agent is not None:
        setattr(agent, "history_cursor", 0)
    ctx.ui.add_system_message("已清空")


# 命令定义：LOCAL_UI 类型，会操作 TUI 状态（清屏 + 创新会话）。
CLEAR_COMMAND = Command(
    name="clear",
    description="清空当前对话并创新会话",
    type=CommandType.LOCAL_UI,
    handler=handle_clear,
    aliases=[],
    usage="/clear",
    arg_prompt="",
)
