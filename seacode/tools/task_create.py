# TaskCreate 工具：在团队共享任务板上创建任务，支持 blocks/blocked_by 依赖跟踪。
"""TaskCreate 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from seacode.teams.shared_task import TaskStoreError
from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.tools.task_context import resolve_task_context

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager


class TaskCreateParams(BaseModel):
    # title 必填；assignee/blocks/blocked_by 可选，用于依赖跟踪。
    title: str
    description: str = ""
    assignee: str = ""
    blocks: list[str] | None = None
    blocked_by: list[str] | None = None
    # Lead 多团队时显式选择目标任务板；teammate 通常省略并使用固定团队。
    team_name: str | None = None


class TaskCreateTool(Tool):
    # 共享任务创建工具；teammate 绑定团队，Lead 在执行时解析团队上下文。
    name = "TaskCreate"
    description = (
        "在团队共享任务板上创建任务，支持 blocks/blocked_by 依赖跟踪；"
        "Lead 多团队时用 team_name 选择任务板"
    )
    params_model = TaskCreateParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = True

    def __init__(
        self,
        team_manager: TeamManager,
        team_name: str = "",
        agent_name: str = "",
        parent_agent: Any = None,
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._agent_name = agent_name
        self._parent_agent = parent_agent

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TaskCreateParams = params  # type: ignore[assignment]

        context, context_error = resolve_task_context(
            self._team_manager,
            self._team_name,
            self._agent_name,
            self._parent_agent,
            tool_params.team_name,
        )
        if context_error is not None:
            return ToolResult(content=context_error, is_error=True)
        assert context is not None
        store = self._team_manager.get_task_store(context.team_name)
        try:
            task = store.create(
                title=tool_params.title,
                description=tool_params.description,
                assignee=tool_params.assignee,
                blocks=tool_params.blocks,
                blocked_by=tool_params.blocked_by,
                created_by=context.agent_name,
            )
        except TaskStoreError as e:
            return ToolResult(content=f"任务板操作失败: {e}", is_error=True)
        return ToolResult(
            content=(
                f"任务已创建:\n"
                f"  ID: {task.id}\n"
                f"  标题: {task.title}\n"
                f"  状态: {task.status}\n"
                f"  负责人: {task.assignee or '(未分配)'}"
            ),
            is_error=False,
        )
