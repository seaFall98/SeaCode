# TaskUpdate 工具：更新共享任务的状态、负责人、描述或依赖关系。
"""TaskUpdate 工具实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from seacode.teams.shared_task import TaskStoreError
from seacode.tools.base import Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from seacode.teams.manager import TeamManager

# 合法状态集合；非法值在执行前直接返回错误。
VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}


class TaskUpdateParams(BaseModel):
    # task_id 必填；status/assignee/description 为增量更新；add_blocks/add_blocked_by 追加依赖。
    task_id: str
    status: str | None = None
    assignee: str | None = None
    description: str | None = None
    add_blocks: list[str] | None = None
    add_blocked_by: list[str] | None = None


class TaskUpdateTool(Tool):
    # 共享任务更新工具；SharedTaskStore.update 处理基础字段，
    # add_blocks/add_blocked_by 走独立方法追加依赖。
    name = "TaskUpdate"
    description = (
        "更新共享任务的状态、负责人、描述或依赖；"
        "用 add_blocks/add_blocked_by 追加依赖关系"
    )
    params_model = TaskUpdateParams
    category = ToolCategory.COMMAND
    is_concurrency_safe = True

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: TaskUpdateParams = params  # type: ignore[assignment]

        if tool_params.status and tool_params.status not in VALID_STATUSES:
            return ToolResult(
                content=(
                    f"非法状态 '{tool_params.status}'，"
                    f"必须是 {', '.join(sorted(VALID_STATUSES))} 之一"
                ),
                is_error=True,
            )

        store = self._team_manager.get_task_store(self._team_name)
        if store is None:
            return ToolResult(
                content=f"任务板未找到: {self._team_name}", is_error=True
            )
        task_id = tool_params.task_id
        task = None

        try:
            # 基础字段（status/assignee/description）走 update；任一非 None 时调用。
            if (
                tool_params.status is not None
                or tool_params.assignee is not None
                or tool_params.description is not None
            ):
                task = store.update(
                    task_id=task_id,
                    status=tool_params.status,
                    assignee=tool_params.assignee,
                    description=tool_params.description,
                )
                if task is None:
                    return ToolResult(
                        content=f"任务 '{task_id}' 不存在", is_error=True
                    )

            # 追加 blocks 依赖；独立方法去重保证唯一。
            if tool_params.add_blocks:
                task = store.add_blocks(task_id, tool_params.add_blocks)
                if task is None:
                    return ToolResult(
                        content=f"任务 '{task_id}' 不存在", is_error=True
                    )

            # 追加 blocked_by 反向依赖；独立方法去重保证唯一。
            if tool_params.add_blocked_by:
                task = store.add_blocked_by(task_id, tool_params.add_blocked_by)
                if task is None:
                    return ToolResult(
                        content=f"任务 '{task_id}' 不存在", is_error=True
                    )

            # 未提供任何更新字段时，确认任务存在。
            if task is None:
                task = store.get(task_id)
                if task is None:
                    return ToolResult(
                        content=f"任务 '{task_id}' 不存在", is_error=True
                    )
        except TaskStoreError as e:
            return ToolResult(content=f"任务板操作失败: {e}", is_error=True)

        changes: list[str] = []
        if tool_params.status:
            changes.append(f"状态 → {tool_params.status}")
        if tool_params.assignee is not None:
            changes.append(f"负责人 → {tool_params.assignee or '(未分配)'}")
        if tool_params.description is not None:
            changes.append("描述已更新")
        if tool_params.add_blocks:
            changes.append(f"阻塞 += {', '.join(tool_params.add_blocks)}")
        if tool_params.add_blocked_by:
            changes.append(
                f"被阻塞 += {', '.join(tool_params.add_blocked_by)}"
            )

        summary = "; ".join(changes) if changes else "无变更"
        return ToolResult(
            content=f"任务 {task.id} 已更新: {summary}", is_error=False
        )
