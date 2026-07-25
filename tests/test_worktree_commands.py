"""/worktree 命令五子命令的集成测试：覆盖 create / list / enter / exit / status 全分支。"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

from seacode.commands.handlers.worktree import create_worktree_command
from seacode.commands.registry import Command, CommandContext
from seacode.worktree.manager import WorktreeError, WorktreeManager
from seacode.worktree.models import Worktree, WorktreeSession


# 假 UI：收集 add_system_message 调用的文本。
class _FakeUI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    # 以下方法仅为满足 UIController 协议占位。
    def send_user_message(self, text: str) -> None:
        pass

    def set_plan_mode(self, enabled: bool) -> None:
        pass

    def get_token_count(self) -> tuple[int, int]:
        return 0, 0

    def refresh_status(self) -> None:
        pass


# 假 Agent：保留可写的 work_dir 字段。
class _FakeAgent:
    def __init__(self, work_dir: str = "/repo") -> None:
        self.work_dir = work_dir


def _make_worktree(name: str = "feat-x") -> Worktree:
    return Worktree(
        name=name,
        path=f"/wt/{name}",
        branch=f"worktree-{name}",
        based_on="HEAD",
        head_commit="abc123",
        created=datetime.datetime.now(),
    )


def _make_session(name: str = "feat-x") -> WorktreeSession:
    return WorktreeSession(
        original_cwd="/repo",
        worktree_path=f"/wt/{name}",
        worktree_name=name,
        original_branch="main",
        original_head_commit="abc123",
        session_id="sess-1",
        hook_based=False,
    )


# 构造 /worktree 命令与 ctx；manager 默认是真实 WorktreeManager，方法按需 mock。
def _make_command_and_ctx(
    manager: WorktreeManager, args: str, agent: _FakeAgent | None = None
) -> tuple[Command, CommandContext, _FakeUI]:
    cmd = create_worktree_command(manager)
    ui = _FakeUI()
    ctx = CommandContext(
        args=args,
        agent=agent or _FakeAgent(),
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config={},
    )
    return cmd, ctx, ui


# 验证 /worktree 无参数时显示用法。
# 构造 args="" 调用 handler，断言 ui.messages 含 "用法"。
async def test_no_args_shows_usage() -> None:
    manager = WorktreeManager("/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "")

    await cmd.handler(ctx)

    assert any("用法" in m for m in ui.messages)


# 验证 /worktree create <name> 创建并进入 worktree，且切换 agent.work_dir。
# mock create/enter 返回 Worktree/Session，断言 work_dir 切换到 worktree 路径。
async def test_create_creates_and_enters_switching_work_dir() -> None:
    manager = WorktreeManager("/repo")
    wt = _make_worktree("feat-x")
    session = _make_session("feat-x")
    manager.create = AsyncMock(return_value=wt)  # type: ignore[method-assign]
    manager.enter = AsyncMock(return_value=session)  # type: ignore[method-assign]
    agent = _FakeAgent(work_dir="/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "create feat-x", agent=agent)

    await cmd.handler(ctx)

    assert agent.work_dir == session.worktree_path
    assert any("feat-x" in m and "/wt/feat-x" in m for m in ui.messages)


# 验证 /worktree create 缺参数时提示用法。
# 构造 args="create" 调用 handler，断言 ui.messages 含 "用法: /worktree create"。
async def test_create_no_name_shows_usage() -> None:
    manager = WorktreeManager("/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "create")

    await cmd.handler(ctx)

    assert any("/worktree create <name>" in m for m in ui.messages)


# 验证 /worktree create 失败时显示错误信息。
# mock create 抛 WorktreeError，断言 ui.messages 含 "创建失败" 与错误信息。
async def test_create_failure_shows_error() -> None:
    manager = WorktreeManager("/repo")
    manager.create = AsyncMock(side_effect=WorktreeError("git failed"))  # type: ignore[method-assign]
    cmd, ctx, ui = _make_command_and_ctx(manager, "create feat-x")

    await cmd.handler(ctx)

    assert any("创建失败" in m and "git failed" in m for m in ui.messages)


# 验证 /worktree list 列出 active worktrees 并标记当前。
# mock list_worktrees 返回两个 worktree，current_session 返回其中一个，断言输出含 "(current)"。
async def test_list_marks_current_worktree() -> None:
    manager = WorktreeManager("/repo")
    manager.list_worktrees = lambda: [_make_worktree("feat-a"), _make_worktree("feat-b")]  # type: ignore[assignment]
    manager.current_session = _make_session("feat-a")
    cmd, ctx, ui = _make_command_and_ctx(manager, "list")

    await cmd.handler(ctx)

    text = "\n".join(ui.messages)
    assert "feat-a" in text
    assert "feat-b" in text
    assert "(current)" in text


# 验证 /worktree list 无 active worktree 时提示。
# mock list_worktrees 返回空列表，断言 ui.messages 含 "无 active worktree"。
async def test_list_empty_shows_message() -> None:
    manager = WorktreeManager("/repo")
    manager.list_worktrees = lambda: []  # type: ignore[assignment]
    cmd, ctx, ui = _make_command_and_ctx(manager, "list")

    await cmd.handler(ctx)

    assert any("无 active worktree" in m for m in ui.messages)


# 验证 /worktree enter <name> 进入已存在 worktree 并切换 work_dir。
# mock enter 返回 session，断言 work_dir 切换到 worktree 路径。
async def test_enter_switches_work_dir() -> None:
    manager = WorktreeManager("/repo")
    session = _make_session("feat-x")
    manager.enter = AsyncMock(return_value=session)  # type: ignore[method-assign]
    agent = _FakeAgent(work_dir="/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "enter feat-x", agent=agent)

    await cmd.handler(ctx)

    assert agent.work_dir == session.worktree_path
    assert any("已进入 worktree" in m for m in ui.messages)


# 验证 /worktree enter 失败时显示错误。
# mock enter 抛 WorktreeError，断言 ui.messages 含 "进入失败"。
async def test_enter_failure_shows_error() -> None:
    manager = WorktreeManager("/repo")
    manager.enter = AsyncMock(side_effect=WorktreeError("not found"))  # type: ignore[method-assign]
    cmd, ctx, ui = _make_command_and_ctx(manager, "enter feat-x")

    await cmd.handler(ctx)

    assert any("进入失败" in m and "not found" in m for m in ui.messages)


# 验证 /worktree exit（无 flag）退出 keep 模式并恢复 work_dir。
# mock exit 与 current_session，断言 work_dir 恢复到 original_cwd。
async def test_exit_keep_restores_work_dir() -> None:
    manager = WorktreeManager("/repo")
    session = _make_session("feat-x")
    manager.current_session = session
    manager.exit = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _FakeAgent(work_dir="/wt/feat-x")
    cmd, ctx, ui = _make_command_and_ctx(manager, "exit", agent=agent)

    await cmd.handler(ctx)

    assert agent.work_dir == session.original_cwd
    manager.exit.assert_awaited_once_with(
        "feat-x", action="keep", discard_changes=False
    )
    assert any("已退出 worktree (kept)" in m for m in ui.messages)


# 验证 /worktree exit --remove 有变更时拒绝（exit 抛 WorktreeError）。
# mock exit 抛 WorktreeError 含 "uncommitted changes"，断言 ui.messages 含 "退出失败"。
async def test_exit_remove_with_changes_returns_error() -> None:
    manager = WorktreeManager("/repo")
    session = _make_session("feat-x")
    manager.current_session = session
    manager.exit = AsyncMock(side_effect=WorktreeError("uncommitted changes"))  # type: ignore[method-assign]
    cmd, ctx, ui = _make_command_and_ctx(manager, "exit --remove")

    await cmd.handler(ctx)

    assert any("退出失败" in m and "uncommitted changes" in m for m in ui.messages)


# 验证 /worktree exit --remove --discard 强制删除并恢复 work_dir。
# mock exit 成功，断言 exit 收到 action="remove" 与 discard_changes=True。
async def test_exit_remove_with_discard_forces_removal() -> None:
    manager = WorktreeManager("/repo")
    session = _make_session("feat-x")
    manager.current_session = session
    manager.exit = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _FakeAgent(work_dir="/wt/feat-x")
    cmd, ctx, ui = _make_command_and_ctx(manager, "exit --remove --discard", agent=agent)

    await cmd.handler(ctx)

    assert agent.work_dir == session.original_cwd
    manager.exit.assert_awaited_once_with(
        "feat-x", action="remove", discard_changes=True
    )
    assert any("已退出 worktree (removed)" in m for m in ui.messages)


# 验证 /worktree exit 未在 session 中时提示。
# current_session=None，断言 ui.messages 含 "未在 worktree session 中"。
async def test_exit_not_in_session_shows_message() -> None:
    manager = WorktreeManager("/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "exit")

    await cmd.handler(ctx)

    assert any("未在 worktree session 中" in m for m in ui.messages)


# 验证 /worktree status 显示 session 状态。
# mock current_session 返回 session，断言 ui.messages 含 session 字段。
async def test_status_shows_session_info() -> None:
    manager = WorktreeManager("/repo")
    session = _make_session("feat-x")
    manager.current_session = session
    cmd, ctx, ui = _make_command_and_ctx(manager, "status")

    await cmd.handler(ctx)

    text = "\n".join(ui.messages)
    assert "feat-x" in text
    assert "/wt/feat-x" in text
    assert "/repo" in text
    assert "main" in text


# 验证 /worktree status 未在 session 中时提示。
# current_session=None，断言 ui.messages 含 "未在 worktree session 中"。
async def test_status_not_in_session_shows_message() -> None:
    manager = WorktreeManager("/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "status")

    await cmd.handler(ctx)

    assert any("未在 worktree session 中" in m for m in ui.messages)


# 验证 /worktree 未知子命令时提示可用列表。
# 构造 args="foobar"，断言 ui.messages 含 "未知子命令" 与可用列表。
async def test_unknown_subcommand_shows_available() -> None:
    manager = WorktreeManager("/repo")
    cmd, ctx, ui = _make_command_and_ctx(manager, "foobar")

    await cmd.handler(ctx)

    text = "\n".join(ui.messages)
    assert "未知子命令" in text
    assert "create" in text
    assert "exit" in text


# 验证 /wt 别名与 /worktree 等价。
# 检查命令的 aliases 字段含 "wt"，断言通过别名查找也能找到 worktree 命令。
async def test_wt_alias_registered() -> None:
    manager = WorktreeManager("/repo")
    cmd = create_worktree_command(manager)

    assert "wt" in cmd.aliases
