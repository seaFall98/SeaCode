from __future__ import annotations

from pathlib import Path

import pytest

from seacode.config import (
    ConfigError,
    MCPServerConfig,
    ProviderConfig,
    build_child_env,
    load_config,
    resolve_env_vars,
)


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


# ---------------------------------------------------------------------------
# MCP 服务器配置解析
# ---------------------------------------------------------------------------


# 写入含 mcp_servers 段的完整配置文件。
def _write_config_with_mcp(
    path: Path,
    *,
    name: str = "primary",
    mcp_block: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "providers:",
        f"  - name: {name}",
        "    protocol: openai-compat",
        "    model: test-model",
        "    base_url: https://api.example.test",
        "    api_key: test-key",
        "    thinking: false",
    ]
    if mcp_block:
        parts.append(mcp_block)
    path.write_text("\n".join(parts), encoding="utf-8")


# 验证 stdio 类型 MCPServerConfig 的 is_stdio 属性。
def test_mcp_server_config_stdio_detection() -> None:
    stdio = MCPServerConfig(name="fs", command="npx")
    assert stdio.is_stdio is True

    http = MCPServerConfig(name="remote", url="https://mcp.example.com")
    assert http.is_stdio is False


# 验证 mcp_servers 段正确解析 stdio 服务器配置。
def test_parse_mcp_stdio_server(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(
        path,
        mcp_block=(
            "mcp_servers:\n"
            "  - name: filesystem\n"
            "    command: npx\n"
            "    args:\n"
            '      - "-y"\n'
            '      - "@modelcontextprotocol/server-filesystem"\n'
            "    env:\n"
            "      NODE_PATH: /usr/lib/node\n"
        ),
    )

    config = load_config(path)

    assert len(config.mcp_servers) == 1
    server = config.mcp_servers[0]
    assert server.name == "filesystem"
    assert server.command == "npx"
    assert server.args == ("-y", "@modelcontextprotocol/server-filesystem")
    assert server.env == {"NODE_PATH": "/usr/lib/node"}
    assert server.is_stdio is True


# 验证 mcp_servers 段正确解析 HTTP 服务器配置。
def test_parse_mcp_http_server(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(
        path,
        mcp_block=(
            "mcp_servers:\n"
            "  - name: remote\n"
            "    url: https://mcp.example.com/sse\n"
            "    headers:\n"
            '      Authorization: "Bearer token"\n'
        ),
    )

    config = load_config(path)

    assert len(config.mcp_servers) == 1
    server = config.mcp_servers[0]
    assert server.name == "remote"
    assert server.url == "https://mcp.example.com/sse"
    assert server.headers == {"Authorization": "Bearer token"}
    assert server.is_stdio is False


# 验证 mcp_servers 缺失时返回空元组，不影响配置加载。
def test_mcp_servers_optional(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(path)

    config = load_config(path)
    assert config.mcp_servers == ()


# 验证 mcp_servers 条目同时缺失 command 与 url 时报错。
def test_mcp_server_requires_command_or_url(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(
        path,
        mcp_block="mcp_servers:\n  - name: broken\n",
    )

    with pytest.raises(ConfigError, match="command or url"):
        load_config(path)


# 验证 mcp_servers 条目 name 缺失时报错。
def test_mcp_server_requires_name(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(
        path,
        mcp_block="mcp_servers:\n  - command: npx\n",
    )

    with pytest.raises(ConfigError, match="non-empty name"):
        load_config(path)


# 验证 mcp_servers 条目 name 重复时报错。
def test_mcp_server_names_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(
        path,
        mcp_block=(
            "mcp_servers:\n"
            "  - name: dup\n"
            "    command: npx\n"
            "  - name: dup\n"
            "    url: https://mcp.example.com\n"
        ),
    )

    with pytest.raises(ConfigError, match="unique"):
        load_config(path)


# 验证 mcp_servers 非 list 时报错。
def test_mcp_servers_must_be_list(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config_with_mcp(
        path,
        mcp_block="mcp_servers: not-a-list\n",
    )

    with pytest.raises(ConfigError, match="must be a list"):
        load_config(path)


# ---------------------------------------------------------------------------
# MCP 三层合并
# ---------------------------------------------------------------------------


# 验证三层配置按 name 去重覆盖合并 mcp_servers。
# 用户层与项目层有同名 fs，项目本地层有新名 remote；合并后 fs 来自后层，remote 追加。
def test_mcp_servers_merge_by_name_across_layers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write_config_with_mcp(
        home / ".seacode" / "config.yaml",
        mcp_block=(
            "mcp_servers:\n"
            "  - name: fs\n"
            "    command: user-npx\n"
        ),
    )
    _write_config_with_mcp(
        project / ".seacode" / "config.yaml",
        mcp_block=(
            "mcp_servers:\n"
            "  - name: fs\n"
            "    command: project-npx\n"
            "  - name: git\n"
            "    command: git-npx\n"
        ),
    )
    _write_config_with_mcp(
        project / ".seacode" / "config.local.yaml",
        mcp_block=(
            "mcp_servers:\n"
            "  - name: remote\n"
            "    url: https://mcp.example.com\n"
        ),
    )

    config = load_config(cwd=project, home=home)

    servers = {s.name: s for s in config.mcp_servers}
    # fs 在用户层和项目层都有，后层（project）覆盖前层（home）。
    assert servers["fs"].command == "project-npx"
    # git 只在项目层，保留。
    assert servers["git"].command == "git-npx"
    # remote 只在本地层，追加。
    assert servers["remote"].url == "https://mcp.example.com"


# ---------------------------------------------------------------------------
# resolve_env_vars / build_child_env
# ---------------------------------------------------------------------------


# 验证 resolve_env_vars 展开已定义的环境变量。
def test_resolve_env_vars_expands_defined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret-value")
    assert resolve_env_vars("Bearer ${MCP_TOKEN}") == "Bearer secret-value"


# 验证 resolve_env_vars 对未定义变量保留原字面量。
def test_resolve_env_vars_keeps_undefined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNDEFINED_VAR", raising=False)
    assert resolve_env_vars("${UNDEFINED_VAR}") == "${UNDEFINED_VAR}"


# 验证 build_child_env 只注入 PATH 与显式声明的 env，不继承全部环境。
def test_build_child_env_injects_path_and_declared_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SECRET_KEY", "should-not-leak")
    monkeypatch.setenv("NODE_PATH", "/usr/lib/node")

    env = build_child_env({"NODE_PATH": "/custom/node"})

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["NODE_PATH"] == "/custom/node"
    assert "SECRET_KEY" not in env


# 验证 build_child_env 展开声明 env 中的 ${VAR} 占位符。
def test_build_child_env_expands_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("API_TOKEN", "tok-123")

    env = build_child_env({"AUTH": "Bearer ${API_TOKEN}"})

    assert env["AUTH"] == "Bearer tok-123"


# 验证 build_child_env 在 PATH 缺失时不注入空 PATH。
def test_build_child_env_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATH", raising=False)
    env = build_child_env(None)
    assert "PATH" not in env


# 验证 build_child_env 接受 None 参数返回空 dict（除 PATH）。
def test_build_child_env_none_returns_path_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_child_env(None)
    assert env == {"PATH": "/usr/bin"}
