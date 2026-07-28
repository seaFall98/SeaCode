"""Skill 加载器：两级搜索、热重载、缓存回退。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from seacode.skills.parser import (
    SkillDef,
    parse_skill_directory,
    parse_skill_file,
)

logger = logging.getLogger(__name__)


class SkillLoader:
    """两级搜索 Skill 加载器，支持热重载与缓存回退。

    搜索路径：项目级 .seacode/skills/ > 用户级 ~/.seacode/skills/。
    同名 Skill 先到先得（项目级覆盖用户级）；内置 Skill 钩子保留返回空列表。
    get() 每次重读磁盘，失败回退 _cache 中上一次成功版本。
    """

    def __init__(self, project_dir: Path, user_dir: Path) -> None:
        self._project_dir = project_dir.resolve()
        self._user_dir = user_dir.resolve()
        self._skills: dict[str, SkillDef] = {}
        self._cache: dict[str, SkillDef] = {}
        self._dir_mod_times: dict[Path, float] = {}
        self._reload_callbacks: list[Callable[[], None]] = []

    # 返回受控的 Skill 安装根目录；调用方不能通过此接口写入任意路径。
    def get_install_root(self, scope: str) -> Path:
        if scope == "project":
            return self._project_dir
        if scope == "user":
            return self._user_dir
        raise ValueError(f"不支持的 Skill 安装范围：{scope}")

    # 全量扫描项目级 + 用户级 + 内置钩子；not in seen 实现先到先得优先级。
    def load_all(self) -> dict[str, SkillDef]:
        self._skills.clear()
        self._cache.clear()
        seen: set[str] = set()

        for skill in self._scan_directory(self._project_dir, "project"):
            if skill.name not in seen:
                self._skills[skill.name] = skill
                self._cache[skill.name] = skill
                seen.add(skill.name)

        for skill in self._scan_directory(self._user_dir, "user"):
            if skill.name not in seen:
                self._skills[skill.name] = skill
                self._cache[skill.name] = skill
                seen.add(skill.name)

        # 内置 Skill 钩子：本步返回空列表，后续步骤可填充。
        for skill in self._load_builtins():
            if skill.name not in seen:
                self._skills[skill.name] = skill
                self._cache[skill.name] = skill
                seen.add(skill.name)

        self._update_dir_mod_times()
        return self._skills

    # 扫描单个目录：文件 .md 调 parse_skill_file，目录调 parse_skill_directory。
    # 解析失败 warning 日志并跳过，不影响其它 Skill 加载。
    def _scan_directory(self, dir: Path, source_label: str) -> list[SkillDef]:
        if not dir.exists() or not dir.is_dir():
            return []
        results: list[SkillDef] = []
        for entry in dir.iterdir():
            try:
                if entry.is_file() and entry.suffix == ".md":
                    results.append(parse_skill_file(entry))
                elif entry.is_dir():
                    results.append(parse_skill_directory(entry))
            except Exception as e:
                logger.warning(
                    "Skill 解析失败 source=%s entry=%s: %s",
                    source_label,
                    entry,
                    e,
                )
        return results

    # 内置 Skill 钩子：本步返回空列表，后续步骤可填充。
    def _load_builtins(self) -> list[SkillDef]:
        return []

    # 按 name 获取 Skill；source_path 不为 None 时重读磁盘，失败回退旧版本。
    # 回退顺序：_cache 命中 → 旧 skill 对象；永不返回 None（除非 name 完全不存在）。
    def get(self, name: str) -> SkillDef | None:
        skill = self._skills.get(name)
        if skill is None:
            return None
        if skill.source_path is not None:
            try:
                refreshed = self._reload_from_disk(skill)
                self._skills[name] = refreshed
                self._cache[name] = refreshed
                return refreshed
            except Exception as e:
                logger.warning("重读 Skill %s 失败: %s，使用缓存版本", name, e)
                # _cache 命中用缓存；否则回退到旧 skill 对象，保证调用方不丢能力。
                return self._cache.get(name, skill)
        return skill

    # 根据 source_path 重读磁盘：单文件走 parse_skill_file，目录型走 parse_skill_directory。
    def _reload_from_disk(self, skill: SkillDef) -> SkillDef:
        assert skill.source_path is not None
        if skill.is_directory:
            return parse_skill_directory(skill.source_path.parent)
        return parse_skill_file(skill.source_path)

    # 返回 [(name, description)] 列表供目录摘要注入。
    def get_catalog(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in self._skills.values()]

    # 返回 Skill 来源标签：project / user / builtin / unknown。
    def get_source_label(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            return "unknown"
        if skill.source_path is None:
            return "builtin"
        try:
            if self._project_dir.resolve() in skill.source_path.resolve().parents:
                return "project"
        except (OSError, ValueError):
            pass
        try:
            if self._user_dir.resolve() in skill.source_path.resolve().parents:
                return "user"
        except (OSError, ValueError):
            pass
        return "unknown"

    # 检测已记录目录的 modtime 变化或新目录创建；任一变化返回 True。
    def needs_reload(self) -> bool:
        for path, old_mtime in self._dir_mod_times.items():
            try:
                new_mtime = path.stat().st_mtime
                if new_mtime != old_mtime:
                    return True
            except OSError:
                # 目录被删除也算变化。
                return True
        # 检测之前不存在的目录是否已创建。
        for path in (self._project_dir, self._user_dir):
            if path not in self._dir_mod_times and path.exists() and path.is_dir():
                return True
        return False

    # 全量重扫并触发 reload 回调；返回最新 _skills。
    def reload(self) -> dict[str, SkillDef]:
        result = self.load_all()
        for cb in self._reload_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning("Skill reload 回调失败: %s", e)
        return result

    # 注册 reload 后回调；app.py 用于触发命令重注册与 catalog 刷新。
    def register_reload_callback(self, callback: Callable[[], None]) -> None:
        self._reload_callbacks.append(callback)

    # 记录 project_dir / user_dir 的 modtime（若存在）。
    def _update_dir_mod_times(self) -> None:
        self._dir_mod_times.clear()
        for path in (self._project_dir, self._user_dir):
            try:
                if path.exists() and path.is_dir():
                    self._dir_mod_times[path] = path.stat().st_mtime
            except OSError:
                pass
