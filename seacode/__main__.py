"""SeaCode 终端入口。"""

from __future__ import annotations

import sys

from .config import ConfigError, load_config


# 加载本地配置并启动交互式终端应用。
def main() -> None:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"SeaCode configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    from .app import SeaCodeApp

    SeaCodeApp(providers=config.providers).run()


if __name__ == "__main__":
    main()
