"""OS 级沙箱：限制 Bash 命令的文件写入和网络访问。

macOS 使用 sandbox-exec（Seatbelt），Linux 使用 bubblewrap（bwrap）。
与 permissions/sandbox.py 中的 PathSandbox（应用层路径检查）不同，
这里是操作系统层面的强制隔离——即使命令绕过了路径检查，
内核也会阻止越权的写操作。
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SandboxConfig:
    """沙箱配置：控制可写路径、禁写路径和网络访问。

    - allow_write：允许写入的路径白名单
    - deny_write：强制只读的路径黑名单（优先级高于 allow_write）
    - network_enabled：是否允许网络访问
    """

    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    network_enabled: bool = False


class Sandbox(ABC):
    """沙箱抽象基类，各平台实现 wrap() 与 available()。"""

    @abstractmethod
    def wrap(self, command: str, config: SandboxConfig) -> str:
        """将原始命令包装为沙箱内执行的命令字符串。"""
        ...

    @abstractmethod
    def available(self) -> bool:
        """检测当前环境是否支持该沙箱。"""
        ...


def create_sandbox() -> Sandbox | None:
    """根据操作系统自动选择沙箱实现。

    - macOS -> SeatbeltSandbox（基于 sandbox-exec）
    - Linux -> BwrapSandbox（基于 bubblewrap）
    - 其它系统（含 Windows） -> None（不支持沙箱，优雅降级）
    """
    system = platform.system()
    if system == "Darwin":
        from .seatbelt import SeatbeltSandbox

        return SeatbeltSandbox()
    elif system == "Linux":
        from .bwrap import BwrapSandbox

        return BwrapSandbox()
    return None


__all__ = ["Sandbox", "SandboxConfig", "create_sandbox"]
