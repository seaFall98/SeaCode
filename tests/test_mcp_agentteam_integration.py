"""MCP wrapper 与 AgentTeam 工具筛选、ToolSearch 的真实生产路径回归。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from mcp import types as mcp_types

from seacode.agents.parser import AgentDef
from seacode.agents.tool_filter import (
    apply_coordinator_filter,
    build_teammate_tools,
    resolve_agent_tools,
)
from seacode.mcp.tool_wrapper import MCPToolWrapper
from seacode.teams.models import BackendType
from seacode.tools import ToolRegistry
from seacode.tools.tool_search import ToolSearchParams, ToolSearchTool


class _MCPClientStub:
    """MCPToolWrapper 初始化所需的最小 client 替身；本测试不执行远端调用。"""

    is_alive = True


def _make_wrapper() -> MCPToolWrapper:
    tool_def = mcp_types.Tool(
        name="get-tickets",
        description="查询车票",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(
        "12306-mcp",
        tool_def,
        cast(Any, _MCPClientStub()),
    )


def _make_agent_definition() -> AgentDef:
    return AgentDef(
        agent_type="test",
        when_to_use="test",
        system_prompt="test",
        source="builtin",
    )


# 验证真实 MCP wrapper 的公开名称与 AgentTeam 的统一命名契约一致。
# 直接构造生产 wrapper，不用只带相同字符串的假 Tool 代替。
@pytest.mark.asyncio
async def test_mcp_wrapper_survives_agentteam_filters_and_exact_search() -> None:
    wrapper = _make_wrapper()
    expected_name = "mcp__12306-mcp__get-tickets"
    assert wrapper.name == expected_name
    assert wrapper.mcp_tool_name == "get-tickets"

    parent = ToolRegistry()
    parent.register(wrapper)

    resolved = resolve_agent_tools(parent, _make_agent_definition(), is_background=True)
    assert resolved.get(expected_name) is wrapper

    coordinator = apply_coordinator_filter(parent)
    assert coordinator.get(expected_name) is wrapper

    teammate = build_teammate_tools(
        parent,
        team_manager=None,
        team_name="demo",
        agent_id="agent-1",
        agent_name="worker",
        backend_type=BackendType.IN_PROCESS,
    )
    assert teammate.get(expected_name) is wrapper

    search = ToolSearchTool(parent, protocol="anthropic")
    result = await search.execute(ToolSearchParams(query=f"select:{expected_name}"))
    assert "Found 1 tool(s)" in result.content
    assert parent.is_discovered(expected_name)
