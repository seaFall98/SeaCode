"""本地模型配置的发现、校验与安全解析。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml

from .validator import (
    DEFAULT_CONTEXT_WINDOW,
    lookup_model_context_window,
    validate_hooks,
    validate_permission_mode,
)

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
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值（最高优先级）。
    context_window: int = 0
    # 单回合最大输出 token 数；0 表示"未设置"，由 client 内部默认值兜底。
    # 显式配置（正数）会覆盖默认值，用于精细控制长输出场景的 token 上限。
    max_output_tokens: int = 0
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不参与相等比较与哈希，避免缓存更新影响身份判断。
    _fetched_context_window: int = field(default=0, repr=False, compare=False)

    # 优先使用 YAML 密钥，空值时才兼容外部协议的环境变量。
    def resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(_ENV_KEY_NAMES[self.protocol], "")

    # 返回配置或默认的最大输出 token 数；未配置时回退到 8192。
    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        return 8192

    # 记录从 provider 自动拉取到的 context window（第 2 层）。
    # 非正数会被忽略，这样一次失败的拉取就不会污染 cache。
    # frozen dataclass 用 object.__setattr__ 绕过只读限制，仅用于此运行时缓存字段。
    def set_fetched_context_window(self, window: int) -> None:
        if window > 0:
            object.__setattr__(self, "_fetched_context_window", window)

    # 通过四层 fallback 解析模型的 context window，按优先级从高到低：
    #   1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
    #   2. 从 provider 的 /v1/models 端点自动拉取并缓存的值（仅 anthropic 协议）。
    #   3. validator.py 中的内置"模型名 -> window"映射表（按子串匹配）。
    #   4. DEFAULT_CONTEXT_WINDOW 保守默认值。
    def get_context_window(self) -> int:
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        return DEFAULT_CONTEXT_WINDOW


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


# batch13：Git Worktree 隔离工作区配置。
# symlink_directories：创建 worktree 后符号链接到主仓库的目录列表（如 .venv、node_modules）；
# stale_cleanup_interval：后台清理任务执行间隔（秒）；
# stale_cutoff_hours：worktree 超过该小时数且无变更时被清理。
@dataclass(frozen=True)
class WorktreeConfig:
    """Git Worktree 隔离工作区的运行配置。"""

    symlink_directories: tuple[str, ...] = ()
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass(frozen=True)
class AppConfig:
    """保存启动本批次对话应用所需的已校验配置。"""

    providers: tuple[ProviderConfig, ...]
    # 默认权限模式；命令行未显式指定时由各运行入口采用。
    permission_mode: str = "default"
    # OS 级沙箱配置；从 .seacode/config.yaml 的 sandbox 段加载，三层合并任一层开启即开启。
    sandbox: SandboxAppConfig = SandboxAppConfig()
    # MCP 服务器配置列表；三层合并按 name 去重覆盖（同 name 替换、新 name 追加）。
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    # Hook 原始配置列表；三层叠加合并（与 Provider 列表完整替换语义不同），
    # 字段级校验延迟到 hooks.loader.load_hooks 中做。
    raw_hooks: list[dict] = field(default_factory=list)
    # 子 Agent 可选能力默认关闭；配置层任一位置显式开启后保持开启。
    enable_fork: bool = False
    enable_verification_agent: bool = False
    # batch13：Worktree 隔离工作区配置；三层合并按字段覆盖（与 sandbox 同语义）。
    worktree: WorktreeConfig = WorktreeConfig()
    # batch14：团队协调配置；teammate_mode 指定 spawn 后端（空/ tmux / iterm2 / in-process），
    # enable_coordinator_mode 开启 Lead 工具收敛与协调者提示词。
    # 三层合并：后层非默认值覆盖前层（与 worktree 同语义，非 sandbox 的 OR 语义）。
    teammate_mode: str = ""
    enable_coordinator_mode: bool = False


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
    raw_hooks = validate_hooks(raw.get("hooks"))
    permission_mode = _parse_permission_mode(raw.get("permission_mode"), path)
    enable_fork, enable_verification_agent = _parse_subagent_feature_flags(raw, path)
    worktree = _parse_worktree(raw.get("worktree"), path)
    teammate_mode, enable_coordinator_mode = _parse_teammate_fields(raw, path)
    return AppConfig(
        providers=providers,
        permission_mode=permission_mode,
        sandbox=sandbox,
        mcp_servers=mcp_servers,
        raw_hooks=raw_hooks,
        enable_fork=enable_fork,
        enable_verification_agent=enable_verification_agent,
        worktree=worktree,
        teammate_mode=teammate_mode,
        enable_coordinator_mode=enable_coordinator_mode,
    )


# 解析可选权限模式，缺失时采用默认；非法值在配置边界给出明确错误。
def _parse_permission_mode(raw: Any, path: Path) -> str:
    if raw is None:
        return "default"
    if not isinstance(raw, str):
        raise ConfigError(f"permission_mode must be a string: {path}")
    try:
        return str(validate_permission_mode(raw).value)
    except ValueError as error:
        raise ConfigError(f"invalid permission_mode: {path}") from error


# 解析子 Agent 可选能力开关；缺失时保持关闭，避免改变既有启动行为。
def _parse_subagent_feature_flags(raw: dict[str, Any], path: Path) -> tuple[bool, bool]:
    flags: list[bool] = []
    for field_name in ("enable_fork", "enable_verification_agent"):
        value = raw.get(field_name, False)
        if not isinstance(value, bool):
            raise ConfigError(f"{field_name} must be a boolean: {path}")
        flags.append(value)
    return flags[0], flags[1]


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


# batch13：解析 worktree 段；非 dict 或字段缺失时返回默认值。
# symlink_directories 接受 list[str]；非正整数的间隔与小时数抛 ConfigError。
def _parse_worktree(raw: Any, path: Path) -> WorktreeConfig:
    from seacode.validator import validate_worktree

    if not isinstance(raw, dict):
        return WorktreeConfig()
    try:
        return validate_worktree(raw)
    except ConfigError as e:
        raise ConfigError(f"{e}: {path}") from e


# batch14：解析 teammate_mode / enable_coordinator_mode 顶层字段。
# 缺失用默认值（空串 / False）；teammate_mode 非 str 抛错，enable_coordinator_mode 非 bool 抛错。
def _parse_teammate_fields(raw: Any, path: Path) -> tuple[str, bool]:
    teammate_mode_raw = raw.get("teammate_mode", "")
    if not isinstance(teammate_mode_raw, str):
        raise ConfigError(f"teammate_mode must be a string: {path}")
    teammate_mode = teammate_mode_raw.strip()

    enable_coordinator_raw = raw.get("enable_coordinator_mode", False)
    if not isinstance(enable_coordinator_raw, bool):
        raise ConfigError(
            f"enable_coordinator_mode must be a boolean: {path}"
        )
    return teammate_mode, enable_coordinator_raw


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

    # context_window 为可选正整数；0 或缺失表示走四层 fallback。
    context_window = raw.get("context_window", 0)
    if not isinstance(context_window, int) or context_window < 0:
        raise ConfigError(
            f"Provider #{index + 1} field context_window must be a non-negative integer: {path}"
        )

    # max_output_tokens 为可选正整数；0 或缺失表示由 client 内部默认值兜底。
    max_output_tokens = raw.get("max_output_tokens", 0)
    if not isinstance(max_output_tokens, int) or max_output_tokens < 0:
        raise ConfigError(
            f"Provider #{index + 1} field max_output_tokens must be a non-negative integer: {path}"
        )

    return ProviderConfig(
        name=values["name"],
        protocol=protocol,
        model=values["model"],
        base_url=values["base_url"].rstrip("/"),
        api_key=values["api_key"],
        thinking=thinking,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


# 按固定层级合并配置；后层完整替换 Provider 列表，sandbox 各字段任一层开启即开启，
# mcp_servers 按 name 去重覆盖（同 name 替换、新 name 追加）。
# batch13：worktree 字段三层合并，后层非默认值覆盖前层（与 sandbox 的 OR 语义不同，
# 因为 symlink_directories 是列表、interval/hours 是覆盖）。
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
    merged_raw_hooks: list[dict] = []
    # worktree 字段：后层非默认值覆盖前层；默认值表示"未配置"，允许前层值穿透。
    merged_symlinks: list[str] = []
    merged_interval: int = 3600
    merged_cutoff: int = 24
    merged_permission_mode = "default"
    # 子 Agent 开关采用 OR 语义；用户级配置启用后，项目配置不会意外关闭它。
    merged_enable_fork = False
    merged_enable_verification_agent = False
    # batch14：团队字段合并；teammate_mode 后层非空覆盖前层，
    # enable_coordinator_mode 后层 True 覆盖前层（与 sandbox 的 OR 语义一致，更安全）。
    merged_teammate_mode: str = ""
    merged_coordinator_mode: bool = False
    for candidate in config_candidates(cwd, home):
        if not candidate.is_file():
            continue
        layer = _load_file(candidate)
        loaded = layer
        if layer.permission_mode != "default":
            merged_permission_mode = layer.permission_mode
        merged_enable_fork = merged_enable_fork or layer.enable_fork
        merged_enable_verification_agent = (
            merged_enable_verification_agent or layer.enable_verification_agent
        )
        # sandbox 字段三层合并：任一层开启即开启（与 Provider 列表的"后层替换"语义不同）。
        sandbox_enabled = sandbox_enabled or layer.sandbox.enabled
        sandbox_auto_allow = sandbox_auto_allow or layer.sandbox.auto_allow
        sandbox_network = sandbox_network or layer.sandbox.network_enabled
        # mcp_servers 按 name 去重覆盖：后层同名替换前层，新名追加。
        for server in layer.mcp_servers:
            merged_mcp[server.name] = server
        # raw_hooks 叠加合并：用户级 + 项目级 + 本地级 Hook 全部累加，不互相覆盖。
        # 与 Provider 列表"后层完整替换"语义不同；dedup 由 once 标记或用户自行控制。
        merged_raw_hooks.extend(layer.raw_hooks)
        # worktree 字段合并：后层非空 symlink_directories 覆盖前层；
        # interval/cutoff 后层非默认值（!= 3600 / != 24）覆盖前层。
        if layer.worktree.symlink_directories:
            merged_symlinks = list(layer.worktree.symlink_directories)
        if layer.worktree.stale_cleanup_interval != 3600:
            merged_interval = layer.worktree.stale_cleanup_interval
        if layer.worktree.stale_cutoff_hours != 24:
            merged_cutoff = layer.worktree.stale_cutoff_hours
        # batch14：团队字段合并。
        # teammate_mode 后层完整替换前层（与 Provider 列表同语义，符合 task.md 规格）。
        merged_teammate_mode = layer.teammate_mode
        # enable_coordinator_mode 用 OR 语义（任一层开启即开启，与 sandbox 同语义，更安全）。
        if layer.enable_coordinator_mode:
            merged_coordinator_mode = True

    if loaded is None:
        raise ConfigError(
            "No configuration found. Add .seacode/config.local.yaml to the project "
            "or ~/.seacode/config.yaml for a user default."
        )
    return AppConfig(
        providers=loaded.providers,
        permission_mode=merged_permission_mode,
        sandbox=SandboxAppConfig(
            enabled=sandbox_enabled,
            auto_allow=sandbox_auto_allow,
            network_enabled=sandbox_network,
        ),
        mcp_servers=tuple(merged_mcp.values()),
        raw_hooks=merged_raw_hooks,
        enable_fork=merged_enable_fork,
        enable_verification_agent=merged_enable_verification_agent,
        worktree=WorktreeConfig(
            symlink_directories=tuple(merged_symlinks),
            stale_cleanup_interval=merged_interval,
            stale_cutoff_hours=merged_cutoff,
        ),
        teammate_mode=merged_teammate_mode,
        enable_coordinator_mode=merged_coordinator_mode,
    )
