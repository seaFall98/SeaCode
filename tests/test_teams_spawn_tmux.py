"""teams/spawn_tmux.py 单测：spawn / send_keys / kill_pane 全分支。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from seacode.teams.spawn_tmux import (
    TmuxPaneInfo,
    kill_pane,
    send_keys_to_pane,
    spawn_tmux_teammate,
)


# 验证 spawn_tmux_teammate 调用 tmux new-window -d -n <team>-<member> <cli>。
# mock subprocess.run 返回 returncode=0 与 pane_id，断言调用参数与返回值。
def test_spawn_tmux_teammate_success() -> None:
    fake_new = MagicMock(returncode=0, stdout="", stderr="")
    fake_list = MagicMock(returncode=0, stdout="%1\n", stderr="")
    with patch(
        "seacode.teams.spawn_tmux.subprocess.run",
        side_effect=[fake_new, fake_list],
    ) as mock_run:
        info = spawn_tmux_teammate("demo", "alice", "/tmp/work")

    assert isinstance(info, TmuxPaneInfo)
    assert info.window_name == "demo-alice"
    assert info.pane_id == "%1"
    # 第一次调用：new-window -d -n <name> <cli>。
    first_call = mock_run.call_args_list[0]
    assert first_call[0][0][0] == "tmux"
    assert "new-window" in first_call[0][0]
    assert "-d" in first_call[0][0]
    assert "-n" in first_call[0][0]
    assert "demo-alice" in first_call[0][0]
    # 第二次调用：list-panes -t <window> -F #{pane_id}。
    second_call = mock_run.call_args_list[1]
    assert "list-panes" in second_call[0][0]
    assert "demo-alice" in second_call[0][0]
    assert "#{pane_id}" in second_call[0][0]


# 验证 spawn_tmux_teammate 在 new-window 失败时抛 RuntimeError。
# mock subprocess.run 返回 returncode=1，断言抛错。
def test_spawn_tmux_teammate_failure() -> None:
    fake_fail = MagicMock(returncode=1, stdout="", stderr="error")
    with patch(
        "seacode.teams.spawn_tmux.subprocess.run", return_value=fake_fail
    ):
        with pytest.raises(RuntimeError, match="tmux new-window failed"):
            spawn_tmux_teammate("demo", "alice", "/tmp/work")


# 验证 spawn_tmux_teammate 在 list-panes 失败时回退用 window_name 作为 pane_id。
# mock new-window 成功但 list-panes 失败，断言 pane_id == window_name。
def test_spawn_tmux_teammate_list_fail_fallback() -> None:
    fake_new = MagicMock(returncode=0, stdout="", stderr="")
    fake_list_fail = MagicMock(returncode=1, stdout="", stderr="error")
    with patch(
        "seacode.teams.spawn_tmux.subprocess.run",
        side_effect=[fake_new, fake_list_fail],
    ):
        info = spawn_tmux_teammate("demo", "alice", "/tmp/work")
    assert info.pane_id == "demo-alice"


# 验证 send_keys_to_pane 调用 tmux send-keys -t <pane_id> <text> Enter。
# mock subprocess.run，断言调用参数。
def test_send_keys_to_pane() -> None:
    with patch(
        "seacode.teams.spawn_tmux.subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        send_keys_to_pane("%1", "ls -la")
    args = mock_run.call_args[0][0]
    assert args[0] == "tmux"
    assert "send-keys" in args
    assert "-t" in args
    assert "%1" in args
    assert "ls -la" in args
    assert "Enter" in args


# 验证 kill_pane 先发 C-c 再 kill-window；异常只记 warning 不传播。
# mock subprocess.run 第一次正常、第二次抛异常，断言不传播。
def test_kill_pane_order_and_swallows_exception() -> None:
    with patch(
        "seacode.teams.spawn_tmux.subprocess.run"
    ) as mock_run:
        # 第一次 C-c 正常，第二次 kill-window 抛 OSError。
        mock_run.side_effect = [MagicMock(returncode=0), OSError("boom")]
        # 不应抛异常。
        kill_pane("%1")

    assert mock_run.call_count == 2
    first_args = mock_run.call_args_list[0][0][0]
    second_args = mock_run.call_args_list[1][0][0]
    assert "C-c" in first_args
    assert "kill-window" in second_args


# 验证 kill_pane 在 C-c 失败时仍尝试 kill-window。
# mock subprocess.run 两次都抛异常，断言不传播且调用 2 次。
def test_kill_pane_both_fail() -> None:
    with patch(
        "seacode.teams.spawn_tmux.subprocess.run"
    ) as mock_run:
        mock_run.side_effect = [OSError("a"), OSError("b")]
        kill_pane("%1")
    assert mock_run.call_count == 2
