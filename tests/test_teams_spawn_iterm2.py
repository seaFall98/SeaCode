"""teams/spawn_iterm2.py 单测：spawn / kill_pane 全分支。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from seacode.teams.spawn_iterm2 import (
    ITermPaneInfo,
    kill_pane,
    spawn_iterm2_teammate,
)


# 验证 spawn_iterm2_teammate 调用 osascript -e 含 create tab / set name / write text。
# mock subprocess.run 返回 session id，断言脚本内容与返回值。
def test_spawn_iterm2_teammate_success() -> None:
    with patch(
        "seacode.teams.spawn_iterm2.subprocess.run"
    ) as mock_run:
        # 第一次 create tab + write text，第二次 get id。
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="session-42\n", stderr=""),
        ]
        info = spawn_iterm2_teammate(
            "demo", "alice", "/tmp/work", "/project/.seacode/teams"
        )

    assert isinstance(info, ITermPaneInfo)
    assert info.tab_id == "demo-alice"
    assert info.session_id == "session-42"
    # 第一次调用的脚本含关键 AppleScript 指令。
    first_script = mock_run.call_args_list[0][0][0]
    assert first_script[0] == "osascript"
    assert first_script[1] == "-e"
    script_text = first_script[2]
    assert "create tab" in script_text
    assert "set name" in script_text
    assert "write text" in script_text
    assert "demo-alice" in script_text
    assert "-m seacode" in script_text
    assert "--teams-root /project/.seacode/teams" in script_text
    # 第二次调用获取 session id。
    second_script = mock_run.call_args_list[1][0][0]
    assert "get id of current session" in second_script[2]


# 验证 spawn_iterm2_teammate 在 osascript 失败时抛 RuntimeError。
# mock subprocess.run 返回 returncode=1，断言抛错。
def test_spawn_iterm2_teammate_failure() -> None:
    with patch(
        "seacode.teams.spawn_iterm2.subprocess.run",
        return_value=MagicMock(returncode=1, stdout="", stderr="osascript error"),
    ):
        with pytest.raises(RuntimeError, match="osascript failed"):
            spawn_iterm2_teammate(
                "demo", "alice", "/tmp/work", "/project/.seacode/teams"
            )


# 验证 kill_pane 调用 osascript 含 close 指令。
# mock subprocess.run，断言脚本含 close 与 tab_id。
def test_kill_pane_calls_close() -> None:
    pane_info = ITermPaneInfo(tab_id="demo-alice", session_id="s1")
    with patch(
        "seacode.teams.spawn_iterm2.subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        kill_pane(pane_info)
    args = mock_run.call_args[0][0]
    assert args[0] == "osascript"
    script_text = args[2]
    assert "close" in script_text
    assert "demo-alice" in script_text


# 验证 kill_pane 异常只记 warning 不传播。
# mock subprocess.run 抛 OSError，断言不抛错。
def test_kill_pane_swallows_exception() -> None:
    pane_info = ITermPaneInfo(tab_id="demo-alice", session_id="s1")
    with patch(
        "seacode.teams.spawn_iterm2.subprocess.run",
        side_effect=OSError("boom"),
    ):
        # 不应抛异常。
        kill_pane(pane_info)
