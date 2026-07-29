"""权限模式与模式矩阵：定义四种权限模式及其在工具分类上的默认决策。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from seacode.tools.base import ToolCategory

# 单次权限决策的三种结果；与具体工具、规则引擎、模式矩阵共用。
DecisionEffect = Literal["allow", "deny", "ask"]


class PermissionMode(StrEnum):
    """权限模式覆盖从默认谨慎到完全信任的光谱。

    - DEFAULT：读放行、写与命令需确认
    - ACCEPT_EDITS：读与写放行、命令需确认
    - PLAN：与 DEFAULT 相同，附加 Plan 模式工具与 plan 文件写入例外
    - BYPASS：全放行，但危险命令黑名单仍硬拦截
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"


# 模式 × 工具分类的默认决策矩阵；Bash 在 SeaCode 中归属 SYSTEM 分类。
_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    PermissionMode.DEFAULT: {
        ToolCategory.READ: "allow",
        ToolCategory.WRITE: "ask",
        ToolCategory.SYSTEM: "ask",
        ToolCategory.COMMAND: "ask",
    },
    PermissionMode.ACCEPT_EDITS: {
        ToolCategory.READ: "allow",
        ToolCategory.WRITE: "allow",
        ToolCategory.SYSTEM: "ask",
        ToolCategory.COMMAND: "ask",
    },
    PermissionMode.PLAN: {
        ToolCategory.READ: "allow",
        ToolCategory.WRITE: "ask",
        ToolCategory.SYSTEM: "ask",
        ToolCategory.COMMAND: "ask",
    },
    PermissionMode.BYPASS: {
        ToolCategory.READ: "allow",
        ToolCategory.WRITE: "allow",
        ToolCategory.SYSTEM: "allow",
        ToolCategory.COMMAND: "allow",
    },
}


def mode_decide(mode: PermissionMode, category: ToolCategory) -> DecisionEffect:
    """按模式矩阵查表返回默认决策；矩阵显式覆盖四种模式。"""
    return _MODE_MATRIX[mode][category]
