"""/help 命令：列出全部命令或显示单命令详情。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /help：无参列出全部命令，带参显示单命令详情，未找到给出提示。
# 参数统一小写处理，让 /HELP、/Help 等大小写混用也能匹配命令名。
async def handle_help(ctx: CommandContext) -> None:
    registry = ctx.config["registry"]
    args = ctx.args.strip()
    if not args:
        cmds = registry.list_commands()
        lines = ["可用命令："]
        for cmd in cmds:
            lines.append(f"  /{cmd.name:<12} - {cmd.description}")
        lines.append("")
        lines.append("输入 /help <命令名> 查看单命令详情")
        ctx.ui.add_system_message("\n".join(lines))
        return
    cmd = registry.find(args.lower())
    if cmd is None:
        ctx.ui.add_system_message(f"未知命令：{args}，输入 /help 查看可用命令")
        return
    aliases = ", ".join(cmd.aliases) if cmd.aliases else "无"
    lines = [
        f"命令：/{cmd.name}",
        f"描述：{cmd.description}",
        f"用法：{cmd.usage}",
        f"别名：{aliases}",
        f"类型：{cmd.type.value}",
    ]
    if cmd.arg_prompt:
        lines.append(f"参数：{cmd.arg_prompt}")
    ctx.ui.add_system_message("\n".join(lines))


# 命令定义：LOCAL 类型，别名 h 与 ?，无参数提示。
HELP_COMMAND = Command(
    name="help",
    description="显示命令列表或单命令详情",
    type=CommandType.LOCAL,
    handler=handle_help,
    aliases=["h", "?"],
    usage="/help [command]",
    arg_prompt="",
)
