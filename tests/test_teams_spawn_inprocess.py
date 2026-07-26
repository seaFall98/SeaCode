"""teams/spawn_inprocess.py 单测：handle 属性、辅助函数与 _run 主循环全分支。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from seacode.teams.mailbox import Mailbox, create_message
from seacode.teams.spawn_inprocess import (
    SHUTDOWN_PREFIX,
    _create_idle_notification,
    _inject_pending_messages,
    _is_shutdown_request,
    _wait_for_next_prompt_or_shutdown,
    spawn_inprocess_teammate,
)


# 验证 _is_shutdown_request 按 message_type 与 content 前缀判定。
# 三种输入分别断言：shutdown_request 类型返回 True；[shutdown] 前缀返回 True；普通消息 False。
def test_is_shutdown_request_branches() -> None:
    msg_type = create_message(
        from_agent="lead", to_agent="alice", content="bye",
        summary="shutdown", message_type="shutdown_request",
    )
    assert _is_shutdown_request(msg_type) is True

    msg_prefix = create_message(
        from_agent="lead", to_agent="alice",
        content=f"{SHUTDOWN_PREFIX} now", summary="shutdown",
    )
    assert _is_shutdown_request(msg_prefix) is True

    msg_plain = create_message(
        from_agent="lead", to_agent="alice",
        content="hello", summary="greeting",
    )
    assert _is_shutdown_request(msg_plain) is False


# 验证 _create_idle_notification 构造正确的 idle 消息。
# 断言 from_agent=name、to_agent=传入的 Lead 标识、content 含 [idle] 与 reason。
def test_create_idle_notification() -> None:
    msg = _create_idle_notification("alice", "lead-123", "task completed")
    assert msg.from_agent == "alice"
    assert msg.to_agent == "lead-123"
    assert "[idle]" in msg.content
    assert "alice" in msg.content
    assert "task completed" in msg.content
    assert msg.summary == "idle"


# 验证 _inject_pending_messages 在邮箱有未读时注入 system-reminder，无未读时不注入。
# 写两条消息后调用 _inject_pending_messages 验证 add_system_reminder 调用；空邮箱时不调用。
def test_inject_pending_messages(tmp_path) -> None:  # type: ignore[no-untyped-def]
    mailbox = Mailbox(tmp_path / "mb")
    conv = MagicMock()
    # 空邮箱：不调用 add_system_reminder。
    _inject_pending_messages(mailbox, "alice", conv)
    conv.add_system_reminder.assert_not_called()

    # 写两条消息到 alice 邮箱后：调用一次 add_system_reminder，content 含两条消息。
    mailbox.write("alice", create_message("lead", "alice", "msg1", "s1"))
    mailbox.write("alice", create_message("lead", "alice", "msg2", "s2"))
    _inject_pending_messages(mailbox, "alice", conv)
    conv.add_system_reminder.assert_called_once()
    text = conv.add_system_reminder.call_args[0][0]
    assert "msg1" in text
    assert "msg2" in text


# 验证 _wait_for_next_prompt_or_shutdown 在 shutdown / 普通消息 / 无消息 三种场景的返回。
# 用 mock 控制 mailbox.consume 返回值与 asyncio.sleep 避免真实等待。
@pytest.mark.asyncio
async def test_wait_for_next_prompt_or_shutdown_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = MagicMock()

    # 无消息：继续轮询；第二次有普通消息返回 (prompt, False)。
    call_count = [0]

    async def fake_sleep(seconds: float) -> None:
        call_count[0] += 1
        if call_count[0] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("seacode.teams.spawn_inprocess.asyncio.sleep", fake_sleep)

    normal_msg = create_message("lead", "alice", "new task", "task")
    mailbox.consume.side_effect = [[], [normal_msg]]
    prompt, shutdown = await _wait_for_next_prompt_or_shutdown(mailbox, "alice")
    assert shutdown is False
    assert "new task" in prompt


# 验证 _wait_for_next_prompt_or_shutdown 收到 shutdown 消息返回 ("", True)。
# mock mailbox.consume 返回 shutdown_request 消息，断言返回空 prompt 与 True。
@pytest.mark.asyncio
async def test_wait_for_next_prompt_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = MagicMock()
    shutdown_msg = create_message(
        "lead", "alice", "bye", "shutdown", message_type="shutdown_request"
    )
    mailbox.consume.return_value = [shutdown_msg]
    prompt, shutdown = await _wait_for_next_prompt_or_shutdown(mailbox, "alice")
    assert shutdown is True
    assert prompt == ""


# 验证 spawn_inprocess_teammate 无 mailbox 时单次返回。
# mock agent.run_to_completion 返回 "done"，断言 handle.result == "done" 且 status="completed"。
@pytest.mark.asyncio
async def test_spawn_inprocess_no_mailbox_single_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agent = MagicMock()
    fake_agent.agent_id = "agent-1"
    fake_agent.run_to_completion = AsyncMock(return_value="done")
    fake_team_manager = MagicMock()
    fake_team_manager.get_team_for_teammate.return_value = ""

    handle = spawn_inprocess_teammate(
        fake_agent, "do task", "alice", fake_team_manager, mailbox=None
    )
    # 等待 _run 完成。
    await handle.task
    assert handle.result == "done"
    assert handle.progress.status == "completed"
    assert handle.done is True


# 验证 spawn_inprocess_teammate 有 mailbox 时长驻循环：执行→idle 通知→等待→shutdown。
# mock run_to_completion 返回 "done"，_wait_for_next_prompt_or_shutdown
# 第一次返回普通消息、第二次 shutdown。
# 断言 run_to_completion 调用 2 次、lead 邮箱收到 idle 通知、最终 status="completed"。
@pytest.mark.asyncio
async def test_spawn_inprocess_with_mailbox_long_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    mailbox = Mailbox(Path(__file__).parent / "_mb_test_alice")
    try:
        fake_agent = MagicMock()
        fake_agent.agent_id = "alice-id"
        fake_agent.run_to_completion = AsyncMock(return_value="done")
        fake_team_manager = MagicMock()
        fake_team_manager.get_team_for_teammate.return_value = "demo"

        # mock _wait_for_next_prompt_or_shutdown：第一次返回普通消息，第二次 shutdown。
        call_count = [0]

        async def fake_wait(mb: Mailbox, agent_id: str) -> tuple[str, bool]:
            call_count[0] += 1
            if call_count[0] == 1:
                return ("new task", False)
            return ("", True)

        monkeypatch.setattr(
            "seacode.teams.spawn_inprocess._wait_for_next_prompt_or_shutdown",
            fake_wait,
        )
        # mock _inject_pending_messages 避免真实邮箱消费干扰。
        monkeypatch.setattr(
            "seacode.teams.spawn_inprocess._inject_pending_messages",
            lambda mb, aid, conv: None,
        )

        handle = spawn_inprocess_teammate(
            fake_agent,
            "first task",
            "alice",
            fake_team_manager,
            mailbox=mailbox,
            lead_agent_id="lead-123",
        )
        await handle.task

        # run_to_completion 应被调用 2 次（首轮 + 续派）。
        assert fake_agent.run_to_completion.call_count == 2
        assert handle.progress.status == "completed"
        # lead 邮箱应收到至少一条 idle 通知。
        lead_msgs = mailbox.read("lead-123")
        assert len(lead_msgs) >= 1
        assert any("[idle]" in m.content for m in lead_msgs)
    finally:
        # 清理测试邮箱目录。
        import shutil

        shutil.rmtree(Path(__file__).parent / "_mb_test_alice", ignore_errors=True)


# 验证 _run 主循环在 CancelledError 时标记 status="stopped"。
# mock agent.run_to_completion 抛 CancelledError，断言 progress.status == "stopped"。
@pytest.mark.asyncio
async def test_spawn_inprocess_cancelled_marks_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agent = MagicMock()
    fake_agent.agent_id = "agent-1"
    fake_agent.run_to_completion = AsyncMock(side_effect=asyncio.CancelledError)
    fake_team_manager = MagicMock()
    fake_team_manager.get_team_for_teammate.return_value = ""

    handle = spawn_inprocess_teammate(
        fake_agent, "task", "alice", fake_team_manager, mailbox=None
    )
    with pytest.raises(asyncio.CancelledError):
        await handle.task
    assert handle.progress.status == "stopped"


# 验证 _run 主循环在普通异常时标记 status="failed" 并写 idle 通知到 lead 邮箱。
# mock agent.run_to_completion 抛 RuntimeError，断言 status="failed" 且 lead 邮箱有 idle 通知。
@pytest.mark.asyncio
async def test_spawn_inprocess_exception_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    mailbox = Mailbox(Path(__file__).parent / "_mb_test_fail")
    try:
        fake_agent = MagicMock()
        fake_agent.agent_id = "alice-id"
        fake_agent.run_to_completion = AsyncMock(side_effect=RuntimeError("boom"))
        fake_team_manager = MagicMock()
        fake_team_manager.get_team_for_teammate.return_value = "demo"

        handle = spawn_inprocess_teammate(
            fake_agent,
            "task",
            "alice",
            fake_team_manager,
            mailbox=mailbox,
            lead_agent_id="lead-123",
        )
        with pytest.raises(RuntimeError):
            await handle.task
        assert handle.progress.status == "failed"
        lead_msgs = mailbox.read("lead-123")
        assert any("[idle]" in m.content and "failed" in m.content for m in lead_msgs)
    finally:
        import shutil

        shutil.rmtree(Path(__file__).parent / "_mb_test_fail", ignore_errors=True)


# 验证 InProcessTeammateHandle.cancel 取消底层 Task。
# 构造一个 pending 的 handle，调用 cancel 后断言 task.done() 且 task.cancelled()。
@pytest.mark.asyncio
async def test_handle_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agent = MagicMock()
    fake_agent.agent_id = "agent-1"

    # 让 run_to_completion 永久挂起，模拟长驻 teammate。
    async def hang(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(100)
        return ""

    fake_agent.run_to_completion = hang
    fake_team_manager = MagicMock()
    fake_team_manager.get_team_for_teammate.return_value = ""

    handle = spawn_inprocess_teammate(
        fake_agent, "task", "alice", fake_team_manager, mailbox=None
    )
    # 给 event loop 一个机会让 _run 开始。
    await asyncio.sleep(0.01)
    assert not handle.done
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle.task
    assert handle.done
