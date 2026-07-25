"""/session 命令：会话管理（list/resume/new/delete）。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.conversation import ConversationManager


# /session 子命令：
#   无参    显示当前会话详情
#   list    列出已保存的会话（按 last_active 倒序）
#   resume  按序号或 ID 恢复会话；先关闭旧 session 再注入恢复的消息
#   new     关闭旧 session 并创建新会话，清空对话历史
#   delete  按 ID 删除会话；不能删除当前活跃会话
async def handle_session(ctx: CommandContext) -> None:
    sm = ctx.session_manager
    if sm is None:
        ctx.ui.add_system_message("会话管理器未初始化")
        return

    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub == "":
        # 显示当前会话详情；无活跃会话时给出提示。
        if ctx.session:
            m = ctx.session.meta
            ts = m.last_active.strftime("%Y-%m-%d %H:%M")
            ctx.ui.add_system_message(
                f"当前会话：{m.id}\n"
                f"  标题：{m.title or '(未命名)'}\n"
                f"  消息：{m.message_count} 条\n"
                f"  Token：{m.total_tokens:,}\n"
                f"  最后活跃：{ts}"
            )
        else:
            ctx.ui.add_system_message("当前没有活跃会话")
        return

    if sub == "list":
        metas = sm.list()
        if not metas:
            ctx.ui.add_system_message("没有已保存的会话。")
            return
        lines: list[str] = ["历史会话："]
        # 仅展示前 10 条避免过长，按 last_active 倒序排列。
        for m in metas[:10]:
            ts = m.last_active.strftime("%Y-%m-%d %H:%M")
            title = m.title or "(未命名)"
            lines.append(f"  {m.id}  {title}  [{m.message_count} msgs, {ts}]")
        ctx.ui.add_system_message("\n".join(lines))
        return

    if sub == "resume":
        if not arg:
            # 无参数时列出可恢复的会话供用户选择，并缓存 ID 列表支持序号选择。
            metas = sm.list()
            if not metas:
                ctx.ui.add_system_message("没有已保存的会话。")
                return
            lines = [
                "可恢复的会话（使用 /session resume <id> 或 /session resume <序号>）："
            ]
            for i, m in enumerate(metas[:15], 1):
                ts = m.last_active.strftime("%Y-%m-%d %H:%M")
                title = m.title or "(未命名)"
                lines.append(
                    f"  {i}. [{m.id[:8]}]  {title}  ({m.message_count} msgs, {ts})"
                )
            ctx.ui.add_system_message("\n".join(lines))
            ctx.config["_resume_candidates"] = [m.id for m in metas[:15]]
            return
        # 支持用序号恢复；从 _resume_candidates 缓存中按序号查 ID。
        candidates = ctx.config.get("_resume_candidates", [])
        session_id = arg
        if arg.isdigit() and candidates:
            idx = int(arg) - 1
            if 0 <= idx < len(candidates):
                session_id = candidates[idx]
        result = sm.resume(session_id)
        # resume 返回 None 表示会话不存在；其它字段直接消费。
        if result is None:
            ctx.ui.add_system_message(f"会话未找到：{session_id}")
            return
        # 恢复前关闭旧 session，避免文件句柄泄漏。
        if ctx.session:
            ctx.session.close()
        ctx.config["set_session"](result.session)
        # 重建 ConversationManager 并把恢复的消息灌入历史。
        conv = ConversationManager()
        for msg in result.messages:
            conv.history.append(msg)
        ctx.config["set_conversation"](conv)
        await ctx.config["render_restored"](result.messages)
        ctx.ui.add_system_message(
            f"会话已恢复：{session_id} ({result.session.meta.message_count} msgs)"
        )
        return

    if sub == "new":
        # 关闭旧 session 并创建新会话，清空对话历史与渲染区域。
        if ctx.session:
            ctx.session.close()
        new_session = sm.create()
        ctx.config["set_session"](new_session)
        ctx.config["set_conversation"](ConversationManager())
        ctx.config["clear_chat"]()
        ctx.ui.add_system_message(f"新会话已创建：{new_session.session_id}")
        return

    if sub == "delete":
        if not arg:
            ctx.ui.add_system_message("用法：/session delete <id>")
            return
        # 安全检查：不能删除当前活跃会话，避免句柄失效与状态不一致。
        if ctx.session and ctx.session.session_id == arg:
            ctx.ui.add_system_message("不能删除当前活跃的会话。")
            return
        if sm.delete(arg):
            ctx.ui.add_system_message(f"会话已删除：{arg}")
        else:
            ctx.ui.add_system_message(f"会话未找到：{arg}")
        return

    ctx.ui.add_system_message(
        "用法：/session [list | resume <id> | new | delete <id>]"
    )


# 命令定义：LOCAL 类型，子命令参数提示。
SESSION_COMMAND = Command(
    name="session",
    description="会话管理",
    type=CommandType.LOCAL,
    handler=handle_session,
    aliases=[],
    usage="/session [list | resume <id> | new | delete <id>]",
    arg_prompt="子命令",
)
