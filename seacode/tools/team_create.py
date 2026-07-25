# TeamCreate 工具：创建长期团队并按配置激活 Coordinator 模式。
"""TeamCreate 工具实现。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from seacode.agents.tool_filter import apply_coordinator_filter
from seacode.teams.coordinator import is_coordinator_mode
from seacode.teams.manager import TeamManager
from seacode.tools.base import Tool, ToolCategory, ToolResult


class TeamCreateParams(BaseModel):
    # team_name 由 Lead 提供；description 可选。
    team_name: str
    description: str = ""


class TeamCreateTool(Tool):
    # 创建团队工具；enable_coordinator_mode=True 时同步收敛 Lead 工具集。
    name = "TeamCreate"
    description = "建立长期团队，Lead 可在团队中 spawn 长驻 teammate"
    params_model = TeamCreateParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = False

    def __init__(
        self,
        parent_agent: Any,
        team_manager: TeamManager,
        config: Any,
    ) -> None:
        self._parent_agent = parent_agent
        self._team_manager = team_manager
        self._config = config

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TeamCreateParams = params  # type: ignore[assignment]
        teammate_mode = getattr(self._config, "teammate_mode", "")
        is_interactive = True
        try:
            team = await self._team_manager.create_team(
                tool_params.team_name,
                self._parent_agent.agent_id,
                tool_params.description,
                teammate_mode,
                is_interactive,
            )
        except Exception as e:
            return ToolResult(content=f"创建团队失败: {e}", is_error=True)

        coordinator_note = ""
        if is_coordinator_mode(
            getattr(self._config, "enable_coordinator_mode", False)
        ) and not getattr(self._parent_agent, "coordinator_mode", False):
            # 保存全量注册表快照，收敛当前 registry 为调度-only 白名单。
            self._parent_agent._full_registry = self._parent_agent.registry
            self._parent_agent.registry = apply_coordinator_filter(
                self._parent_agent.registry
            )
            self._parent_agent.coordinator_mode = True
            coordinator_note = "\n(coordinator mode 已激活，工具集收敛为调度-only)"

        backend = self._team_manager.detect_backend(teammate_mode, is_interactive)
        return ToolResult(
            content=(
                f"团队 {team.name} 已创建\n"
                f"backend: {backend.value}\n"
                f"config: {team.config_path}{coordinator_note}"
            ),
            is_error=False,
        )
