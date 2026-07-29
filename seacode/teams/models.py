# 团队数据模型：后端类型、成员信息、团队实体与目录解析
"""teams 子包的数据模型层。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅用于类型注解；运行时通过成员注入避免循环导入。
    from seacode.teams.progress import TeammateProgress

log = logging.getLogger(__name__)


class BackendType(Enum):
    # teammate spawn 后端：tmux 窗口 / iTerm2 标签 / 同进程 asyncio Task。
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


@dataclass
class TeammateInfo:
    # 团队成员的运行时元数据；progress 字段不参与序列化（含 threading.Lock）。
    name: str
    agent_id: str
    agent_type: str
    model: str
    worktree_path: str
    backend_type: BackendType
    is_active: bool | None = None
    progress: TeammateProgress | None = None

    def to_dict(self) -> dict:
        # 排除 progress（运行时对象，含 Lock，不可 JSON 序列化）。
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "backend_type": self.backend_type.value,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeammateInfo:
        # 兼容旧配置缺失字段；backend_type 用 value 字符串重建枚举。
        return cls(
            name=data["name"],
            agent_id=data["agent_id"],
            agent_type=data.get("agent_type", ""),
            model=data.get("model", ""),
            worktree_path=data.get("worktree_path", ""),
            backend_type=BackendType(data.get("backend_type", "in-process")),
            is_active=data.get("is_active"),
        )


@dataclass
class AgentTeam:
    # 一个长期团队的元数据与成员列表；config_path 指向磁盘 config.json。
    name: str
    lead_agent_id: str
    members: list[TeammateInfo] = field(default_factory=list)
    config_path: str = ""
    description: str = ""

    def get_member(self, name: str) -> TeammateInfo | None:
        # 按 name 查找成员；未找到返回 None。
        for m in self.members:
            if m.name == name:
                return m
        return None

    def add_member(self, member: TeammateInfo) -> None:
        # 直接追加；同名去重由调用方处理（_unique_teammate_name）。
        self.members.append(member)

    def remove_member(self, name: str) -> None:
        # 按名移除成员；不存在时静默无操作。
        self.members = [m for m in self.members if m.name != name]

    def set_member_active(self, name: str, is_active: bool | None) -> None:
        # 更新成员活跃状态；None 表示未知，False 表示 idle，True 表示活跃。
        member = self.get_member(name)
        if member:
            member.is_active = is_active

    def all_idle(self) -> bool:
        # 所有成员 is_active 均为 False 时返回 True；空列表也返回 True。
        return all(m.is_active is False for m in self.members)

    def active_members(self) -> list[TeammateInfo]:
        # 返回未标记为 idle 的成员（含 None 与 True，排除 False）。
        return [m for m in self.members if m.is_active is not False]

    def to_dict(self) -> dict:
        # 序列化团队元数据与成员列表（不含 config_path，磁盘路径由调用方管理）。
        return {
            "name": self.name,
            "lead_agent_id": self.lead_agent_id,
            "members": [m.to_dict() for m in self.members],
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict, config_path: str = "") -> AgentTeam:
        # 反序列化；config_path 由调用方传入以便 save() 回写原路径。
        return cls(
            name=data["name"],
            lead_agent_id=data["lead_agent_id"],
            members=[TeammateInfo.from_dict(m) for m in data.get("members", [])],
            config_path=config_path,
            description=data.get("description", ""),
        )

    def save(self) -> None:
        # 把当前状态写入 config_path 指向的 JSON 文件。
        Path(self.config_path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, config_path: str | Path) -> AgentTeam | None:
        # 从磁盘加载团队配置；文件不存在或损坏时返回 None 并记 warning。
        p = Path(config_path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls.from_dict(data, config_path=str(p))
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("failed to load team config: %s", e)
            return None


# 把任意名字规整为安全 slug：非 [a-zA-Z0-9_-] 替换为 '-'，连续 '-' 合并，空串回退 "team"。
def _sanitize_name(name: str) -> str:
    if not name:
        return "team"
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.lower()
    return slug or "team"


# 返回给定团队根下的 <slug> 团队目录路径；未提供根时保留旧 API 的用户级默认值。
def resolve_team_dir(
    team_name: str, teams_root: str | Path | None = None
) -> Path:
    slug = _sanitize_name(team_name)
    root = Path(teams_root) if teams_root is not None else Path.home() / ".seacode" / "teams"
    return root / slug


# 同名团队已存在时追加 -2 / -3 后缀；用于给定团队根中的 create_team 防止目录覆盖。
def unique_team_name(
    team_name: str, teams_root: str | Path | None = None
) -> str:
    base = _sanitize_name(team_name)
    root = Path(teams_root) if teams_root is not None else Path.home() / ".seacode" / "teams"
    team_dir = root / base
    if not team_dir.exists():
        return base
    i = 2
    while True:
        candidate = f"{base}-{i}"
        if not (root / candidate).exists():
            return candidate
        i += 1
