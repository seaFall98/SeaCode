"""__main__.py teammate 入口单测：_parse_teammate_flags 与 _run_teammate 错误路径。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from seacode.__main__ import _parse_teammate_flags, _run_teammate

# ---------------------------------------------------------------------------
# _parse_teammate_flags
# ---------------------------------------------------------------------------


# 验证 _parse_teammate_flags 无任何标志时返回 (False, "", "")。
# 传入空 argv，断言三个字段都为默认值。
def test_parse_teammate_flags_no_flags() -> None:
    is_teammate, team_name, agent_name = _parse_teammate_flags([])
    assert is_teammate is False
    assert team_name == ""
    assert agent_name == ""


# 验证 _parse_teammate_flags 仅 --teammate 时返回 (True, "", "")。
# 缺 --team-name / --agent-name 时对应字段为空。
def test_parse_teammate_flags_only_teammate() -> None:
    is_teammate, team_name, agent_name = _parse_teammate_flags(["--teammate"])
    assert is_teammate is True
    assert team_name == ""
    assert agent_name == ""


# 验证 _parse_teammate_flags 完整三标志时返回 (True, team, agent)。
def test_parse_teammate_flags_full() -> None:
    argv = ["--teammate", "--team-name", "demo", "--agent-name", "alice"]
    is_teammate, team_name, agent_name = _parse_teammate_flags(argv)
    assert is_teammate is True
    assert team_name == "demo"
    assert agent_name == "alice"


# 验证 _parse_teammate_flags 在 --team-name 为最后一个参数时不越界。
# --team-name 后无值，team_name 保持空。
def test_parse_teammate_flags_team_name_no_value() -> None:
    argv = ["--teammate", "--team-name"]
    is_teammate, team_name, agent_name = _parse_teammate_flags(argv)
    assert is_teammate is True
    assert team_name == ""
    assert agent_name == ""


# 验证 _parse_teammate_flags 在 --agent-name 为最后一个参数时不越界。
def test_parse_teammate_flags_agent_name_no_value() -> None:
    argv = ["--teammate", "--agent-name"]
    is_teammate, team_name, agent_name = _parse_teammate_flags(argv)
    assert is_teammate is True
    assert team_name == ""
    assert agent_name == ""


# 验证 _parse_teammate_flags 不受其它参数影响。
# 混入 sea / --foo 等参数，--teammate 相关字段仍正确解析。
def test_parse_teammate_flags_ignores_other_args() -> None:
    argv = ["sea", "--foo", "bar", "--teammate", "--team-name", "t1", "--agent-name", "a1"]
    is_teammate, team_name, agent_name = _parse_teammate_flags(argv)
    assert is_teammate is True
    assert team_name == "t1"
    assert agent_name == "a1"


# ---------------------------------------------------------------------------
# _run_teammate 错误路径
# ---------------------------------------------------------------------------


# 验证 _run_teammate 在 ConfigError 时提前返回，不构造 Agent。
# mock load_config 抛 ConfigError，断言后续 TeamManager 未被调用。
@pytest.mark.asyncio
async def test_run_teammate_config_error_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from seacode.config import ConfigError

    def fake_load_config() -> None:
        raise ConfigError("bad config")

    # 关键：patch load_config 在 seacode.__main__ 命名空间里的引用。
    monkeypatch.setattr("seacode.__main__.load_config", fake_load_config)

    # spy TeamManager 构造，确保未被调用。
    with patch("seacode.teams.manager.TeamManager") as mock_tm_cls:
        await _run_teammate("demo", "alice")

    mock_tm_cls.assert_not_called()


# 验证 _run_teammate 在无 Provider 时提前返回。
# mock load_config 返回空 providers，断言 TeamManager 未构造。
@pytest.mark.asyncio
async def test_run_teammate_no_providers_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config = MagicMock()
    fake_config.providers = ()
    monkeypatch.setattr("seacode.__main__.load_config", lambda: fake_config)

    with patch("seacode.teams.manager.TeamManager") as mock_tm_cls:
        await _run_teammate("demo", "alice")

    mock_tm_cls.assert_not_called()


# 验证 _run_teammate 在 create_client 失败时提前返回。
# mock create_client 抛异常，断言 TeamManager.get_team 未被调用。
@pytest.mark.asyncio
async def test_run_teammate_create_client_failure_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config = MagicMock()
    fake_provider = MagicMock()
    fake_provider.protocol = "anthropic"
    fake_config.providers = (fake_provider,)
    monkeypatch.setattr("seacode.__main__.load_config", lambda: fake_config)

    def fake_create_client(provider: MagicMock) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr("seacode.client.create_client", fake_create_client)

    with patch("seacode.teams.manager.TeamManager") as mock_tm_cls:
        await _run_teammate("demo", "alice")

    mock_tm_cls.assert_not_called()


# 验证 _run_teammate 在团队不存在时提前返回。
# mock TeamManager.get_team 返回 None，断言 Agent 未构造、spawn 未调用。
@pytest.mark.asyncio
async def test_run_teammate_team_not_found_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config = MagicMock()
    fake_provider = MagicMock()
    fake_provider.protocol = "anthropic"
    fake_config.providers = (fake_provider,)
    monkeypatch.setattr("seacode.__main__.load_config", lambda: fake_config)

    fake_client = MagicMock()
    monkeypatch.setattr("seacode.client.create_client", lambda p: fake_client)

    fake_manager = MagicMock()
    fake_manager.get_team.return_value = None
    monkeypatch.setattr(
        "seacode.teams.manager.TeamManager",
        lambda **kwargs: fake_manager,
    )

    with patch("seacode.agent.Agent") as mock_agent_cls, \
         patch("seacode.teams.spawn_inprocess.spawn_inprocess_teammate") as mock_spawn:
        await _run_teammate("missing", "alice")

    mock_agent_cls.assert_not_called()
    mock_spawn.assert_not_called()


# ---------------------------------------------------------------------------
# _TEAMMATE_ADDENDUM 常量
# ---------------------------------------------------------------------------


# 验证 _TEAMMATE_ADDENDUM 含 teammate 上下文关键提示。
# 断言包含 TEAMMATE CONTEXT、SendMessage、worktree 等关键短语。
def test_teammate_addendum_contains_key_phrases() -> None:
    from seacode.__main__ import _TEAMMATE_ADDENDUM

    assert "[TEAMMATE CONTEXT]" in _TEAMMATE_ADDENDUM
    assert "SendMessage" in _TEAMMATE_ADDENDUM
    assert "worktree" in _TEAMMATE_ADDENDUM
    assert "relative paths" in _TEAMMATE_ADDENDUM


# ---------------------------------------------------------------------------
# _read_max_steps
# ---------------------------------------------------------------------------


# 验证 _read_max_steps 在 SEA_MAX_STEPS 未设置时返回默认值 100。
def test_read_max_steps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEA_MAX_STEPS", raising=False)
    from seacode.__main__ import _DEFAULT_MAX_STEPS, _read_max_steps

    assert _read_max_steps() == _DEFAULT_MAX_STEPS == 100


# 验证 _read_max_steps 在 SEA_MAX_STEPS 为正整数时返回该值。
def test_read_max_steps_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEA_MAX_STEPS", "50")
    from seacode.__main__ import _read_max_steps

    assert _read_max_steps() == 50


# 验证 _read_max_steps 在 SEA_MAX_STEPS 为非数字时回退默认值。
def test_read_max_steps_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEA_MAX_STEPS", "abc")
    from seacode.__main__ import _DEFAULT_MAX_STEPS, _read_max_steps

    assert _read_max_steps() == _DEFAULT_MAX_STEPS


# 验证 _read_max_steps 在 SEA_MAX_STEPS 为非正整数时回退默认值。
def test_read_max_steps_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEA_MAX_STEPS", "0")
    from seacode.__main__ import _DEFAULT_MAX_STEPS, _read_max_steps

    assert _read_max_steps() == _DEFAULT_MAX_STEPS

    monkeypatch.setenv("SEA_MAX_STEPS", "-5")
    assert _read_max_steps() == _DEFAULT_MAX_STEPS
