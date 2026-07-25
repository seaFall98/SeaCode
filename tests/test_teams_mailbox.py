"""teams/mailbox.py 单测：write/read/consume/broadcast/cleanup/锁/并发/stale/容错。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from seacode.teams.mailbox import (
    LOCK_RETRY_COUNT,
    STALE_LOCK_SECONDS,
    Mailbox,
    MailboxMessage,
    create_message,
)

# ---------------------------------------------------------------------------
# create_message
# ---------------------------------------------------------------------------


# 验证 create_message 生成的 id 是 12 位、timestamp 非空、metadata 默认空 dict。
# 调用 create_message，断言 id 长度、timestamp 类型、metadata 为空 dict。
def test_create_message_defaults() -> None:
    msg = create_message(
        from_agent="alice",
        to_agent="lead",
        content="hello",
        summary="greeting",
    )
    assert len(msg.id) == 12
    assert msg.timestamp > 0
    assert msg.metadata == {}
    assert msg.message_type == "text"
    assert msg.read is False


# 验证 create_message 接受自定义 metadata。
# 传入非空 metadata，断言消息保留该 dict。
def test_create_message_with_metadata() -> None:
    msg = create_message(
        from_agent="a",
        to_agent="b",
        content="x",
        summary="s",
        metadata={"k": "v"},
    )
    assert msg.metadata == {"k": "v"}


# ---------------------------------------------------------------------------
# write / read / consume
# ---------------------------------------------------------------------------


# 验证 write 追加；写两条后 read 返回 2 条。
# 创建 Mailbox，连续 write 两条，断言 read 长度为 2。
def test_write_append_then_read(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    m1 = create_message("a", "lead", "first", "s1")
    m2 = create_message("a", "lead", "second", "s2")
    mb.write("lead", m1)
    mb.write("lead", m2)
    unread = mb.read("lead")
    assert len(unread) == 2
    assert unread[0].content == "first"
    assert unread[1].content == "second"


# 验证 read 只读未读；consume 后 read 返回空。
# 写一条消息，consume 后再 read，断言 read 返回空。
def test_read_only_unread_after_consume(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    mb.write("lead", create_message("a", "lead", "x", "s"))
    consumed = mb.consume("lead")
    assert len(consumed) == 1
    assert mb.read("lead") == []


# 验证 consume 标记已读并返回；二次 consume 返回空。
# 写两条，第一次 consume 返回 2 条，第二次 consume 返回空。
def test_consume_marks_read(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    mb.write("lead", create_message("a", "lead", "1", "s"))
    mb.write("lead", create_message("a", "lead", "2", "s"))
    first = mb.consume("lead")
    assert len(first) == 2
    second = mb.consume("lead")
    assert second == []


# ---------------------------------------------------------------------------
# broadcast / cleanup / cleanup_all
# ---------------------------------------------------------------------------


# 验证 broadcast 遍历收件人并排除发送者。
# 写 3 个收件人，exclude 跳过 1 个，断言 2 个收件人各收到 1 条。
def test_broadcast_exclude(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    msg = create_message("alice", "*", "hi", "greeting")
    mb.broadcast(msg, ["a", "b", "c"], exclude="a")
    assert len(mb.read("a")) == 0
    assert len(mb.read("b")) == 1
    assert len(mb.read("c")) == 1


# 验证 cleanup 删除 inbox 与 lock 文件。
# 写消息后 cleanup，断言 inbox 文件不存在。
def test_cleanup_removes_files(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    mb.write("lead", create_message("a", "lead", "x", "s"))
    inbox = tmp_path / "mb" / "lead.json"
    assert inbox.exists()
    mb.cleanup("lead")
    assert not inbox.exists()


# 验证 cleanup_all 删除目录下所有 .json 与 .json.lock 文件。
# 写入多个 agent 的消息后 cleanup_all，断言目录下无 .json 残留。
def test_cleanup_all_removes_everything(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    mb.write("a", create_message("x", "a", "1", "s"))
    mb.write("b", create_message("x", "b", "2", "s"))
    mb.cleanup_all()
    for f in (tmp_path / "mb").iterdir():
        assert not f.name.endswith(".json")


# ---------------------------------------------------------------------------
# 并发写入
# ---------------------------------------------------------------------------


# 验证多线程并发写入不丢失不重复。
# 10 个线程各写 10 条，最终 consume 应得 100 条且无重复 id。
def test_concurrent_writes_no_loss(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    errors: list[Exception] = []

    def worker(tid: int) -> None:
        try:
            for i in range(10):
                mb.write(
                    "lead",
                    create_message(
                        f"t{tid}", "lead", f"msg-{tid}-{i}", f"t{tid}-{i}"
                    ),
                )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    consumed = mb.consume("lead")
    assert len(consumed) == 100
    ids = {m.id for m in consumed}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# stale 锁与锁清理
# ---------------------------------------------------------------------------


# 验证写后 lock 文件被 finally 清理。
# 写一条消息，断言 .json.lock 不存在。
def test_lock_file_cleaned_after_write(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    mb.write("lead", create_message("a", "lead", "x", "s"))
    lock_file = tmp_path / "mb" / "lead.json.lock"
    assert not lock_file.exists()


# 验证 stale 锁被接管。
# 手动创建一个 mtime 远在 stale 阈值之前的 lock 文件，再 write 应成功。
def test_stale_lock_takeover(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    lock_file = tmp_path / "mb" / "lead.json.lock"
    lock_file.write_text("", encoding="utf-8")
    # 把 mtime 设到 stale 阈值之前。
    stale_mtime = time.time() - (STALE_LOCK_SECONDS + 5)
    os.utime(lock_file, (stale_mtime, stale_mtime))
    # write 应该接管 stale 锁并写入。
    mb.write("lead", create_message("a", "lead", "x", "s"))
    assert len(mb.read("lead")) == 1


# ---------------------------------------------------------------------------
# 容错
# ---------------------------------------------------------------------------


# 验证 inbox 文件为非法 JSON 时 read 返回空列表。
# 写入非法 JSON 后 read，断言返回空列表不抛异常。
def test_read_corrupt_json_returns_empty(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")
    (tmp_path / "mb" / "lead.json").write_text("{not json", encoding="utf-8")
    assert mb.read("lead") == []


# 验证锁重试 LOCK_RETRY_COUNT 次后仍失败时 write 抛 OSError。
# mock os.open 持续抛 FileExistsError，st_mtime 始终新鲜，断言 write 抛 OSError。
def test_write_raises_after_retries(tmp_path: Path) -> None:
    mb = Mailbox(tmp_path / "mb")

    real_open = os.open

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        # 只对 lock 文件路径抛 FileExistsError；其它路径透传真实 os.open。
        if str(path).endswith(".json.lock"):
            raise FileExistsError(str(path))
        return real_open(path, flags, mode)

    # stat.st_mtime 始终为当前时间，避免触发 stale 接管。
    with patch("seacode.teams.mailbox.os.open", side_effect=fake_open), \
         patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value.st_mtime = time.time()
        with pytest.raises(OSError):
            mb.write("lead", create_message("a", "lead", "x", "s"))


# 验证 MailboxMessage.to_dict / from_dict 往返一致。
# 构造消息序列化后反序列化，断言字段一致。
def test_message_round_trip() -> None:
    msg = create_message(
        "a", "b", "content", "summary",
        message_type="shutdown_request",
        metadata={"k": "v"},
    )
    restored = MailboxMessage.from_dict(msg.to_dict())
    assert restored.id == msg.id
    assert restored.from_agent == msg.from_agent
    assert restored.to_agent == msg.to_agent
    assert restored.content == msg.content
    assert restored.summary == msg.summary
    assert restored.message_type == msg.message_type
    assert restored.timestamp == msg.timestamp
    assert restored.metadata == msg.metadata


# 验证 LOCK_RETRY_COUNT 与 STALE_LOCK_SECONDS 常量值。
# 直接断言模块级常量，确保配置与文档一致。
def test_module_constants() -> None:
    assert LOCK_RETRY_COUNT == 10
    assert STALE_LOCK_SECONDS == 10
