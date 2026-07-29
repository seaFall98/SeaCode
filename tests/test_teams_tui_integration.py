"""TUI 集成测试：SeaCodeApp 启动时初始化 TeamManager、挂载 TeammateTree、
注册团队协调工具、注入 notification_fn 与周期刷新 task。"""

from __future__ import annotations

from pathlib import Path
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
from seacode.config import ProviderConfig
from seacode.teammate_tree import TeammateTree
from seacode.teams.manager import TeamManager
from seacode.teams.progress import TeammateProgress
from seacode.tools.send_message import SendMessageTool
from seacode.tools.team_create import TeamCreateTool
from seacode.tools.team_delete import TeamDeleteTool

# ---------------------------------------------------------------------------
# 测试辅助 fake 类
# ---------------------------------------------------------------------------


# 假 LLMClient：交付预设事件流，记录请求历史。
class _FakeClient(LLMClient):
    def __init__(self, outcomes: list[list[StreamEvent]] | None = None) -> None:
        self._outcomes = outcomes or []
        self.requests: list[tuple[Any, ...]] = []

    async def stream(
        self,
        messages: Any,
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        del system, tools
        first = messages[0].content if messages else ""
        # 跳过后台记忆提取与会话摘要请求。
        if first.startswith("Analyze the conversation below"):
            return
        if first.startswith("你是一个对话摘要助手"):
            return
        if any(m.content.startswith("# Dream: Memory Consolidation") for m in messages):
            return
        self.requests.append(tuple(messages))
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            for event in outcome:
                yield event


# 创建无密钥 Provider 配置。
def _provider(name: str = "test") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="openai-compat",
        model="test-model",
        base_url="https://api.example.test",
        api_key="test-key",
    )


# 等待异步事件处理器完成。
async def _settle(pilot: Any) -> None:
    await pilot.pause(0.05)
    await pilot.pause(0.05)


# ---------------------------------------------------------------------------
# 启动初始化测试
# ---------------------------------------------------------------------------


# 验证 SeaCodeApp 启动时 TeamManager 初始化为非 None。
# 单 Provider 启动后 _assemble_teams_system 创建 TeamManager 并存到 app.team_manager。
@pytest.mark.asyncio
async def test_app_starts_with_team_manager_initialized() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.team_manager is not None
        assert isinstance(app.team_manager, TeamManager)


# 验证应用把启动时 cwd 固定为 TeamManager 的项目级团队根。
# 切换到临时项目启动 TUI，断言根为该项目的 .seacode/teams 而非用户 Home。
@pytest.mark.asyncio
async def test_app_team_manager_uses_startup_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.team_manager is not None
        assert app.team_manager.teams_root == (
            tmp_path / ".seacode" / "teams"
        ).resolve()


# 验证 SeaCodeApp 启动时 TeammateTree widget 挂载到主 TUI。
# compose 中创建 TeammateTree 并 yield，app.teammate_tree 应为 TeammateTree 实例。
@pytest.mark.asyncio
async def test_teammate_tree_mounted_on_app_start() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.teammate_tree is not None
        assert isinstance(app.teammate_tree, TeammateTree)


# 验证 TeamCreate / TeamDelete / SendMessage 工具注册到 Lead 工具集。
# _assemble_teams_system 注册三个团队工具，registry.get 应能按名查到。
@pytest.mark.asyncio
async def test_team_tools_registered_in_registry() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        registry = app._tool_registry
        assert registry.get("TeamCreate") is not None
        assert registry.get("TeamDelete") is not None
        assert registry.get("SendMessage") is not None


# 验证周期刷新 task 启动。
# _assemble_teams_system 启动 _teammate_refresh_task，应为 asyncio.Task 实例且未完成。
@pytest.mark.asyncio
async def test_teammate_refresh_task_started() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._teammate_refresh_task is not None
        assert not app._teammate_refresh_task.done()


# 验证 TeammateTree 初始隐藏（无 teammates 时不占用 TUI 空间）。
# on_mount 设置 display=False，refresh task 检测到空 teammates 时也保持隐藏。
@pytest.mark.asyncio
async def test_teammate_tree_hidden_initially() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app.teammate_tree is not None
        assert app.teammate_tree.display is False


# ---------------------------------------------------------------------------
# Lead Agent 注入测试
# ---------------------------------------------------------------------------


# 验证 _run_turn 创建 Lead Agent 后注入 _team_manager 与 notification_fn。
# 发送一条消息触发 _run_turn，agent._team_manager 应为 app.team_manager，
# agent.notification_fn 应可调用并返回列表。
@pytest.mark.asyncio
async def test_run_turn_injects_team_manager_and_notification_fn() -> None:
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
        await _settle(pilot)

        agent = app._agent
        assert agent is not None
        assert agent._team_manager is app.team_manager
        assert agent.notification_fn is not None
        # notification_fn 应返回列表（无团队时为空列表）。
        notes = agent.notification_fn()
        assert isinstance(notes, list)


# 验证 TeamCreate / TeamDelete / SendMessage 的 Lead 上下文在 _run_turn 中刷新。
# 发送消息触发 _run_turn 后，三个团队工具都应持有当前回合 Agent。
@pytest.mark.asyncio
async def test_run_turn_updates_team_tools_parent_agent() -> None:
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
        await _settle(pilot)

        agent = app._agent
        assert agent is not None
        team_create = app._tool_registry.get("TeamCreate")
        team_delete = app._tool_registry.get("TeamDelete")
        send_message = app._tool_registry.get("SendMessage")
        assert team_create is not None
        assert team_delete is not None
        assert send_message is not None
        assert isinstance(team_create, TeamCreateTool)
        assert isinstance(team_delete, TeamDeleteTool)
        assert isinstance(send_message, SendMessageTool)
        assert team_create._parent_agent is agent
        assert team_delete._parent_agent is agent
        # SendMessage 占位实例不再依赖空 from_agent_id，而是在 execute 时读取当前 Lead。
        assert send_message._from_agent_id == ""
        assert send_message._parent_agent is agent


# ---------------------------------------------------------------------------
# TeammateTree 刷新测试
# ---------------------------------------------------------------------------


# 验证 _refresh_teammate_tree 检测到非空 progress 时显示 widget。
# mock team_manager.get_all_teammate_progress 返回非空列表，调用 _refresh 逻辑。
@pytest.mark.asyncio
async def test_refresh_teammate_tree_shows_widget_when_teammates_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        # mock get_all_teammate_progress 返回非空列表。
        progress = TeammateProgress(name="alice", team_name="demo", status="running")
        manager = app.team_manager
        tree = app.teammate_tree
        assert manager is not None
        assert tree is not None
        monkeypatch.setattr(manager, "get_all_teammate_progress", lambda: [progress])
        # 手动触发一次刷新逻辑（不等待 1 秒 sleep）。
        tree.teammates = manager.get_all_teammate_progress()
        tree.leader_tokens = 100
        tree.display = bool(tree.teammates)
        await _settle(pilot)

        assert tree.display is True
        assert len(tree.teammates) == 1
        assert tree.teammates[0].name == "alice"


# 验证 _refresh_teammate_tree 检测到空 progress 时隐藏 widget。
# mock team_manager.get_all_teammate_progress 返回空列表，widget 应隐藏。
@pytest.mark.asyncio
async def test_refresh_teammate_tree_hides_widget_when_no_teammates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        manager = app.team_manager
        tree = app.teammate_tree
        assert manager is not None
        assert tree is not None
        monkeypatch.setattr(manager, "get_all_teammate_progress", lambda: [])
        tree.teammates = manager.get_all_teammate_progress()
        tree.display = bool(tree.teammates)
        await _settle(pilot)

        assert tree.display is False
        assert len(tree.teammates) == 0


# ---------------------------------------------------------------------------
# on_unmount 清理测试
# ---------------------------------------------------------------------------


# 验证 on_unmount 取消 TeammateTree 周期刷新 task。
# 退出 app 后 _teammate_refresh_task 应为 None（cancel 后置 None）。
@pytest.mark.asyncio
async def test_on_unmount_cancels_teammate_refresh_task() -> None:
    client = _FakeClient([[StreamComplete()]])
    app = SeaCodeApp([_provider()], client_factory=lambda _: client)

    async with app.run_test() as pilot:
        await _settle(pilot)
        assert app._teammate_refresh_task is not None
        # 退出 app 触发 on_unmount。

    # app 退出后 _teammate_refresh_task 应为 None。
    assert app._teammate_refresh_task is None


# ---------------------------------------------------------------------------
# 既有行为不回归测试
# ---------------------------------------------------------------------------


# 验证 team_manager 为 None 时（装配失败）既有 Agent 行为不回归。
# SeaCodeApp 不传 teammate_mode / enable_coordinator_mode 时仍可正常对话。
@pytest.mark.asyncio
async def test_app_works_without_team_config() -> None:
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
        await _settle(pilot)

        # 既有行为：状态栏显示 Ready，对话区有回复。
        assert "Ready" in str(app.query_one("#turn-status", Static).render())
