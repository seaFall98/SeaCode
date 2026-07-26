"""/rewind 命令：回滚到历史快照，支持代码+对话、仅对话、仅代码三种模式。"""

from __future__ import annotations

from datetime import datetime

from seacode.commands.registry import Command, CommandContext, CommandType

# /rewind 列表项中 user_text 的截断长度，避免长提示词污染输出。
_USER_TEXT_LIMIT = 50


# /rewind：无参数列出快照与选项说明；带参数按 option 回滚代码与/或对话。
async def handle_rewind(ctx: CommandContext) -> None:
    fh = getattr(ctx.agent, "file_history", None) if ctx.agent else None
    if fh is None or not fh.has_snapshots():
        ctx.ui.add_system_message("No checkpoints to rewind to.")
        return

    snapshots = fh.get_snapshots()

    lines = ["⟲ Rewind — select a checkpoint:\n"]
    for i, snap in enumerate(snapshots):
        ago = int((datetime.now() - snap.timestamp).total_seconds())
        user_text = snap.user_text
        if len(user_text) > _USER_TEXT_LIMIT:
            label = user_text[:_USER_TEXT_LIMIT] + "…"
        else:
            label = user_text
        lines.append(f"  [{i + 1}] {label} ({ago}s ago, {len(snap.backups)} file(s))")
    lines.append("\nOptions after selecting:")
    lines.append("  1) Restore code and conversation")
    lines.append("  2) Restore conversation only")
    lines.append("  3) Restore code only")
    lines.append(f"\nUsage: /rewind <checkpoint> [option]  (e.g. /rewind {len(snapshots)} 1)")
    ctx.ui.add_system_message("\n".join(lines))

    args = ctx.args.strip()
    if not args:
        return

    parts = args.split()
    try:
        # 1-based 索引：用户输入 /rewind 1 选第一个快照。
        idx = int(parts[0]) - 1
    except (ValueError, IndexError):
        ctx.ui.add_system_message("Invalid checkpoint number.")
        return

    if idx < 0 or idx >= len(snapshots):
        ctx.ui.add_system_message(f"Checkpoint {idx + 1} not found. Valid: 1-{len(snapshots)}")
        return

    option = 1
    if len(parts) > 1:
        try:
            option = int(parts[1])
        except ValueError:
            pass

    snap = snapshots[idx]

    # option 1：代码+对话；option 2：仅对话；option 3：仅代码。
    if option == 1:
        changed = fh.rewind(idx)
        ctx.conversation.replace_history(ctx.conversation.history[: snap.message_index])
        ctx.ui.add_system_message(
            f"⟲ Rewound to checkpoint {idx + 1}. Restored {len(changed)} file(s) and conversation."
        )
    elif option == 2:
        ctx.conversation.replace_history(ctx.conversation.history[: snap.message_index])
        ctx.ui.add_system_message(
            f"⟲ Rewound conversation to checkpoint {idx + 1}. Files unchanged."
        )
    elif option == 3:
        changed = fh.rewind(idx)
        ctx.ui.add_system_message(
            f"⟲ Restored {len(changed)} file(s) to checkpoint {idx + 1}. Conversation unchanged."
        )
    else:
        ctx.ui.add_system_message("Invalid option. Use 1 (both), 2 (conversation), or 3 (code).")


REWIND_COMMAND = Command(
    name="rewind",
    description="Rewind to a previous checkpoint",
    type=CommandType.LOCAL,
    handler=handle_rewind,
    aliases=[],
    usage="/rewind [checkpoint_number] [option]",
    arg_prompt="快照编号与选项",
)
