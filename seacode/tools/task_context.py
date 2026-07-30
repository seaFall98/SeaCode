"""共享任务工具的团队与身份解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from seacode.teams.spawn_inprocess import LEAD_NAME

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


@dataclass(frozen=True)
class TaskContext:
    """一次任务板操作解析出的团队与创建者身份。"""

    team_name: str
    agent_name: str


def resolve_task_context(
    team_manager: TeamManager,
    configured_team_name: str,
    configured_agent_name: str,
    parent_agent: Any,
    requested_team_name: str | None,
) -> tuple[TaskContext | None, str | None]:
    """解析 Lead 的动态团队上下文或 teammate 的固定团队上下文。"""
    if parent_agent is not None:
        lead_agent_id = getattr(parent_agent, "agent_id", "")
        if not isinstance(lead_agent_id, str) or not lead_agent_id:
            return None, "Lead 身份未初始化，无法操作共享任务板"
        agent_name = getattr(parent_agent, "agent_name", "")
        if not isinstance(agent_name, str) or not agent_name:
            agent_name = LEAD_NAME

        if requested_team_name:
            team = team_manager.get_team(requested_team_name)
            if team is None:
                return None, f"团队不存在: {requested_team_name}"
            if team.lead_agent_id != lead_agent_id:
                return None, f"当前 Lead 不属于团队: {requested_team_name}"
            return TaskContext(team.name, agent_name), None

        teams = team_manager.get_teams_for_lead(lead_agent_id)
        if not teams:
            return None, "当前 Lead 没有可操作的团队任务板"
        if len(teams) > 1:
            return (
                None,
                "当前 Lead 有多个团队，请在任务工具中提供 team_name",
            )
        return TaskContext(teams[0].name, agent_name), None

    if (
        requested_team_name
        and configured_team_name
        and requested_team_name != configured_team_name
    ):
        return (
            None,
            f"当前成员只能操作所属团队任务板: {configured_team_name}",
        )
    team_name = requested_team_name or configured_team_name
    if not team_name:
        return None, "任务工具缺少团队上下文"
    if team_manager.get_team(team_name) is None:
        return None, f"团队不存在: {team_name}"
    return TaskContext(team_name, configured_agent_name), None
