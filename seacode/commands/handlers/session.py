"""/session 命令：会话管理（list/resume/new/delete）。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /session list：列出会话；resume：恢复会话（支持序号或 ID）；new：创新会话；delete：删除会话。
async def handle_session(ctx: CommandContext) -> None:
    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else "list"
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("list", ""):
        sessions = ctx.session_manager.list()
        if not sessions:
            ctx.ui.add_system_message("无历史会话")
            return
        lines = ["历史会话："]
        for i, s in enumerate(sessions, start=1):
            title = getattr(s, "title", "") or "未命名"
            session_id = getattr(s, "session_id", "")
            updated = getattr(s, "updated_at", "")
            lines.append(f"  {i}. [{session_id[:8]}] {title} (最后活跃：{updated})")
        ctx.ui.add_system_message("\n".join(lines))
        return

    if sub == "resume":
        if not arg:
            ctx.ui.add_system_message("用法：/session resume <id_or_index>")
            return
        sessions = ctx.session_manager.list()
        if arg.isdigit():
            idx = int(arg)
            if idx < 1 or idx > len(sessions):
                ctx.ui.add_system_message(f"序号超出范围：{idx}（共 {len(sessions)} 个会话）")
                return
            session_id = getattr(sessions[idx - 1], "session_id", "")
        else:
            session_id = arg
        result = ctx.session_manager.resume(session_id)
        if not getattr(result, "success", False):
            ctx.ui.add_system_message(f"恢复会话失败：{getattr(result, 'error', '未知错误')}")
            return
        ctx.config["set_session"](result.session)
        ctx.config["set_conversation"](result.conversation)
        ctx.config["render_restored"](result.messages)
        ctx.ui.add_system_message(f"已恢复会话：{session_id[:8]}")
        return

    if sub == "new":
        ctx.config["clear_chat"]()
        new_session = ctx.session_manager.create()
        ctx.config["set_session"](new_session)
        ctx.ui.add_system_message("已创建新会话")
        return

    if sub == "delete":
        if not arg:
            ctx.ui.add_system_message("用法：/session delete <id_or_index>")
            return
        sessions = ctx.session_manager.list()
        if arg.isdigit():
            idx = int(arg)
            if idx < 1 or idx > len(sessions):
                ctx.ui.add_system_message(f"序号超出范围：{idx}（共 {len(sessions)} 个会话）")
                return
            target_id = getattr(sessions[idx - 1], "session_id", "")
        else:
            target_id = arg
        ctx.session_manager.delete(target_id)
        ctx.ui.add_system_message("已删除")
        return

    ctx.ui.add_system_message(f"未知子命令：{sub}，用法：/session [list|resume|new|delete]")


# 命令定义：LOCAL 类型，子命令参数提示。
SESSION_COMMAND = Command(
    name="session",
    description="会话管理",
    type=CommandType.LOCAL,
    handler=handle_session,
    aliases=[],
    usage="/session [list|resume|new|delete]",
    arg_prompt="子命令",
)
