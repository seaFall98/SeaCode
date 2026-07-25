"""FileHistory：按文件路径生成版本化备份，支持快照与回滚。

设计要点：
- ``track_edit`` 在写工具实际写文件之前调用，备份"本次编辑前"的内容；
  若同文件多次编辑，版本号递增，每次都留一份独立备份。
- ``make_snapshot`` 在用户回合起点调用，把当前已跟踪文件的最新版本引用
  落到 ``Snapshot``，作为回滚目标。
- ``rewind`` 把指定快照引用的备份内容写回原路径，并把跟踪表回滚到该快照版本。
- 备份文件名用 sha256(path)[:16] + @vN 避免路径字符冲突；落盘目录按 session_id 隔离。
- ``threading.Lock`` 保护 ``_tracked`` 与 ``_snapshots``，保证多线程 track_edit 不竞争。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# 单会话保留的快照上限；超出后滚动保留最近 MAX_SNAPSHOTS 条。
MAX_SNAPSHOTS = 100


@dataclass
class Backup:
    """单个文件某版本的备份记录；path 是备份文件路径，version 是该文件的版本号。"""

    path: str
    version: int
    timestamp: datetime


@dataclass
class Snapshot:
    """用户回合起点的快照；backups 映射「原文件绝对路径 -> Backup」。"""

    message_index: int
    user_text: str
    backups: dict[str, Backup]
    timestamp: datetime


class FileHistory:
    """按会话隔离的文件历史管理器；track_edit / make_snapshot / rewind 三组核心操作。"""

    def __init__(self, base_dir: str | Path, session_id: str) -> None:
        self._session_dir = Path(base_dir) / ".seacode" / "file-history" / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        # _tracked: 原文件绝对路径 -> 当前最新版本号（每次 track_edit 递增）。
        self._tracked: dict[str, int] = {}
        self._snapshots: list[Snapshot] = []
        self._lock = threading.Lock()

    # 备份文件名：sha256(原路径)[:16] + @vN，避免路径分隔符与非法字符进入文件名。
    def _backup_name(self, file_path: str, version: int) -> str:
        h = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        return f"{h}@v{version}"

    # 在写工具覆盖文件前调用：备份当前内容到新版本号，并推进 _tracked。
    # 原文件不存在（新建文件）时静默忽略，仍把版本号推进，确保后续 snapshot 引用一致。
    def track_edit(self, path: str | Path) -> None:
        with self._lock:
            abs_path = str(Path(path).resolve())
            ver = self._tracked.get(abs_path, 0)
            new_ver = ver + 1
            try:
                data = Path(abs_path).read_bytes()
                bp = self._session_dir / self._backup_name(abs_path, new_ver)
                bp.write_bytes(data)
            except FileNotFoundError:
                # 新建文件无原内容可备份；版本号仍推进以保持跟踪表一致。
                pass
            self._tracked[abs_path] = new_ver

    # 在用户回合起点调用：为所有已跟踪文件留下当前版本的引用快照。
    # 若某版本备份文件丢失（如手工清理），尝试用当前文件内容补齐；补不上则跳过。
    def make_snapshot(self, msg_index: int, user_text: str) -> Snapshot:
        with self._lock:
            backups: dict[str, Backup] = {}
            for path, ver in self._tracked.items():
                bp = self._session_dir / self._backup_name(path, ver)
                if not bp.exists():
                    try:
                        data = Path(path).read_bytes()
                        bp.write_bytes(data)
                    except (FileNotFoundError, OSError):
                        # 文件已不存在且无法补齐，跳过此路径不进入快照。
                        continue
                backups[path] = Backup(
                    path=str(bp), version=ver, timestamp=datetime.now()
                )
            snapshot = Snapshot(
                message_index=msg_index,
                user_text=user_text,
                backups=backups,
                timestamp=datetime.now(),
            )
            self._snapshots.append(snapshot)
            # 滚动保留最近 MAX_SNAPSHOTS 条，避免长会话无界增长。
            if len(self._snapshots) > MAX_SNAPSHOTS:
                self._snapshots = self._snapshots[-MAX_SNAPSHOTS:]
            return snapshot

    # 把指定快照引用的备份内容写回原路径；返回被修改的文件列表。
    # 越界 index 返回空列表；备份不存在时按"删除当前文件"语义处理。
    def rewind(self, snapshot_index: int) -> list[str]:
        with self._lock:
            if snapshot_index < 0 or snapshot_index >= len(self._snapshots):
                return []
            target = self._snapshots[snapshot_index]
            changed: list[str] = []
            for path, backup in target.backups.items():
                bp = Path(backup.path)
                current = Path(path)
                if not bp.exists():
                    # 备份丢失但当前文件存在：按"回滚到无文件"语义删除当前文件。
                    if current.exists():
                        try:
                            current.unlink()
                            changed.append(path)
                        except OSError as e:
                            log.warning("failed to delete %s: %s", path, e)
                    continue
                try:
                    backup_data = bp.read_bytes()
                    current_data = (
                        current.read_bytes() if current.exists() else b""
                    )
                    if backup_data != current_data:
                        current.parent.mkdir(parents=True, exist_ok=True)
                        current.write_bytes(backup_data)
                        changed.append(path)
                except OSError as e:
                    log.warning("failed to restore %s: %s", path, e)
            # 截断 _snapshots 到目标快照之后，丢弃更新的快照。
            self._snapshots = self._snapshots[: snapshot_index + 1]
            # 跟踪表回滚到目标快照的版本号，后续 track_edit 从此版本继续递增。
            self._tracked = {
                path: backup.version for path, backup in target.backups.items()
            }
            return changed

    # 返回快照列表的浅拷贝，避免外部修改内部状态。
    def get_snapshots(self) -> list[Snapshot]:
        with self._lock:
            return list(self._snapshots)

    # 是否存在任何快照；供 /rewind 无参列出时快速判断。
    def has_snapshots(self) -> bool:
        with self._lock:
            return len(self._snapshots) > 0
