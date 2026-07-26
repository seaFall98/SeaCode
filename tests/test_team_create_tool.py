"""tools/team_create.py 单测：正常创建、Coordinator 激活、异常处理。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seacode.tools.team_create import TeamCreateParams, TeamCreateTool


# 验证 TeamCreate 正常创建返回团队信息 + backend + config 路径。
# fake team_manager.create_team 返回 AgentTeam，断言 ToolResult 含关键字段。
@pytest.mark.asyncio
async def test_team_create_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.coordinator_mode = False
    fake_config = MagicMock()
    fake_config.teammate_mode = "in-process"
    fake_config.enable_coordinator_mode = False

    tool = TeamCreateTool(fake_agent, mgr, fake_config)
    params = TeamCreateParams(team_name="demo", description="test")
    result = await tool.execute(params)

    assert not result.is_error
    assert "demo" in result.content
    assert "backend:" in result.content
    assert "config:" in result.content
    # coordinator_mode 未启用，不应激活。
    assert not hasattr(fake_agent, "coordinator_mode") or fake_agent.coordinator_mode is False


# 验证 enable_coordinator_mode=True 时激活 Coordinator：
# 保存 _full_registry、apply_coordinator_filter、coordinator_mode=True。
# mock apply_coordinator_filter，断言调用与字段设置。
@pytest.mark.asyncio
async def test_team_create_activates_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    fake_full_registry = MagicMock()
    fake_filtered_registry = MagicMock()
    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.coordinator_mode = False
    fake_agent.registry = fake_full_registry
    fake_config = MagicMock()
    fake_config.teammate_mode = "in-process"
    fake_config.enable_coordinator_mode = True

    with patch(
        "seacode.tools.team_create.apply_coordinator_filter",
        return_value=fake_filtered_registry,
    ) as mock_filter:
        tool = TeamCreateTool(fake_agent, mgr, fake_config)
        params = TeamCreateParams(team_name="demo")
        result = await tool.execute(params)

    assert not result.is_error
    assert "coordinator mode 已激活" in result.content
    # _full_registry 保存原 registry。
    assert fake_agent._full_registry is fake_full_registry
    # registry 替换为过滤后。
    assert fake_agent.registry is fake_filtered_registry
    # coordinator_mode 设为 True。
    assert fake_agent.coordinator_mode is True
    mock_filter.assert_called_once_with(fake_full_registry)


# 验证重复 TeamCreate 不覆盖快照：第一次激活后 _full_registry 非 None；
# 第二次 coordinator_mode 已 True 不保存。
@pytest.mark.asyncio
async def test_team_create_no_double_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from seacode.teams.manager import TeamManager

    mgr = TeamManager()
    original_registry = MagicMock()
    fake_filtered = MagicMock()
    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_agent.coordinator_mode = True  # 已激活
    fake_agent.registry = fake_filtered  # 已是过滤后
    fake_agent._full_registry = original_registry  # 已有快照
    fake_config = MagicMock()
    fake_config.teammate_mode = "in-process"
    fake_config.enable_coordinator_mode = True

    with patch(
        "seacode.tools.team_create.apply_coordinator_filter"
    ) as mock_filter:
        tool = TeamCreateTool(fake_agent, mgr, fake_config)
        params = TeamCreateParams(team_name="demo2")
        result = await tool.execute(params)

    assert not result.is_error
    # 不应再次调用 apply_coordinator_filter。
    mock_filter.assert_not_called()
    # _full_registry 不被覆盖。
    assert fake_agent._full_registry is original_registry


# 验证 create_team 异常返回 is_error=True。
@pytest.mark.asyncio
async def test_team_create_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mgr = MagicMock()
    fake_mgr.create_team = AsyncMock(side_effect=RuntimeError("boom"))
    fake_mgr.detect_backend.return_value = MagicMock(value="in-process")
    fake_agent = MagicMock()
    fake_agent.agent_id = "lead-1"
    fake_config = MagicMock()
    fake_config.teammate_mode = "in-process"
    fake_config.enable_coordinator_mode = False

    tool = TeamCreateTool(fake_agent, fake_mgr, fake_config)
    params = TeamCreateParams(team_name="demo")
    result = await tool.execute(params)

    assert result.is_error
    assert "创建团队失败" in result.content


# 验证 TeamCreateTool.description 含团队工作流关键提示，让模型能正确使用。
# 直接断言 description 包含 team_name / name / SendMessage / idle 等关键字。
def test_team_create_description_covers_workflow() -> None:
    desc = TeamCreateTool.description
    assert "team_name" in desc
    assert "name" in desc
    assert "SendMessage" in desc
    assert "idle" in desc
    # 必须明确告知不传 team_name 走一次性子 Agent 路径。
    assert "不会成为团队成员" in desc
