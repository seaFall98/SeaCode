# tmux 后端：在 tmux 新窗口中 spawn teammate worker 进程。
"""teams 子包的 tmux spawn 实现。"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from seacode.teams.spawn import build_teammate_cli

log = logging.getLogger(__name__)


@dataclass
class TmuxPaneInfo:
    # tmux 新窗口的运行时元数据；pane_id 用于后续 send-keys / kill。
    pane_id: str
    window_name: str


# 执行 tmux 子命令；统一 capture_output 与 timeout。
def _run_tmux(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux"] + args, capture_output=True, text=True, timeout=10
    )


# 在 tmux 新窗口中启动 teammate worker；失败抛 RuntimeError。
def spawn_tmux_teammate(
    team_name: str, member_name: str, workdir: str
) -> TmuxPaneInfo:
    window_name = f"{team_name}-{member_name}"
    cli = build_teammate_cli(team_name, member_name, workdir)
    result = _run_tmux(["new-window", "-d", "-n", window_name, cli])
    if result.returncode != 0:
        raise RuntimeError(f"tmux new-window failed: {result.stderr}")
    # 解析 pane_id：list-panes -t <window> -F '#{pane_id}' 取第一行。
    list_result = _run_tmux(
        ["list-panes", "-t", window_name, "-F", "#{pane_id}"]
    )
    if list_result.returncode == 0 and list_result.stdout.strip():
        pane_id = list_result.stdout.strip().splitlines()[0]
    else:
        # 回退用 window_name 作为 pane_id 引用。
        pane_id = window_name
    return TmuxPaneInfo(pane_id=pane_id, window_name=window_name)


# 向指定 pane 发送按键序列；末尾自动按 Enter。
def send_keys_to_pane(pane_id: str, text: str) -> None:
    _run_tmux(["send-keys", "-t", pane_id, text, "Enter"])


# 关闭 pane：先发 C-c 中断，再 kill-window；异常只记 warning 不传播。
def kill_pane(pane_id: str) -> None:
    try:
        _run_tmux(["send-keys", "-t", pane_id, "C-c"])
    except Exception as e:
        log.warning("failed to send C-c to %s: %s", pane_id, e)
    try:
        _run_tmux(["kill-window", "-t", pane_id])
    except Exception as e:
        log.warning("failed to kill-window %s: %s", pane_id, e)
