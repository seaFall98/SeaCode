# TaskList 工具：列出团队共享任务板上的任务，支持按状态/负责人过滤。
"""TaskList 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from seacode.teams.shared_task import TaskStoreError
from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


class TaskListParams(BaseModel):
    # status 取 pending/in_progress/completed/blocked；assignee 按负责人过滤；均为空时列出全部。
    status: str | None = None
    assignee: str | None = None


class TaskListTool(Tool):
    # 共享任务列表工具；按状态图标展示任务及依赖摘要。
    name = "TaskList"
    description = (
        "列出团队共享任务板上的任务，"
        "可按状态 (pending/in_progress/completed/blocked) 或负责人过滤"
    )
    params_model = TaskListParams
    category = ToolCategory.READ
    is_concurrency_safe = True

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TaskListParams = params  # type: ignore[assignment]

        store = self._team_manager.get_task_store(self._team_name)
        if store is None:
            return ToolResult(
                content=f"任务板未找到: {self._team_name}", is_error=True
            )
        try:
            tasks = store.list_tasks(
                status=tool_params.status, assignee=tool_params.assignee
            )
        except TaskStoreError as e:
            return ToolResult(content=f"任务板读取失败: {e}", is_error=True)

        if not tasks:
            filters: list[str] = []
            if tool_params.status:
                filters.append(f"status={tool_params.status}")
            if tool_params.assignee:
                filters.append(f"assignee={tool_params.assignee}")
            filter_str = f" (过滤: {', '.join(filters)})" if filters else ""
            return ToolResult(content=f"未找到任务{filter_str}", is_error=False)

        status_icons = {
            "pending": "○",
            "in_progress": "◐",
            "completed": "●",
            "blocked": "✕",
        }

        lines = [f"任务 (共 {len(tasks)} 条):"]
        for t in tasks:
            icon = status_icons.get(t.status, "?")
            assignee = f" [{t.assignee}]" if t.assignee else ""
            deps = ""
            if t.blocked_by:
                deps = f" (被阻塞: {', '.join(t.blocked_by)})"
            lines.append(f"  {icon} [{t.id}] {t.title}{assignee}{deps}")

        return ToolResult(content="\n".join(lines), is_error=False)
