"""tools/team_delete.py 单测：正常删除、Coordinator 恢复、活跃成员错误。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from seacode.tools.team_delete import TeamDeleteParams, TeamDeleteTool


# 验证 TeamDelete 正常删除返回确认。
# fake team_manager.delete_team 成功，断言 ToolResult 含删除确认。
@pytest.mark.asyncio
async def test_team_delete_success() -> None:
    fake_mgr = MagicMock()
    fake_mgr.delete_team = AsyncMock()
    fake_mgr.list_teams.return_value = ["other-team"]
    fake_agent = MagicMock()
    fake_agent.coordinator_mode = False

    tool = TeamDeleteTool(fake_agent, fake_mgr)
    params = TeamDeleteParams(team_name="demo")
    result = await tool.execute(params)

    assert not result.is_error
    assert "demo" in result.content
    assert "已删除" in result.content


# 验证 coordinator_mode=True 且无剩余团队时恢复全量注册表。
# fake list_teams 返回空，断言 _full_registry 恢复、coordinator_mode=False。
@pytest.mark.asyncio
async def test_team_delete_restores_full_registry() -> None:
    fake_mgr = MagicMock()
    fake_mgr.delete_team = AsyncMock()
    fake_mgr.list_teams.return_value = []
    full_registry = MagicMock()
    fake_agent = MagicMock()
    fake_agent.coordinator_mode = True
    fake_agent._full_registry = full_registry
    filtered_registry = MagicMock()
    fake_agent.registry = filtered_registry

    tool = TeamDeleteTool(fake_agent, fake_mgr)
    params = TeamDeleteParams(team_name="demo")
    result = await tool.execute(params)

    assert not result.is_error
    assert "工具集恢复全量" in result.content
    assert fake_agent.registry is full_registry
    assert fake_agent._full_registry is None
    assert fake_agent.coordinator_mode is False


# 验证多团队共存时第二个 TeamDelete 不恢复。
# fake list_teams 返回非空，断言不恢复。
@pytest.mark.asyncio
async def test_team_delete_no_restore_with_remaining_teams() -> None:
    fake_mgr = MagicMock()
    fake_mgr.delete_team = AsyncMock()
    fake_mgr.list_teams.return_value = ["other-team"]
    full_registry = MagicMock()
    fake_agent = MagicMock()
    fake_agent.coordinator_mode = True
    fake_agent._full_registry = full_registry
    filtered = MagicMock()
    fake_agent.registry = filtered

    tool = TeamDeleteTool(fake_agent, fake_mgr)
    params = TeamDeleteParams(team_name="demo")
    result = await tool.execute(params)

    assert not result.is_error
    assert "工具集恢复全量" not in result.content
    # registry 不变。
    assert fake_agent.registry is filtered
    assert fake_agent.coordinator_mode is True


# 验证活跃成员存在时 TeamError 返回 is_error=True。
@pytest.mark.asyncio
async def test_team_delete_active_members_error() -> None:
    from seacode.teams.manager import TeamError

    fake_mgr = MagicMock()
    fake_mgr.delete_team = AsyncMock(side_effect=TeamError("has active members"))
    fake_agent = MagicMock()
    fake_agent.coordinator_mode = False

    tool = TeamDeleteTool(fake_agent, fake_mgr)
    params = TeamDeleteParams(team_name="demo")
    result = await tool.execute(params)

    assert result.is_error
    assert "active members" in result.content
