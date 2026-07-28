"""Skill 加载器单元测试：两级搜索、热重载、缓存回退、来源标签。"""

from __future__ import annotations

import time
from pathlib import Path

from seacode.skills import SkillLoader
from seacode.skills.parser import SkillDef


# 写入合法 SKILL.md 单文件并返回路径。
def _write_skill(path: Path, name: str, description: str, body: str = "body\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    return path


# ---------- 两级搜索 ----------


# 验证两级搜索项目级同名覆盖用户级。
# 项目级与用户级各放同名 commit.md（不同 description），断言 get 返回项目级。
def test_load_all_project_overrides_user(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    user_dir = tmp_path / "user" / "skills"
    _write_skill(project_dir / "commit.md", "commit", "项目级提交")
    _write_skill(user_dir / "commit.md", "commit", "用户级提交")
    loader = SkillLoader(project_dir=project_dir, user_dir=user_dir)
    loader.load_all()
    skill = loader.get("commit")
    assert skill is not None
    assert skill.description == "项目级提交"


# 验证目录不存在时 load_all 返回空。
# 项目级与用户级目录都不存在，断言 load_all 返回 {}。
def test_load_all_nonexistent_dirs_return_empty(tmp_path: Path) -> None:
    loader = SkillLoader(
        project_dir=tmp_path / "noexist-project",
        user_dir=tmp_path / "noexist-user",
    )
    assert loader.load_all() == {}


# 验证公开安装根目录契约返回规范化绝对路径。
# 传入相对项目/用户目录，断言两种 scope 都被解析为绝对路径。
def test_get_install_root_returns_absolute_paths() -> None:
    loader = SkillLoader(
        project_dir=Path("relative-project"),
        user_dir=Path("relative-user"),
    )

    assert loader.get_install_root("project").is_absolute()
    assert loader.get_install_root("user").is_absolute()


# 验证单文件解析失败 warning 跳过不影响其它。
# 项目级放 bad.md（缺 frontmatter）与 good.md（合法），断言 good 加载、bad 为 None。
def test_load_all_skips_unparseable_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    project_dir.mkdir(parents=True)
    (project_dir / "bad.md").write_text("no frontmatter here", encoding="utf-8")
    _write_skill(project_dir / "good.md", "good", "好的")
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    assert loader.get("good") is not None
    assert loader.get("bad") is None


# 验证项目级与用户级不同名 Skill 都加载。
# 项目级放 commit.md，用户级放 review.md，断言两者都能 get 到。
def test_load_all_loads_both_levels(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    user_dir = tmp_path / "user" / "skills"
    _write_skill(project_dir / "commit.md", "commit", "提交")
    _write_skill(user_dir / "review.md", "review", "审查")
    loader = SkillLoader(project_dir=project_dir, user_dir=user_dir)
    loader.load_all()
    assert loader.get("commit") is not None
    assert loader.get("review") is not None


# ---------- get_catalog 与 get_source_label ----------


# 验证 get_catalog 返回 name+description 列表。
# 放 2 个合法 Skill，断言 get_catalog 返回对应元组列表。
def test_get_catalog_returns_list(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    _write_skill(project_dir / "commit.md", "commit", "提交")
    _write_skill(project_dir / "review.md", "review", "审查")
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    catalog = loader.get_catalog()
    assert ("commit", "提交") in catalog
    assert ("review", "审查") in catalog
    assert len(catalog) == 2


# 验证 get_catalog 空列表。
# 无 Skill 时断言 get_catalog 返回 []。
def test_get_catalog_empty(tmp_path: Path) -> None:
    project_dir = tmp_path / "empty" / "skills"
    project_dir.mkdir(parents=True)
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    assert loader.get_catalog() == []


# 验证 get_source_label 返回 project。
# 项目级 Skill 断言标签为 "project"。
def test_get_source_label_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    _write_skill(project_dir / "commit.md", "commit", "提交")
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    assert loader.get_source_label("commit") == "project"


# 验证 get_source_label 返回 user。
# 用户级 Skill 断言标签为 "user"。
def test_get_source_label_user(tmp_path: Path) -> None:
    user_dir = tmp_path / "user" / "skills"
    _write_skill(user_dir / "review.md", "review", "审查")
    loader = SkillLoader(project_dir=tmp_path / "noexist", user_dir=user_dir)
    loader.load_all()
    assert loader.get_source_label("review") == "user"


# 验证 get_source_label 返回 builtin。
# 手动注入 source_path=None 的 SkillDef，断言标签为 "builtin"。
def test_get_source_label_builtin(tmp_path: Path) -> None:
    loader = SkillLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    loader._skills["builtin-skill"] = SkillDef(
        name="builtin-skill", description="d", prompt_body="b", source_path=None
    )
    assert loader.get_source_label("builtin-skill") == "builtin"


# 验证 get_source_label 未知 name 返回 unknown。
# 查询不存在的 name，断言返回 "unknown"。
def test_get_source_label_unknown(tmp_path: Path) -> None:
    loader = SkillLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    loader.load_all()
    assert loader.get_source_label("nonexistent") == "unknown"


# ---------- _load_builtins 钩子 ----------


# 验证 _load_builtins 默认返回空列表。
# 直接调用 _load_builtins，断言返回 []。
def test_load_builtins_returns_empty(tmp_path: Path) -> None:
    loader = SkillLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    assert loader._load_builtins() == []


# 验证 _load_builtins 在 load_all 中被调用。
# 子类覆盖 _load_builtins 记录调用，断言 load_all 触发标志。
def test_load_builtins_called_in_load_all(tmp_path: Path) -> None:
    class _RecordingLoader(SkillLoader):
        def __init__(self, project_dir: Path, user_dir: Path) -> None:
            super().__init__(project_dir, user_dir)
            self.builtins_called = False

        def _load_builtins(self) -> list[SkillDef]:
            self.builtins_called = True
            return []

    loader = _RecordingLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    loader.load_all()
    assert loader.builtins_called is True


# ---------- 热重载：get 重读磁盘与缓存回退 ----------


# 验证 get(name) 重读磁盘返回新版本。
# 写入 v1 → load_all → 改写 v2 → get(name) 返回 v2。
def test_get_reloads_from_disk_returns_new_version(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    path = _write_skill(project_dir / "commit.md", "commit", "v1")
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    path.write_text("---\nname: commit\ndescription: v2\n---\nbody\n", encoding="utf-8")
    refreshed = loader.get("commit")
    assert refreshed is not None
    assert refreshed.description == "v2"


# 验证 get(name) 文件缺失回退 _cache。
# load_all 后删除文件，get(name) 返回缓存版本。
def test_get_falls_back_to_cache_on_missing_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    path = _write_skill(project_dir / "commit.md", "commit", "提交")
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    path.unlink()
    result = loader.get("commit")
    assert result is not None
    assert result.description == "提交"


# 验证 get(name) 重读 YAML 语法错误回退缓存。
# load_all 后改写为非法 YAML，get(name) 返回缓存版本。
def test_get_falls_back_to_cache_on_yaml_error(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    path = _write_skill(project_dir / "commit.md", "commit", "提交")
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    path.write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")
    result = loader.get("commit")
    assert result is not None
    assert result.description == "提交"


# 验证 get(name) 不存在返回 None。
# 查询不存在的 name，断言返回 None。
def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    project_dir = tmp_path / "empty" / "skills"
    project_dir.mkdir(parents=True)
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    assert loader.get("nonexistent") is None


# 验证 get(name) source_path 为 None 不重读磁盘。
# 注入 source_path=None 的 SkillDef，断言 get 返回同一对象不重读。
def test_get_skips_reload_when_source_path_none(tmp_path: Path) -> None:
    loader = SkillLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    builtin = SkillDef(name="b", description="d", prompt_body="b", source_path=None)
    loader._skills["b"] = builtin
    result = loader.get("b")
    assert result is builtin


# ---------- 热重载：needs_reload 与 reload ----------


# 验证 needs_reload 检测目录 modtime 变化。
# load_all 后新增文件改变目录 modtime，断言 needs_reload 返回 True。
def test_needs_reload_detects_modtime_change(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    project_dir.mkdir(parents=True)
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    time.sleep(0.05)
    _write_skill(project_dir / "new.md", "new", "新")
    assert loader.needs_reload() is True


# 验证 needs_reload 检测新目录创建。
# load_all 时目录不存在，之后创建目录，断言 needs_reload 返回 True。
def test_needs_reload_detects_new_directory(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    project_dir.mkdir(parents=True)
    assert loader.needs_reload() is True


# 验证 needs_reload 无变化返回 False。
# load_all 后无任何改动，断言 needs_reload 返回 False。
def test_needs_reload_no_change_returns_false(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    project_dir.mkdir(parents=True)
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    assert loader.needs_reload() is False


# 验证 reload 全量刷新 catalog。
# load_all 后新增 Skill 文件，reload 后 get_catalog 包含新 Skill。
def test_reload_refreshes_catalog(tmp_path: Path) -> None:
    project_dir = tmp_path / "project" / "skills"
    project_dir.mkdir(parents=True)
    loader = SkillLoader(project_dir=project_dir, user_dir=tmp_path / "noexist")
    loader.load_all()
    assert loader.get_catalog() == []
    _write_skill(project_dir / "new.md", "new", "新")
    loader.reload()
    catalog = loader.get_catalog()
    assert ("new", "新") in catalog


# 验证 reload 触发 reload 回调。
# 注册回调后 reload，断言回调被调用。
def test_reload_triggers_callback(tmp_path: Path) -> None:
    loader = SkillLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    calls: list[int] = []
    loader.register_reload_callback(lambda: calls.append(1))
    loader.reload()
    assert calls == [1]


# 验证 register_reload_callback 支持多回调。
# 注册两个回调后 reload，断言两个回调都被调用。
def test_reload_triggers_multiple_callbacks(tmp_path: Path) -> None:
    loader = SkillLoader(project_dir=tmp_path / "p", user_dir=tmp_path / "u")
    calls: list[str] = []
    loader.register_reload_callback(lambda: calls.append("a"))
    loader.register_reload_callback(lambda: calls.append("b"))
    loader.reload()
    assert calls == ["a", "b"]


# ---------- __init__.py 重导出 ----------


# 验证 seacode.skills 包重导出 SkillLoader。
# 从包根导入模块，断言 SkillLoader 与子模块同源。
def test_skills_package_reexports_skill_loader() -> None:
    import seacode.skills as skills_pkg

    assert skills_pkg.SkillLoader is SkillLoader
