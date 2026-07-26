"""/clear 重建 FileHistory 单测：覆盖新 session_id、工具同步注入与旧快照隔离。"""

from __future__ import annotations

from typing import Any

import pytest

from seacode.commands.handlers.clear import handle_clear
from seacode.commands.registry import CommandContext
from seacode.filehistory.history import FileHistory

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假 UI：收集系统消息。
class _FakeUI:
    def __init__(self) -> None:
        self.system_messages: list[str] = []
        self.refresh_calls = 0

    def add_system_message(self, text: str) -> None:
        self.system_messages.append(text)

    def refresh_status(self) -> None:
        self.refresh_calls += 1


# 假 Session：携带可读 session_id。
class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def close(self) -> None:
        pass


# 假 SessionManager：create() 返回新 session，session_id 递增。
class _FakeSessionManager:
    def __init__(self) -> None:
        self._counter = 0
        self.create_calls = 0

    def create(self) -> _FakeSession:
        self._counter += 1
        self.create_calls += 1
        return _FakeSession(f"sess-{self._counter}")


# 假工具：模拟 WriteFile/EditFile 持有 file_history 属性。
class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.file_history: Any = None


# 假工具注册表：list_tools 返回已注册工具实例列表。
class _FakeRegistry:
    def __init__(self, tools: list[_FakeTool] | None = None) -> None:
        self._tools = tools or []

    def list_tools(self) -> list[_FakeTool]:
        return list(self._tools)


# 假回调：记录 clear_chat / set_session / set_conversation 调用。
class _FakeCallbacks:
    def __init__(self) -> None:
        self.clear_chat_calls = 0
        self.set_session_calls: list[Any] = []
        self.set_conversation_calls: list[Any] = []

    def clear_chat(self) -> None:
        self.clear_chat_calls += 1

    def set_session(self, session: Any) -> None:
        self.set_session_calls.append(session)

    def set_conversation(self, conv: Any) -> None:
        self.set_conversation_calls.append(conv)


# 假 Agent：携带 work_dir / registry / file_history / _loop_count / tokens / active_skills。
class _FakeAgent:
    def __init__(
        self,
        work_dir: str,
        registry: _FakeRegistry,
        file_history: Any = None,
    ) -> None:
        self.work_dir = work_dir
        self.registry = registry
        self.file_history = file_history
        self._loop_count = 5
        self.total_input_tokens = 100
        self.total_output_tokens = 50
        self.active_skills: dict[str, str] = {"old": "sop"}

    def clear_active_skills(self) -> None:
        self.active_skills.clear()


# 构造 CommandContext。
def _make_ctx(
    args: str = "",
    agent: Any = None,
    session_manager: Any = None,
    ui: _FakeUI | None = None,
    config: dict[str, Any] | None = None,
) -> CommandContext:
    return CommandContext(
        args=args,
        agent=agent,
        conversation=None,
        session=None,
        session_manager=session_manager,
        memory_manager=None,
        ui=ui if ui is not None else _FakeUI(),
        config=config if config is not None else {},
    )


# 构造回调 config 字典。
def _config(cb: _FakeCallbacks) -> dict[str, Any]:
    return {
        "clear_chat": cb.clear_chat,
        "set_session": cb.set_session,
        "set_conversation": cb.set_conversation,
    }


# ---------------------------------------------------------------------------
# /clear 重建 FileHistory 测试
# ---------------------------------------------------------------------------


# 验证 /clear 基于新 session_id 重建 Agent.file_history。
# 注入旧 FileHistory，调 handle_clear 后断言 agent.file_history 是新实例且 id() 不同。
@pytest.mark.asyncio
async def test_clear_rebuilds_file_history_with_new_session_id(
    tmp_path: Any,
) -> None:
    work_dir = str(tmp_path)
    old_fh = FileHistory(work_dir, "old-session")
    registry = _FakeRegistry()
    agent = _FakeAgent(work_dir=work_dir, registry=registry, file_history=old_fh)
    sm = _FakeSessionManager()
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(
        agent=agent, session_manager=sm, ui=ui, config=_config(cb)
    )

    await handle_clear(ctx)

    assert agent.file_history is not old_fh
    assert isinstance(agent.file_history, FileHistory)
    # 新 FileHistory 没有快照。
    assert agent.file_history.has_snapshots() is False


# 验证 /clear 同步注入 file_history 到 write_file/edit_file 工具。
# 注册两个 _FakeTool 模拟写文件工具，调 handle_clear 后断言 tool.file_history 同步更新。
@pytest.mark.asyncio
async def test_clear_syncs_file_history_to_write_edit_tools(
    tmp_path: Any,
) -> None:
    work_dir = str(tmp_path)
    write_tool = _FakeTool("WriteFile")
    edit_tool = _FakeTool("EditFile")
    # 不带 file_history 属性的工具应被跳过。
    class _PlainTool:
        name = "PlainTool"

    plain_tool = _PlainTool()
    registry = _FakeRegistry([write_tool, edit_tool])
    old_fh = FileHistory(work_dir, "old")
    agent = _FakeAgent(work_dir=work_dir, registry=registry, file_history=old_fh)
    sm = _FakeSessionManager()
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(
        agent=agent, session_manager=sm, ui=ui, config=_config(cb)
    )

    await handle_clear(ctx)

    new_fh = agent.file_history
    assert new_fh is not None
    assert write_tool.file_history is new_fh
    assert edit_tool.file_history is new_fh
    # plain_tool 没有 file_history 属性，不应被注入。
    assert not hasattr(plain_tool, "file_history")


# 验证 /clear 后旧快照不可访问。
# 旧 FileHistory 有快照，调 handle_clear 后断言新 agent.file_history.has_snapshots() 为 False。
@pytest.mark.asyncio
async def test_clear_old_snapshots_inaccessible(tmp_path: Any) -> None:
    work_dir = str(tmp_path)
    old_fh = FileHistory(work_dir, "old-session")
    # 制造一个文件并 track_edit 后 make_snapshot，让旧 FileHistory 有快照。
    file_path = tmp_path / "test.txt"
    file_path.write_text("content", encoding="utf-8")
    old_fh.track_edit(str(file_path))
    old_fh.make_snapshot(0, "old user text")
    assert old_fh.has_snapshots() is True

    registry = _FakeRegistry()
    agent = _FakeAgent(work_dir=work_dir, registry=registry, file_history=old_fh)
    sm = _FakeSessionManager()
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(
        agent=agent, session_manager=sm, ui=ui, config=_config(cb)
    )

    await handle_clear(ctx)

    new_fh = agent.file_history
    assert isinstance(new_fh, FileHistory)
    assert new_fh is not old_fh
    # 新 FileHistory 的 snapshots 列表为空。
    assert new_fh.has_snapshots() is False
    assert new_fh.get_snapshots() == []


# 验证 /clear 在 agent 为 None 时不抛异常。
# agent=None 调 handle_clear，断言只调用 clear_chat 与 session_manager.create。
@pytest.mark.asyncio
async def test_clear_without_agent_does_not_raise() -> None:
    sm = _FakeSessionManager()
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(session_manager=sm, ui=ui, config=_config(cb))

    await handle_clear(ctx)

    assert cb.clear_chat_calls == 1
    assert sm.create_calls == 1
    assert "对话已清除" in ui.system_messages[0]


# 验证 /clear 在 session_manager 为 None 时跳过 FileHistory 重建。
# session_manager=None 调 handle_clear，断言 agent.file_history 不变。
@pytest.mark.asyncio
async def test_clear_without_session_manager_skips_rebuild(
    tmp_path: Any,
) -> None:
    work_dir = str(tmp_path)
    old_fh = FileHistory(work_dir, "old")
    registry = _FakeRegistry()
    agent = _FakeAgent(work_dir=work_dir, registry=registry, file_history=old_fh)
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(agent=agent, session_manager=None, ui=ui, config=_config(cb))

    await handle_clear(ctx)

    # session_manager 为 None 时 new_session_id 为空，跳过重建。
    assert agent.file_history is old_fh


# 验证 /clear 完整流程：清屏 + 创新会话 + 重建 ConversationManager +
# 重置 _loop_count / active_skills / token 计数 + refresh_status。
@pytest.mark.asyncio
async def test_clear_preserves_original_behavior(tmp_path: Any) -> None:
    work_dir = str(tmp_path)
    registry = _FakeRegistry()
    agent = _FakeAgent(work_dir=work_dir, registry=registry)
    sm = _FakeSessionManager()
    cb = _FakeCallbacks()
    ui = _FakeUI()
    ctx = _make_ctx(
        agent=agent, session_manager=sm, ui=ui, config=_config(cb)
    )

    await handle_clear(ctx)

    assert cb.clear_chat_calls == 1
    assert sm.create_calls == 1
    assert len(cb.set_session_calls) == 1
    assert len(cb.set_conversation_calls) == 1
    # Agent 运行时状态已重置。
    assert agent._loop_count == 0
    assert agent.total_input_tokens == 0
    assert agent.total_output_tokens == 0
    assert agent.active_skills == {}
    # UI 刷新已调用。
    assert ui.refresh_calls == 1
    assert "对话已清除" in ui.system_messages[0]
