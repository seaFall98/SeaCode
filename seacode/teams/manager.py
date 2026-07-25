# TeamManager：团队的创建、成员注册、邮箱与任务板懒加载、6 步全链路清理。
"""teams 子包的 TeamManager 团队生命周期管理。"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from seacode.teams.backend_detect import detect_backend
from seacode.teams.mailbox import Mailbox, create_message
from seacode.teams.models import (
    AgentTeam,
    BackendType,
    TeammateInfo,
    resolve_team_dir,
    unique_team_name,
)
from seacode.teams.progress import TeammateProgress
from seacode.teams.registry import AgentNameRegistry
from seacode.teams.shared_task import SharedTaskStore
from seacode.teams.spawn_inprocess import LEAD_NAME, InProcessTeammateHandle

log = logging.getLogger(__name__)


class TeamError(Exception):
    """团队生命周期错误。"""


class TeamManager:
    # 管理多个团队的内存缓存与磁盘持久化；worktree_manager 与 trace_manager 由 app 注入。
    def __init__(self, worktree_manager: Any = None, trace_manager: Any = None) -> None:
        self._worktree_manager = worktree_manager
        self._trace_manager = trace_manager
        self._teams: dict[str, AgentTeam] = {}
        self._task_stores: dict[str, SharedTaskStore] = {}
        self._mailboxes: dict[str, Mailbox] = {}
        self._inprocess_handles: dict[tuple[str, str], InProcessTeammateHandle] = {}
        self._pane_ids: dict[tuple[str, str], str] = {}
        self._teammate_team_map: dict[str, str] = {}
        self._detected_backend: BackendType | None = None

    # 检测 spawn 后端；首次调用后缓存，避免重复 env / 平台检测。
    def detect_backend(self, teammate_mode: str, is_interactive: bool) -> BackendType:
        if self._detected_backend is None:
            self._detected_backend = detect_backend(teammate_mode, is_interactive)
        return self._detected_backend

    # 创建新团队：unique 命名、建目录、初始化 config/tasks/mailbox 三件套并缓存。
    async def create_team(
        self,
        name: str,
        lead_agent_id: str,
        description: str = "",
        teammate_mode: str = "",
        is_interactive: bool = True,
    ) -> AgentTeam:
        # 触发后端检测以缓存结果（即便本团队用 in-process，后续 spawn 也需统一后端）。
        self.detect_backend(teammate_mode, is_interactive)
        unique = unique_team_name(name)
        team_dir = resolve_team_dir(unique)
        team_dir.mkdir(parents=True, exist_ok=True)
        team = AgentTeam(
            name=unique,
            lead_agent_id=lead_agent_id,
            config_path=str(team_dir / "config.json"),
            description=description,
        )
        team.save()
        task_store = SharedTaskStore(team_dir / "tasks.json")
        task_store.init_empty()
        mailbox = Mailbox(team_dir / "mailbox")
        self._teams[unique] = team
        self._task_stores[unique] = task_store
        self._mailboxes[unique] = mailbox
        return team

    # 按名获取团队：内存优先，未命中时从磁盘 config.json 懒加载并缓存。
    def get_team(self, name: str) -> AgentTeam | None:
        if name in self._teams:
            return self._teams[name]
        team_dir = resolve_team_dir(name)
        team = AgentTeam.load(team_dir / "config.json")
        if team is not None:
            self._teams[name] = team
        return team

    # 按名获取任务板：内存优先，未命中时从磁盘懒加载并缓存。
    def get_task_store(self, name: str) -> SharedTaskStore:
        if name in self._task_stores:
            return self._task_stores[name]
        team_dir = resolve_team_dir(name)
        store = SharedTaskStore(team_dir / "tasks.json")
        self._task_stores[name] = store
        return store

    # 按名获取邮箱：内存优先，未命中时从磁盘目录懒加载并缓存。
    def get_mailbox(self, name: str) -> Mailbox:
        if name in self._mailboxes:
            return self._mailboxes[name]
        team_dir = resolve_team_dir(name)
        mailbox = Mailbox(team_dir / "mailbox")
        self._mailboxes[name] = mailbox
        return mailbox

    # 注册成员到团队并持久化；同时记录 teammate→team 映射，附加已有 handle 的 progress。
    def register_member(self, team_name: str, member: TeammateInfo) -> None:
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"team {team_name} not found")
        team.add_member(member)
        # 若已有 in-process handle，附加其 progress 供 get_all_teammate_progress 收集。
        handle = self._inprocess_handles.get((team_name, member.name))
        if handle is not None and handle.progress is not None:
            member.progress = handle.progress
        team.save()
        self._teammate_team_map[member.name] = team_name

    # 标记成员 idle 并向 lead 邮箱写 idle 通知。
    def set_member_idle(self, team_name: str, member_name: str, reason: str) -> None:
        team = self.get_team(team_name)
        if team is None:
            return
        team.set_member_active(member_name, False)
        team.save()
        mailbox = self.get_mailbox(team_name)
        mailbox.write(
            LEAD_NAME,
            create_message(
                from_agent=member_name,
                to_agent=LEAD_NAME,
                content=f"[idle] {member_name} (reason: {reason})",
                summary="idle",
            ),
        )

    # 注册 in-process handle；若成员已注册，附加 progress。
    def register_inprocess_handle(
        self, team_name: str, member_name: str, handle: InProcessTeammateHandle
    ) -> None:
        self._inprocess_handles[(team_name, member_name)] = handle
        team = self.get_team(team_name)
        if team is not None:
            member = team.get_member(member_name)
            if member is not None and handle.progress is not None:
                member.progress = handle.progress

    # 注册 pane 后端的 pane_id（tmux pane_id 或 iTerm2 session_id）。
    def register_pane_id(self, team_name: str, member_name: str, pane_id: str) -> None:
        self._pane_ids[(team_name, member_name)] = pane_id

    # 获取成员的 pane_id；in-process 后端返回 None。
    def get_pane_id(self, team_name: str, member_name: str) -> str | None:
        return self._pane_ids.get((team_name, member_name))

    # 6 步全链路删除团队：活跃检查 → 注销名字 → cancel/kill → 清理 worktree → 清理邮箱 → 删目录。
    async def delete_team(self, name: str) -> None:
        team = self.get_team(name)
        if team is None:
            raise TeamError(f"team {name} not found")

        # 第 1 步：活跃成员检查。
        active = team.active_members()
        if active:
            raise TeamError(
                f"team {name} has {len(active)} active members"
            )

        # 第 2 步：注销所有成员名字。
        for member in team.members:
            AgentNameRegistry.instance().unregister(member.name)

        # 第 3 步：cancel in-process handle 与 kill pane。
        for member in team.members:
            handle = self._inprocess_handles.pop((name, member.name), None)
            if handle is not None:
                try:
                    handle.cancel()
                except Exception as e:
                    log.warning("cancel handle %s failed: %s", member.name, e)
            pane_id = self._pane_ids.pop((name, member.name), None)
            if pane_id:
                self._kill_pane(pane_id)

        # 第 4 步：清理 worktree 与 trace。
        for member in team.members:
            if member.worktree_path:
                self._cleanup_worktree(member.worktree_path)
            if self._trace_manager is not None:
                try:
                    self._trace_manager.remove(member.agent_id)
                except Exception as e:
                    log.warning("trace remove %s failed: %s", member.agent_id, e)

        # 第 5 步：清理邮箱。
        mailbox = self.get_mailbox(name)
        mailbox.cleanup_all()

        # 第 6 步：删除团队目录。
        team_dir = resolve_team_dir(name)
        self._remove_dir(str(team_dir))

        # 清理内存缓存。
        self._teams.pop(name, None)
        self._task_stores.pop(name, None)
        self._mailboxes.pop(name, None)
        for m in team.members:
            self._teammate_team_map.pop(m.name, None)

    # 返回当前内存中所有团队名。
    def list_teams(self) -> list[str]:
        return list(self._teams.keys())

    # 按 teammate 名字反查所属团队名。
    def get_team_for_teammate(self, member_name: str) -> str | None:
        return self._teammate_team_map.get(member_name)

    # 消费 lead 在所有团队邮箱中的未读消息，拼成 <team-notification> XML 列表。
    def drain_lead_mailbox(self, lead_agent_id: str) -> list[str]:
        notes: list[str] = []
        for team_name in self.list_teams():
            mailbox = self.get_mailbox(team_name)
            msgs = mailbox.consume(lead_agent_id)
            if msgs:
                content = "\n".join(
                    f"From {m.from_agent}: {m.content}" for m in msgs
                )
                notes.append(
                    f'<team-notification team="{team_name}">\n{content}\n</team-notification>'
                )
        return notes

    # 收集所有团队所有成员的 progress；仅返回已附加 progress 的成员。
    def get_all_teammate_progress(self) -> list[TeammateProgress]:
        result: list[TeammateProgress] = []
        for team in self._teams.values():
            for member in team.members:
                if member.progress is not None:
                    result.append(member.progress)
        return result

    # teammate 完成时调用；标记 idle 并写 idle 通知。
    def on_teammate_completed(self, team_name: str, member_name: str) -> None:
        self.set_member_idle(team_name, member_name, "completed")

    # kill pane 后端；tmux/iTerm2 不可用时只记 warning。
    def _kill_pane(self, pane_id: str) -> None:
        try:
            from seacode.teams.spawn_tmux import kill_pane

            kill_pane(pane_id)
        except Exception as e:
            log.warning("failed to kill pane %s: %s", pane_id, e)

    # 清理 worktree：先 git worktree remove，失败回退 shutil.rmtree。
    def _cleanup_worktree(self, worktree_path: str) -> None:
        try:
            subprocess.run(
                ["git", "worktree", "remove", worktree_path, "--force"],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("git worktree remove failed: %s", e)
        try:
            shutil.rmtree(worktree_path, ignore_errors=True)
        except OSError as e:
            log.warning("shutil.rmtree failed: %s", e)

    # 删除目录；失败只记 warning 不抛错。
    def _remove_dir(self, path: str) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError as e:
            log.warning("failed to remove dir %s: %s", path, e)
