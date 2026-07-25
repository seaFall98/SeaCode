"""命令处理 handler 的单元测试：覆盖 11 条内置命令与批量注册。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
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
from seacode.permissions import PermissionMode


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


# 假规则引擎：模拟 RuleEngine 三层规则加载、本地规则追加与本地文件清空。
class _FakeRuleEngine:
    def __init__(
        self,
        tiers: list[list[Any]] | None = None,
        local_path: Path | None = None,
    ) -> None:
        # tiers 形如 [[user_rules], [project_rules], [local_rules]]。
        self._tiers = tiers or [[], [], []]
        self._local_path = local_path
        self.appended_rules: list[Any] = []
        self.reset_calls = 0

    def _load_tiers(self) -> list[list[Any]]:
        return self._tiers

    def append_local_rule(self, rule: Any) -> None:
        self.appended_rules.append(rule)
        # 同步追加到内存中的本地层，便于 _load_tiers 立即看到。
        if self._tiers and len(self._tiers) >= 3:
            self._tiers[2].append(rule)

    # 测试辅助：清空本地层规则并模拟文件清空。
    def reset_local(self) -> None:
        self.reset_calls += 1
        if self._tiers and len(self._tiers) >= 3:
            self._tiers[2] = []
        if self._local_path is not None and self._local_path.exists():
            self._local_path.write_text("", encoding="utf-8")


# 假规则：携带 tool_name/pattern/effect 字段，模拟 permissions.rules.Rule。
class _FakeRule:
    def __init__(self, tool_name: str, pattern: str, effect: str) -> None:
        self.tool_name = tool_name
        self.pattern = pattern
        self.effect = effect


# 假权限检查器：携带 mode/rule_engine/sandbox_enabled，模拟 PermissionChecker。
class _FakePermissionChecker:
    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        rule_engine: _FakeRuleEngine | None = None,
        sandbox_enabled: bool = False,
    ) -> None:
        self.mode = mode
        self.rule_engine = rule_engine
        self.sandbox_enabled = sandbox_enabled


# 假 Bash 工具：携带 sandbox/sandbox_config 属性，模拟 tools.bash.Bash。
class _FakeBashTool:
    def __init__(self, sandbox: Any = None, sandbox_config: Any = None) -> None:
        self.sandbox = sandbox
        self.sandbox_config = sandbox_config


# 假工具注册中心：get 返回指定工具，list_tools 返回预设列表。
class _FakeToolRegistry:
    def __init__(
        self, tools: list[str] | None = None, bash_tool: _FakeBashTool | None = None
    ) -> None:
        self._tools = tools or []
        self._bash_tool = bash_tool

    def list_tools(self) -> list[str]:
        return self._tools

    def get(self, name: str) -> _FakeBashTool | None:
        if name == "Bash":
            return self._bash_tool
        return None


# 假沙箱实例：模拟 seacode.sandbox.Sandbox，available() 由构造参数控制。
class _FakeSandbox:
    def __init__(self, available: bool = True) -> None:
        self._available = available

    def available(self) -> bool:
        return self._available


# 假 Agent：携带 handler 实际访问的属性与 manual_compact 行为记录。
class _FakeAgent:
    def __init__(self, **kwargs: Any) -> None:
        self.model = kwargs.get("model", "test-model")
        self.plan_mode = kwargs.get("plan_mode", False)
        self.tool_registry = kwargs.get("tool_registry")
        self.permission_checker = kwargs.get("permission_checker")
        # 权限模式与 checker.mode 保持同步；handler 通过 set_permission_mode 切换。
        self.permission_mode: PermissionMode = (
            self.permission_checker.mode
            if self.permission_checker is not None
            else kwargs.get("permission_mode", PermissionMode.DEFAULT)
        )
        self.mcp_manager = kwargs.get("mcp_manager")
        # sandbox 命令通过 registry.get("Bash") 与 work_dir 操作沙箱。
        self.registry = kwargs.get("registry")
        self.work_dir = kwargs.get("work_dir", "/tmp/fake-work")
        self.history_cursor = kwargs.get("history_cursor", 0)
        # manual_compact 行为记录；可注入返回值或抛出异常。
        self.manual_compact_calls = 0
        self.manual_compact_result = kwargs.get("manual_compact_result")
        self.manual_compact_error = kwargs.get("manual_compact_error")

    # 切换权限模式；同步更新 checker.mode 保持与真实 Agent 一致。
    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker is not None:
            self.permission_checker.mode = mode

    async def manual_compact(self, conversation: Any) -> Any:
        self.manual_compact_calls += 1
        if self.manual_compact_error is not None:
            raise self.manual_compact_error
        return self.manual_compact_result


# 假会话元数据：模拟 memory.session.SessionMeta。
class _FakeMeta:
    def __init__(
        self,
        id: str = "sess-test",
        title: str = "",
        message_count: int = 0,
        total_tokens: int = 0,
        last_active: datetime | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.message_count = message_count
        self.total_tokens = total_tokens
        self.last_active = last_active or datetime.now(UTC)


# 假会话：携带 session_id 与 meta，模拟 memory.session.Session。
class _FakeSession:
    def __init__(
        self,
        session_id: str,
        title: str = "",
        message_count: int = 0,
        total_tokens: int = 0,
        last_active: datetime | None = None,
    ) -> None:
        self.session_id = session_id
        self.meta = _FakeMeta(
            id=session_id,
            title=title,
            message_count=message_count,
            total_tokens=total_tokens,
            last_active=last_active,
        )
        self.closed = False

    def close(self) -> None:
        self.closed = True


# 假会话管理器：记录 list/create/resume/delete 调用，可预设恢复结果。
class _FakeSessionManager:
    def __init__(
        self,
        metas: list[_FakeMeta] | None = None,
        resume_result: _FakeResumeResult | None = None,
    ) -> None:
        # list() 返回 SessionMeta 列表；resume_result 为 None 表示会话不存在。
        self._metas = metas or []
        self.resume_result = resume_result
        self.resume_calls: list[str] = []
        self.create_calls = 0
        # create() 返回的新会话；测试可通过赋值替换。
        self.created: _FakeSession = _FakeSession("new-session", "新会话")
        self.delete_calls: list[str] = []
        self._deleted: set[str] = set()

    def list(self) -> list[_FakeMeta]:
        return self._metas

    def create(self) -> _FakeSession:
        self.create_calls += 1
        return self.created

    def resume(self, session_id: str) -> _FakeResumeResult | None:
        self.resume_calls.append(session_id)
        return self.resume_result

    def delete(self, session_id: str) -> bool:
        self.delete_calls.append(session_id)
        if session_id in self._deleted:
            return False
        self._deleted.add(session_id)
        return True


# 假恢复结果：携带 session/messages，模拟 memory.session.ResumeResult。
class _FakeResumeResult:
    def __init__(
        self,
        session: Any = None,
        messages: Any = None,
    ) -> None:
        self.session = session
        self.messages = messages


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

    async def render_restored(self, messages: Any) -> None:
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
    assert "无需压缩" in text
    assert "3,000" in text  # 用 :, 格式化，3 位数后带逗号
    assert agent.manual_compact_calls == 0


# 验证 /compact 在 token 达到阈值时调用 manual_compact。
# mock get_token_count 返回 8000，manual_compact 返回 CompactNotification，断言被调用且展示消息。
async def test_compact_invokes_manual_compact_above_threshold() -> None:
    from seacode.agent import CompactNotification

    notification = CompactNotification(
        before_tokens=8000, message="上下文已压缩（压缩前 8,000 tokens）"
    )
    agent = _FakeAgent(manual_compact_result=notification)
    ui = _FakeUI(token_count=(8000, 100000))
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_compact(ctx)
    assert agent.manual_compact_calls == 1
    assert "上下文已压缩" in ui.system_messages[0]


# 验证 /compact 在 manual_compact 返回 ErrorEvent 时显示失败提示。
# mock manual_compact 返回 ErrorEvent，断言输出含"压缩失败"与错误消息。
async def test_compact_reports_failure_on_error_event() -> None:
    from seacode.agent import ErrorEvent

    agent = _FakeAgent(manual_compact_result=ErrorEvent(message="boom"))
    ui = _FakeUI(token_count=(8000, 100000))
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_compact(ctx)
    text = ui.system_messages[0]
    assert "压缩失败" in text
    assert "boom" in text


# 验证 /compact 在 manual_compact 抛异常时显示失败提示且不崩溃。
# mock manual_compact 抛 RuntimeError，断言输出含"压缩失败"。
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


# 验证 /session 无参显示当前会话详情。
# 构造带 meta 的 session，调 handle_session(args="")，断言输出含会话 ID 与标题。
async def test_session_shows_current_details() -> None:
    session = _FakeSession("sess-abc12345", "项目A", message_count=5, total_tokens=1000)
    sm = _FakeSessionManager()
    ui = _FakeUI()
    ctx = _make_ctx(args="", session=session, session_manager=sm, ui=ui)
    await handle_session(ctx)
    text = ui.system_messages[0]
    assert "当前会话：sess-abc12345" in text
    assert "项目A" in text
    assert "5 条" in text
    assert "1,000" in text


# 验证 /session list 列出历史会话。
# mock 2 个 SessionMeta，调 handle_session(args="list")，断言输出含会话标题与 ID。
async def test_session_list_shows_sessions() -> None:
    metas = [
        _FakeMeta("sess-aaa11111", "项目A", 3, 500, datetime(2026, 1, 1, tzinfo=UTC)),
        _FakeMeta("sess-bbb22222", "项目B", 7, 800, datetime(2026, 1, 2, tzinfo=UTC)),
    ]
    sm = _FakeSessionManager(metas=metas)
    ui = _FakeUI()
    ctx = _make_ctx(args="list", session_manager=sm, ui=ui)
    await handle_session(ctx)
    text = ui.system_messages[0]
    assert "历史会话" in text
    assert "项目A" in text
    assert "sess-aaa11111" in text


# 验证 /session resume 按序号选择会话并恢复。
# 预填充 _resume_candidates 缓存，传 args="resume 2"，断言 resume 收到第 2 个会话 ID。
async def test_session_resume_by_index() -> None:
    candidates = ["sess-aaa11111", "sess-bbb22222", "sess-ccc33333"]
    restored_session = _FakeSession("sess-bbb22222", "B", 4)
    restored_msgs = ["msg1"]
    sm = _FakeSessionManager(
        resume_result=_FakeResumeResult(
            session=restored_session,
            messages=restored_msgs,
        ),
    )
    cb = _FakeCallbacks()
    ui = _FakeUI()
    cfg = _config(cb)
    cfg["_resume_candidates"] = candidates
    ctx = _make_ctx(args="resume 2", session_manager=sm, ui=ui, config=cfg)
    await handle_session(ctx)
    assert sm.resume_calls == ["sess-bbb22222"]
    assert cb.set_session_calls == [restored_session]
    assert cb.set_conversation_calls != []  # 已注入新 ConversationManager
    assert cb.render_restored_calls == [restored_msgs]


# 验证 /session resume 直接按 ID 恢复。
# 传 args="resume session_xxx"，断言 resume 收到该 ID 且 set_session 被调用。
async def test_session_resume_by_id() -> None:
    restored_session = _FakeSession("session_xxx")
    sm = _FakeSessionManager(
        resume_result=_FakeResumeResult(
            session=restored_session,
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
# mock resume 返回 None，断言输出含"会话未找到"且 set_session 未被调用。
async def test_session_resume_failure() -> None:
    sm = _FakeSessionManager(resume_result=None)
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(args="resume nope", session_manager=sm, ui=ui, config=_config(cb))
    await handle_session(ctx)
    text = ui.system_messages[0]
    assert "会话未找到" in text
    assert "nope" in text
    assert cb.set_session_calls == []


# 验证 /session new 关闭旧 session 并创建新会话。
# 预设旧 session，传 args="new"，断言 close/set_session/clear_chat 被调用。
async def test_session_new_clears_and_creates() -> None:
    old_session = _FakeSession("old-1")
    new_session = _FakeSession("new-1")
    sm = _FakeSessionManager()
    sm.created = new_session
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(
        args="new", session=old_session, session_manager=sm, ui=ui, config=_config(cb)
    )
    await handle_session(ctx)
    assert old_session.closed is True
    assert cb.clear_chat_calls == 1
    assert cb.set_session_calls == [new_session]
    assert "新会话已创建" in ui.system_messages[0]


# 验证 /session delete 按 ID 删除会话。
# 传 args="delete sess-aaa11111"，断言 delete 收到该 ID 且提示"已删除"。
async def test_session_delete_by_id() -> None:
    sm = _FakeSessionManager()
    ui = _FakeUI()
    ctx = _make_ctx(args="delete sess-aaa11111", session_manager=sm, ui=ui)
    await handle_session(ctx)
    assert sm.delete_calls == ["sess-aaa11111"]
    assert "已删除" in ui.system_messages[0]


# 验证 /session delete 不存在的会话给出未找到提示。
# 传 args="delete missing"，重复 delete 后第二次返回 False，断言输出含"未找到"。
async def test_session_delete_missing() -> None:
    sm = _FakeSessionManager()
    # 预先标记 sess-gone 已删除，使下一次 delete 返回 False。
    sm._deleted.add("sess-gone")
    ui = _FakeUI()
    ctx = _make_ctx(args="delete sess-gone", session_manager=sm, ui=ui)
    await handle_session(ctx)
    assert sm.delete_calls == ["sess-gone"]
    assert "未找到" in ui.system_messages[0]


# 验证 /session delete 拒绝删除当前活跃会话。
# mock 当前 session 与 delete 目标相同，断言输出含"不能删除当前活跃"且 delete 未被调用。
async def test_session_delete_rejects_active_session() -> None:
    current = _FakeSession("sess-active")
    sm = _FakeSessionManager()
    ui = _FakeUI()
    ctx = _make_ctx(
        args="delete sess-active", session=current, session_manager=sm, ui=ui
    )
    await handle_session(ctx)
    assert sm.delete_calls == []
    assert "不能删除当前活跃" in ui.system_messages[0]


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


# 验证 /permission 无参显示当前模式与规则数量。
# mock permission_checker 与 rule_engine，断言输出含模式值与规则数。
async def test_permission_status_shows_mode_and_count() -> None:
    rule = _FakeRule("Bash", "git*", "allow")
    engine = _FakeRuleEngine(tiers=[[], [], [rule]])
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_permission(ctx)
    text = ui.system_messages[0]
    assert "当前模式: default" in text
    assert "规则数量: 1" in text


# 验证 /permission mode 切换权限模式。
# 传 args="mode acceptEdits"，断言 set_permission_mode 被调用且提示切换成功。
async def test_permission_mode_switches() -> None:
    engine = _FakeRuleEngine()
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="mode acceptEdits", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert agent.permission_mode == PermissionMode.ACCEPT_EDITS
    assert checker.mode == PermissionMode.ACCEPT_EDITS
    assert "权限模式已切换为：acceptEdits" in ui.system_messages[0]
    assert ui.refresh_calls == 1


# 验证 /permission mode 未知模式给出提示。
# 传 args="mode unknown"，断言输出含"未知模式"且未切换。
async def test_permission_mode_unknown() -> None:
    engine = _FakeRuleEngine()
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="mode unknown", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert "未知模式" in ui.system_messages[0]
    assert agent.permission_mode == PermissionMode.DEFAULT


# 验证 /permission rules 按三层展示规则。
# mock 三层规则，断言输出含用户级/项目级/本地级标签与规则文本。
async def test_permission_rules_lists_rules() -> None:
    user_rule = _FakeRule("Bash", "ls*", "allow")
    local_rule = _FakeRule("Bash", "rm*", "deny")
    engine = _FakeRuleEngine(tiers=[[user_rule], [], [local_rule]])
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="rules", agent=agent, ui=ui)
    await handle_permission(ctx)
    text = ui.system_messages[0]
    assert "用户级" in text
    assert "项目级" in text
    assert "本地级" in text
    assert "Bash(ls*)" in text
    assert "Bash(rm*)" in text


# 验证 /permission add 解析规则并追加到本地层。
# 传 args="add Bash(git*) allow"，断言 append_local_rule 收到规则。
async def test_permission_add_invokes_append_local_rule() -> None:
    engine = _FakeRuleEngine()
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="add Bash(git*) allow", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert len(engine.appended_rules) == 1
    rule = engine.appended_rules[0]
    assert rule.tool_name == "Bash"
    assert rule.pattern == "git*"
    assert rule.effect == "allow"
    assert "规则已添加" in ui.system_messages[0]


# 验证 /permission add 缺少效果给出用法提示。
# 传 args="add Bash(git*)"，断言输出含"用法"且未追加规则。
async def test_permission_add_missing_effect() -> None:
    engine = _FakeRuleEngine()
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="add Bash(git*)", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert "用法" in ui.system_messages[0]
    assert engine.appended_rules == []


# 验证 /permission reset 清空本地规则文件。
# mock _local_path 指向临时文件，传 args="reset"，断言文件被清空且提示"已清空"。
async def test_permission_reset_clears_local_file(tmp_path: Path) -> None:
    local_file = tmp_path / "permissions.local.yaml"
    local_file.write_text("- rule: Bash(ls*)\n  effect: allow\n", encoding="utf-8")
    engine = _FakeRuleEngine(local_path=local_file)
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine
    )
    agent = _FakeAgent(permission_checker=checker)
    ui = _FakeUI()
    ctx = _make_ctx(args="reset", agent=agent, ui=ui)
    await handle_permission(ctx)
    assert local_file.read_text(encoding="utf-8") == ""
    assert "本地规则已清空" in ui.system_messages[0]


# 验证 /permission 在权限系统未初始化时给出提示。
# mock agent 无 permission_checker，断言输出含"Agent 未初始化"或权限相关提示。
async def test_permission_not_initialized() -> None:
    agent = _FakeAgent(permission_checker=None)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_permission(ctx)
    # permission_checker 为 None 时仍可显示模式（无规则引擎）。
    text = ui.system_messages[0]
    assert "权限状态" in text


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


# 验证 /sandbox 无参显示当前沙箱状态。
# mock checker.sandbox_enabled=False 与无 Bash 工具，断言输出含"沙箱状态"。
async def test_sandbox_shows_status_when_disabled() -> None:
    engine = _FakeRuleEngine()
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine, sandbox_enabled=False
    )
    registry = _FakeToolRegistry(bash_tool=None)
    agent = _FakeAgent(permission_checker=checker, registry=registry)
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    text = ui.system_messages[0]
    assert "沙箱状态" in text
    assert "OS 沙箱：未启用" in text


# 验证 /sandbox off 卸载 Bash 上的沙箱并清空 checker 标志。
# 预挂载沙箱，传 args="off"，断言 bash_tool.sandbox 为 None 且 checker.sandbox_enabled=False。
async def test_sandbox_off_disables_sandbox() -> None:
    sandbox = _FakeSandbox(available=True)
    bash_tool = _FakeBashTool(sandbox=sandbox, sandbox_config=object())
    registry = _FakeToolRegistry(bash_tool=bash_tool)
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=_FakeRuleEngine(), sandbox_enabled=True
    )
    agent = _FakeAgent(permission_checker=checker, registry=registry)
    ui = _FakeUI()
    ctx = _make_ctx(args="off", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert bash_tool.sandbox is None
    assert bash_tool.sandbox_config is None
    assert checker.sandbox_enabled is False
    assert "沙箱已关闭" in ui.system_messages[0]
    assert ui.refresh_calls == 1


# 验证 /sandbox on-auto 创建并挂载沙箱且开启自动放行。
# mock 无已挂载沙箱，传 args="on-auto"，monkeypatch create_sandbox 返回 _FakeSandbox。
async def test_sandbox_on_auto_enables_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bash_tool = _FakeBashTool(sandbox=None, sandbox_config=None)
    registry = _FakeToolRegistry(bash_tool=bash_tool)
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=_FakeRuleEngine(), sandbox_enabled=False
    )
    agent = _FakeAgent(
        permission_checker=checker, registry=registry, work_dir="/tmp/proj"
    )
    fake_sandbox = _FakeSandbox(available=True)
    monkeypatch.setattr(
        "seacode.sandbox.create_sandbox", lambda: fake_sandbox
    )
    ui = _FakeUI()
    ctx = _make_ctx(args="on-auto", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert bash_tool.sandbox is fake_sandbox
    assert bash_tool.sandbox_config is not None
    assert checker.sandbox_enabled is True
    assert "沙箱已启用" in ui.system_messages[0]
    assert "自动放行" in ui.system_messages[0]


# 验证 /sandbox 在系统不支持时给出错误提示。
# mock create_sandbox 返回 None，传 args="on"，断言输出含"不支持沙箱"。
async def test_sandbox_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bash_tool = _FakeBashTool(sandbox=None, sandbox_config=None)
    registry = _FakeToolRegistry(bash_tool=bash_tool)
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=_FakeRuleEngine()
    )
    agent = _FakeAgent(permission_checker=checker, registry=registry)
    monkeypatch.setattr("seacode.sandbox.create_sandbox", lambda: None)
    ui = _FakeUI()
    ctx = _make_ctx(args="on", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    text = ui.system_messages[0]
    assert "不支持沙箱" in text
    assert bash_tool.sandbox is None


# 验证 /sandbox 非法参数显示用法且不改状态。
# 传 args="invalid"，断言输出含"用法"且 checker.sandbox_enabled 不变。
async def test_sandbox_invalid_arg_shows_usage() -> None:
    engine = _FakeRuleEngine()
    checker = _FakePermissionChecker(
        mode=PermissionMode.DEFAULT, rule_engine=engine, sandbox_enabled=False
    )
    registry = _FakeToolRegistry(bash_tool=None)
    agent = _FakeAgent(permission_checker=checker, registry=registry)
    ui = _FakeUI()
    ctx = _make_ctx(args="invalid", agent=agent, ui=ui)
    await handle_sandbox(ctx)
    assert "用法" in ui.system_messages[0]
    assert checker.sandbox_enabled is False


# 验证 /sandbox 在 Agent 未初始化时给出提示。
# mock agent=None，断言输出含"Agent 未初始化"。
async def test_sandbox_agent_not_initialized() -> None:
    ui = _FakeUI()
    ctx = _make_ctx(args="", agent=None, ui=ui)
    await handle_sandbox(ctx)
    assert "Agent 未初始化" in ui.system_messages[0]


# ---------- register_all_commands ----------


# 验证 register_all_commands 批量注册 11 条内置命令。
# 构造空注册中心，调 register_all_commands，断言 list_commands 返回 11 条。
def test_register_all_commands_registers_eleven() -> None:
    registry = CommandRegistry()
    register_all_commands(registry)
    assert len(registry.list_commands()) == 11
