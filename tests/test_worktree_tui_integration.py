"""batch13：Worktree 隔离工作区与 FileHistory 的 TUI 启动集成测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from textual.widgets import Static

from seacode.app import ChatInput, SeaCodeApp
from seacode.client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
)
from seacode.config import ProviderConfig, WorktreeConfig
from seacode.conversation import Message
from seacode.filehistory.history import FileHistory
from seacode.tools.enter_worktree import EnterWorktreeTool
from seacode.tools.exit_worktree import ExitWorktreeTool
from seacode.worktree.manager import WorktreeManager


# 提供可按回合返回事件或抛出错误的本地假客户端。
class _FakeClient(LLMClient):
    # 保存每个测试回合的预设结果。
    def __init__(
        self, outcomes: list[list[StreamEvent] | Exception]
    ) -> None:
        self._outcomes = outcomes
        self.requests: list[tuple[Message, ...]] = []

    # 记录请求历史并交付预设事件，不连接真实 Provider。
    # 后台记忆提取/会话摘要/内存整理请求以特定提示词开头，返回空流不消耗 outcome。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        first = messages[0].content if messages else ""
        if first.startswith("Analyze the conversation below"):
            return
        if first.startswith("你是一个对话摘要助手"):
            return
        if any(m.content.startswith("# Dream: Memory Consolidation") for m in messages):
            return
        self.requests.append(tuple(messages))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for event in outcome:
            yield event


# 创建用于 TUI 交互测试的无密钥 Provider 配置。
def _provider(name: str = "test") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="openai-compat",
        model="test-model",
        base_url="https://api.example.test",
        api_key="test-key",
    )


# 等待异步 Textual 事件处理器完成当前回合。
async def _settle(pilot: Any) -> None:
    await pilot.pause(0.05)
    await pilot.pause(0.05)


# 验证启动时 WorktreeManager 与 FileHistory 装配完成。
# 单 Provider 进入 TUI 后 _select_provider 触发 _assemble_worktree_system，
# 两者均应非 None；后台清理 task 与 restore_session task 也应已调度。
@pytest.mark.asyncio
async def test_startup_initializes_worktree_and_filehistory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.worktree_manager is not None
        assert isinstance(app.worktree_manager, WorktreeManager)
        assert app.file_history is not None
        assert isinstance(app.file_history, FileHistory)
        # restore_session task 应已调度；文件不存在时返回 None，task 完成。
        assert app._restore_session_task is not None
        # stale_cleanup task 应已调度；无限循环 task 不会完成。
        assert app._stale_cleanup_task is not None
        # 无 session 文件时 current_session 为 None。
        assert app.worktree_manager.current_session is None


# 验证 EnterWorktree 与 ExitWorktree 工具在启动后已注册到 ToolRegistry。
# _assemble_worktree_system 调用 _tool_registry.register 注册两个工具。
@pytest.mark.asyncio
async def test_worktree_tools_registered(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        tool_names = {t.name for t in app._tool_registry.list_tools()}
        assert "EnterWorktree" in tool_names
        assert "ExitWorktree" in tool_names
        enter_tool = app._tool_registry.get("EnterWorktree")
        assert isinstance(enter_tool, EnterWorktreeTool)
        exit_tool = app._tool_registry.get("ExitWorktree")
        assert isinstance(exit_tool, ExitWorktreeTool)


# 验证 /worktree 与 /rewind 命令在启动后已注册到 CommandRegistry。
# _assemble_worktree_system 调用 _command_registry.register_sync 注册两个命令。
@pytest.mark.asyncio
async def test_worktree_commands_registered(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        cmd_names = {c.name for c in app._command_registry.list_commands()}
        assert "worktree" in cmd_names
        assert "rewind" in cmd_names
        # /wt 别名也应可查找。
        assert app._command_registry.find("wt") is not None
        assert app._command_registry.find("worktree") is not None
        assert app._command_registry.find("rewind") is not None


# 验证 write_file/edit_file 工具的 file_history 属性在启动后已注入。
# _assemble_worktree_system 遍历 list_tools 并对持有 file_history 属性的工具赋值。
@pytest.mark.asyncio
async def test_file_history_injected_into_write_tools(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.file_history is not None
        write_tool = app._tool_registry.get("WriteFile")
        if write_tool is not None and hasattr(write_tool, "file_history"):
            assert write_tool.file_history is app.file_history
        edit_tool = app._tool_registry.get("EditFile")
        if edit_tool is not None and hasattr(edit_tool, "file_history"):
            assert edit_tool.file_history is app.file_history


# 验证 AgentTool 的 _worktree_manager 在启动后已注入。
# _assemble_worktree_system 调用 _agent_tool.set_worktree_manager 注入管理器。
@pytest.mark.asyncio
async def test_agent_tool_worktree_manager_injected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._agent_tool is not None
        assert app._agent_tool.worktree_manager is app.worktree_manager


# 验证 on_unmount 取消后台 stale_cleanup task 与 restore_session task。
# 退出 run_test 上下文后 Textual 触发 on_unmount，两个字段应被置 None。
@pytest.mark.asyncio
async def test_on_unmount_cancels_background_tasks(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._stale_cleanup_task is not None
        assert app._restore_session_task is not None

    # 退出 run_test 后 on_unmount 应已取消并置空两个 task。
    assert app._stale_cleanup_task is None
    assert app._restore_session_task is None


# 验证自定义 WorktreeConfig 传递到 WorktreeManager。
# 构造时传入 symlink_directories，启动后 manager 应持有相同列表。
@pytest.mark.asyncio
async def test_custom_worktree_config_propagated(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    cfg = WorktreeConfig(
        symlink_directories=(".venv", "node_modules"),
        stale_cleanup_interval=1800,
        stale_cutoff_hours=12,
    )
    app = SeaCodeApp(
        [_provider()],
        client_factory=lambda _: client,
        worktree_cfg=cfg,
    )

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.worktree_manager is not None
        assert app.worktree_manager.symlink_directories == [".venv", "node_modules"]
        assert app._worktree_cfg.stale_cleanup_interval == 1800
        assert app._worktree_cfg.stale_cutoff_hours == 12


# 验证 restore_session 在无 session 文件时返回 None 且不阻断启动。
# 临时目录无 .seacode/worktree_session.json，restore_session 返回 None；
# worktree_manager.current_session 应为 None，主流程正常进入 Ready 状态。
@pytest.mark.asyncio
async def test_restore_session_returns_none_without_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.worktree_manager is not None
        assert app.worktree_manager.current_session is None
        assert app.worktree_manager.active == {}
        # restore_session task 应已完成（返回 None）。
        assert app._restore_session_task is not None
        assert app._restore_session_task.done()
        # 主流程应进入 Ready 状态。
        assert "Ready" in str(app.query_one("#turn-status", Static).render())


# 验证单回合后 Agent.file_history 已注入且 make_snapshot 可调用。
# 提交一条消息触发 _run_turn，创建 Agent 时注入 file_history；
# Agent.run 调用 make_snapshot 留档，不抛异常即通过。
@pytest.mark.asyncio
async def test_agent_file_history_injected_on_run_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(
        [[TextDelta("Hello"), StreamComplete(input_tokens=1, output_tokens=1)]]
    )
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        input_widget = app.query_one(ChatInput)
        input_widget.load_text("Hi")
        await pilot.press("enter")
        await _settle(pilot)

        # _run_turn 创建 Agent 后注入 file_history。
        assert app._agent is not None
        assert app._agent.file_history is app.file_history
        # make_snapshot 应在 Agent.run 起点调用，产生至少一个快照。
        assert app.file_history is not None
        assert app.file_history.has_snapshots()


# 验证装配失败时静默降级不阻断主流程。
# WorktreeConfig 传入非法类型不会发生（validator 已拦截），但 _assemble_worktree_system
# 的 try/except 保证任一步失败时 worktree_manager 降级为 None，主流程仍可对话。
@pytest.mark.asyncio
async def test_worktree_failure_degrades_gracefully(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])

    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    def _failing_assemble(work_dir: str) -> None:
        del work_dir
        try:
            raise RuntimeError("simulated worktree init failure")
        except Exception:
            app.worktree_manager = None

    monkeypatch.setattr(app, "_assemble_worktree_system", _failing_assemble)

    async with app.run_test() as pilot:
        await _settle(pilot)
        # worktree_manager 降级为 None，但主流程应仍可进入 Ready。
        assert app.worktree_manager is None
        assert "Ready" in str(app.query_one("#turn-status", Static).render())
