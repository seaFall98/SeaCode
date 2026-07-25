# 后端检测：按 env / 平台 / 交互模式确定 teammate spawn 后端。
"""teams 子包的后端检测函数。"""

from __future__ import annotations

import os
import sys

from seacode.teams.models import BackendType


# 按环境变量检测当前所处会话后端：TMUX > ITERM2 > IN_PROCESS。
def detect_backend_from_env() -> BackendType:
    if os.environ.get("TMUX"):
        return BackendType.TMUX
    if os.environ.get("ITERM_SESSION_ID"):
        return BackendType.ITERM2
    return BackendType.IN_PROCESS


# 按 teammate_mode / 交互性 / 平台优先级检测 spawn 后端。
# in-process 模式或非交互场景固定 IN_PROCESS；Windows 护栏固定 IN_PROCESS；
# 其它场景按 env 检测。
def detect_backend(teammate_mode: str, is_interactive: bool) -> BackendType:
    if teammate_mode == "in-process" or not is_interactive:
        return BackendType.IN_PROCESS
    if sys.platform == "win32":
        # Windows 上 tmux/iTerm2 不可用；强制 in-process 避免后续 spawn 失败。
        return BackendType.IN_PROCESS
    return detect_backend_from_env()


# 检测当前 pane 是否处于 tmux/iTerm2 会话；仅在已身处会话时返回对应后端。
# in-process 模式、非交互、Windows、env 为 IN_PROCESS 时均返回 None。
def detect_pane_backend(
    teammate_mode: str, is_interactive: bool
) -> BackendType | None:
    if teammate_mode == "in-process" or not is_interactive:
        return None
    if sys.platform == "win32":
        return None
    env_backend = detect_backend_from_env()
    if env_backend == BackendType.IN_PROCESS:
        # 未身处 tmux/iTerm2 会话；不能在 pane 中 spawn。
        return None
    return env_backend
