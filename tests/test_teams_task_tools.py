"""团队任务工具的任务板错误映射测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from seacode.teams.manager import TeamManager
from seacode.tools.task_create import TaskCreateParams, TaskCreateTool
from seacode.tools.task_get import TaskGetParams, TaskGetTool
from seacode.tools.task_list import TaskListParams, TaskListTool
from seacode.tools.task_update import TaskUpdateParams, TaskUpdateTool


# 验证任务板损坏时四个任务工具均返回可恢复的 ToolResult 错误。
# 创建真实团队后写入非法 JSON，分别调用 create/get/list/update，断言无工具抛出异常。
@pytest.mark.asyncio
async def test_task_tools_return_errors_for_corrupt_shared_task_board(
    tmp_path: Path,
) -> None:
    teams_root = tmp_path / "project" / ".seacode" / "teams"
    manager = TeamManager(teams_root=teams_root)
    await manager.create_team("demo", "lead-1")
    task_path = teams_root / "demo" / "tasks.json"
    original = "{invalid json"
    task_path.write_text(original, encoding="utf-8")

    results = [
        await TaskCreateTool(manager, "demo", "lead").execute(
            TaskCreateParams(title="must fail")
        ),
        await TaskGetTool(manager, "demo").execute(TaskGetParams(task_id="1")),
        await TaskListTool(manager, "demo").execute(TaskListParams()),
        await TaskUpdateTool(manager, "demo").execute(
            TaskUpdateParams(task_id="1", status="completed")
        ),
    ]

    assert all(result.is_error for result in results)
    assert all("任务板" in result.content for result in results)
    assert task_path.read_text(encoding="utf-8") == original
