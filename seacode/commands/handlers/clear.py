"""/clear 命令：清空当前对话并创建新会话；同步重建 FileHistory 与工具注入。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType
from seacode.conversation import ConversationManager


# /clear：close 旧 session → 创建新 session → 重建 file_history →
# 重建 ConversationManager → 重置 loop_count/skills/tokens →
# clear_chat → refresh_status → 提示。
async def handle_clear(ctx: CommandContext) -> None:
    new_session_id = ""
    if ctx.session_manager is not None:
        new_session = ctx.session_manager.create()
        switch_session = ctx.config.get("switch_session")
        if callable(switch_session):
            switch_session(new_session, [])
        else:
            if ctx.session:
                ctx.session.close()
            ctx.config["set_session"](new_session)
        new_session_id = getattr(new_session, "session_id", "")

        # 用新 session ID 重建 file history，让新会话的 /rewind 列表只显示新快照。
        if ctx.agent and not callable(switch_session):
            from seacode.filehistory.history import FileHistory
            work_dir = getattr(ctx.agent, "_work_dir", None) or getattr(ctx.agent, "work_dir", None)
            if work_dir and new_session_id:
                file_history = FileHistory(work_dir, new_session_id)
                ctx.agent.file_history = file_history
                for tool in ctx.agent.registry.list_tools():
                    if hasattr(tool, "file_history"):
                        tool.file_history = file_history

    # 没有统一切换回调时保留旧运行时的显式历史替换。
    if not callable(ctx.config.get("switch_session")):
        ctx.config["set_conversation"](ConversationManager())

    # 重置 Agent 运行时状态：循环计数、技能、token 统计。
    if ctx.agent:
        ctx.agent._loop_count = 0
        ctx.agent.clear_active_skills()
        ctx.agent.total_input_tokens = 0
        ctx.agent.total_output_tokens = 0

    ctx.config["clear_chat"]()
    ctx.ui.refresh_status()
    ctx.ui.add_system_message("对话已清除，新会话已创建")


CLEAR_COMMAND = Command(
    name="clear",
    description="清除对话历史",
    usage="/clear",
    type=CommandType.LOCAL_UI,
    handler=handle_clear,
    aliases=[],
    arg_prompt="",
)
