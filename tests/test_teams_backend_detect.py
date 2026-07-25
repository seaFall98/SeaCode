"""teams/backend_detect.py 单测：env / 平台 / 交互模式三级优先级检测。"""

from __future__ import annotations

import pytest

from seacode.teams.backend_detect import (
    detect_backend,
    detect_backend_from_env,
    detect_pane_backend,
)
from seacode.teams.models import BackendType


# 验证 detect_backend_from_env 按 TMUX > ITERM2 > IN_PROCESS 优先级返回。
# 用 monkeypatch 分别设置/清除 TMUX 与 ITERM_SESSION_ID，覆盖三个分支。
def test_detect_backend_from_env_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert detect_backend_from_env() == BackendType.IN_PROCESS

    monkeypatch.setenv("ITERM_SESSION_ID", "session-1")
    assert detect_backend_from_env() == BackendType.ITERM2

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    assert detect_backend_from_env() == BackendType.TMUX


# 验证 detect_backend 在 in-process 模式 / 非交互 / Windows 上强制 IN_PROCESS。
# 用 monkeypatch 模拟 sys.platform 与 env，覆盖四条分支。
def test_detect_backend_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    # in-process 模式优先级最高，覆盖 env 与平台。
    monkeypatch.setenv("TMUX", "/tmp/tmux")
    assert detect_backend("in-process", True) == BackendType.IN_PROCESS

    # 非交互场景固定 IN_PROCESS。
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert detect_backend("tmux", False) == BackendType.IN_PROCESS

    # Windows 平台护栏：即便 TMUX 设置也强制 IN_PROCESS。
    monkeypatch.setenv("TMUX", "/tmp/tmux")
    monkeypatch.setattr("seacode.teams.backend_detect.sys.platform", "win32")
    assert detect_backend("tmux", True) == BackendType.IN_PROCESS

    # Linux + TMUX env + 交互 + tmux 模式 → TMUX。
    monkeypatch.setattr("seacode.teams.backend_detect.sys.platform", "linux")
    assert detect_backend("tmux", True) == BackendType.TMUX


# 验证 detect_pane_backend 仅在身处 tmux/iTerm2 会话时返回对应后端，否则 None。
# 用 monkeypatch 覆盖 in-process 模式、Windows、env IN_PROCESS、env TMUX 四条分支。
def test_detect_pane_backend_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux")

    # in-process 模式 → None（不能在 pane 中 spawn）。
    assert detect_pane_backend("in-process", True) is None

    # 非交互 → None。
    assert detect_pane_backend("tmux", False) is None

    # Windows → None。
    monkeypatch.setattr("seacode.teams.backend_detect.sys.platform", "win32")
    assert detect_pane_backend("tmux", True) is None

    # Linux + env IN_PROCESS → None（未身处会话）。
    monkeypatch.setattr("seacode.teams.backend_detect.sys.platform", "linux")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert detect_pane_backend("tmux", True) is None

    # Linux + env TMUX → TMUX（已身处会话）。
    monkeypatch.setenv("TMUX", "/tmp/tmux")
    assert detect_pane_backend("tmux", True) == BackendType.TMUX

    # Linux + env ITERM2 → ITERM2。
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("ITERM_SESSION_ID", "session-1")
    assert detect_pane_backend("iterm2", True) == BackendType.ITERM2
