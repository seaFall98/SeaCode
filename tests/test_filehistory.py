"""FileHistory 单元测试：track_edit / make_snapshot / rewind 全分支与并发安全。"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from seacode.filehistory.history import MAX_SNAPSHOTS, Backup, FileHistory, Snapshot

# ---------------------------------------------------------------------------
# track_edit
# ---------------------------------------------------------------------------


# 验证 track_edit 把版本号从 1 递增到 3，且每次都生成独立备份文件。
# 对同一文件连续 track_edit 三次（每次前修改内容），断言 _tracked 版本号为 3 且三个备份文件都存在。
def test_track_edit_increments_version_and_creates_backup_files(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("v0", encoding="utf-8")

    # 三次 track_edit，每次先改内容再 track（track 备份的是改之前的版本）。
    target.write_text("v1", encoding="utf-8")
    fh.track_edit(target)
    target.write_text("v2", encoding="utf-8")
    fh.track_edit(target)
    target.write_text("v3", encoding="utf-8")
    fh.track_edit(target)

    assert fh._tracked[str(target.resolve())] == 3
    # 三个版本的备份文件都应存在。
    for ver in (1, 2, 3):
        name = fh._backup_name(str(target.resolve()), ver)
        assert (fh._session_dir / name).exists()


# 验证 track_edit 对新建文件（FileNotFoundError）静默忽略但版本号仍推进。
# 调用 track_edit 一个不存在的路径，断言 _tracked 版本号为 1 且不抛异常。
def test_track_edit_silently_ignores_new_file(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "new.txt"

    fh.track_edit(target)  # 文件不存在，应静默忽略。

    assert fh._tracked[str(target.resolve())] == 1
    # 备份文件不应被创建。
    name = fh._backup_name(str(target.resolve()), 1)
    assert not (fh._session_dir / name).exists()


# 验证 track_edit 备份文件名格式为 sha256(path)[:16]@vN。
# 对一个已知路径 track_edit，断言备份文件名符合 hash@v1 格式。
def test_track_edit_backup_name_format(tmp_path: Path) -> None:
    import hashlib

    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "name.txt"
    target.write_text("x", encoding="utf-8")

    fh.track_edit(target)

    abs_path = str(target.resolve())
    expected_hash = hashlib.sha256(abs_path.encode()).hexdigest()[:16]
    expected_name = f"{expected_hash}@v1"
    assert (fh._session_dir / expected_name).exists()


# ---------------------------------------------------------------------------
# make_snapshot
# ---------------------------------------------------------------------------


# 验证 make_snapshot 追加一个 Snapshot 到 _snapshots，且 backups 含已跟踪文件。
# track_edit 一个文件后调用 make_snapshot，断言 _snapshots 长度为 1 且 backups 含该路径。
def test_make_snapshot_appends_snapshot_with_tracked_files(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)

    snap = fh.make_snapshot(0, "user text")

    assert len(fh._snapshots) == 1
    assert snap.message_index == 0
    assert snap.user_text == "user text"
    assert str(target.resolve()) in snap.backups
    assert snap.backups[str(target.resolve())].version == 1


# 验证 make_snapshot 在 _snapshots 超过 MAX_SNAPSHOTS 时滚动保留最近 MAX_SNAPSHOTS 条。
# 构造 105 个快照，断言 _snapshots 长度为 MAX_SNAPSHOTS 且保留的是最后 100 条。
def test_make_snapshot_rolls_over_max_snapshots(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)

    for i in range(MAX_SNAPSHOTS + 5):
        fh.make_snapshot(i, f"msg-{i}")

    snaps = fh.get_snapshots()
    assert len(snaps) == MAX_SNAPSHOTS
    # 保留的应是最后 100 条，message_index 从 5 开始。
    assert snaps[0].message_index == 5
    assert snaps[-1].message_index == MAX_SNAPSHOTS + 4


# 验证 make_snapshot 在备份文件丢失时尝试用当前文件内容补齐。
# 手工删除备份文件后调用 make_snapshot，断言 backups 仍含该路径且备份文件被重建。
def test_make_snapshot_recreates_missing_backup_from_current_file(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)
    # 手工删除备份文件，模拟外部清理。
    abs_path = str(target.resolve())
    bp = fh._session_dir / fh._backup_name(abs_path, 1)
    bp.unlink()
    assert not bp.exists()

    snap = fh.make_snapshot(0, "msg")

    # 备份应被当前文件内容重建。
    assert bp.exists()
    assert abs_path in snap.backups


# 验证 make_snapshot 在备份丢失且当前文件也不存在时跳过该路径。
# track_edit 一个新文件后删除当前文件再 snapshot，断言 backups 不含该路径。
def test_make_snapshot_skips_path_when_backup_and_current_both_missing(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "ghost.txt"
    # track_edit 一个不存在的文件，版本号推进但无备份文件。
    fh.track_edit(target)
    abs_path = str(target.resolve())
    # 当前文件也不存在，make_snapshot 应跳过。
    snap = fh.make_snapshot(0, "msg")

    assert abs_path not in snap.backups


# ---------------------------------------------------------------------------
# rewind
# ---------------------------------------------------------------------------


# 验证 rewind 越界 index 返回空列表。
# 在只有一个快照的情况下 rewind(5)，断言返回 []。
def test_rewind_out_of_range_returns_empty(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)
    fh.make_snapshot(0, "msg-0")
    fh.make_snapshot(1, "msg-1")

    assert fh.rewind(5) == []


# 验证 rewind 备份存在且当前内容不同时覆盖当前文件。
# track_edit 后修改文件内容，rewind(0) 应把文件内容还原到备份时的状态。
def test_rewind_restores_file_to_backup_content(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)  # 备份 "orig" 到 v1
    fh.make_snapshot(0, "msg-0")
    # 修改文件内容。
    target.write_text("modified", encoding="utf-8")

    changed = fh.rewind(0)

    assert str(target.resolve()) in changed
    assert target.read_text(encoding="utf-8") == "orig"


# 验证 rewind 备份丢失但当前文件存在时删除当前文件。
# 删除备份文件后 rewind，断言当前文件被删除。
def test_rewind_deletes_current_file_when_backup_missing(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)
    fh.make_snapshot(0, "msg-0")
    # 删除备份文件。
    abs_path = str(target.resolve())
    bp = fh._session_dir / fh._backup_name(abs_path, 1)
    bp.unlink()

    changed = fh.rewind(0)

    assert abs_path in changed
    assert not target.exists()


# 验证 rewind 截断 _snapshots 到目标快照之后。
# 构造 3 个快照后 rewind(0)，断言 _snapshots 只剩 1 条。
def test_rewind_truncates_snapshots_to_target(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    fh.track_edit(target)
    for i in range(3):
        fh.make_snapshot(i, f"msg-{i}")

    fh.rewind(0)

    assert len(fh.get_snapshots()) == 1
    assert fh.get_snapshots()[0].message_index == 0


# 验证 rewind 把 _tracked 重置为目标快照的版本号。
# track_edit 推进到 v2 后 rewind(0)（目标快照引用 v1），断言 _tracked 为 1。
def test_rewind_resets_tracked_to_target_versions(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("v0", encoding="utf-8")
    fh.track_edit(target)  # v1 备份 "v0"
    fh.make_snapshot(0, "msg-0")  # snapshot 0 引用 v1
    target.write_text("v1", encoding="utf-8")
    fh.track_edit(target)  # v2 备份 "v1"
    fh.make_snapshot(1, "msg-1")  # snapshot 1 引用 v2

    fh.rewind(0)

    assert fh._tracked[str(target.resolve())] == 1


# 验证 rewind 内容相同时不计入 changed 列表。
# 备份内容与当前内容一致时 rewind，断言 changed 为空。
def test_rewind_skips_unchanged_files(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("same", encoding="utf-8")
    fh.track_edit(target)
    fh.make_snapshot(0, "msg-0")
    # 不修改文件，rewind 应识别为无变化。

    changed = fh.rewind(0)

    assert changed == []


# ---------------------------------------------------------------------------
# get_snapshots / has_snapshots
# ---------------------------------------------------------------------------


# 验证 get_snapshots 返回 _snapshots 的浅拷贝，外部修改不影响内部。
# 取出 snapshots 后 append 一项，断言 fh.get_snapshots() 长度不变。
def test_get_snapshots_returns_copy(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    fh.track_edit(target)
    fh.make_snapshot(0, "msg")

    snaps = fh.get_snapshots()
    snaps.append(
        Snapshot(
            message_index=99,
            user_text="x",
            backups={},
            timestamp=datetime.now(),
        )
    )

    assert len(fh.get_snapshots()) == 1


# 验证 has_snapshots 在无快照时返回 False，有快照时返回 True。
# 初始无快照断言 False；make_snapshot 后断言 True。
def test_has_snapshots_reflects_state(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")

    assert fh.has_snapshots() is False

    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    fh.track_edit(target)
    fh.make_snapshot(0, "msg")

    assert fh.has_snapshots() is True


# ---------------------------------------------------------------------------
# 并发安全
# ---------------------------------------------------------------------------


# 验证多线程并发 track_edit 同一文件不丢版本号且不破坏 _tracked。
# 启 10 个线程各 track_edit 5 次，断言最终版本号为 50 且无异常。
def test_track_edit_concurrent_threads_safe(tmp_path: Path) -> None:
    fh = FileHistory(tmp_path, "sess-1")
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")

    def worker() -> None:
        for _ in range(5):
            fh.track_edit(target)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fh._tracked[str(target.resolve())] == 50


# ---------------------------------------------------------------------------
# Snapshot / Backup 数据类
# ---------------------------------------------------------------------------


# 验证 Backup 三字段构造与默认值。
# 构造 Backup，断言 path/version/timestamp 字段正确保留。
def test_backup_dataclass_fields() -> None:
    from datetime import datetime

    ts = datetime.now()
    b = Backup(path="/tmp/x", version=2, timestamp=ts)
    assert b.path == "/tmp/x"
    assert b.version == 2
    assert b.timestamp is ts


# 验证 Snapshot 四字段构造与默认值。
# 构造 Snapshot，断言 message_index/user_text/backups/timestamp 字段正确保留。
def test_snapshot_dataclass_fields() -> None:
    from datetime import datetime

    ts = datetime.now()
    backups = {"p": Backup(path="bp", version=1, timestamp=ts)}
    snap = Snapshot(
        message_index=3,
        user_text="hello",
        backups=backups,
        timestamp=ts,
    )
    assert snap.message_index == 3
    assert snap.user_text == "hello"
    assert snap.backups is backups
    assert snap.timestamp is ts


# 验证 FileHistory 构造时创建 .seacode/file-history/<session_id> 目录。
# 在 tmp_path 构造 FileHistory，断言目录存在。
def test_filehistory_creates_session_dir(tmp_path: Path) -> None:
    FileHistory(tmp_path, "sess-xyz")

    assert (tmp_path / ".seacode" / "file-history" / "sess-xyz").is_dir()
