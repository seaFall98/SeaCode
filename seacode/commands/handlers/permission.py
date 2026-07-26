"""/permission 命令：权限管理（mode/rules/add/reset）。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.permissions import PermissionMode


# /permission 子命令：
#   无参    显示当前模式与规则数量
#   mode    切换权限模式（default/acceptEdits/plan/bypassPermissions）
#   rules   按 三层（用户级/项目级/本地级）展示规则
#   add     解析 "ToolName(pattern) allow|deny" 并追加到本地规则文件
#   reset   清空本地规则文件
async def handle_permission(ctx: CommandContext) -> None:
    checker = ctx.permission_checker
    if checker is None and ctx.agent is not None:
        checker = ctx.agent.permission_checker
    if checker is None and ctx.agent is None:
        ctx.ui.add_system_message("权限系统未初始化")
        return

    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub == "":
        # 显示当前模式与规则数量；rule_engine 缺失时只显示模式。
        mode = checker.mode if checker is not None else ctx.agent.permission_mode
        rule_count = 0
        if checker and checker.rule_engine:
            tiers = checker.rule_engine._load_tiers()
            rule_count = sum(len(t) for t in tiers)
        ctx.ui.add_system_message(
            f"权限状态\n"
            f"  当前模式: {mode.value}\n"
            f"  规则数量: {rule_count}"
        )
        return

    if sub == "mode":
        if not arg:
            modes = ", ".join(m.value for m in PermissionMode)
            ctx.ui.add_system_message(f"用法：/permission mode <模式>\n可选：{modes}")
            return
        # 把字符串映射回 PermissionMode 枚举值，避免大小写或别名误用。
        target_mode: PermissionMode | None = None
        for m in PermissionMode:
            if m.value == arg:
                target_mode = m
                break
        if target_mode is None:
            modes = ", ".join(m.value for m in PermissionMode)
            ctx.ui.add_system_message(f"未知模式：{arg}\n可选：{modes}")
            return
        if ctx.agent is not None:
            ctx.agent.set_permission_mode(target_mode)
        ctx.ui.set_permission_mode(target_mode)
        ctx.ui.add_system_message(f"权限模式已切换为：{target_mode.value}")
        return

    if sub == "rules":
        # 按三层（用户级/项目级/本地级）展示规则，便于定位来源。
        if not checker or not checker.rule_engine:
            ctx.ui.add_system_message("规则引擎未初始化")
            return
        tiers = checker.rule_engine._load_tiers()
        names = ["用户级", "项目级", "本地级"]
        lines: list[str] = ["权限规则："]
        for name, rules in zip(names, tiers):
            if rules:
                lines.append(f"  [{name}]")
                for r in rules:
                    lines.append(f"    {r.tool_name}({r.pattern}) → {r.effect}")
            else:
                lines.append(f"  [{name}] (无规则)")
        ctx.ui.add_system_message("\n".join(lines))
        return

    if sub == "add":
        if not arg:
            ctx.ui.add_system_message(
                "用法：/permission add <ToolName(pattern)> <allow|deny>\n"
                "示例：/permission add Bash(git*) allow"
            )
            return
        # 把 "ToolName(pattern) effect" 拆为规则串与效果，效果只接受 allow/deny。
        from seacode.permissions.rules import parse_rule

        rule_parts = arg.rsplit(None, 1)
        if len(rule_parts) < 2 or rule_parts[1] not in ("allow", "deny"):
            ctx.ui.add_system_message(
                "用法：/permission add <ToolName(pattern)> <allow|deny>\n"
                "示例：/permission add Bash(git*) allow"
            )
            return
        try:
            rule = parse_rule(rule_parts[0], rule_parts[1])  # type: ignore[arg-type]
        except ValueError as e:
            ctx.ui.add_system_message(str(e))
            return
        if checker and checker.rule_engine:
            checker.rule_engine.append_local_rule(rule)
            ctx.ui.add_system_message(
                f"规则已添加：{rule.tool_name}({rule.pattern}) → {rule.effect}"
            )
        else:
            ctx.ui.add_system_message("规则引擎未初始化")
        return

    if sub == "reset":
        # 清空本地规则文件内容，保留文件本身以便后续 append_local_rule 续写。
        if checker and checker.rule_engine and checker.rule_engine._local_path:
            path = checker.rule_engine._local_path
            if path.exists():
                path.write_text("", encoding="utf-8")
            ctx.ui.add_system_message("本地规则已清空")
        else:
            ctx.ui.add_system_message("无本地规则文件")
        return

    ctx.ui.add_system_message(
        "用法：/permission [mode <模式> | rules | add <规则> <效果> | reset]"
    )


# 命令定义：LOCAL 类型，子命令参数提示。
PERMISSION_COMMAND = Command(
    name="permission",
    description="权限管理",
    type=CommandType.LOCAL,
    handler=handle_permission,
    aliases=[],
    usage="/permission [mode <模式> | rules | add <规则> <效果> | reset]",
    arg_prompt="子命令",
)
