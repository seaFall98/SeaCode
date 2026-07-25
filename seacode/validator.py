"""模型相关常量与内置映射表，服务于上下文窗口的四层降级解析；并校验 hooks 段结构。

补充运行时校验函数（validate_permission_mode / validate_teammate_mode 等），
让命令处理器与配置加载层共用同一份校验逻辑，避免分散判断导致的边界漂移。
"""

from __future__ import annotations

from typing import Any

# 上下文窗口的保守默认值（第 4 层 fallback）。
# 与主流 Claude 模型的窗口对齐；其它模型由映射表或显式配置覆盖。
DEFAULT_CONTEXT_WINDOW: int = 200_000

# 内置"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是上下文窗口回退链的第 3 层。按从最具体到最通用排序，第一个子串命中即生效。
# 值仅为合理起始点，模型更新/重命名后可能过时；用户可在配置中显式设置
# context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
]

# 允许的 teammate_mode 值；空串表示不启用团队功能。
# 命令处理器与配置层共用此集合做校验，避免散落字符串比较。
VALID_TEAMMATE_MODES: frozenset[str] = frozenset(
    {"", "tmux", "iterm2", "in-process"}
)


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配返回内置映射表中该模型对应的 context window；未命中返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0


# 校验 hooks 配置段结构；只做 list 或 None 校验，字段级校验在 hooks.loader.load_hooks。
# None 视为未配置返回空 list；非 list 抛 ConfigError。ConfigError 延迟导入避免循环依赖。
def validate_hooks(raw_hooks: Any) -> list[dict]:
    if raw_hooks is None:
        return []
    if isinstance(raw_hooks, list):
        return raw_hooks
    from seacode.config import ConfigError
    raise ConfigError("'hooks' must be a list of hook definitions")


# batch13：校验 worktree 配置段；返回 WorktreeConfig 实例。
# symlink_directories 必须是 list[str]；stale_cleanup_interval / stale_cutoff_hours 必须是正整数。
# 缺字段用默认值；类型非法抛 ConfigError（延迟导入避免循环依赖）。
def validate_worktree(data: dict[str, Any]) -> Any:
    from seacode.config import WorktreeConfig

    if not isinstance(data, dict):
        return WorktreeConfig()

    raw_symlinks = data.get("symlink_directories", [])
    if raw_symlinks is None:
        raw_symlinks = []
    if not isinstance(raw_symlinks, list):
        from seacode.config import ConfigError
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")
    symlinks = tuple(str(s) for s in raw_symlinks)

    raw_interval = data.get("stale_cleanup_interval", 3600)
    if not isinstance(raw_interval, int) or isinstance(raw_interval, bool) or raw_interval <= 0:
        from seacode.config import ConfigError
        raise ConfigError(
            "'worktree.stale_cleanup_interval' must be a positive integer"
        )

    raw_cutoff = data.get("stale_cutoff_hours", 24)
    if not isinstance(raw_cutoff, int) or isinstance(raw_cutoff, bool) or raw_cutoff <= 0:
        from seacode.config import ConfigError
        raise ConfigError(
            "'worktree.stale_cutoff_hours' must be a positive integer"
        )

    return WorktreeConfig(
        symlink_directories=symlinks,
        stale_cleanup_interval=raw_interval,
        stale_cutoff_hours=raw_cutoff,
    )


# 校验权限模式字符串是否对应有效 PermissionMode 枚举值。
# 命令处理器 /permission mode <模式> 与配置层共用此函数；
# 无效字符串抛 ValueError 让调用方决定如何提示用户。
def validate_permission_mode(mode: str) -> Any:
    from seacode.permissions import PermissionMode

    for m in PermissionMode:
        if m.value == mode:
            return m
    valid = ", ".join(m.value for m in PermissionMode)
    raise ValueError(f"unknown permission mode: {mode!r} (valid: {valid})")


# 校验 teammate_mode 字符串是否在允许集合内。
# 空串表示不启用团队功能；非空必须是 tmux / iterm2 / in-process 之一。
def validate_teammate_mode(mode: str) -> str:
    if mode not in VALID_TEAMMATE_MODES:
        valid = ", ".join(sorted(VALID_TEAMMATE_MODES - {""}))
        raise ValueError(
            f"unknown teammate_mode: {mode!r} (valid: '' or {valid})"
        )
    return mode


# 校验字段值是否为 bool；接受真正的 bool（拒绝 int 子类如 0/1 误传）。
# name 参数用于错误消息定位字段，便于配置文件调试。
def validate_bool_field(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean, got {type(value).__name__}")
    return value


# 校验 sandbox 配置段的三个布尔字段；返回归一化后的 dict 供调用方构造 SandboxAppConfig。
# 缺字段用默认值（全部关闭）；类型非法抛 ValueError。
def validate_sandbox(data: Any) -> dict[str, bool]:
    if data is None or not isinstance(data, dict):
        return {"enabled": False, "auto_allow": False, "network_enabled": False}
    return {
        "enabled": validate_bool_field(data.get("enabled", False), "sandbox.enabled"),
        "auto_allow": validate_bool_field(data.get("auto_allow", False), "sandbox.auto_allow"),
        "network_enabled": validate_bool_field(
            data.get("network_enabled", False), "sandbox.network_enabled"
        ),
    }


# 校验 providers 列表的非空与唯一性；字段级校验由 _parse_provider 完成。
# 此函数作为 post-parse 校验，确认解析后的 provider 列表满足约束。
def validate_providers(providers: Any) -> None:
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ValueError("providers must be a non-empty list")
    # name 取自 ProviderConfig.name；getattr 兜底 None 后过滤，避免 sorted 类型不匹配。
    names: list[str] = [getattr(p, "name", "") or "" for p in providers]
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"duplicate provider names: {duplicates}")


# 校验 MCP 服务器列表的 name 唯一性；字段级校验由 _parse_mcp_server 完成。
def validate_mcp_servers(servers: Any) -> None:
    if not isinstance(servers, (list, tuple)):
        return
    names: list[str] = [getattr(s, "name", "") or "" for s in servers]
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"duplicate MCP server names: {duplicates}")


# 顶层配置结构校验：确认 raw 是 dict 且含 providers 键。
# 在 _parse_config 解析前调用，fail-fast 给出清晰错误而不是让后续解析抛模糊异常。
def validate_config_structure(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a mapping")
    if "providers" not in data:
        raise ValueError("configuration requires a 'providers' key")
    if not isinstance(data["providers"], list) or not data["providers"]:
        raise ValueError("'providers' must be a non-empty list")
