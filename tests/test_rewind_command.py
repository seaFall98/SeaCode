"""/rewind 命令单元测试：覆盖无快照、列出快照、三种回滚模式、越界与无效输入。"""

from __future__ import annotations

import datetime
from typing import Any

from seacode.commands.handlers.rewind import REWIND_COMMAND
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
    ) -> None:
        self._snapshots = snapshots or []
        self._rewind_result = rewind_result if rewind_result is not None else []
        self.rewind_calls: list[int] = []

    def get_snapshots(self) -> list[Snapshot]:
        return list(self._snapshots)

    def has_snapshots(self) -> bool:
        return len(self._snapshots) > 0

    def rewind(self, idx: int) -> list[str]:
        self.rewind_calls.append(idx)
        return self._rewind_result


# 假 Conversation：记录 replace_history 调用。
class _FakeConversation:
    def __init__(self, history: list[Any] | None = None) -> None:
        self._history = history or []
        self.replace_calls: list[list[Any]] = []

    @property
    def history(self) -> list[Any]:
        return list(self._history)

    def replace_history(self, new_history: list[Any]) -> None:
        self.replace_calls.append(list(new_history))


# 假 Agent：保留 file_history 字段。
class _FakeAgent:
    def __init__(self, file_history: Any = None) -> None:
        self.file_history = file_history


# 构造 /rewind 命令与 ctx；file_history 默认 None。
def _make_command_and_ctx(
    args: str,
    file_history: Any = None,
    conversation: Any = None,
) -> tuple[Any, CommandContext, _FakeUI]:
    cmd = REWIND_COMMAND
    ui = _FakeUI()
    agent = _FakeAgent(file_history=file_history)
    ctx = CommandContext(
        args=args,
        agent=agent,
        conversation=conversation or _FakeConversation(),
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config={},
    )
    return cmd, ctx, ui


# 验证 file_history 未初始化时提示。
# agent.file_history=None，断言 ui.messages 含 "No checkpoints to rewind to."。
async def test_no_file_history_shows_message() -> None:
    cmd, ctx, ui = _make_command_and_ctx("1", file_history=None)

    await cmd.handler(ctx)

    assert any("No checkpoints to rewind to." in m for m in ui.messages)


# 验证无快照时 /rewind 提示 "No checkpoints to rewind to."。
async def test_no_snapshots_shows_message() -> None:
    fh = _FakeFileHistory(snapshots=[])
    cmd, ctx, ui = _make_command_and_ctx("", file_history=fh)

    await cmd.handler(ctx)

    assert any("No checkpoints to rewind to." in m for m in ui.messages)


# 验证无参数列出快照与 Options 说明。
# fake FileHistory 返回 2 个快照，断言 ui.messages 含 1-based 索引与 Options 说明。
async def test_list_snapshots_shows_options_and_1based_index() -> None:
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
    # 1-based 索引
    assert "[1]" in text
    assert "[2]" in text
    # Options 说明
    assert "Options after selecting:" in text
    assert "1) Restore code and conversation" in text
    assert "2) Restore conversation only" in text
    assert "3) Restore code only" in text


# 验证 /rewind 1（默认 option=1）同时回滚代码与对话。
async def test_rewind_default_option_restores_code_and_conversation() -> None:
    ts = datetime.datetime.now()
    snap = Snapshot(
        message_index=2,
        user_text="test",
        backups={"/a": Backup(path="/bp/a", version=1, timestamp=ts)},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap], rewind_result=["/a"])
    conv = _FakeConversation(history=["m0", "m1", "m2", "m3"])
    cmd, ctx, ui = _make_command_and_ctx("1", file_history=fh, conversation=conv)

    await cmd.handler(ctx)

    # rewind 用 0-based 内部索引 0 调用
    assert fh.rewind_calls == [0]
    # 对话历史被截断到 snap.message_index=2
    assert conv.replace_calls == [["m0", "m1"]]
    text = "\n".join(ui.messages)
    assert "Restored 1 file(s) and conversation" in text


# 验证 /rewind 1 2（option=2）只回滚对话，不动文件。
async def test_rewind_option_2_restores_conversation_only() -> None:
    ts = datetime.datetime.now()
    snap = Snapshot(
        message_index=1,
        user_text="test",
        backups={"/a": Backup(path="/bp/a", version=1, timestamp=ts)},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap], rewind_result=["/a"])
    conv = _FakeConversation(history=["m0", "m1", "m2"])
    cmd, ctx, ui = _make_command_and_ctx("1 2", file_history=fh, conversation=conv)

    await cmd.handler(ctx)

    # 不调用 rewind
    assert fh.rewind_calls == []
    # 对话历史被截断到 snap.message_index=1
    assert conv.replace_calls == [["m0"]]
    text = "\n".join(ui.messages)
    assert "Rewound conversation" in text
    assert "Files unchanged" in text


# 验证 /rewind 1 3（option=3）只回滚文件，不动对话。
async def test_rewind_option_3_restores_code_only() -> None:
    ts = datetime.datetime.now()
    snap = Snapshot(
        message_index=1,
        user_text="test",
        backups={"/a": Backup(path="/bp/a", version=1, timestamp=ts)},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap], rewind_result=["/a", "/b"])
    conv = _FakeConversation(history=["m0", "m1", "m2"])
    cmd, ctx, ui = _make_command_and_ctx("1 3", file_history=fh, conversation=conv)

    await cmd.handler(ctx)

    assert fh.rewind_calls == [0]
    # 对话历史未被修改
    assert conv.replace_calls == []
    text = "\n".join(ui.messages)
    assert "Restored 2 file(s)" in text
    assert "Conversation unchanged" in text


# 验证无效 option 提示 "Invalid option"。
async def test_rewind_invalid_option_shows_message() -> None:
    ts = datetime.datetime.now()
    snap = Snapshot(
        message_index=0,
        user_text="test",
        backups={},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap], rewind_result=[])
    cmd, ctx, ui = _make_command_and_ctx("1 9", file_history=fh)

    await cmd.handler(ctx)

    assert any("Invalid option" in m for m in ui.messages)


# 验证 /rewind 0（1-based 越界）提示 "not found"。
async def test_rewind_zero_index_shows_not_found() -> None:
    ts = datetime.datetime.now()
    snap = Snapshot(
        message_index=0,
        user_text="test",
        backups={},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap], rewind_result=[])
    cmd, ctx, ui = _make_command_and_ctx("0", file_history=fh)

    await cmd.handler(ctx)

    assert any("not found" in m for m in ui.messages)


# 验证 /rewind abc（非数字）提示 "Invalid checkpoint number."。
async def test_rewind_non_numeric_idx_shows_message() -> None:
    ts = datetime.datetime.now()
    snap = Snapshot(
        message_index=0,
        user_text="test",
        backups={},
        timestamp=ts,
    )
    fh = _FakeFileHistory(snapshots=[snap], rewind_result=[])
    cmd, ctx, ui = _make_command_and_ctx("abc", file_history=fh)

    await cmd.handler(ctx)

    assert any("Invalid checkpoint number" in m for m in ui.messages)
    assert fh.rewind_calls == []


# 验证列表中的 user_text 超过截断长度时被截断。
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
    assert "…" in text
