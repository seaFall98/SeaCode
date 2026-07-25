"""/rewind 命令：回滚文件到历史快照。

无参数时列出可用快照；``/rewind <idx>`` 调用 ``FileHistory.rewind(idx)`` 把指定
快照引用的备份内容写回原路径。handler 通过 ``ctx.agent.file_history`` 取历史管理器，
为 None 时提示未初始化。``FileHistory`` 实例由 app.py 在启动时注入到 Agent。
"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType

# /rewind 列表项中 user_text 的截断长度，避免长提示词污染输出。
_USER_TEXT_LIMIT = 60


# /rewind：无参数列出快照；带 index 调用 rewind 还原文件。
async def handle_rewind(ctx: CommandContext) -> None:
    file_history = getattr(ctx.agent, "file_history", None) if ctx.agent else None
    if file_history is None:
        ctx.ui.add_system_message("文件历史未初始化")
        return

    parts = ctx.args.split(None, 1)
    if not parts:
        # 无参数：列出所有快照。
        snapshots = file_history.get_snapshots()
        if not snapshots:
            ctx.ui.add_system_message("No checkpoints to rewind to.")
            return
        lines = ["Available checkpoints:"]
        for i, snap in enumerate(snapshots):
            user_text = snap.user_text or "(empty)"
            if len(user_text) > _USER_TEXT_LIMIT:
                user_text = user_text[:_USER_TEXT_LIMIT] + "..."
            lines.append(
                f"  [{i}] msg#{snap.message_index} "
                f"files={len(snap.backups)} text={user_text!r}"
            )
        ctx.ui.add_system_message("\n".join(lines))
        return

    # /rewind <idx>：调用 rewind 还原文件。
    try:
        idx = int(parts[0])
    except ValueError:
        ctx.ui.add_system_message(f"无效的快照索引: {parts[0]}")
        return
    try:
        changed = file_history.rewind(idx)
    except Exception as e:
        ctx.ui.add_system_message(f"回滚失败: {e}")
        return
    if not changed:
        ctx.ui.add_system_message(f"快照 {idx} 无需还原或越界")
        return
    file_list = "\n".join(f"  - {p}" for p in changed)
    ctx.ui.add_system_message(
        f"已回滚到快照 {idx}，还原 {len(changed)} 个文件:\n{file_list}"
    )


# 构造 /rewind 命令定义；无别名。
def create_rewind_command() -> Command:
    return Command(
        name="rewind",
        description="回滚文件到历史快照",
        type=CommandType.LOCAL,
        handler=handle_rewind,
        aliases=[],
        usage="/rewind [snapshot-index]",
        arg_prompt="快照索引（留空列出）",
    )
