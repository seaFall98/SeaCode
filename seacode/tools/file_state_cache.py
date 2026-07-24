"""文件状态缓存，实现 read-before-edit 门控。"""

from __future__ import annotations

from pathlib import Path


class FileStateCache:
    """跟踪已读文件的 mtime，防止盲目覆盖未读或已被外部修改的文件。

    门控分两层：文件必须先被 ReadFile 读过（缓存命中），
    且自上次读取后 mtime 未发生变化。
    """

    def __init__(self) -> None:
        self._cache: dict[str, int] = {}

    # 记录一次成功读取后的文件 mtime（纳秒）。
    def record(self, path: str, mtime_ns: int) -> None:
        self._cache[path] = mtime_ns

    # 检查文件是否可安全编辑/写入，返回 (是否通过, 错误信息)。
    def check(self, path: str) -> tuple[bool, str]:
        cached_mtime_ns = self._cache.get(path)
        if cached_mtime_ns is None:
            return False, "Error: file has not been read yet. Read it first before editing."

        try:
            current_mtime_ns = Path(path).stat().st_mtime_ns
        except OSError:
            return True, ""

        if current_mtime_ns != cached_mtime_ns:
            return (
                False,
                "Error: file has been modified since last read. Read it again before editing.",
            )

        return True, ""

    # 编辑/写入成功后更新缓存条目。
    def update(self, path: str) -> None:
        try:
            mtime_ns = Path(path).stat().st_mtime_ns
            self._cache[path] = mtime_ns
        except OSError:
            self._cache.pop(path, None)
