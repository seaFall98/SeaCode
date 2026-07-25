# 团队协调：长期多成员小组的数据模型、生命周期、跨进程邮箱、共享任务板、三后端 spawn、Lead 协调模式
"""seacode.teams 子包入口；重导出团队协调的公开类与函数。"""

from __future__ import annotations

from seacode.teams.mailbox import Mailbox, MailboxMessage, create_message
from seacode.teams.manager import TeamError, TeamManager
from seacode.teams.models import (
    AgentTeam,
    BackendType,
    TeammateInfo,
    resolve_team_dir,
    unique_team_name,
)
from seacode.teams.progress import TeammateProgress, ToolActivity
from seacode.teams.registry import AgentNameRegistry
from seacode.teams.shared_task import SharedTask, SharedTaskStore

__all__ = [
    "AgentNameRegistry",
    "AgentTeam",
    "BackendType",
    "Mailbox",
    "MailboxMessage",
    "SharedTask",
    "SharedTaskStore",
    "TeamError",
    "TeamManager",
    "TeammateInfo",
    "TeammateProgress",
    "ToolActivity",
    "create_message",
    "resolve_team_dir",
    "unique_team_name",
]
