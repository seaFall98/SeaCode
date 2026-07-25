"""/memory 命令：记忆管理（list/clear/edit）。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /memory list：显示记忆索引；clear：清空项目与用户级记忆（保留 MEMORY.md 索引并清空）；
# edit：提示记忆文件路径供用户手动编辑。
async def handle_memory(ctx: CommandContext) -> None:
    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else "list"

    if sub in ("list", ""):
        if ctx.memory_manager is None:
            ctx.ui.add_system_message("记忆系统未初始化")
            return
        text = ctx.memory_manager.get_display_text()
        ctx.ui.add_system_message(text if text else "无记忆")
        return

    if sub == "clear":
        if ctx.memory_manager is None:
            ctx.ui.add_system_message("记忆系统未初始化")
            return
        ctx.memory_manager.clear_memories()
        ctx.ui.add_system_message("已清空")
        return

    if sub == "edit":
        if ctx.memory_manager is None:
            ctx.ui.add_system_message("记忆系统未初始化")
            return
        paths = ctx.memory_manager.get_memory_file_paths()
        lines = ["记忆文件路径（手动编辑后重启生效）："]
        for p in paths:
            lines.append(f"  {p}")
        ctx.ui.add_system_message("\n".join(lines))
        return

    ctx.ui.add_system_message(f"未知子命令：{sub}，用法：/memory [list|clear|edit]")


# 命令定义：LOCAL 类型，子命令参数提示。
MEMORY_COMMAND = Command(
    name="memory",
    description="记忆管理",
    type=CommandType.LOCAL,
    handler=handle_memory,
    aliases=[],
    usage="/memory [list|clear|edit]",
    arg_prompt="子命令",
)
