"""/clear 命令：清空当前对话并创建新会话；同步重建 FileHistory 与工具注入。"""

from __future__ import annotations

from seacode.commands.registry import Command, CommandContext, CommandType


# /clear：清屏、创建新会话、重置 Agent 历史游标，避免旧上下文污染新会话。
# batch13：清空后基于新 session_id 重建 FileHistory 并同步到 write_file/edit_file 工具，
# 让新会话的 /rewind 列表只显示新会话产生的快照。
async def handle_clear(ctx: CommandContext) -> None:
    ctx.config["clear_chat"]()
    new_session_id = ""
    if ctx.session_manager is not None:
        new_session = ctx.session_manager.create()
        ctx.config["set_session"](new_session)
        new_session_id = getattr(new_session, "session_id", "")
    # 重置 Agent 的 history_cursor，避免新会话重复写前缀。
    agent = ctx.agent
    if agent is not None:
        setattr(agent, "history_cursor", 0)
    # batch13：重建 FileHistory；新 session_id 隔离旧快照，工具同步注入。
    _rebuild_file_history(agent, new_session_id)
    ctx.ui.add_system_message("已清空")


# 基于新 session_id 重建 Agent.file_history 并同步到注册表中的写文件工具。
# agent 或 work_dir 缺失时跳过；新 FileHistory 的 has_snapshots() 返回 False。
def _rebuild_file_history(agent: object | None, session_id: str) -> None:
    if agent is None:
        return
    work_dir = getattr(agent, "work_dir", None)
    if not work_dir or not session_id:
        return
    try:
        from seacode.filehistory.history import FileHistory

        new_fh = FileHistory(work_dir, session_id)
    except Exception:
        # FileHistory 装配失败不阻断 /clear 主流程。
        return
    setattr(agent, "file_history", new_fh)
    # 同步注入到 write_file/edit_file 等持有 file_history 属性的工具。
    registry = getattr(agent, "registry", None)
    if registry is None:
        return
    list_tools = getattr(registry, "list_tools", None)
    if list_tools is None:
        return
    try:
        tools = list_tools()
    except Exception:
        return
    for tool in tools:
        if hasattr(tool, "file_history"):
            try:
                tool.file_history = new_fh
            except Exception:
                # 单个工具注入失败不阻断其它工具。
                continue


# 命令定义：LOCAL_UI 类型，会操作 TUI 状态（清屏 + 创新会话）。
CLEAR_COMMAND = Command(
    name="clear",
    description="清空当前对话并创新会话",
    type=CommandType.LOCAL_UI,
    handler=handle_clear,
    aliases=[],
    usage="/clear",
    arg_prompt="",
)
