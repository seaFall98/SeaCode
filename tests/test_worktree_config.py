"""WorktreeConfig 与 validate_worktree 单测：覆盖默认值、合法输入、字段缺失与非法类型。"""

from __future__ import annotations

from typing import Any

import pytest

from seacode.config import ConfigError, WorktreeConfig, load_config
from seacode.validator import validate_worktree

# ---------------------------------------------------------------------------
# WorktreeConfig 默认值
# ---------------------------------------------------------------------------


# 验证 WorktreeConfig 默认值。
# 不传任何参数构造 WorktreeConfig，断言三个字段均为默认值。
def test_worktree_config_defaults() -> None:
    cfg = WorktreeConfig()

    assert cfg.symlink_directories == ()
    assert cfg.stale_cleanup_interval == 3600
    assert cfg.stale_cutoff_hours == 24


# 验证 WorktreeConfig 可通过参数构造非默认值。
# 传入自定义值构造 WorktreeConfig，断言三个字段反映新值。
def test_worktree_config_custom_values() -> None:
    cfg = WorktreeConfig(
        symlink_directories=(".venv", "node_modules"),
        stale_cleanup_interval=1800,
        stale_cutoff_hours=48,
    )

    assert cfg.symlink_directories == (".venv", "node_modules")
    assert cfg.stale_cleanup_interval == 1800
    assert cfg.stale_cutoff_hours == 48


# ---------------------------------------------------------------------------
# validate_worktree
# ---------------------------------------------------------------------------


# 验证 validate_worktree 合法输入返回 WorktreeConfig。
# 传入合法 dict，断言返回 WorktreeConfig 且字段匹配。
def test_validate_worktree_valid_input() -> None:
    data = {
        "symlink_directories": [".venv", "node_modules"],
        "stale_cleanup_interval": 7200,
        "stale_cutoff_hours": 12,
    }

    cfg = validate_worktree(data)

    assert isinstance(cfg, WorktreeConfig)
    assert cfg.symlink_directories == (".venv", "node_modules")
    assert cfg.stale_cleanup_interval == 7200
    assert cfg.stale_cutoff_hours == 12


# 验证 validate_worktree 缺字段用默认值。
# 传入空 dict，断言三个字段均为默认值。
def test_validate_worktree_missing_fields_use_defaults() -> None:
    cfg = validate_worktree({})

    assert isinstance(cfg, WorktreeConfig)
    assert cfg.symlink_directories == ()
    assert cfg.stale_cleanup_interval == 3600
    assert cfg.stale_cutoff_hours == 24


# 验证 validate_worktree symlink_directories 为 None 时返回空元组。
# 传入 symlink_directories: None，断言返回空元组而非抛异常。
def test_validate_worktree_symlinks_none_returns_empty() -> None:
    data: dict[str, Any] = {"symlink_directories": None}

    cfg = validate_worktree(data)

    assert cfg.symlink_directories == ()


# 验证 validate_worktree symlink_directories 非法类型抛 ConfigError。
# 传入 symlink_directories: "not-a-list"，断言抛 ConfigError。
def test_validate_worktree_symlinks_non_list_raises() -> None:
    data: dict[str, Any] = {"symlink_directories": "not-a-list"}

    with pytest.raises(ConfigError, match="symlink_directories"):
        validate_worktree(data)


# 验证 validate_worktree stale_cleanup_interval 非正整数抛 ConfigError。
# 传入 stale_cleanup_interval: 0，断言抛 ConfigError。
def test_validate_worktree_interval_zero_raises() -> None:
    data: dict[str, Any] = {"stale_cleanup_interval": 0}

    with pytest.raises(ConfigError, match="stale_cleanup_interval"):
        validate_worktree(data)


# 验证 validate_worktree stale_cleanup_interval 负数抛 ConfigError。
# 传入 stale_cleanup_interval: -1，断言抛 ConfigError。
def test_validate_worktree_interval_negative_raises() -> None:
    data: dict[str, Any] = {"stale_cleanup_interval": -1}

    with pytest.raises(ConfigError, match="stale_cleanup_interval"):
        validate_worktree(data)


# 验证 validate_worktree stale_cutoff_hours 非正整数抛 ConfigError。
# 传入 stale_cutoff_hours: 0，断言抛 ConfigError。
def test_validate_worktree_cutoff_zero_raises() -> None:
    data: dict[str, Any] = {"stale_cutoff_hours": 0}

    with pytest.raises(ConfigError, match="stale_cutoff_hours"):
        validate_worktree(data)


# 验证 validate_worktree interval 为 bool 时抛 ConfigError（bool 是 int 子类）。
# 传入 stale_cleanup_interval: True，断言抛 ConfigError。
def test_validate_worktree_interval_bool_raises() -> None:
    data: dict[str, Any] = {"stale_cleanup_interval": True}

    with pytest.raises(ConfigError, match="stale_cleanup_interval"):
        validate_worktree(data)


# 验证 validate_worktree interval 为字符串时抛 ConfigError。
# 传入 stale_cleanup_interval: "3600"，断言抛 ConfigError。
def test_validate_worktree_interval_string_raises() -> None:
    data: dict[str, Any] = {"stale_cleanup_interval": "3600"}

    with pytest.raises(ConfigError, match="stale_cleanup_interval"):
        validate_worktree(data)


# 验证 validate_worktree 非 dict 输入返回默认 WorktreeConfig。
# 传入 None，断言返回默认 WorktreeConfig。
def test_validate_worktree_non_dict_returns_default() -> None:
    cfg = validate_worktree(None)  # type: ignore[arg-type]

    assert isinstance(cfg, WorktreeConfig)
    assert cfg.symlink_directories == ()
    assert cfg.stale_cleanup_interval == 3600
    assert cfg.stale_cutoff_hours == 24


# 验证 validate_worktree 把 list[str] 元素统一转为 str。
# 传入含数字的 list，断言返回的 symlink_directories 元素均为 str。
def test_validate_worktree_coerces_list_elements_to_str() -> None:
    data: dict[str, Any] = {"symlink_directories": [".venv", 123, "node_modules"]}

    cfg = validate_worktree(data)

    assert cfg.symlink_directories == (".venv", "123", "node_modules")
    assert all(isinstance(s, str) for s in cfg.symlink_directories)


# ---------------------------------------------------------------------------
# load_config 集成测试：worktree 段解析与三层合并
# ---------------------------------------------------------------------------


# 写入仅含测试占位符的 Provider YAML，可选附加 worktree 段。
def _write_config(
    path: Any, *, name: str, worktree_yaml: str = ""
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "providers:",
        f"  - name: {name}",
        "    protocol: openai-compat",
        "    model: test-model",
        "    base_url: https://api.example.test",
        "    api_key: test-key",
        "    thinking: false",
    ]
    if worktree_yaml:
        lines.append(worktree_yaml)
    path.write_text("\n".join(lines), encoding="utf-8")


# 验证 load_config 解析 worktree 段。
# 写入含 worktree 段的配置，断言 config.worktree 字段反映配置值。
def test_load_config_parses_worktree_section(tmp_path: Any) -> None:
    project = tmp_path / "project"
    worktree_yaml = (
        "worktree:\n"
        "  symlink_directories:\n"
        "    - .venv\n"
        "    - node_modules\n"
        "  stale_cleanup_interval: 7200\n"
        "  stale_cutoff_hours: 48\n"
    )
    _write_config(
        project / ".seacode" / "config.local.yaml",
        name="test",
        worktree_yaml=worktree_yaml,
    )

    config = load_config(cwd=project, home=tmp_path / "home")

    assert config.worktree.symlink_directories == (".venv", "node_modules")
    assert config.worktree.stale_cleanup_interval == 7200
    assert config.worktree.stale_cutoff_hours == 48


# 验证 load_config 无 worktree 段时返回默认 WorktreeConfig。
# 写入不含 worktree 段的配置，断言 config.worktree 为默认值。
def test_load_config_missing_worktree_uses_defaults(tmp_path: Any) -> None:
    project = tmp_path / "project"
    _write_config(project / ".seacode" / "config.local.yaml", name="test")

    config = load_config(cwd=project, home=tmp_path / "home")

    assert config.worktree.symlink_directories == ()
    assert config.worktree.stale_cleanup_interval == 3600
    assert config.worktree.stale_cutoff_hours == 24


# 验证 load_config 三层合并 worktree 段。
# 用户级配置 symlink_directories=[.venv]；项目本地级覆盖为 [node_modules]，断言最终值为后者。
def test_load_config_merges_worktree_layers(tmp_path: Any) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_config(
        home / ".seacode" / "config.yaml",
        name="user",
        worktree_yaml=(
            "worktree:\n"
            "  symlink_directories:\n"
            "    - .venv\n"
            "  stale_cleanup_interval: 1800\n"
        ),
    )
    _write_config(
        project / ".seacode" / "config.local.yaml",
        name="local",
        worktree_yaml=(
            "worktree:\n"
            "  symlink_directories:\n"
            "    - node_modules\n"
            "  stale_cutoff_hours: 12\n"
        ),
    )

    config = load_config(cwd=project, home=home)

    # 后层 symlink_directories 覆盖前层。
    assert config.worktree.symlink_directories == ("node_modules",)
    # 前层的 interval 保留（后层未配置，默认值 3600 不覆盖）。
    assert config.worktree.stale_cleanup_interval == 1800
    # 后层的 cutoff 覆盖默认值 24。
    assert config.worktree.stale_cutoff_hours == 12
