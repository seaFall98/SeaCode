# TeamDelete 工具：删除团队并全链路清理；所有团队删除后恢复 Lead 全量工具集。
"""TeamDelete 工具实现。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from seacode.teams.manager import TeamError, TeamManager
from seacode.tools.base import Tool, ToolCategory, ToolResult


class TeamDeleteParams(BaseModel):
    # team_name 由 Lead 提供。
    team_name: str


class TeamDeleteTool(Tool):
    # 删除团队工具；全链路清理后若 Lead 无剩余团队则恢复全量注册表。
    name = "TeamDelete"
    description = (
        "删除团队：终止所有 pane 进程、移除 worktree、清理邮箱与团队目录。"
        "要求所有成员处于 idle 状态，否则需要先等待或手动收尾。"
    )
    params_model = TeamDeleteParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = False

    def __init__(self, parent_agent: Any, team_manager: TeamManager) -> None:
        self._parent_agent = parent_agent
        self._team_manager = team_manager

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TeamDeleteParams = params  # type: ignore[assignment]
        try:
            await self._team_manager.delete_team(tool_params.team_name)
        except TeamError as e:
            return ToolResult(content=str(e), is_error=True)

        coordinator_note = ""
        # Lead 处于 Coordinator 模式且无剩余团队时恢复全量工具集。
        if getattr(self._parent_agent, "coordinator_mode", False) and not \
                self._team_manager.list_teams():
            full = getattr(self._parent_agent, "_full_registry", None)
            if full is not None:
                self._parent_agent.registry = full
            self._parent_agent._full_registry = None
            self._parent_agent.coordinator_mode = False
            coordinator_note = "\n(所有团队已删除，工具集恢复全量)"

        return ToolResult(
            content=f"团队 {tool_params.team_name} 已删除{coordinator_note}",
            is_error=False,
        )
