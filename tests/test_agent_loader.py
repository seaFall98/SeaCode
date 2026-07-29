"""子 Agent 加载器的单元测试：覆盖三级搜索、热重载、缓存回退与内置加载。"""

from __future__ import annotations

from pathlib import Path

import pytest

from seacode.agents.loader import AgentLoader


# 构造合法的 .md 子 Agent 定义文件并返回路径。
# frontmatter 含 name/description，body 为 system_prompt 内容。
def _write_agent_md(
    dir_path: Path, name: str, description: str, body: str = "body"
) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    return path


# 构造非法 .md 文件（缺 name）用于测试解析失败跳过。
def _write_invalid_md(dir_path: Path, filename: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / filename
    path.write_text(
        "---\ndescription: missing name\n---\nbody",
        encoding="utf-8",
    )
    return path


# 用 monkeypatch 替换 Path.home，让用户级目录指向临时目录。
def _patch_home(monkeypatch: pytest.MonkeyPatch, home_dir: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: home_dir)


# ---------------------------------------------------------------------------
# 三级搜索优先级
# ---------------------------------------------------------------------------


# 验证项目级同名定义覆盖用户级与内置级。
# 构造同名项目级与用户级 .md，断言 loader.get 返回 source=project。
def test_loader_project_overrides_user_and_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    _write_agent_md(tmp_path / ".seacode" / "agents", "test", "project")
    _write_agent_md(home_dir / ".seacode" / "agents", "test", "user")
    loader = AgentLoader(tmp_path, enable_verification=False)
    agent_def = loader.get("test")
    assert agent_def is not None
    assert agent_def.source == "project"


# 验证项目级不存在时用户级生效。
# 只在用户级目录放 .md，断言 loader.get 返回 source=user。
def test_loader_user_level_when_project_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    _write_agent_md(home_dir / ".seacode" / "agents", "test", "user")
    loader = AgentLoader(tmp_path, enable_verification=False)
    agent_def = loader.get("test")
    assert agent_def is not None
    assert agent_def.source == "user"


# 验证项目级与用户级都不存在时回退到内置级。
# 不构造任何项目级或用户级 .md，断言 loader.get("Explore") 返回 source=builtin。
def test_loader_falls_back_to_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    agent_def = loader.get("Explore")
    assert agent_def is not None
    assert agent_def.source == "builtin"


# 验证内置 Explore 默认继承当前 Provider 模型。
# 不提供项目级覆盖，断言内置定义不会携带供应商专属模型别名。
def test_builtin_explore_inherits_provider_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)

    agent_def = loader.get("Explore")

    assert agent_def is not None
    assert agent_def.model == "inherit"


# 验证目录不存在时仅返回内置定义。
# work_dir 与 home_dir 都不含 .seacode/agents，断言 list_agents 仅含内置 3 个。
def test_loader_returns_only_builtins_when_dirs_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    agents = loader.list_agents()
    agent_names = [name for name, _ in agents]
    assert "Explore" in agent_names
    assert "Plan" in agent_names
    assert "general-purpose" in agent_names
    assert "Verification" not in agent_names


# ---------------------------------------------------------------------------
# 单文件解析失败与 list_agents
# ---------------------------------------------------------------------------


# 验证单文件解析失败 warning 跳过不影响其它文件。
# 项目级目录含一个非法 .md 与一个合法 .md，断言 list_agents 含合法那个。
def test_loader_skips_invalid_file_and_keeps_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    agents_dir = tmp_path / ".seacode" / "agents"
    _write_invalid_md(agents_dir, "bad.md")
    _write_agent_md(agents_dir, "good", "good agent")
    loader = AgentLoader(tmp_path, enable_verification=False)
    agent_def = loader.get("good")
    assert agent_def is not None
    assert agent_def.agent_type == "good"
    assert loader.get("bad") is None or loader.get("bad") is None


# 验证 list_agents 返回元组列表 [(agent_type, when_to_use)]。
# 构造 loader，断言每个元素是 (str, str) 元组。
def test_loader_list_agents_returns_tuples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    agents = loader.list_agents()
    assert all(isinstance(item, tuple) and len(item) == 2 for item in agents)
    assert all(isinstance(name, str) and isinstance(desc, str) for name, desc in agents)


# ---------------------------------------------------------------------------
# get 热重载与缓存回退
# ---------------------------------------------------------------------------


# 验证 get(name) 热重载返回文件最新版本。
# 先写 v1 版本，调用 get，再覆盖文件为 v2，再调 get，断言返回 v2。
def test_loader_get_hot_reloads_latest_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    agents_dir = tmp_path / ".seacode" / "agents"
    path = _write_agent_md(agents_dir, "test", "v1")
    loader = AgentLoader(tmp_path, enable_verification=False)
    v1 = loader.get("test")
    assert v1 is not None
    assert v1.when_to_use == "v1"
    # 覆盖文件为 v2 版本。
    path.write_text(
        "---\nname: test\ndescription: v2\n---\nnew body",
        encoding="utf-8",
    )
    v2 = loader.get("test")
    assert v2 is not None
    assert v2.when_to_use == "v2"


# 验证 get(name) 文件被改为非法内容时回退缓存。
# 先写合法文件，get 后改为非法（缺 name），再调 get，断言回退缓存返回原版本。
def test_loader_get_falls_back_to_cache_on_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    agents_dir = tmp_path / ".seacode" / "agents"
    path = _write_agent_md(agents_dir, "test", "valid")
    loader = AgentLoader(tmp_path, enable_verification=False)
    cached = loader.get("test")
    assert cached is not None
    assert cached.when_to_use == "valid"
    # 覆盖文件为非法内容。
    path.write_text(
        "---\ndescription: missing name\n---\nbody",
        encoding="utf-8",
    )
    reloaded = loader.get("test")
    assert reloaded is not None
    # 回退缓存返回原版本。
    assert reloaded.when_to_use == "valid"


# 验证 get(name) 不存在返回 None。
# 直接查询不存在的名称，断言返回 None。
def test_loader_get_returns_none_for_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    assert loader.get("nonexistent") is None


# ---------------------------------------------------------------------------
# _load_builtins 与 enable_verification
# ---------------------------------------------------------------------------


# 验证 _load_builtins 默认加载 Explore / Plan / general-purpose 三个内置子 Agent。
# enable_verification=False，断言三个内置都可用。
def test_loader_loads_three_builtins_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    assert loader.get("Explore") is not None
    assert loader.get("Plan") is not None
    assert loader.get("general-purpose") is not None


# 验证 enable_verification=True 时加载 Verification 内置子 Agent。
# 构造 enable_verification=True 的 loader，断言 Verification 可用。
def test_loader_loads_verification_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=True)
    assert loader.get("Verification") is not None


# 验证 enable_verification=False 时跳过 Verification。
# 构造 enable_verification=False 的 loader，断言 Verification 不可用。
def test_loader_skips_verification_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    assert loader.get("Verification") is None


# 验证内置 .md 通过 importlib.resources 加载且 file_path 为 None、source 为 builtin。
# 构造 loader，取 Explore 定义，断言 file_path 与 source 字段。
def test_loader_builtins_have_null_file_path_and_builtin_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    explore = loader.get("Explore")
    assert explore is not None
    assert explore.file_path is None
    assert explore.source == "builtin"


# 验证内置 .md 内容非空（采用 SeaCode 品牌身份与 v1 行为约束）。
# 取 Explore / Plan / general-purpose 三个内置的 system_prompt，断言非空。
def test_loader_builtins_have_non_empty_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    _patch_home(monkeypatch, home_dir)
    loader = AgentLoader(tmp_path, enable_verification=False)
    for name in ("Explore", "Plan", "general-purpose"):
        agent_def = loader.get(name)
        assert agent_def is not None
        assert agent_def.system_prompt.strip() != ""
