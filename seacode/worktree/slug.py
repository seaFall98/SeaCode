"""worktree name 校验与扁平化。"""

from __future__ import annotations

import re

# 单段合法字符集合：字母、数字、点、下划线、连字符。
_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# name 长度上限，避免目录名过长。
_MAX_LENGTH = 64


def validate_slug(name: str) -> str | None:
    """校验 worktree name 安全性；通过返回 None，失败返回错误信息。"""
    if not name:
        return "name must not be empty"
    if len(name) > _MAX_LENGTH:
        return f"name exceeds {_MAX_LENGTH} characters"
    segments = name.split("/")
    for seg in segments:
        if not seg:
            return "empty segment"
        if seg in (".", ".."):
            return "segment must not be . or .."
        if not _SEGMENT_RE.match(seg):
            return f"invalid segment: {seg}"
    return None


def flatten_slug(name: str) -> str:
    """把 name 中的 / 替换为 +，用于目录名与分支名。"""
    return name.replace("/", "+")
