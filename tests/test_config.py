from __future__ import annotations

from pathlib import Path

import pytest

from seacode.config import ConfigError, ProviderConfig, load_config


# 写入仅含测试占位符的 Provider YAML。
def _write_config(path: Path, *, name: str, api_key: str = "test-key") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "providers:",
                f"  - name: {name}",
                "    protocol: openai-compat",
                "    model: test-model",
                "    base_url: https://api.example.test",
                f"    api_key: {api_key}",
                "    thinking: false",
            )
        ),
        encoding="utf-8",
    )


# 验证项目本地配置完整替换用户和项目默认 Provider 列表。
# 三层文件都使用独立名称，避免测试只验证到部分覆盖。
def test_load_config_prefers_later_provider_layer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_config(home / ".seacode" / "config.yaml", name="user-default")
    _write_config(project / ".seacode" / "config.yaml", name="project-default")
    _write_config(project / ".seacode" / "config.local.yaml", name="project-local")

    config = load_config(cwd=project, home=home)

    assert [provider.name for provider in config.providers] == ["project-local"]


# 验证解析失败不会把 Provider 密钥放入异常文本。
# 故意省略必需字段，同时使用可识别的秘密占位符。
def test_invalid_config_error_redacts_api_key(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            (
                "providers:",
                "  - name: broken",
                "    protocol: openai",
                "    base_url: https://api.example.test",
                "    api_key: secret-value-must-not-appear",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_config(path)

    assert "secret-value-must-not-appear" not in str(error.value)
    assert "model" in str(error.value)


# 验证空 YAML 密钥可回退到对应协议的环境变量。
# 该回退不改变 YAML 作为主配置路径的优先级。
def test_provider_uses_protocol_environment_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ProviderConfig(
        name="environment",
        protocol="openai",
        model="test-model",
        base_url="https://api.example.test",
        api_key="",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-key")

    assert provider.resolve_api_key() == "environment-test-key"


# 验证缺少任何配置时给出可修复的发现路径提示。
# 临时目录不创建配置文件，确保不会依赖机器上的真实配置。
def test_load_config_reports_missing_local_configuration(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config.local.yaml"):
        load_config(cwd=tmp_path / "project", home=tmp_path / "home")


# 验证未知协议与非法端点在配置边界被拒绝。
# 参数化替换单一字段，避免错误由无关的 YAML 结构触发。
@pytest.mark.parametrize(
    ("field_name", "field_value", "expected_message"),
    [
        ("protocol", "unknown", "unsupported protocol"),
        ("base_url", "not-a-url", "invalid base_url"),
    ],
)
def test_provider_rejects_unknown_protocol_and_invalid_endpoint(
    tmp_path: Path,
    field_name: str,
    field_value: str,
    expected_message: str,
) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, name="invalid")
    content = path.read_text(encoding="utf-8").replace(
        f"    {field_name}: "
        + ("openai-compat" if field_name == "protocol" else "https://api.example.test"),
        f"    {field_name}: {field_value}",
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=expected_message):
        load_config(path)


# 验证多个 Provider 的同名配置不会产生歧义选择。
# 两个有效条目只修改 name，确保校验失败原因专属于重复名称。
def test_provider_names_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, name="duplicate")
    content = path.read_text(encoding="utf-8")
    duplicate = content.replace("providers:", "", 1)
    path.write_text(content + duplicate, encoding="utf-8")

    with pytest.raises(ConfigError, match="unique"):
        load_config(path)
