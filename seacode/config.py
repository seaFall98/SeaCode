"""本地模型配置的发现、校验与安全解析。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml

SUPPORTED_PROTOCOLS: Final = frozenset({"anthropic", "openai", "openai-compat"})
_ENV_KEY_NAMES: Final = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

# ${VAR} 环境变量占位符的正则；未定义变量保留原字面量。
_ENV_VAR_RE: Final = re.compile(r"\$\{([^}]+)\}")


class ConfigError(Exception):
    """表示不安全或不可用的本地配置。"""


@dataclass(frozen=True)
class ProviderConfig:
    """表示一个可在启动时选择的模型连接。"""

    name: str
    protocol: str
    model: str
    base_url: str
    api_key: str
    thinking: bool = False

    # 优先使用 YAML 密钥，空值时才兼容外部协议的环境变量。
    def resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(_ENV_KEY_NAMES[self.protocol], "")


@dataclass(frozen=True)
class MCPServerConfig:
    """单个 MCP 服务器的连接配置：command/args 走 stdio，url/headers 走 HTTP。

    command 与 url 二选一：有 command 走 stdio 子进程，有 url 走 Streamable HTTP。
    env 中的 ${VAR} 在连接时展开；headers 中的 ${VAR} 同样展开（用于鉴权令牌）。
    """

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)

    # 有 command 走 stdio；否则走 HTTP。
    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass(frozen=True)
class SandboxAppConfig:
    """OS 级沙箱启用配置：enabled 控制挂载，auto_allow 控制 Layer 1c 联动。

    - enabled：是否在装配时调用 create_sandbox() 挂载到 Bash
    - auto_allow：是否在 Layer 1c 自动放行命令类工具（需 enabled + 平台支持）
    - network_enabled：沙箱内是否允许网络访问
    """

    enabled: bool = False
    auto_allow: bool = False
    network_enabled: bool = False


@dataclass(frozen=True)
class AppConfig:
    """保存启动本批次对话应用所需的已校验配置。"""

    providers: tuple[ProviderConfig, ...]
    # OS 级沙箱配置；从 .seacode/config.yaml 的 sandbox 段加载，三层合并任一层开启即开启。
    sandbox: SandboxAppConfig = SandboxAppConfig()
    # MCP 服务器配置列表；三层合并按 name 去重覆盖（同 name 替换、新 name 追加）。
    mcp_servers: tuple[MCPServerConfig, ...] = ()


# 展开 ${VAR} 占位符；未定义变量保留原字面量，便于发现配置错误。
def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)), value
    )


# 构造子进程环境：只注入 PATH 与显式声明的 env，不继承 SeaCode 全部环境。
# PATH 注入保证 npx 等命令可发现；不注入 SEA_*、API_KEY 等敏感变量。
def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


# 返回 SeaCode 用户级与项目级配置的固定发现顺序。
def config_candidates(cwd: Path | None = None, home: Path | None = None) -> tuple[Path, ...]:
    project_dir = cwd or Path.cwd()
    user_home = home or Path.home()
    return (
        user_home / ".seacode" / "config.yaml",
        project_dir / ".seacode" / "config.yaml",
        project_dir / ".seacode" / "config.local.yaml",
    )


# 从单个 YAML 文件读取本批次需要的 Provider 列表。
def _load_file(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Unable to read configuration file: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in configuration file: {path}") from error
    return _parse_config(raw, path)


# 校验 YAML 结构，避免错误信息回显秘密字段值。
def _parse_config(raw: Any, path: Path) -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration must be a mapping: {path}")
    entries = raw.get("providers")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"Configuration requires a non-empty providers list: {path}")

    providers = tuple(_parse_provider(entry, path, index) for index, entry in enumerate(entries))
    names = [provider.name for provider in providers]
    if len(names) != len(set(names)):
        raise ConfigError(f"Provider names must be unique: {path}")
    sandbox = _parse_sandbox(raw.get("sandbox"), path)
    mcp_servers = _parse_mcp_servers(raw.get("mcp_servers"), path)
    return AppConfig(providers=providers, sandbox=sandbox, mcp_servers=mcp_servers)


# 解析 mcp_servers 段；缺失或非 list 时返回空元组，逐条校验字段。
def _parse_mcp_servers(raw: Any, path: Path) -> tuple[MCPServerConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"mcp_servers must be a list: {path}")

    servers: list[MCPServerConfig] = []
    for index, entry in enumerate(raw):
        servers.append(_parse_mcp_server(entry, path, index))

    names = [s.name for s in servers]
    if len(names) != len(set(names)):
        raise ConfigError(f"MCP server names must be unique within a file: {path}")
    return tuple(servers)


# 校验单个 MCP 服务器条目；command 与 url 二选一，缺失即报错。
def _parse_mcp_server(raw: Any, path: Path, index: int) -> MCPServerConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"MCP server #{index + 1} must be a mapping: {path}")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"MCP server #{index + 1} requires non-empty name: {path}")
    name = name.strip()

    command = raw.get("command")
    url = raw.get("url")
    if command is None and url is None:
        raise ConfigError(
            f"MCP server '{name}' requires either command or url: {path}"
        )
    if command is not None and not isinstance(command, str):
        raise ConfigError(f"MCP server '{name}' command must be a string: {path}")
    if url is not None and not isinstance(url, str):
        raise ConfigError(f"MCP server '{name}' url must be a string: {path}")

    args_raw = raw.get("args", [])
    if not isinstance(args_raw, list):
        raise ConfigError(f"MCP server '{name}' args must be a list: {path}")
    args = tuple(str(a) for a in args_raw)

    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict):
        raise ConfigError(f"MCP server '{name}' headers must be a mapping: {path}")
    headers = {str(k): str(v) for k, v in headers_raw.items()}

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ConfigError(f"MCP server '{name}' env must be a mapping: {path}")
    env = {str(k): str(v) for k, v in env_raw.items()}

    return MCPServerConfig(
        name=name,
        command=command,
        args=args,
        url=url,
        headers=headers,
        env=env,
    )


# 解析 sandbox 段；非 dict 或字段缺失时返回默认（全部关闭）。
def _parse_sandbox(raw: Any, path: Path) -> SandboxAppConfig:
    if not isinstance(raw, dict):
        return SandboxAppConfig()
    enabled = raw.get("enabled", False)
    auto_allow = raw.get("auto_allow", False)
    network_enabled = raw.get("network_enabled", False)
    if not isinstance(enabled, bool) or not isinstance(auto_allow, bool) \
            or not isinstance(network_enabled, bool):
        raise ConfigError(f"sandbox section requires boolean fields: {path}")
    return SandboxAppConfig(
        enabled=enabled,
        auto_allow=auto_allow,
        network_enabled=network_enabled,
    )


# 校验单个 Provider 的公开字段与协议边界。
def _parse_provider(raw: Any, path: Path, index: int) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"Provider #{index + 1} must be a mapping: {path}")

    values: dict[str, str] = {}
    for field_name in ("name", "protocol", "model", "base_url", "api_key"):
        value = raw.get(field_name)
        if not isinstance(value, str):
            raise ConfigError(f"Provider #{index + 1} requires string field {field_name}: {path}")
        values[field_name] = value.strip()

    for field_name in ("name", "protocol", "model", "base_url"):
        if not values[field_name]:
            raise ConfigError(f"Provider #{index + 1} field {field_name} cannot be empty: {path}")

    protocol = values["protocol"]
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ConfigError(f"Provider #{index + 1} has unsupported protocol: {path}")
    parsed_url = urlsplit(values["base_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError(f"Provider #{index + 1} has invalid base_url: {path}")

    thinking = raw.get("thinking", False)
    if not isinstance(thinking, bool):
        raise ConfigError(f"Provider #{index + 1} field thinking must be boolean: {path}")

    return ProviderConfig(
        name=values["name"],
        protocol=protocol,
        model=values["model"],
        base_url=values["base_url"].rstrip("/"),
        api_key=values["api_key"],
        thinking=thinking,
    )


# 按固定层级合并配置；后层完整替换 Provider 列表，sandbox 各字段任一层开启即开启，
# mcp_servers 按 name 去重覆盖（同 name 替换、新 name 追加）。
def load_config(
    path: Path | None = None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> AppConfig:
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"Configuration file not found: {path}")
        return _load_file(path)

    loaded: AppConfig | None = None
    sandbox_enabled = False
    sandbox_auto_allow = False
    sandbox_network = False
    merged_mcp: dict[str, MCPServerConfig] = {}
    for candidate in config_candidates(cwd, home):
        if not candidate.is_file():
            continue
        layer = _load_file(candidate)
        loaded = layer
        # sandbox 字段三层合并：任一层开启即开启（与 Provider 列表的"后层替换"语义不同）。
        sandbox_enabled = sandbox_enabled or layer.sandbox.enabled
        sandbox_auto_allow = sandbox_auto_allow or layer.sandbox.auto_allow
        sandbox_network = sandbox_network or layer.sandbox.network_enabled
        # mcp_servers 按 name 去重覆盖：后层同名替换前层，新名追加。
        for server in layer.mcp_servers:
            merged_mcp[server.name] = server

    if loaded is None:
        raise ConfigError(
            "No configuration found. Add .seacode/config.local.yaml to the project "
            "or ~/.seacode/config.yaml for a user default."
        )
    return AppConfig(
        providers=loaded.providers,
        sandbox=SandboxAppConfig(
            enabled=sandbox_enabled,
            auto_allow=sandbox_auto_allow,
            network_enabled=sandbox_network,
        ),
        mcp_servers=tuple(merged_mcp.values()),
    )
