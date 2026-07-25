"""Plan 模式退出工具：LLM 完成计划编写后调用，触发用户审批流程。"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from seacode.tools.base import Tool, ToolCategory, ToolResult


class ExitPlanModeParams(BaseModel):
    """ExitPlanMode 入参：summary 携带计划摘要，供审批对话框展示。"""

    summary: str = ""


class ExitPlanModeTool(Tool):
    """Plan 模式退出入口。

    LLM 在 Plan 模式下完成计划文件编写后调用此工具；工具本身不切换权限模式，
    只校验当前确实处于 Plan 模式且计划文件已生成，随后由上层在回合结束后
    弹出审批对话框并完成模式切换。非 Plan 模式或无计划文件时返回错误结果，
    引导模型先进入正确状态。
    """

    name = "ExitPlanMode"
    description = (
        "Exit plan mode and present the plan for user approval. "
        "Call this when your plan is complete and written to the plan file."
    )
    params_model = ExitPlanModeParams
    category = ToolCategory.SYSTEM
    is_system_tool = True

    def __init__(
        self,
        is_plan_mode: Callable[[], bool] | None = None,
        plan_exists: Callable[[], bool] | None = None,
    ) -> None:
        # 两个回调由上层注入：is_plan_mode 判定当前是否处于 Plan 模式，
        # plan_exists 判定计划文件是否已生成；为 None 时跳过对应校验。
        self._is_plan_mode = is_plan_mode
        self._plan_exists = plan_exists

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, ExitPlanModeParams)
        # 非 Plan 模式直接调用属于误用，返回错误引导模型纠正。
        if self._is_plan_mode is not None and not self._is_plan_mode():
            return ToolResult(
                content=(
                    "You are not in plan mode. This tool is only for exiting "
                    "plan mode after writing a plan."
                ),
                is_error=True,
            )
        # 未生成计划文件时拒绝退出，避免空计划进入审批。
        if self._plan_exists is not None and not self._plan_exists():
            return ToolResult(
                content=(
                    "No plan file found. Please write your plan to the plan "
                    "file before calling ExitPlanMode."
                ),
                is_error=True,
            )
        return ToolResult(
            content=(
                "Plan mode will be exited after this turn. "
                "The user will be shown the plan approval dialog. "
                "Do not call any more tools — end your turn now."
            )
        )
