"""/permission 命令：权限管理（mode/rules/add/reset）。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /permission mode：显示当前模式；rules：显示规则列表；add：添加规则；reset：重置规则。
async def handle_permission(ctx: CommandContext) -> None:
    agent = ctx.agent
    checker = getattr(agent, "permission_checker", None)
    if checker is None:
        ctx.ui.add_system_message("权限系统未初始化")
        return

    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else "mode"
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("mode", ""):
        mode = getattr(checker, "mode", "未知")
        ctx.ui.add_system_message(f"当前权限模式：{mode}")
        return

    if sub == "rules":
        rules = getattr(checker, "rules", [])
        if not rules:
            ctx.ui.add_system_message("无权限规则")
            return
        lines = ["权限规则："]
        for i, rule in enumerate(rules, start=1):
            lines.append(f"  {i}. {rule}")
        ctx.ui.add_system_message("\n".join(lines))
        return

    if sub == "add":
        if not arg:
            ctx.ui.add_system_message("用法：/permission add <rule>")
            return
        try:
            checker.add_rule(arg)
            ctx.ui.add_system_message(f"已添加规则：{arg}")
        except Exception as exc:
            ctx.ui.add_system_message(f"添加规则失败：{exc}")
        return

    if sub == "reset":
        checker.reset_rules()
        ctx.ui.add_system_message("已重置权限规则")
        return

    ctx.ui.add_system_message(f"未知子命令：{sub}，用法：/permission [mode|rules|add|reset]")


# 命令定义：LOCAL 类型，子命令参数提示。
PERMISSION_COMMAND = Command(
    name="permission",
    description="权限管理",
    type=CommandType.LOCAL,
    handler=handle_permission,
    aliases=[],
    usage="/permission [mode|rules|add|reset]",
    arg_prompt="子命令",
)
