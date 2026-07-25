"""命令处理 handler 的单元测试：覆盖 11 条内置命令与批量注册。"""

from __future__ import annotations

from typing import Any

import pytest

from seacode.commands.handlers import register_all_commands
from seacode.commands.handlers.clear import handle_clear
from seacode.commands.handlers.compact import handle_compact
from seacode.commands.handlers.help import handle_help
from seacode.commands.handlers.mcp import handle_mcp
from seacode.commands.handlers.memory import handle_memory
from seacode.commands.handlers.permission import handle_permission
from seacode.commands.handlers.plan import handle_plan
from seacode.commands.handlers.review import handle_review
from seacode.commands.handlers.sandbox import handle_sandbox
from seacode.commands.handlers.session import handle_session
from seacode.commands.handlers.status import handle_status
from seacode.commands.registry import CommandContext, CommandRegistry


# 实现 UIController 协议的假对象：记录各方法调用以便断言 handler 行为。
class _FakeUI:
    def __init__(self, token_count: tuple[int, int] = (0, 100000)) -> None:
        self.system_messages: list[str] = []
        self.user_messages: list[str] = []
        self.plan_mode_calls: list[bool] = []
        self.refresh_calls = 0
        self._token_count = token_count

    def add_system_message(self, text: str) -> None:
        self.system_messages.append(text)

    def send_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def set_plan_mode(self, enabled: bool) -> None:
        self.plan_mode_calls.append(enabled)

    def get_token_count(self) -> tuple[int, int]:
        return self._token_count

    def refresh_status(self) -> None:
        self.refresh_calls += 1


# 假 Agent：携带 handler 实际访问的属性与 manual_compact 行为记录。
class _FakeAgent:
    def __init__(self, **kwargs: Any) -> None:
        self.model = kwargs.get("model", "test-model")
        self.plan_mode = kwargs.get("plan_mode", False)
        self.tool_registry = kwargs.get("tool_registry")
        self.permission_checker = kwargs.get("permission_checker")
        self.mcp_manager = kwargs.get("mcp_manager")
        self.sandbox_cfg = kwargs.get("sandbox_cfg")
        self.history_cursor = kwargs.get("history_cursor", 0)
        self.manual_compact_calls = 0
        self.manual_compact_error = kwargs.get("manual_compact_error")

    async def manual_compact(self) -> None:
        self.manual_compact_calls += 1
        if self.manual_compact_error is not None:
            raise self.manual_compact_error


# 假工具注册中心：list_tools 返回预设列表，长度即工具数。
class _FakeToolRegistry:
    def __init__(self, tools: list[str] | None = None) -> None:
        self._tools = tools or []

    def list_tools(self) -> list[str]:
        return self._tools


# 假会话：携带 session_id / title / updated_at 供 /session 与 /status 读取。
class _FakeSession:
    def __init__(self, session_id: str, title: str = "", updated_at: str = "") -> None:
        self.session_id = session_id
        self.title = title
        self.updated_at = updated_at


# 假会话管理器：记录 list/create/resume/delete 调用，可预设恢复结果。
class _FakeSessionManager:
    def __init__(
        self,
        sessions: list[_FakeSession] | None = None,
        resume_result: _FakeResumeResult | None = None,
    ) -> None:
        self._sessions = sessions or []
        self.resume_result = resume_result
        self.resume_calls: list[str] = []
        self.create_calls = 0
        self.created: _FakeSession = _FakeSession("new-session", "新会话", "now")
        self.delete_calls: list[str] = []

    def list(self) -> list[_FakeSession]:
        return self._sessions

    def create(self) -> _FakeSession:
        self.create_calls += 1
        return self.created

    def resume(self, session_id: str) -> _FakeResumeResult:
        self.resume_calls.append(session_id)
        return self.resume_result  # type: ignore[return-value]

    def delete(self, session_id: str) -> None:
        self.delete_calls.append(session_id)


# 假恢复结果：携带 success/session/conversation/messages/error 字段。
class _FakeResumeResult:
    def __init__(
        self,
        success: bool = True,
        session: Any = None,
        conversation: Any = None,
        messages: Any = None,
        error: str = "",
    ) -> None:
        self.success = success
        self.session = session
        self.conversation = conversation
        self.messages = messages
        self.error = error


# 假记忆管理器：记录 clear 调用，可预设展示文本与文件路径。
class _FakeMemoryManager:
    def __init__(
        self,
        display_text: str = "记忆内容",
        memories: list[str] | None = None,
        file_paths: list[str] | None = None,
    ) -> None:
        self.memories = memories if memories is not None else ["m1"]
        self._display_text = display_text
        self._file_paths = file_paths or ["/path/MEMORY.md"]
        self.clear_calls = 0

    def get_display_text(self) -> str:
        return self._display_text

    def clear_memories(self) -> None:
        self.clear_calls += 1

    def get_memory_file_paths(self) -> list[str]:
        return self._file_paths


# 假权限检查器：记录 add/reset 调用，可预设 mode 与 rules。
class _FakePermissionChecker:
    def __init__(self, mode: str = "auto", rules: list[str] | None = None) -> None:
        self.mode = mode
        self.rules = rules or []
        self.added_rules: list[str] = []
        self.reset_calls = 0

    def add_rule(self, rule: str) -> None:
        self.added_rules.append(rule)

    def reset_rules(self) -> None:
        self.reset_calls += 1


# 假 MCP 服务器：携带 name/tool_count/status 供 /mcp 展示。
class _FakeMcpServer:
    def __init__(self, name: str, tool_count: int, status: str) -> None:
        self.name = name
        self.tool_count = tool_count
        self.status = status


# 假 MCP 管理器：list_servers 返回预设列表。
class _FakeMcpManager:
    def __init__(self, servers: list[_FakeMcpServer] | None = None) -> None:
        self._servers = servers or []

    def list_servers(self) -> list[_FakeMcpServer]:
        return self._servers


# 假沙箱配置：仅持有可读写的 mode 属性。
class _FakeSandboxCfg:
    def __init__(self, mode: str = "off") -> None:
        self.mode = mode


# 假 UI 状态回调：记录 clear_chat/set_session/set_conversation/render_restored 调用。
class _FakeCallbacks:
    def __init__(self) -> None:
        self.clear_chat_calls = 0
        self.set_session_calls: list[Any] = []
        self.set_conversation_calls: list[Any] = []
        self.render_restored_calls: list[Any] = []

    def clear_chat(self) -> None:
        self.clear_chat_calls += 1

    def set_session(self, session: Any) -> None:
        self.set_session_calls.append(session)

    def set_conversation(self, conversation: Any) -> None:
        self.set_conversation_calls.append(conversation)

    def render_restored(self, messages: Any) -> None:
        self.render_restored_calls.append(messages)


# 构造 CommandContext，注入各 handler 所需的依赖与默认空值。
def _make_ctx(
    args: str = "",
    agent: Any = None,
    session: Any = None,
    session_manager: Any = None,
    memory_manager: Any = None,
    ui: _FakeUI | None = None,
    config: dict[str, Any] | None = None,
) -> CommandContext:
    return CommandContext(
        args=args,
        agent=agent,
        conversation=None,
        session=session,
        session_manager=session_manager,
        memory_manager=memory_manager,
        ui=ui if ui is not None else _FakeUI(),
        config=config if config is not None else {},
    )


# 由回调对象构造 config 字典，可选附加 registry 供 /help 使用。
def _config(cb: _FakeCallbacks, registry: CommandRegistry | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "clear_chat": cb.clear_chat,
        "set_session": cb.set_session,
        "set_conversation": cb.set_conversation,
        "render_restored": cb.render_restored,
    }
    if registry is not None:
        cfg["registry"] = registry
    return cfg


# ---------- /help ----------


# 验证 /help 无参列出全部命令名。
# 注册 11 条命令后调用 handle_help，断言输出含 /help 与 /status。
async def test_help_lists_commands_when_no_args() -> None:
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = _FakeUI()
    ctx = _make_ctx(args="", ui=ui, config={"registry": registry})
    await handle_help(ctx)
    text = ui.system_messages[0]
    assert "/help" in text
    assert "/status" in text


# 验证 /help 带参显示单命令详情。
# 传 args="status" 调 handle_help，断言输出含命令名与描述。
async def test_help_shows_single_command_detail() -> None:
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = _FakeUI()
    ctx = _make_ctx(args="status", ui=ui, config={"registry": registry})
    await handle_help(ctx)
    text = ui.system_messages[0]
    assert "命令：/status" in text
    assert "显示当前状态" in text


# 验证 /help 查未知命令给出未知提示。
# 传 args="unknown" 调 handle_help，断言输出含"未知命令：unknown"。
async def test_help_unknown_command() -> None:
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = _FakeUI()
    ctx = _make_ctx(args="unknown", ui=ui, config={"registry": registry})
    await handle_help(ctx)
    assert "未知命令：unknown" in ui.system_messages[0]


# ---------- /status ----------


# 验证 /status 聚合显示模式、会话、Token、工具、记忆、工作目录与版本。
# 构造带工具与记忆的 mock，调 handle_status，断言输出含各关键字段。
async def test_status_displays_aggregated_info() -> None:
    tool_registry = _FakeToolRegistry(["tool1", "tool2"])
    agent = _FakeAgent(model="claude-test", plan_mode=True, tool_registry=tool_registry)
    session = _FakeSession("sess-abc12345")
    memory_manager = _FakeMemoryManager(memories=["m1", "m2", "m3"])
    ui = _FakeUI(token_count=(5000, 100000))
    ctx = _make_ctx(
        args="",
        agent=agent,
        session=session,
        memory_manager=memory_manager,
        ui=ui,
    )
    await handle_status(ctx)
    text = ui.system_messages[0]
    assert "claude-test" in text
    assert "Plan 模式：是" in text
    assert "会话 ID：sess-abc12345" in text
    assert "Token：5000 / 100000" in text
    assert "工具数：2" in text
    assert "记忆数：3" in text
    assert "工作目录：" in text
    assert "版本：0.1.0" in text


# ---------- /clear ----------


# 验证 /clear 清屏、创新会话并重置 Agent 历史游标。
# 构造回调与 session_manager，调 handle_clear，断言各回调被调用且游标归零。
async def test_clear_resets_chat_and_creates_session() -> None:
    cb = _FakeCallbacks()
    new_session = _FakeSession("new-1")
    sm = _FakeSessionManager()
    sm.created = new_session
    agent = _FakeAgent(history_cursor=5)
    ui = _FakeUI()
    ctx = _make_ctx(
        args="",
        agent=agent,
        session_manager=sm,
        ui=ui,
        config=_config(cb),
    )
    await handle_clear(ctx)
    assert cb.clear_chat_calls == 1
    assert cb.set_session_calls == [new_session]
    assert ui.system_messages[0] == "已清空"
    assert agent.history_cursor == 0


# ---------- /compact ----------


# 验证 /compact 在 token 低于 5000 时跳过压缩。
# mock get_token_count 返回 3000，调 handle_compact，断言跳过且未调 manual_compact。
async def test_compact_skips_when_tokens_below_threshold() -> None:
    agent = _FakeAgent()
    ui = _FakeUI(token_count=(3000, 100000))
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_compact(ctx)
    text = ui.system_messages[0]
    assert "低于 5000" in text
    assert "3000" in text
    assert agent.manual_compact_calls == 0


# 验证 /compact 在 token 达到阈值时调用 manual_compact。
# mock get_token_count 返回 8000，调 handle_compact，断言 manual_compact 被调用。
async def test_compact_invokes_manual_compact_above_threshold() -> None:
    agent = _FakeAgent()
    ui = _FakeUI(token_count=(8000, 100000))
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_compact(ctx)
    assert agent.manual_compact_calls == 1


# 验证 /compact 在 manual_compact 抛异常时显示失败提示且不崩溃。
# mock manual_compact 抛 RuntimeError，调 handle_compact，断言输出含"压缩失败"。
async def test_compact_reports_failure_on_exception() -> None:
    agent = _FakeAgent(manual_compact_error=RuntimeError("boom"))
    ui = _FakeUI(token_count=(8000, 100000))
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_compact(ctx)
    text = ui.system_messages[0]
    assert "压缩失败" in text
    assert "boom" in text


# ---------- /plan ----------


# 验证 /plan 在非 Plan 模式下切换到 Plan 模式。
# mock agent plan_mode=False，调 handle_plan，断言 set_plan_mode(True) 被调用且不发消息。
async def test_plan_enters_plan_mode_when_idle() -> None:
    agent = _FakeAgent(plan_mode=False)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_plan(ctx)
    assert ui.plan_mode_calls == [True]
    assert ui.user_messages == []


# 验证 /plan 带参数时切换模式并发送参数作为用户消息。
# 传 args="分析目录"，断言 set_plan_mode(True) 与 send_user_message 被调用。
async def test_plan_with_args_sends_user_message() -> None:
    agent = _FakeAgent(plan_mode=False)
    ui = _FakeUI()
    ctx = _make_ctx(args="分析目录", agent=agent, ui=ui)
    await handle_plan(ctx)
    assert ui.plan_mode_calls == [True]
    assert ui.user_messages == ["分析目录"]


# 验证 /plan 在已处于 Plan 模式时提示且不重复切换。
# mock agent plan_mode=True，断言输出"已在 Plan 模式"且未调 set_plan_mode。
async def test_plan_warns_when_already_in_plan_mode() -> None:
    agent = _FakeAgent(plan_mode=True)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_plan(ctx)
    assert "已在 Plan 模式" in ui.system_messages[0]
    assert ui.plan_mode_calls == []


# ---------- /session ----------


# 验证 /session list 列出历史会话。
# mock 2 个会话，调 handle_session(args="list")，断言输出含会话标题与 ID 前缀。
async def test_session_list_shows_sessions() -> None:
    sessions = [
        _FakeSession("sess-aaa11111", "项目A", "2026-01-01"),
        _FakeSession("sess-bbb22222", "项目B", "2026-01-02"),
    ]
    sm = _FakeSessionManager(sessions=sessions)
    ui = _FakeUI()
    ctx = _make_ctx(args="list", session_manager=sm, ui=ui)
    await handle_session(ctx)
    text = ui.system_messages[0]
    assert "历史会话" in text
    assert "项目A" in text
    assert "sess-aaa" in text


# 验证 /session resume 按序号选择会话并恢复。
# mock 3 个会话，传 args="resume 2"，断言 resume 收到第 2 个会话 ID 且恢复回调被调用。
async def test_session_resume_by_index() -> None:
    sessions = [
        _FakeSession("sess-aaa11111", "A", "t1"),
        _FakeSession("sess-bbb22222", "B", "t2"),
        _FakeSession("sess-ccc33333", "C", "t3"),
    ]
    restored_session = _FakeSession("sess-bbb22222")
    restored_conv = object()
    restored_msgs = ["msg1"]
    sm = _FakeSessionManager(
        sessions=sessions,
        resume_result=_FakeResumeResult(
            success=True,
            session=restored_session,
            conversation=restored_conv,
            messages=restored_msgs,
        ),
    )
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(args="resume 2", session_manager=sm, ui=ui, config=_config(cb))
    await handle_session(ctx)
    assert sm.resume_calls == ["sess-bbb22222"]
    assert cb.set_session_calls == [restored_session]
    assert cb.set_conversation_calls == [restored_conv]
    assert cb.render_restored_calls == [restored_msgs]


# 验证 /session resume 直接按 ID 恢复。
# 传 args="resume session_xxx"，断言 resume 收到该 ID 且 set_session 被调用。
async def test_session_resume_by_id() -> None:
    restored_session = _FakeSession("session_xxx")
    sm = _FakeSessionManager(
        sessions=[],
        resume_result=_FakeResumeResult(
            success=True,
            session=restored_session,
            conversation=object(),
            messages=[],
        ),
    )
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(args="resume session_xxx", session_manager=sm, ui=ui, config=_config(cb))
    await handle_session(ctx)
    assert sm.resume_calls == ["session_xxx"]
    assert cb.set_session_calls == [restored_session]


# 验证 /session resume 失败时显示错误且不调用恢复回调。
# mock resume 返回 success=False，断言输出含错误且 set_session 未被调用。
async def test_session_resume_failure() -> None:
    sm = _FakeSessionManager(
        sessions=[],
        resume_result=_FakeResumeResult(success=False, error="会话不存在"),
    )
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(args="resume nope", session_manager=sm, ui=ui, config=_config(cb))
    await handle_session(ctx)
    text = ui.system_messages[0]
    assert "恢复会话失败" in text
    assert "会话不存在" in text
    assert cb.set_session_calls == []


# 验证 /session new 清屏并创建新会话。
# 传 args="new"，断言 clear_chat 与 set_session 被调用且提示"已创建新会话"。
async def test_session_new_clears_and_creates() -> None:
    new_session = _FakeSession("new-1")
    sm = _FakeSessionManager()
    sm.created = new_session
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(args="new", session_manager=sm, ui=ui, config=_config(cb))
    await handle_session(ctx)
    assert cb.clear_chat_calls == 1
    assert cb.set_session_calls == [new_session]
    assert "已创建新会话" in ui.system_messages[0]


# 验证 /session delete 按序号删除会话。
# mock 2 个会话，传 args="delete 1"，断言 delete 收到第 1 个会话 ID 且提示"已删除"。
async def test_session_delete_by_index() -> None:
    sessions = [
        _FakeSession("sess-aaa11111", "A", "t1"),
        _FakeSession("sess-bbb22222", "B", "t2"),
    ]
    sm = _FakeSessionManager(sessions=sessions)
    ui = _FakeUI()
    ctx = _make_ctx(args="delete 1", session_manager=sm, ui=ui)
    await handle_session(ctx)
    assert sm.delete_calls == ["sess-aaa11111"]
    assert "已删除" in ui.system_messages[0]


# ---------- /memory ----------


# 验证 /memory list 显示记忆内容。
# mock get_display_text 返回文本，调 handle_memory(args="list")，断言输出为该文本。
async def test_memory_list_displays_text() -> None:
    mm = _FakeMemoryManager(display_text="记忆索引内容")
    ui = _FakeUI()
    ctx = _make_ctx(args="list", memory_manager=mm, ui=ui)
    await handle_memory(ctx)
    assert ui.system_messages[0] == "记忆索引内容"


# 验证 /memory clear 清空记忆。
# 调 handle_memory(args="clear")，断言 clear_memories 被调用且提示"已清空"。
async def test_memory_clear_invokes_clear() -> None:
    mm = _FakeMemoryManager()
    ui = _FakeUI()
    ctx = _make_ctx(args="clear", memory_manager=mm, ui=ui)
    await handle_memory(ctx)
    assert mm.clear_calls == 1
    assert ui.system_messages[0] == "已清空"


# 验证 /memory edit 显示记忆文件路径。
# mock get_memory_file_paths 返回路径列表，断言输出含各路径。
async def test_memory_edit_shows_paths() -> None:
    mm = _FakeMemoryManager(file_paths=["/proj/.seacode/MEMORY.md", "/home/.seacode/MEMORY.md"])
    ui = _FakeUI()
    ctx = _make_ctx(args="edit", memory_manager=mm, ui=ui)
    await handle_memory(ctx)
    text = ui.system_messages[0]
    assert "/proj/.seacode/MEMORY.md" in text
    assert "/home/.seacode/MEMORY.md" in text


# 验证 /memory 在记忆系统未初始化时给出提示。
# mock memory_manager=None，断言输出含"记忆系统未初始化"。
async def test_memory_not_initialized() -> None:
    ui = _FakeUI()
    ctx = _make_ctx(args="list", memory_manager=None, ui=ui)
    await handle_memory(ctx)
    assert "记忆系统未初始化" in ui.system_messages[0]


# ---------- /permission ----------


# 验证 /permission mode 显示当前权限模式。
# mock permission_checker.mode，断言输出含当前模式值。
async def test_permission_mode_shows_current() -> None:
    checker = _FakePermissionChecker(mode="auto")
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="mode", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert "当前权限模式：auto" in ui.system_messages[0]


# 验证 /permission rules 显示规则列表。
# mock rules 返回列表，断言输出含各规则文本。
async def test_permission_rules_lists_rules() -> None:
    checker = _FakePermissionChecker(rules=["deny_write /etc", "allow_read /var"])
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="rules", agent=agent, ui=ui)
    await handle_permission(ctx)
    text = ui.system_messages[0]
    assert "deny_write /etc" in text
    assert "allow_read /var" in text


# 验证 /permission add 添加规则。
# 传 args="add deny_write /etc"，断言 add_rule 收到该规则且提示"已添加规则"。
async def test_permission_add_invokes_add_rule() -> None:
    checker = _FakePermissionChecker()
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="add deny_write /etc", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert checker.added_rules == ["deny_write /etc"]
    assert "已添加规则" in ui.system_messages[0]


# 验证 /permission reset 重置规则。
# 传 args="reset"，断言 reset_rules 被调用且提示"已重置权限规则"。
async def test_permission_reset_invokes_reset() -> None:
    checker = _FakePermissionChecker()
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="reset", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert checker.reset_calls == 1
    assert "已重置权限规则" in ui.system_messages[0]


# 验证 /permission 在权限系统未初始化时给出提示。
# mock agent 无 permission_checker，断言输出含"权限系统未初始化"。
async def test_permission_not_initialized() -> None:
    agent = _FakeAgent(permission_checker=None)
    ui = _FakeUI()
    ctx = _make_ctx(args="mode", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert "权限系统未初始化" in ui.system_messages[0]


# ---------- /review ----------


# 验证 /review 无参构造审查提示词并发送。
# 调 handle_review，断言 send_user_message 被调用且文本含审查与工作目录。
async def test_review_constructs_prompt() -> None:
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=None, ui=ui)
    await handle_review(ctx)
    assert len(ui.user_messages) == 1
    text = ui.user_messages[0]
    assert "审查" in text
    assert "工作目录" in text


# 验证 /review 带参追加额外关注点。
# 传 args="关注并发安全"，断言发送文本含"额外关注点：关注并发安全"。
async def test_review_appends_extra_focus() -> None:
    ui = _FakeUI()
    ctx = _make_ctx(args="关注并发安全", agent=None, ui=ui)
    await handle_review(ctx)
    text = ui.user_messages[0]
    assert "额外关注点：关注并发安全" in text


# ---------- /mcp ----------


# 验证 /mcp 显示服务器状态与工具数。
# mock list_servers 返回服务器列表，断言输出含服务器名与工具数。
async def test_mcp_shows_servers() -> None:
    servers = [
        _FakeMcpServer("fs", 3, "connected"),
        _FakeMcpServer("git", 2, "connected"),
    ]
    manager = _FakeMcpManager(servers=servers)
    agent = _FakeAgent(mcp_manager=manager)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_mcp(ctx)
    text = ui.system_messages[0]
    assert "MCP 服务器状态" in text
    assert "fs" in text
    assert "git" in text
    assert "3 个工具" in text
    assert "2 个工具" in text


# 验证 /mcp 在未配置时给出提示。
# mock agent 无 mcp_manager，断言输出含"未配置 MCP 服务器"。
async def test_mcp_not_configured() -> None:
    agent = _FakeAgent(mcp_manager=None)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_mcp(ctx)
    assert "未配置 MCP 服务器" in ui.system_messages[0]


# 验证 /mcp 在服务器列表为空时给出提示。
# mock list_servers 返回空列表，断言输出含"无连接的 MCP 服务器"。
async def test_mcp_empty_servers() -> None:
    manager = _FakeMcpManager(servers=[])
    agent = _FakeAgent(mcp_manager=manager)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_mcp(ctx)
    assert "无连接的 MCP 服务器" in ui.system_messages[0]


# ---------- /sandbox ----------


# 验证 /sandbox 无参显示当前模式。
# mock sandbox_cfg.mode，断言输出含当前模式值。
async def test_sandbox_shows_current_mode() -> None:
    cfg = _FakeSandboxCfg(mode="off")
    agent = _FakeAgent(sandbox_cfg=cfg)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert "当前沙箱模式：off" in ui.system_messages[0]


# 验证 /sandbox off 切换模式。
# 传 args="off"，断言 sandbox_cfg.mode 被设置为 "off" 且提示切换成功。
async def test_sandbox_switch_to_off() -> None:
    cfg = _FakeSandboxCfg(mode="on-auto")
    agent = _FakeAgent(sandbox_cfg=cfg)
    ui = _FakeUI()
    ctx = _make_ctx(args="off", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert cfg.mode == "off"
    assert "沙箱模式已切换为：off" in ui.system_messages[0]


# 验证 /sandbox 非法参数显示用法且不改模式。
# 传 args="invalid"，断言输出含"用法"且 mode 不变。
async def test_sandbox_invalid_arg_shows_usage() -> None:
    cfg = _FakeSandboxCfg(mode="off")
    agent = _FakeAgent(sandbox_cfg=cfg)
    ui = _FakeUI()
    ctx = _make_ctx(args="invalid", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert "用法" in ui.system_messages[0]
    assert cfg.mode == "off"


# 验证 /sandbox 在 Windows 上 on 切换不支持。
# monkeypatch sys.platform 为 win32，传 args="on"，断言输出含"当前系统不支持沙箱"。
async def test_sandbox_windows_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    cfg = _FakeSandboxCfg(mode="off")
    agent = _FakeAgent(sandbox_cfg=cfg)
    ui = _FakeUI()
    ctx = _make_ctx(args="on", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert "当前系统不支持沙箱" in ui.system_messages[0]
    assert cfg.mode == "off"


# 验证 /sandbox 在沙箱未初始化时给出提示。
# mock agent 无 sandbox_cfg，断言输出含"沙箱未初始化"。
async def test_sandbox_not_initialized() -> None:
    agent = _FakeAgent(sandbox_cfg=None)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert "沙箱未初始化" in ui.system_messages[0]


# ---------- register_all_commands ----------


# 验证 register_all_commands 批量注册 11 条内置命令。
# 构造空注册中心，调 register_all_commands，断言 list_commands 返回 11 条。
def test_register_all_commands_registers_eleven() -> None:
    registry = CommandRegistry()
    register_all_commands(registry)
    assert len(registry.list_commands()) == 11
