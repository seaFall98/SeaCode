# 团队配置校验：teammate_mode / enable_coordinator_mode 字段、合并语义、coordinator 提示词。
# 覆盖 AppConfig 默认值、_parse_teammate_fields 校验、load_config 三层合并、prompt 分支。
from __future__ import annotations

from pathlib import Path

import pytest

from seacode.config import AppConfig, ConfigError, ProviderConfig, load_config
from seacode.prompts import build_environment_context, build_system_prompt


# 构造合法的 minimal provider dict 供配置文件测试复用。
def _minimal_provider_dict() -> dict:
    return {
        "name": "p1",
        "protocol": "anthropic",
        "model": "claude-3-5-sonnet",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-test",
    }


# 写入配置文件并返回路径。
def _write_config(path: Path, data: dict) -> Path:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AppConfig 默认值
# ---------------------------------------------------------------------------


# 验证 AppConfig 新字段默认值不影响既有行为。
# teammate_mode="" / enable_coordinator_mode=False。
def test_app_config_defaults() -> None:
    provider = ProviderConfig(
        name="p1",
        protocol="anthropic",
        model="claude-3-5-sonnet",
        base_url="https://api.anthropic.com",
        api_key="sk-test",
    )
    config = AppConfig(providers=(provider,))
    assert config.teammate_mode == ""
    assert config.enable_coordinator_mode is False


# ---------------------------------------------------------------------------
# _parse_teammate_fields 校验
# ---------------------------------------------------------------------------


# 验证单文件配置含 teammate_mode 与 enable_coordinator_mode 字段。
# 写入配置文件，load_config 返回的 AppConfig 含新字段。
def test_load_config_with_teammate_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_data = {
        "providers": [_minimal_provider_dict()],
        "teammate_mode": "tmux",
        "enable_coordinator_mode": True,
    }
    _write_config(tmp_path / ".seacode" / "config.yaml", config_data)
    config = load_config()
    assert config.teammate_mode == "tmux"
    assert config.enable_coordinator_mode is True


# 验证缺字段用默认值。
# 配置文件不含 teammate_mode / enable_coordinator_mode，结果为默认值。
def test_load_config_missing_fields_use_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_data = {"providers": [_minimal_provider_dict()]}
    _write_config(tmp_path / ".seacode" / "config.yaml", config_data)
    config = load_config()
    assert config.teammate_mode == ""
    assert config.enable_coordinator_mode is False


# 验证 teammate_mode 非 str 抛 ConfigError。
# 配置文件 teammate_mode=123（int），load_config 抛错。
def test_load_config_teammate_mode_non_str_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_data = {
        "providers": [_minimal_provider_dict()],
        "teammate_mode": 123,
    }
    _write_config(tmp_path / ".seacode" / "config.yaml", config_data)
    with pytest.raises(ConfigError):
        load_config()


# 验证 enable_coordinator_mode 非 bool 抛 ConfigError。
# 配置文件 enable_coordinator_mode="yes"（str），load_config 抛错。
def test_load_config_enable_coordinator_non_bool_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_data = {
        "providers": [_minimal_provider_dict()],
        "enable_coordinator_mode": "yes",
    }
    _write_config(tmp_path / ".seacode" / "config.yaml", config_data)
    with pytest.raises(ConfigError):
        load_config()


# ---------------------------------------------------------------------------
# merge_configs 三层合并语义
# ---------------------------------------------------------------------------


# 验证 teammate_mode 后层完整替换前层。
# 用户级 teammate_mode="tmux"，项目级 teammate_mode=""，结果为 ""。
def test_merge_configs_teammate_mode_last_layer_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # 用户级配置 teammate_mode="tmux"。
    _write_config(
        tmp_path / ".seacode" / "config.yaml",
        {"providers": [_minimal_provider_dict()], "teammate_mode": "tmux"},
    )
    # 项目级配置 teammate_mode=""（显式空串）。
    _write_config(
        project_dir / ".seacode" / "config.yaml",
        {"providers": [_minimal_provider_dict()], "teammate_mode": ""},
    )
    config = load_config(cwd=project_dir)
    assert config.teammate_mode == ""


# 验证 enable_coordinator_mode 任一层开启即开启（OR 语义）。
# 用户级 enable_coordinator_mode=True，项目级 enable_coordinator_mode=False，结果为 True。
def test_merge_configs_coordinator_mode_or_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_config(
        tmp_path / ".seacode" / "config.yaml",
        {"providers": [_minimal_provider_dict()], "enable_coordinator_mode": True},
    )
    _write_config(
        project_dir / ".seacode" / "config.yaml",
        {"providers": [_minimal_provider_dict()], "enable_coordinator_mode": False},
    )
    config = load_config(cwd=project_dir)
    assert config.enable_coordinator_mode is True


# ---------------------------------------------------------------------------
# build_system_prompt coordinator 分支
# ---------------------------------------------------------------------------


# 验证 coordinator_mode=True 时返回协调者提示词。
# build_system_prompt(coordinator_mode=True) 含 6 节标题。
def test_build_system_prompt_coordinator_mode_true() -> None:
    prompt = build_system_prompt(coordinator_mode=True)
    assert "## Your Role" in prompt
    assert "## Your Tools" in prompt
    assert "## Workers" in prompt
    assert "## Task Workflow" in prompt
    assert "## Writing Worker Prompts" in prompt
    assert "## Example Session" in prompt


# 验证 coordinator_mode=False 时返回既有提示词（不含 Coordinator 内容）。
# build_system_prompt(coordinator_mode=False) 含 Identity 段落，不含 ## Your Role。
def test_build_system_prompt_coordinator_mode_false() -> None:
    prompt = build_system_prompt(coordinator_mode=False, work_dir=".")
    assert "SeaCode" in prompt
    assert "## Your Role" not in prompt


# ---------------------------------------------------------------------------
# build_environment_context 注入 agent_catalog
# ---------------------------------------------------------------------------


# 验证 build_environment_context 接受 agent_catalog 参数并注入。
# 传入 agent_catalog="## Available Sub-Agent Types"，断言输出含该字符串。
def test_build_environment_context_with_agent_catalog() -> None:
    catalog = "## Available Sub-Agent Types\n- explore: code exploration"
    context = build_environment_context(work_dir=".", agent_catalog=catalog)
    assert "Available Sub-Agent Types" in context
    assert "explore" in context


# 验证 build_environment_context 不传 agent_catalog 时不报错。
# agent_catalog 默认空串，输出仅含工作目录等基本信息。
def test_build_environment_context_without_agent_catalog() -> None:
    context = build_environment_context(work_dir="/tmp/test")
    assert "Current working directory: /tmp/test" in context
    assert "Available Sub-Agent Types" not in context
