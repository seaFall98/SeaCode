# iTerm2 后端：通过 AppleScript 在新标签页中 spawn teammate worker 进程。
"""teams 子包的 iTerm2 spawn 实现。"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from seacode.teams.spawn import build_teammate_cli

log = logging.getLogger(__name__)


@dataclass
class ITermPaneInfo:
    # iTerm2 新标签页的运行时元数据；tab_id 用于后续 kill。
    tab_id: str
    session_id: str


# 执行 AppleScript；失败抛 RuntimeError。
def _run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr}")
    return result.stdout.strip()


# 在 iTerm2 新标签页中启动 teammate worker；失败抛 RuntimeError。
def spawn_iterm2_teammate(
    team_name: str, member_name: str, workdir: str, teams_root: str
) -> ITermPaneInfo:
    window_name = f"{team_name}-{member_name}"
    cli = build_teammate_cli(team_name, member_name, workdir, teams_root)
    # 转义 CLI 中的双引号，避免破坏 AppleScript 字符串。
    cli_escaped = cli.replace('"', '\\"')
    script = (
        'tell application "iTerm"\n'
        "  create tab with default profile\n"
        f'  set name of current session of current tab of current window to "{window_name}"\n'
        f'  write text of current session of current tab of current window "{cli_escaped}"\n'
        "end tell"
    )
    _run_osascript(script)
    # 获取当前 session id 用于后续引用。
    id_script = (
        'tell application "iTerm" to get id of current session '
        "of current tab of current window"
    )
    session_id = _run_osascript(id_script)
    return ITermPaneInfo(tab_id=window_name, session_id=session_id)


# 关闭 iTerm2 标签页：遍历窗口查找匹配 tab_id 并关闭；异常只记 warning 不传播。
def kill_pane(pane_info: ITermPaneInfo) -> None:
    tab_id_escaped = pane_info.tab_id.replace('"', '\\"')
    script = (
        'tell application "iTerm"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        f'      if name of t is "{tab_id_escaped}" then\n'
        "        close t\n"
        "      end if\n"
        "    end repeat\n"
        "  end repeat\n"
        "end tell"
    )
    try:
        _run_osascript(script)
    except Exception as e:
        log.warning("failed to close iTerm2 tab %s: %s", pane_info.tab_id, e)
