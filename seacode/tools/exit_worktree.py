"""ExitWorktree 工具：退出当前 worktree 会话，可选保留或删除 worktree。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from seacode.tools.base import Tool, ToolCategory, ToolResult
from seacode.worktree.changes import count_worktree_changes
from seacode.worktree.manager import WorktreeError, WorktreeManager


class ExitWorktreeParams(BaseModel):
    """ExitWorktree 参数；action=remove 时若检测到变更且未 discard 则拒绝执行。"""

    action: Literal["keep", "remove"] = Field(default="keep")
    discard_changes: bool = Field(default=False)


class ExitWorktreeTool(Tool):
    """退出当前 worktree 会话；action=remove 时根据变更检测结果决定是否清理。

    should_defer=True 保证此工具不参与流式并发分批，避免与其它工具同时修改 cwd。
    """

    name = "ExitWorktree"
    description = "退出当前 worktree 会话，可选保留或删除 worktree"
    params_model = ExitWorktreeParams
    category = ToolCategory.SYSTEM
    should_defer = True

    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    async def execute(self, params: BaseModel) -> ToolResult:
        tool_params: ExitWorktreeParams = params  # type: ignore[assignment]
        session = self._manager.get_current_session()
        if session is None:
            return ToolResult(content="未在 worktree session 中", is_error=True)
        # Literal 已保证 action 是 keep/remove；此处再加一层守卫以防 model_construct 绕过。
        action_val: Any = tool_params.action
        if action_val not in ("keep", "remove"):
            return ToolResult(
                content=f"Invalid action: {action_val}, must be 'keep' or 'remove'",
                is_error=True,
            )
        if tool_params.action == "remove" and not tool_params.discard_changes:
            changes = count_worktree_changes(
                session.worktree_path, session.original_head_commit
            )
            if changes.uncommitted > 0 or changes.new_commits > 0:
                return ToolResult(
                    content=(
                        f"worktree has {changes.uncommitted} uncommitted changes and "
                        f"{changes.new_commits} new commits. Confirm with the user, then "
                        "re-invoke with discard_changes: true — or use action: 'keep'"
                    ),
                    is_error=True,
                )
        try:
            await self._manager.exit(
                session.worktree_name,
                action=tool_params.action,
                discard_changes=tool_params.discard_changes,
            )
            return ToolResult(
                content=f"已退出 worktree (action={tool_params.action})",
                is_error=False,
            )
        except WorktreeError as e:
            return ToolResult(content=str(e), is_error=True)
