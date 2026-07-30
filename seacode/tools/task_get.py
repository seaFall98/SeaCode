# TaskGet 工具：按 ID 查询共享任务详情，含依赖信息。
"""TaskGet 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from seacode.teams.shared_task import TaskStoreError
from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.tools.task_context import resolve_task_context

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


class TaskGetParams(BaseModel):
    # 目标任务 ID。
    task_id: str
    # Lead 多团队时显式选择目标任务板。
    team_name: str | None = None


class TaskGetTool(Tool):
    # 共享任务查询工具；teammate 绑定团队，Lead 在执行时解析团队上下文。
    name = "TaskGet"
    description = "按 ID 查询共享任务详情，含依赖信息；Lead 多团队时用 team_name"
    params_model = TaskGetParams
    category = ToolCategory.READ
    is_concurrency_safe = True

    def __init__(
        self,
        team_manager: TeamManager,
        team_name: str = "",
        parent_agent: Any = None,
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._parent_agent = parent_agent

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TaskGetParams = params  # type: ignore[assignment]

        context, context_error = resolve_task_context(
            self._team_manager,
            self._team_name,
            "",
            self._parent_agent,
            tool_params.team_name,
        )
        if context_error is not None:
            return ToolResult(content=context_error, is_error=True)
        assert context is not None
        store = self._team_manager.get_task_store(context.team_name)
        try:
            task = store.get(tool_params.task_id)
        except TaskStoreError as e:
            return ToolResult(content=f"任务板读取失败: {e}", is_error=True)
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
