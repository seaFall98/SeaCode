# TaskGet 工具：按 ID 查询共享任务详情，含依赖信息。
"""TaskGet 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


class TaskGetParams(BaseModel):
    # 目标任务 ID。
    task_id: str


class TaskGetTool(Tool):
    # 共享任务查询工具；返回任务全部字段与依赖关系。
    name = "TaskGet"
    description = "按 ID 查询共享任务详情，含依赖信息"
    params_model = TaskGetParams
    category = ToolCategory.READ
    is_concurrency_safe = True

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TaskGetParams = params  # type: ignore[assignment]

        store = self._team_manager.get_task_store(self._team_name)
        if store is None:
            return ToolResult(
                content=f"任务板未找到: {self._team_name}", is_error=True
            )
        task = store.get(tool_params.task_id)
        if task is None:
            return ToolResult(
                content=f"任务 '{tool_params.task_id}' 不存在", is_error=True
            )

        lines = [
            f"任务 {task.id}:",
            f"  标题:     {task.title}",
            f"  状态:     {task.status}",
            f"  负责人:   {task.assignee or '(未分配)'}",
            f"  创建者:   {task.created_by or '(未知)'}",
        ]
        if task.description:
            lines.append(f"  描述:     {task.description}")
        if task.blocks:
            lines.append(f"  阻塞:     {', '.join(task.blocks)}")
        if task.blocked_by:
            lines.append(f"  被阻塞:   {', '.join(task.blocked_by)}")

        return ToolResult(content="\n".join(lines), is_error=False)
