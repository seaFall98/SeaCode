"""/rewind 命令单元测试：覆盖无快照、列出快照、回滚成功、越界与无效索引。"""

from __future__ import annotations

import datetime
from typing import Any

from seacode.commands.handlers.rewind import create_rewind_command
from seacode.commands.registry import CommandContext
from seacode.filehistory.history import Backup, Snapshot


# 假 UI：收集 add_system_message 调用的文本。
class _FakeUI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def send_user_message(self, text: str) -> None:
        pass

    def set_plan_mode(self, enabled: bool) -> None:
        pass

    def get_token_count(self) -> tuple[int, int]:
        return 0, 0

    def refresh_status(self) -> None:
        pass


# 假 FileHistory：可控的 get_snapshots / rewind 行为。
class _FakeFileHistory:
    def __init__(
        self,
        snapshots: list[Snapshot] | None = None,
        rewind_result: list[str] | None = None,
        rewind_raises: Exception | None = None,
    ) -> None:
        self._snapshots = snapshots or []
        self._rewind_result = rewind_result if rewind_result is not None else []
        self._rewind_raises = rewind_raises
        self.rewind_calls: list[int] = []

    def get_snapshots(self) -> list[Snapshot]:
        return list(self._snapshots)

    def has_snapshots(self) -> bool:
        return len(self._snapshots) > 0

    def rewind(self, idx: int) -> list[str]:
        self.rewind_calls.append(idx)
        if self._rewind_raises is not None:
            raise self._rewind_raises
        return self._rewind_result


# 假 Agent：保留 file_history 字段。
class _FakeAgent:
    def __init__(self, file_history: Any = None) -> None:
        self.file_history = file_history


# 构造 /rewind 命令与 ctx；file_history 默认 None。
def _make_command_and_ctx(
    args: str, file_history: Any = None
) -> tuple[Any, CommandContext, _FakeUI]:
    cmd = create_rewind_command()
    ui = _FakeUI()
    agent = _FakeAgent(file_history=file_history)
    ctx = CommandContext(
        args=args,
        agent=agent,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config={},
    )
    return cmd, ctx, ui


# 验证 file_history 未初始化时提示。
# agent.file_history=None，断言 ui.messages 含 "文件历史未初始化"。
async def test_no_file_history_shows_message() -> None:
    cmd, ctx, ui = _make_command_and_ctx("0", file_history=None)

    await cmd.handler(ctx)

    assert any("文件历史未初始化" in m for m in ui.messages)


# 验证无快照时 /rewind 提示 "No checkpoints to rewind to."。
# fake FileHistory 返回空 snapshots，断言 ui.messages 含提示。
async def test_no_snapshots_shows_message() -> None:
    fh = _FakeFileHistory(snapshots=[])
    cmd, ctx, ui = _make_command_and_ctx("", file_history=fh)

    await cmd.handler(ctx)

    assert any("No checkpoints to rewind to." in m for m in ui.messages)


# 验证无参数列出快照。
# fake FileHistory 返回 2 个快照，断言 ui.messages 含每个快照的 message_index 与版本数。
async def test_list_snapshots_shows_message_index_and_file_count() -> None:
    ts = datetime.datetime.now()
    snap1 = Snapshot(
        message_index=0,
        user_text="hello",
        backups={"/a": Backup(path="/bp/a", version=1, timestamp=ts)},
        timestamp=ts,
    )
    snap2 = Snapshot(
        message_index=1,
        user_text="world",
        backups={
            "/a": Backup(path="/bp/a", version=2, timestamp=ts),
            "/b": Backup(path="/bp/b", version=1, timestamp=ts),
        },
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap1, snap2])
    cmd, ctx, ui = _make_command_and_ctx("", file_history=fh)

    await cmd.handler(ctx)

    text = "\n".join(ui.messages)
    assert "[0]" in text
    assert "msg#0" in text
    assert "files=1" in text
    assert "[1]" in text
    assert "msg#1" in text
    assert "files=2" in text


# 验证 /rewind <idx> 调用 rewind(idx) 显示 changed 列表。
# fake FileHistory rewind 返回 ["/a", "/b"]，断言 ui.messages 含两个文件路径。
async def test_rewind_with_idx_returns_changed_files() -> None:
    fh = _FakeFileHistory(rewind_result=["/a", "/b"])
    cmd, ctx, ui = _make_command_and_ctx("0", file_history=fh)

    await cmd.handler(ctx)

    assert fh.rewind_calls == [0]
    text = "\n".join(ui.messages)
    assert "已回滚到快照 0" in text
    assert "还原 2 个文件" in text
    assert "/a" in text
    assert "/b" in text


# 验证 /rewind 越界 idx 提示 "无需还原或越界"。
# fake FileHistory rewind 返回空列表，断言 ui.messages 含 "无需还原或越界"。
async def test_rewind_out_of_range_shows_message() -> None:
    fh = _FakeFileHistory(rewind_result=[])
    cmd, ctx, ui = _make_command_and_ctx("99", file_history=fh)

    await cmd.handler(ctx)

    assert any("无需还原或越界" in m for m in ui.messages)


# 验证 /rewind 非数字 idx 提示 "无效的快照索引"。
# 传入 "abc"，断言 ui.messages 含 "无效的快照索引: abc"。
async def test_rewind_non_numeric_idx_shows_message() -> None:
    fh = _FakeFileHistory(rewind_result=[])
    cmd, ctx, ui = _make_command_and_ctx("abc", file_history=fh)

    await cmd.handler(ctx)

    assert any("无效的快照索引" in m and "abc" in m for m in ui.messages)
    assert fh.rewind_calls == []


# 验证 /rewind rewind 抛异常时显示 "回滚失败"。
# fake FileHistory rewind_raises=RuntimeError("disk error")，断言 ui.messages 含 "回滚失败"。
async def test_rewind_raises_shows_error() -> None:
    fh = _FakeFileHistory(rewind_raises=RuntimeError("disk error"))
    cmd, ctx, ui = _make_command_and_ctx("0", file_history=fh)

    await cmd.handler(ctx)

    assert any("回滚失败" in m and "disk error" in m for m in ui.messages)


# 验证列表中的 user_text 超过截断长度时被截断。
# 构造 user_text 长度 > 60，断言 ui.messages 含 "..."。
async def test_list_truncates_long_user_text() -> None:
    ts = datetime.datetime.now()
    long_text = "x" * 100
    snap = Snapshot(
        message_index=0,
        user_text=long_text,
        backups={},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap])
    cmd, ctx, ui = _make_command_and_ctx("", file_history=fh)

    await cmd.handler(ctx)

    text = "\n".join(ui.messages)
    assert "..." in text
