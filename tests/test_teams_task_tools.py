"""团队任务工具的任务板错误映射测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


# 验证 Lead 可通过动态上下文操作自己团队的共享任务板。
# 创建一个真实团队后省略 team_name，依次调用 create/list/get/update，断言四个工具路由到同一任务板。
@pytest.mark.asyncio
async def test_lead_task_tools_resolve_single_team_context(tmp_path: Path) -> None:
    teams_root = tmp_path / "project" / ".seacode" / "teams"
    manager = TeamManager(teams_root=teams_root)
    await manager.create_team("demo", "lead-1")
    lead = SimpleNamespace(agent_id="lead-1")

    create_result = await TaskCreateTool(
        manager, parent_agent=lead
    ).execute(TaskCreateParams(title="lead task"))
    assert not create_result.is_error
    task = manager.get_task_store("demo").list_tasks()[0]
    assert task.created_by == "lead"

    list_result = await TaskListTool(
        manager, parent_agent=lead
    ).execute(TaskListParams())
    get_result = await TaskGetTool(
        manager, parent_agent=lead
    ).execute(TaskGetParams(task_id=task.id))
    update_result = await TaskUpdateTool(
        manager, parent_agent=lead
    ).execute(TaskUpdateParams(task_id=task.id, status="completed"))

    assert not list_result.is_error
    assert task.id in list_result.content
    assert not get_result.is_error
    assert task.id in get_result.content
    assert not update_result.is_error
    updated_task = manager.get_task_store("demo").get(task.id)
    assert updated_task is not None
    assert updated_task.status == "completed"


# 验证 Lead 有多个团队时不会把任务静默写入错误任务板。
# 创建两个同 Lead 团队，省略 team_name 应返回可恢复错误，显式 team_name 才允许操作。
@pytest.mark.asyncio
async def test_lead_task_tools_require_team_name_for_multiple_teams(
    tmp_path: Path,
) -> None:
    teams_root = tmp_path / "project" / ".seacode" / "teams"
    manager = TeamManager(teams_root=teams_root)
    await manager.create_team("first", "lead-1")
    await manager.create_team("second", "lead-1")
    lead = SimpleNamespace(agent_id="lead-1")

    missing_team = await TaskCreateTool(
        manager, parent_agent=lead
    ).execute(TaskCreateParams(title="ambiguous"))
    explicit_team = await TaskCreateTool(
        manager, parent_agent=lead
    ).execute(TaskCreateParams(title="routed", team_name="second"))

    assert missing_team.is_error
    assert "team_name" in missing_team.content
    assert not explicit_team.is_error
    assert [t.title for t in manager.get_task_store("first").list_tasks()] == []
    assert [t.title for t in manager.get_task_store("second").list_tasks()] == [
        "routed"
    ]
