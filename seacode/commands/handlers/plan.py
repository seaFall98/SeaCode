"""/plan 命令：进入 Plan 模式或带参数直接规划。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.prompts import build_plan_mode_reentry_reminder


# /plan：切换到 Plan 模式；重入时若曾退出过 Plan Mode 且 plan 文件存在，
# 注入 reentry reminder 帮助模型从上次 plan 继续；带参数时作为用户消息发给 LLM。
async def handle_plan(ctx: CommandContext) -> None:
    ctx.ui.set_plan_mode(True)
    ctx.ui.add_system_message("已切换到 Plan 模式 — 只读，禁止写入和命令执行")

    # 重入检测：本次会话曾退出过 Plan Mode 且 plan 文件已存在时注入重入提示。
    app = ctx.ui
    if getattr(app, "_has_exited_plan_mode", False) and ctx.agent is not None:
        plan_path = ctx.agent._get_plan_path()
        plan_exists = plan_path.exists()
        reentry_msg = build_plan_mode_reentry_reminder(str(plan_path), plan_exists)
        if reentry_msg:
            ctx.ui.add_system_message(reentry_msg)
            app._has_exited_plan_mode = False  # type: ignore[attr-defined]

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
