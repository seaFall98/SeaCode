"""本地模型配置的发现、校验与安全解析。"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    return AppConfig(providers=providers, sandbox=sandbox)


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


# 按固定层级合并配置；后层完整替换 Provider 列表，sandbox 各字段任一层开启即开启。
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
    for candidate in config_candidates(cwd, home):
        if not candidate.is_file():
            continue
        layer = _load_file(candidate)
        loaded = layer
        # sandbox 字段三层合并：任一层开启即开启（与 Provider 列表的"后层替换"语义不同）。
        sandbox_enabled = sandbox_enabled or layer.sandbox.enabled
        sandbox_auto_allow = sandbox_auto_allow or layer.sandbox.auto_allow
        sandbox_network = sandbox_network or layer.sandbox.network_enabled

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
    )
