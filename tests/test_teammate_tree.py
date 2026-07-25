# TeammateTree widget 渲染测试。
# 覆盖空列表、team-lead 顶行、连接符、状态着色、leader_tokens 格式化。
from __future__ import annotations

import pytest

from seacode.teammate_tree import TeammateTree, _format_tokens
from seacode.teams.progress import TeammateProgress


# 验证 _format_tokens 按阈值返回 k/M 缩写。
# 三档：原样 / 1.5k / 1.2M。
def test_format_tokens_thresholds() -> None:
    assert _format_tokens(0) == "0"
    assert _format_tokens(999) == "999"
    assert _format_tokens(1500) == "1.5k"
    assert _format_tokens(1_200_000) == "1.2M"


# 验证空 teammates 列表返回空 Text。
# 不含 team-lead 行，避免无 teammate 时 TUI 出现孤立标题。
def test_render_empty_returns_empty_text() -> None:
    tree = TeammateTree()
    tree.teammates = []
    rendered = tree.render()
    assert len(rendered) == 0


# 验证有 teammates 时渲染 team-lead 顶行 + 子节点行。
# 含 team-lead 名称、thinking 提示、@name 前缀与工具计数。
def test_render_with_teammates_draws_lead_and_members() -> None:
    tree = TeammateTree()
    tree.leader_tokens = 0
    p1 = TeammateProgress(name="alice", team_name="demo", status="running")
    p1.tool_use_count = 3
    tree.teammates = [p1]
    rendered = tree.render()
    text_str = str(rendered)
    assert "team-lead" in text_str
    assert "@alice" in text_str
    assert "3 tools" in text_str
    # 单个 teammate 用 └─ 连接符。
    assert "└─" in text_str


# 验证多 teammate 时连接符 ├─ 与 └─ 正确。
# 最后一个用 └─，其余用 ├─。
def test_render_multiple_teammates_branch_prefixes() -> None:
    tree = TeammateTree()
    p1 = TeammateProgress(name="alice", team_name="demo", status="running")
    p2 = TeammateProgress(name="bob", team_name="demo", status="running")
    p3 = TeammateProgress(name="carol", team_name="demo", status="running")
    tree.teammates = [p1, p2, p3]
    rendered = tree.render()
    text_str = str(rendered)
    # 前两个用 ├─，最后一个用 └─。
    assert text_str.count("├─") == 2
    assert text_str.count("└─") == 1


# 验证状态着色：completed 绿 / failed 红 / idle 灰 / stopped 黄。
# 通过 Spans 的 style 字段断言颜色映射。
def test_render_status_colors() -> None:
    tree = TeammateTree()
    completed = TeammateProgress(name="a", team_name="t", status="completed")
    failed = TeammateProgress(name="b", team_name="t", status="failed")
    idle = TeammateProgress(name="c", team_name="t", status="idle")
    stopped = TeammateProgress(name="d", team_name="t", status="stopped")
    tree.teammates = [completed, failed, idle, stopped]
    rendered = tree.render()
    # 从 spans 收集 style 字符串。
    styles = [str(span.style) for span in rendered.spans if span.style]
    styles_text = " ".join(styles)
    assert "green" in styles_text
    assert "red" in styles_text
    assert "dim" in styles_text
    assert "yellow" in styles_text


# 验证 running 状态显示 activity_summary 而非状态字符串。
# mock activity_summary 返回 "reading"，断言渲染含 cyan 着色与 "reading"。
def test_render_running_shows_activity_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = TeammateTree()
    p = TeammateProgress(name="alice", team_name="demo", status="running")
    monkeypatch.setattr(p, "activity_summary", lambda: "reading")
    tree.teammates = [p]
    rendered = tree.render()
    text_str = str(rendered)
    assert "reading" in text_str
    # activity_summary 走 cyan 着色分支。
    styles = [str(span.style) for span in rendered.spans if span.style]
    assert "cyan" in " ".join(styles)


# 验证 leader_tokens 按 k/M 格式化显示。
# 设置 leader_tokens=1500，渲染应含 "1.5k tokens"。
def test_render_leader_tokens_formatted() -> None:
    tree = TeammateTree()
    tree.leader_tokens = 1500
    p = TeammateProgress(name="alice", team_name="demo", status="running")
    tree.teammates = [p]
    rendered = tree.render()
    text_str = str(rendered)
    assert "1.5k tokens" in text_str
