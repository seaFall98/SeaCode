"""/skill 命令：Skill 管理（list / info / reload）。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /skill 命令：无参或 list 列出所有 Skill；info <name> 显示详情；reload 重扫刷新。
async def handle_skill(ctx: CommandContext) -> None:
    agent = ctx.agent
    loader = getattr(agent, "skill_loader", None) if agent is not None else None
    if loader is None:
        ctx.ui.add_system_message("Skill 系统未初始化")
        return

    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else "list"
    arg = parts[1] if len(parts) > 1 else ""

    if sub in ("", "list"):
        catalog = loader.get_catalog()
        if not catalog:
            ctx.ui.add_system_message("Skills (0 entries)")
            return
        lines = [f"Skills ({len(catalog)} entries):"]
        for name, desc in sorted(catalog):
            label = loader.get_source_label(name)
            lines.append(f"- {name} [{label}]: {desc}")
        ctx.ui.add_system_message("\n".join(lines))
    elif sub == "info":
        if not arg:
            ctx.ui.add_system_message("用法：/skill info <name>")
            return
        skill = loader.get(arg)
        if skill is None:
            ctx.ui.add_system_message(f"未知 Skill：{arg}")
            return
        source = loader.get_source_label(arg)
        text = (
            f"name: {skill.name}\n"
            f"description: {skill.description}\n"
            f"mode: {skill.mode}\n"
            f"context: {skill.context}\n"
            f"model: {skill.model or 'default'}\n"
            f"source: {source}\n"
            f"source_path: {skill.source_path}\n"
            f"is_directory: {skill.is_directory}"
        )
        ctx.ui.add_system_message(text)
    elif sub == "reload":
        loader.reload()
        register_cb = ctx.config.get("register_skill_commands") if ctx.config else None
        if register_cb is not None:
            register_cb()
        build_catalog_cb = ctx.config.get("build_skill_catalog") if ctx.config else None
        if build_catalog_cb is not None:
            catalog_text = build_catalog_cb()
            agent.set_skill_catalog(catalog_text)
        count = len(loader.get_catalog())
        ctx.ui.add_system_message(f"已重载 {count} 个 Skill")
    else:
        ctx.ui.add_system_message(
            f"未知子命令：{sub}，用法：/skill [list|info|reload]"
        )


# 命令定义：LOCAL 类型，管理命令本身是本地操作；/skills 是 /skill 的别名。
SKILL_COMMAND = Command(
    name="skill",
    description="Skill 管理（list / info / reload）",
    type=CommandType.LOCAL,
    handler=handle_skill,
    aliases=["skills"],
    usage="/skill [list|info|reload]",
    arg_prompt="子命令",
)
