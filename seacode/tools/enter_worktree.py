"""EnterWorktree 工具：创建并进入一个隔离的 Git worktree 工作区。"""

from __future__ import annotations

import secrets

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.worktree.manager import WorktreeError, WorktreeManager
from seacode.worktree.slug import validate_slug


class EnterWorktreeParams(BaseModel):
    """EnterWorktree 参数；name 留空时自动生成 wt-<hex> 形式的临时名。"""

    name: str = Field(default="", description="worktree 名称（留空自动生成 wt-{hex}）")


class EnterWorktreeTool(Tool):
    """创建并进入隔离 worktree；模型可在隔离环境中执行有副作用的操作后由调用方清理。

    should_defer=True 保证此工具不参与流式并发分批，避免与其它工具同时修改 cwd。
    """

    name = "EnterWorktree"
    description = "创建并进入一个隔离的 Git worktree 工作区"
    params_model = EnterWorktreeParams
    category = ToolCategory.SYSTEM
    should_defer = True

    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    async def execute(self, params: BaseModel) -> ToolResult:
        # 基类签名是 BaseModel，运行时由 params_model 校验为 EnterWorktreeParams。
        tool_params: EnterWorktreeParams = params  # type: ignore[assignment]
        # 已在 worktree session 中时拒绝，避免嵌套进入导致 cwd 与 session 状态错乱。
        if self._manager.get_current_session() is not None:
            return ToolResult(
                content="已在 worktree session 中，请先退出当前 worktree",
                is_error=True,
            )
        name = tool_params.name or f"wt-{secrets.token_hex(4)}"
        err = validate_slug(name)
        if err:
            return ToolResult(
                content=f"Invalid worktree name: {err}", is_error=True
            )
        try:
            wt = await self._manager.create(name)
            await self._manager.enter(name)
            return ToolResult(
                content=(
                    f"已创建并进入 worktree: name={wt.name}, "
                    f"path={wt.path}, branch={wt.branch}"
                ),
                is_error=False,
            )
        except WorktreeError as e:
            return ToolResult(content=str(e), is_error=True)
