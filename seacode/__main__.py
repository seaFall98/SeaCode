"""SeaCode 终端入口。"""

from __future__ import annotations

import os
import sys

from .config import ConfigError, load_config

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


# 加载本地配置并启动交互式终端应用。
def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"SeaCode configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    from .app import SeaCodeApp

    SeaCodeApp(providers=config.providers, max_steps=_read_max_steps()).run()


if __name__ == "__main__":
    main()
