"""/plan 命令：进入 Plan 模式或带参数直接规划。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /plan：已在 Plan 模式则提示，否则切模式；带参数时立即把参数作为用户消息发给 LLM。
async def handle_plan(ctx: CommandContext) -> None:
    agent = ctx.agent
    if getattr(agent, "plan_mode", False):
        ctx.ui.add_system_message("已在 Plan 模式")
        return
    ctx.ui.set_plan_mode(True)
    if ctx.args.strip():
        ctx.ui.send_user_message(ctx.args.strip())


# 命令定义：LOCAL_UI 类型，会切换 Plan 模式；别名 p。
PLAN_COMMAND = Command(
    name="plan",
    description="进入 Plan 模式或带参数直接规划",
    type=CommandType.LOCAL_UI,
    handler=handle_plan,
    aliases=["p"],
    usage="/plan [task]",
    arg_prompt="",
)
