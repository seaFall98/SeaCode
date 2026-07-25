"""/compact 命令：手动触发上下文压缩。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /compact：token 数低于 5000 时跳过，否则调 Agent.manual_compact；失败时显示错误不崩溃。
async def handle_compact(ctx: CommandContext) -> None:
    used, _ = ctx.ui.get_token_count()
    if used < 5000:
        ctx.ui.add_system_message(f"当前 token 数 {used} 低于 5000，跳过压缩")
        return
    try:
        await ctx.agent.manual_compact()
    except Exception as exc:
        ctx.ui.add_system_message(f"压缩失败：{exc}")


# 命令定义：LOCAL 类型，别名 c。
COMPACT_COMMAND = Command(
    name="compact",
    description="手动压缩上下文",
    type=CommandType.LOCAL,
    handler=handle_compact,
    aliases=["c"],
    usage="/compact",
    arg_prompt="",
)
