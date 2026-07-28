"""/compact 命令：手动触发上下文压缩。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /compact：token 数低于 5000 时跳过，否则调 Agent.manual_compact；
# 成功时持久化 compact_boundary 让 resume 可重建压缩后状态；失败显示错误不崩溃。
async def handle_compact(ctx: CommandContext) -> None:
    if ctx.agent is None:
        ctx.ui.add_system_message("Agent 未初始化")
        return

    used, _ = ctx.ui.get_token_count()
    if used < 5000:
        ctx.ui.add_system_message(f"当前 token 数 {used:,}，无需压缩")
        return

    # 延迟导入避免 commands 包初始化时拉起 agent 依赖。
    from seacode.agent import CompactNotification, ErrorEvent

    try:
        result = await ctx.agent.manual_compact(ctx.conversation)
    except Exception as exc:
        # manual_compact 抛异常时显示错误且不崩溃，保持命令循环可用。
        ctx.ui.add_system_message(f"压缩失败：{exc}")
        return
    if isinstance(result, CompactNotification):
        # 持久化 compact_boundary，使后续 resume 可重建压缩后的状态。
        # manual_compact 已重写了 ctx.conversation；本命令同步标记 boundary
        # 覆盖的历史，后续回合只追加新的 canonical 消息。
        if ctx.session is not None and result.boundary is not None:
            from seacode.memory.session import make_compact_boundary

            ctx.session.append_record(
                make_compact_boundary(result.boundary.summary, result.boundary.keep)
            )
            mark_all_persisted = getattr(ctx.conversation, "mark_all_persisted", None)
            if callable(mark_all_persisted):
                mark_all_persisted()
        ctx.ui.add_system_message(result.message)
    elif isinstance(result, ErrorEvent):
        ctx.ui.add_system_message(f"压缩失败：{result.message}")


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
