"""SeaCode 终端入口。"""

from __future__ import annotations

import asyncio
import os
import sys

from .config import ConfigError, ProviderConfig, load_config

# Agent Loop 默认最大迭代次数，与 SEA_MAX_STEPS 未设置时的回退值一致。
_DEFAULT_MAX_STEPS: int = 100


# 读取 SEA_MAX_STEPS 环境变量，非正整数或缺失时回退到默认值。
def _read_max_steps() -> int:
    raw = os.environ.get("SEA_MAX_STEPS")
    if not raw:
        return _DEFAULT_MAX_STEPS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_STEPS
    return value if value > 0 else _DEFAULT_MAX_STEPS


# 对每个 Provider 尝试拉取 context window 并缓存到 ProviderConfig 上。
# 完全尽力而为：非 anthropic、网络失败或超时都静默降级，由 get_context_window()
# 走内置映射表或默认值。拉取是并发的，避免多 Provider 时串行等待。
async def _resolve_context_windows_async(
    providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
) -> None:
    from .client import resolve_context_window

    await asyncio.gather(
        *(resolve_context_window(provider) for provider in providers),
        return_exceptions=True,
    )


# 加载本地配置并启动交互式终端应用。
def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"SeaCode configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    # 启动前尽力拉取 anthropic Provider 的 context window；失败静默降级。
    # 同步入口里跑一次事件循环，拉取超时由 ANTHROPIC_MODEL_FETCH_TIMEOUT 兜底。
    try:
        asyncio.run(_resolve_context_windows_async(config.providers))
    except Exception:
        pass

    from .app import SeaCodeApp

    SeaCodeApp(providers=config.providers, max_steps=_read_max_steps()).run()


if __name__ == "__main__":
    main()
