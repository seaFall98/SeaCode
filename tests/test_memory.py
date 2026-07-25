from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from seacode.conversation import (
    Message,
    ToolResultBlock,
    ToolUseBlock,
)
from seacode.memory.auto_memory import (
    ENTRYPOINT_NAME,
    MAX_ENTRYPOINT_LINES,
    MemoryManager,
    parse_frontmatter,
    truncate_entrypoint_content,
)
from seacode.memory.consolidation import (
    LOCK_FILE,
    MemoryConsolidator,
    _is_process_running,
    _list_sessions_since,
    _read_last_consolidated_at,
    _rollback_lock,
    _try_acquire_lock,
)
from seacode.memory.instructions import (
    MAX_INCLUDE_DEPTH,
    load_instructions,
    process_includes,
)
from seacode.memory.recall import (
    RelevantMemory,
    find_relevant_memories,
    format_memory_manifest,
    memory_age,
    memory_age_days,
    memory_freshness_text,
    render_reminder,
    scan_memory_files,
)
from seacode.memory.session import (
    DEFAULT_MAX_AGE_DAYS,
    TITLE_MAX_LENGTH,
    RecordType,
    SessionManager,
    SessionMeta,
    SessionRecord,
    make_compact_boundary,
    parse_compact_boundary,
    records_to_messages,
    validate_message_chain,
)

# =========================================================================
# A. 指令文件 @include 展开
# =========================================================================


# 验证不含 @ 引用的文本原样返回。
# process_includes 对纯文本不做任何替换。
def test_process_includes_no_includes_returns_content_unchanged(
    tmp_path: Path,
) -> None:
    content = "line1\nline2\nline3"
    assert process_includes(content, tmp_path, tmp_path) == content


# 验证基本 @./ 引用：把子文件内容内联到引用位置。
# 引用前后的文本保留，被引用文件内容出现在中间。
def test_process_includes_basic_relative_include(tmp_path: Path) -> None:
    child = tmp_path / "child.md"
    child.write_text("included content", encoding="utf-8")
    content = "before\n@./child.md\nafter"
    result = process_includes(content, tmp_path, tmp_path)
    assert "included content" in result
    assert "before" in result
    assert "after" in result


# 验证递归 @ 引用：A 引用 B，B 引用 C，最终内容包含最深层的 C。
# 递归深度未超过 MAX_INCLUDE_DEPTH 时正常展开。
def test_process_includes_recursive_include(tmp_path: Path) -> None:
    grandchild = tmp_path / "grandchild.md"
    grandchild.write_text("deep content", encoding="utf-8")
    child = tmp_path / "child.md"
    child.write_text("@./grandchild.md", encoding="utf-8")
    result = process_includes("@./child.md", tmp_path, tmp_path)
    assert "deep content" in result


# 验证深度上限：超过 MAX_INCLUDE_DEPTH 时原样返回不展开。
# 防止恶意构造的循环引用导致栈溢出。
def test_process_includes_depth_limit_returns_content(tmp_path: Path) -> None:
    content = "should stop"
    result = process_includes(content, tmp_path, tmp_path, depth=MAX_INCLUDE_DEPTH)
    assert result == content


# 验证不存在的引用路径：输出 "skipped: file not found" 注释而非抛异常。
# 保证单条坏引用不会让整个指令文件加载失败。
def test_process_includes_missing_file_emits_skip_comment(tmp_path: Path) -> None:
    content = "@./nonexistent.md"
    result = process_includes(content, tmp_path, tmp_path)
    assert "skipped: file not found" in result


# 验证循环检测：A→B→A 不会无限递归。
# 第二次遇到 A 时跳过，结果里 "start" 只出现一次。
def test_process_includes_cycle_detection_skips_second_visit(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("start\n@./b.md\nend-a", encoding="utf-8")
    b.write_text("middle\n@./a.md\nend-b", encoding="utf-8")
    result = process_includes("@./a.md", tmp_path, tmp_path)
    assert "start" in result
    assert "middle" in result
    assert "end-b" in result
    # 循环检测生效：a.md 不会被第二次展开。
    assert result.count("start") == 1


# 验证代码块内的 @ 引用不展开。
# ```围栏内的 @./xxx.md 应原样保留，避免误展开示例代码。
def test_process_includes_ignores_at_inside_code_block(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.md"
    child.write_text("should not appear", encoding="utf-8")
    content = "```\n@./child.md\n```"
    result = process_includes(content, tmp_path, tmp_path)
    assert "should not appear" not in result
    assert "@./child.md" in result


# 验证 load_instructions 按优先级拼接：用户级 → 项目级 → 本地覆盖。
# 同时验证 SEACODE.md 与 AGENTS.md 都被发现，且用 --- 分隔。
def test_load_instructions_concatenates_user_project_and_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 模拟用户级 home 目录，避免污染真实 ~/.seacode/。
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    user_seacode = fake_home / ".seacode"
    user_seacode.mkdir()
    (user_seacode / "SEACODE.md").write_text("user-global-instructions", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # 项目级 SEACODE.md 与本地覆盖。
    (tmp_path / "SEACODE.md").write_text("project-instructions", encoding="utf-8")
    (tmp_path / "SEACODE.local.md").write_text("local-override", encoding="utf-8")

    result = load_instructions(str(tmp_path))
    assert "user-global-instructions" in result
    assert "project-instructions" in result
    assert "local-override" in result
    # 优先级顺序：用户级在前，本地覆盖在最后。
    assert result.index("user-global-instructions") < result.index("project-instructions")
    assert result.index("project-instructions") < result.index("local-override")


# 验证无任何指令文件时返回空字符串，不抛异常。
def test_load_instructions_returns_empty_when_no_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert load_instructions(str(tmp_path)) == ""


# =========================================================================
# B. SessionRecord 序列化与消息互转
# =========================================================================


# 验证 SessionRecord.to_jsonl / from_jsonl 的往返一致性。
# timestamp、type、content、tool_use_id、is_error 五个字段都能无损还原。
def test_session_record_jsonl_roundtrip() -> None:
    record = SessionRecord(
        type=RecordType.TOOL_RESULT,
        content="some output",
        timestamp=datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC),
        tool_use_id="tool_123",
        is_error=True,
    )
    line = record.to_jsonl()
    restored = SessionRecord.from_jsonl(line)
    assert restored is not None
    assert restored.type == RecordType.TOOL_RESULT
    assert restored.content == "some output"
    assert restored.tool_use_id == "tool_123"
    assert restored.is_error is True
    assert restored.timestamp == record.timestamp


# 验证损坏的 JSONL 行返回 None 而非抛异常。
# resume 时遇到坏行应跳过继续处理后续行。
def test_session_record_from_jsonl_returns_none_on_corrupt_line() -> None:
    assert SessionRecord.from_jsonl("not json") is None
    assert SessionRecord.from_jsonl('{"missing_type": true}') is None
    assert SessionRecord.from_jsonl('{"type": "unknown_type", "content": "x"}') is None


# 验证普通 user 消息转为单条 USER 记录，content 原样保留。
def test_session_record_from_message_user_text() -> None:
    msg = Message(role="user", content="hello")
    records = SessionRecord.from_message(msg)
    assert len(records) == 1
    assert records[0].type == RecordType.USER
    assert records[0].content == "hello"


# 验证 assistant 工具调用消息转为单条 ASSISTANT 记录，content 是 content-blocks 列表。
# 列表含 text 块（如果有正文）与 tool_use 块（每个工具调用一个）。
def test_session_record_from_message_assistant_with_tool_uses() -> None:
    msg = Message(
        role="assistant",
        content="let me read",
        tool_uses=[
            ToolUseBlock(
                tool_use_id="t1", tool_name="ReadFile", arguments={"file_path": "a"}
            )
        ],
    )
    records = SessionRecord.from_message(msg)
    assert len(records) == 1
    assert records[0].type == RecordType.ASSISTANT
    assert isinstance(records[0].content, list)
    assert records[0].content[0] == {"type": "text", "text": "let me read"}
    assert records[0].content[1]["type"] == "tool_use"
    assert records[0].content[1]["name"] == "ReadFile"


# 验证 tool_results 消息转为多条 TOOL_RESULT 记录，每条带 tool_use_id。
def test_session_record_from_message_tool_results() -> None:
    msg = Message(
        role="user",
        content="",
        tool_results=[
            ToolResultBlock(tool_use_id="t1", content="result1"),
            ToolResultBlock(tool_use_id="t2", content="result2", is_error=True),
        ],
    )
    records = SessionRecord.from_message(msg)
    assert len(records) == 2
    assert all(r.type == RecordType.TOOL_RESULT for r in records)
    assert records[0].tool_use_id == "t1"
    assert records[0].content == "result1"
    assert records[1].tool_use_id == "t2"
    assert records[1].is_error is True


# 验证 records_to_messages 能把 SessionRecord 列表还原为 Message 列表。
# 重点验证 tool_use 与 tool_result 的配对关系在往返后仍然正确。
def test_records_to_messages_roundtrip_preserves_tool_pairing() -> None:
    original = [
        Message(role="user", content="please read"),
        Message(
            role="assistant",
            content="reading",
            tool_uses=[
                ToolUseBlock(
                    tool_use_id="t1", tool_name="ReadFile", arguments={"file_path": "a"}
                )
            ],
        ),
        Message(
            role="user",
            content="",
            tool_results=[ToolResultBlock(tool_use_id="t1", content="file content")],
        ),
        Message(role="assistant", content="done"),
    ]
    records: list[SessionRecord] = []
    for msg in original:
        records.extend(SessionRecord.from_message(msg))
    restored = records_to_messages(records)
    assert len(restored) == 4
    assert restored[0].role == "user"
    assert restored[0].content == "please read"
    assert restored[1].role == "assistant"
    assert restored[1].content == "reading"
    assert len(restored[1].tool_uses) == 1
    assert restored[1].tool_uses[0].tool_use_id == "t1"
    assert restored[2].role == "user"
    assert len(restored[2].tool_results) == 1
    assert restored[2].tool_results[0].tool_use_id == "t1"
    assert restored[2].tool_results[0].content == "file content"
    assert restored[3].role == "assistant"
    assert restored[3].content == "done"


# =========================================================================
# C. Compact boundary 内联重建
# =========================================================================


# 验证 make_compact_boundary / parse_compact_boundary 的往返一致性。
# boundary 内联了 summary 与 keep 尾部，往返后 summary 字符串与 keep 消息列表都能还原。
def test_compact_boundary_roundtrip_preserves_summary_and_keep() -> None:
    keep = [
        Message(role="user", content="recent question"),
        Message(role="assistant", content="recent answer"),
    ]
    record = make_compact_boundary(summary="early summary", keep=keep)
    assert record.type == RecordType.COMPACT_BOUNDARY
    summary, keep_messages = parse_compact_boundary(record)
    assert summary == "early summary"
    assert len(keep_messages) == 2
    assert keep_messages[0].content == "recent question"
    assert keep_messages[1].content == "recent answer"


# 验证格式异常的 boundary payload 降级返回 ("", [])，不抛异常。
# 单条损坏的 boundary 不应导致 resume 崩溃。
def test_parse_compact_boundary_degrades_on_malformed_payload() -> None:
    record = SessionRecord(
        type=RecordType.COMPACT_BOUNDARY,
        content="not a dict",
        timestamp=datetime.now(UTC),
    )
    summary, keep = parse_compact_boundary(record)
    assert summary == ""
    assert keep == []


# 验证 records_to_messages 遇 COMPACT_BOUNDARY 时展开为摘要 user 消息 + keep 尾部。
# boundary 之前的原始前缀不重放，只展开 boundary 自身。
def test_records_to_messages_expands_compact_boundary() -> None:
    keep = [Message(role="user", content="recent")]
    boundary = make_compact_boundary(summary="old summary", keep=keep)
    messages = records_to_messages([boundary])
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert "old summary" in messages[0].content
    assert messages[1].content == "recent"


# =========================================================================
# D. 消息链校验
# =========================================================================


# 验证完整的 tool_use↔tool_result 配对返回最后位置（截断点等于记录数）。
def test_validate_message_chain_returns_full_length_when_all_paired() -> None:
    records = [
        SessionRecord(
            type=RecordType.ASSISTANT,
            content=[{"type": "tool_use", "id": "t1", "name": "X", "input": {}}],
            timestamp=datetime.now(UTC),
        ),
        SessionRecord(
            type=RecordType.TOOL_RESULT,
            content="ok",
            timestamp=datetime.now(UTC),
            tool_use_id="t1",
        ),
    ]
    assert validate_message_chain(records) == 2


# 验证孤立的 tool_use（无配对 tool_result）被截断到最后一个完整位置。
# 截断点应停在 tool_result 之后，不包含孤立的 tool_use。
def test_validate_message_chain_truncates_orphan_tool_use() -> None:
    records = [
        SessionRecord(
            type=RecordType.USER,
            content="hello",
            timestamp=datetime.now(UTC),
        ),
        SessionRecord(
            type=RecordType.ASSISTANT,
            content=[{"type": "tool_use", "id": "t1", "name": "X", "input": {}}],
            timestamp=datetime.now(UTC),
        ),
        SessionRecord(
            type=RecordType.TOOL_RESULT,
            content="ok",
            timestamp=datetime.now(UTC),
            tool_use_id="t1",
        ),
        # 孤立 tool_use，无配对 tool_result。
        SessionRecord(
            type=RecordType.ASSISTANT,
            content=[{"type": "tool_use", "id": "t2", "name": "Y", "input": {}}],
            timestamp=datetime.now(UTC),
        ),
    ]
    # 截断点应在第 3 条（索引 2）之后，即位置 3。
    assert validate_message_chain(records) == 3


# =========================================================================
# E. SessionManager 持久化与 resume
# =========================================================================


# 验证 create 创建新会话并写入空 .meta 文件；append 追加消息后 meta 更新。
# 重点验证：JSONL 增量追加、message_count 递增、title 取首条 user 消息。
def test_session_manager_create_and_append_persists_to_jsonl(
    tmp_path: Path,
) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id

    # 初始 .meta 已写入，message_count 为 0。
    meta_path = tmp_path / ".seacode" / "sessions" / f"{session_id}.meta"
    assert meta_path.exists()
    assert session.meta.message_count == 0

    # 追加首条 user 消息：title 取前 50 字符，message_count 递增。
    session.append(Message(role="user", content="hello world"))
    assert session.meta.message_count == 1
    assert session.meta.title == "hello world"

    # JSONL 文件非空，包含 USER 记录。
    jsonl_path = tmp_path / ".seacode" / "sessions" / f"{session_id}.jsonl"
    assert jsonl_path.exists()
    content = jsonl_path.read_text(encoding="utf-8")
    assert '"type":"user"' in content
    assert "hello world" in content

    session.close()


# 验证 title 截断到 TITLE_MAX_LENGTH 字符，防止超长 user 消息撑爆 .meta。
def test_session_append_truncates_title_to_max_length(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    long_text = "x" * (TITLE_MAX_LENGTH + 50)
    session.append(Message(role="user", content=long_text))
    assert len(session.meta.title) == TITLE_MAX_LENGTH
    session.close()


# 验证 resume 能从 JSONL 重建消息列表，且以追加模式重开文件句柄。
# 重点验证：往返后的消息内容与原始一致；新句柄可继续 append 续写。
def test_session_manager_resume_rebuilds_messages_and_reopens_handle(
    tmp_path: Path,
) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id
    session.append(Message(role="user", content="first question"))
    session.append(Message(role="assistant", content="first answer"))
    session.close()

    # resume 重建消息列表。
    result = manager.resume(session_id)
    assert result is not None
    assert len(result.messages) == 2
    assert result.messages[0].content == "first question"
    assert result.messages[1].content == "first answer"

    # 新句柄可继续 append 续写。
    result.session.append(Message(role="user", content="follow up"))
    result.session.close()

    # 再次 resume 应看到 3 条消息。
    result2 = manager.resume(session_id)
    assert result2 is not None
    assert len(result2.messages) == 3
    assert result2.messages[2].content == "follow up"
    result2.session.close()


# 验证 resume 不存在的 session_id 返回 None。
def test_session_manager_resume_returns_none_for_missing_session(
    tmp_path: Path,
) -> None:
    manager = SessionManager(str(tmp_path))
    assert manager.resume("nonexistent_session_id") is None


# 验证 list 按 last_active 倒序排列，最近活跃的在前。
def test_session_manager_list_sorts_by_last_active_descending(
    tmp_path: Path,
) -> None:
    manager = SessionManager(str(tmp_path))
    # 创建 3 个会话，手动调整 last_active 制造顺序差异。
    sessions = [manager.create() for _ in range(3)]
    sessions[0].meta.last_active = datetime.now(UTC) - timedelta(hours=2)
    sessions[1].meta.last_active = datetime.now(UTC) - timedelta(hours=1)
    sessions[2].meta.last_active = datetime.now(UTC)
    for s in sessions:
        s.meta.save(tmp_path / ".seacode" / "sessions" / f"{s.session_id}.meta")
        s.close()

    metas = manager.list()
    assert len(metas) == 3
    # 最近活跃的（索引 2）应排第一。
    assert metas[0].id == sessions[2].session_id
    assert metas[1].id == sessions[1].session_id
    assert metas[2].id == sessions[0].session_id


# 验证 delete 删除 .jsonl 与 .meta，返回 True；再次删除返回 False。
def test_session_manager_delete_removes_files(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id
    session.close()

    assert manager.delete(session_id) is True
    assert not (tmp_path / ".seacode" / "sessions" / f"{session_id}.jsonl").exists()
    assert not (tmp_path / ".seacode" / "sessions" / f"{session_id}.meta").exists()
    # 已删除时返回 False。
    assert manager.delete(session_id) is False


# 验证 cleanup 删除超过 max_age_days 未活跃的会话。
def test_session_manager_cleanup_removes_stale_sessions(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    fresh = manager.create()
    stale = manager.create()
    # 把 stale 的 last_active 设为 40 天前（超过默认 30 天保留期）。
    stale.meta.last_active = datetime.now(UTC) - timedelta(days=40)
    stale.meta.save(tmp_path / ".seacode" / "sessions" / f"{stale.session_id}.meta")
    fresh.close()
    stale.close()

    removed = manager.cleanup(max_age_days=DEFAULT_MAX_AGE_DAYS)
    assert removed == 1
    # fresh 仍在，stale 已删。
    assert manager.resume(fresh.session_id) is not None
    assert manager.resume(stale.session_id) is None


# 验证 resume 遇 COMPACT_BOUNDARY 时只从最后一个 boundary 重放。
# boundary 之前的原始前缀保留在磁盘但不重放；boundary 内联的 keep 尾部被展开。
def test_session_manager_resume_replays_from_last_compact_boundary(
    tmp_path: Path,
) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id

    # 写入：早期 user 消息 → boundary（含摘要 + keep 尾部）→ 续写消息。
    session.append(Message(role="user", content="old question before compact"))
    keep = [Message(role="user", content="recent after compact")]
    boundary = make_compact_boundary(summary="old context summarized", keep=keep)
    session.append_record(boundary)
    session.append(Message(role="assistant", content="follow up answer"))
    session.close()

    result = manager.resume(session_id)
    assert result is not None
    # resume 应只展开 boundary：摘要 user 消息 + keep 尾部 + 续写消息。
    # "old question before compact" 不应出现在重放结果里。
    contents = [m.content for m in result.messages]
    assert "old question before compact" not in contents
    assert any("old context summarized" in c for c in contents)
    assert "recent after compact" in contents
    assert "follow up answer" in contents
    result.session.close()


# =========================================================================
# F. SessionMeta 序列化
# =========================================================================


# 验证 SessionMeta.save / load 的往返一致性。
# 所有字段（含中文 title）都能无损还原。
def test_session_meta_save_load_roundtrip(tmp_path: Path) -> None:
    meta = SessionMeta(
        id="session_test",
        title="测试会话标题",
        summary="a summary",
        message_count=5,
        total_tokens=1234,
        created_at=datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC),
        last_active=datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC),
    )
    path = tmp_path / "session_test.meta"
    meta.save(path)
    loaded = SessionMeta.load(path)
    assert loaded is not None
    assert loaded.id == "session_test"
    assert loaded.title == "测试会话标题"
    assert loaded.summary == "a summary"
    assert loaded.message_count == 5
    assert loaded.total_tokens == 1234
    assert loaded.created_at == meta.created_at
    assert loaded.last_active == meta.last_active


# 验证损坏的 .meta 文件返回 None，不抛异常。
def test_session_meta_load_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.meta"
    path.write_text("not json", encoding="utf-8")
    assert SessionMeta.load(path) is None


# =========================================================================
# G. Auto memory: frontmatter 解析与 MEMORY.md 截断
# =========================================================================


# 验证 parse_frontmatter 提取 name/description/type 三个已知字段。
def test_parse_frontmatter_extracts_known_fields() -> None:
    content = (
        "---\n"
        'name: "test memory"\n'
        "description: a test\n"
        "type: project\n"
        "---\n\nbody"
    )
    mf = parse_frontmatter(content)
    assert mf.name == "test memory"
    assert mf.description == "a test"
    assert mf.type == "project"


# 验证无 frontmatter 的文件返回空字段，不抛异常。
def test_parse_frontmatter_returns_empty_on_no_frontmatter() -> None:
    mf = parse_frontmatter("just plain text")
    assert mf.name == ""
    assert mf.description == ""
    assert mf.type == ""


# 验证未知 type 值被忽略，type 字段保持为空。
def test_parse_frontmatter_ignores_unknown_type() -> None:
    content = "---\nname: x\ntype: unknown_category\n---\n\nbody"
    mf = parse_frontmatter(content)
    assert mf.type == ""


# 验证 truncate_entrypoint_content 在未超限时原样返回（strip 后）。
def test_truncate_entrypoint_content_returns_trimmed_when_under_limit() -> None:
    raw = "  short content  \n"
    assert truncate_entrypoint_content(raw) == "short content"


# 验证超过行数限制时截断并附加 WARNING。
def test_truncate_entrypoint_content_truncates_over_line_limit() -> None:
    lines = [f"line {i}" for i in range(MAX_ENTRYPOINT_LINES + 50)]
    raw = "\n".join(lines)
    result = truncate_entrypoint_content(raw)
    assert "WARNING" in result
    # 截断后行数不超过 MAX_ENTRYPOINT_LINES + WARNING 段落。
    assert len(result.split("\n")) <= MAX_ENTRYPOINT_LINES + 10


# =========================================================================
# H. MemoryManager 加载与展示
# =========================================================================


# 验证 MemoryManager.load 在两个目录都为空时返回非空提示文本（含行为指令）。
# build_memory_prompt 总会返回 "# auto memory" 段，即使 MEMORY.md 为空。
def test_memory_manager_load_returns_prompt_even_when_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("SEA_REMOTE_MEMORY_DIR", "")

    mgr = MemoryManager(str(tmp_path))
    prompt = mgr.load()
    assert "# auto memory" in prompt
    # 两个 MEMORY.md 都是空，应提示 "currently empty"。
    assert "currently empty" in prompt


# 验证 MemoryManager.load 读取已有 MEMORY.md 索引内容。
def test_memory_manager_load_reads_existing_memory_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # 项目级 MEMORY.md 已有索引内容。
    mem_dir = tmp_path / ".seacode" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / ENTRYPOINT_NAME).write_text("- [existing](file.md) — a hook\n", encoding="utf-8")

    mgr = MemoryManager(str(tmp_path))
    prompt = mgr.load()
    assert "existing" in prompt
    assert "a hook" in prompt


# 验证 get_display_text 在无记忆时返回"当前没有任何自动记忆"提示。
def test_memory_manager_get_display_text_empty_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    mgr = MemoryManager(str(tmp_path))
    text = mgr.get_display_text()
    assert "当前没有任何自动记忆" in text


# 验证 get_display_text 在有记忆时列出每条记忆的类型、名称与描述。
def test_memory_manager_get_display_text_lists_memories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    mem_dir = tmp_path / ".seacode" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "topic.md").write_text(
        "---\nname: topic\ndescription: a topic memory\ntype: project\n---\n\nbody\n",
        encoding="utf-8",
    )

    mgr = MemoryManager(str(tmp_path))
    text = mgr.get_display_text()
    assert "[project]" in text
    assert "topic" in text
    assert "a topic memory" in text


# =========================================================================
# I. Recall 扫描与渲染
# =========================================================================


# 验证 scan_memory_files 扫描目录下所有 .md（排除 MEMORY.md），返回 newest-first 排序。
def test_scan_memory_files_returns_newest_first_excluding_entrypoint(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    # MEMORY.md 应被排除。
    (mem_dir / ENTRYPOINT_NAME).write_text("index", encoding="utf-8")
    # 两个记忆文件，b 比 a 新（mtime 更大）。
    a = mem_dir / "a.md"
    b = mem_dir / "b.md"
    a.write_text("---\nname: a\ntype: project\n---\n\na body\n", encoding="utf-8")
    b.write_text("---\nname: b\ntype: project\n---\n\nb body\n", encoding="utf-8")
    # 显式设置 mtime：b 更新。
    now = time.time()
    os.utime(a, (now - 100, now - 100))
    os.utime(b, (now, now))

    headers = scan_memory_files(mem_dir, "project")
    assert len(headers) == 2
    assert headers[0].filename == "b.md"
    assert headers[1].filename == "a.md"
    assert headers[0].scope == "project"


# 验证 scan_memory_files 不存在的目录返回空列表。
def test_scan_memory_files_returns_empty_on_missing_dir(tmp_path: Path) -> None:
    assert scan_memory_files(tmp_path / "nonexistent", "project") == []


# 验证 memory_age_days 把 mtime_ms 转成"距今 N 天"，非负。
def test_memory_age_days_returns_nonnegative_days() -> None:
    now_ms = int(time.time() * 1000)
    # 3 天前。
    old_ms = now_ms - 3 * 86_400_000
    assert memory_age_days(old_ms) == 3
    # 未来时间应返回 0（非负）。
    future_ms = now_ms + 100_000
    assert memory_age_days(future_ms) == 0


# 验证 memory_age 返回 today / yesterday / N days ago 三种文本。
def test_memory_age_text_variants() -> None:
    now_ms = int(time.time() * 1000)
    assert memory_age(now_ms) == "today"
    assert memory_age(now_ms - 86_400_000) == "yesterday"
    assert memory_age(now_ms - 5 * 86_400_000) == "5 days ago"


# 验证 memory_freshness_text：1 天内返回空串；超过 1 天返回过时警告。
def test_memory_freshness_text_empty_for_fresh_warning_for_old() -> None:
    now_ms = int(time.time() * 1000)
    assert memory_freshness_text(now_ms) == ""
    assert memory_freshness_text(now_ms - 86_400_000) == ""
    old_text = memory_freshness_text(now_ms - 3 * 86_400_000)
    assert "3 days old" in old_text
    assert "Verify against current code" in old_text


# 验证 format_memory_manifest 把 MemoryHeader 列表格式化为选择器输入文本。
# 每条含 scope 标签、type 标签、路径、时间戳与描述。
def test_format_memory_manifest_includes_scope_type_path_and_description() -> None:
    from seacode.memory.recall import MemoryHeader

    headers = [
        MemoryHeader(
            filename="topic.md",
            file_path="/abs/topic.md",
            scope="project",
            mtime_ms=int(time.time() * 1000),
            description="a topic",
            type="project",
        )
    ]
    manifest = format_memory_manifest(headers)
    assert "[project-scope]" in manifest
    assert "[project]" in manifest
    assert "/abs/topic.md" in manifest
    assert "a topic" in manifest


# 验证 find_relevant_memories 用选择器返回的文件名匹配候选列表。
# 选择器返回 JSON {"selected_memories": ["topic.md"]}，应只返回匹配的那条。
@pytest.mark.asyncio
async def test_find_relevant_memories_filters_by_selector_output(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "topic.md").write_text(
        "---\nname: topic\ntype: project\n---\n\nbody\n", encoding="utf-8"
    )
    (mem_dir / "other.md").write_text(
        "---\nname: other\ntype: project\n---\n\nother body\n", encoding="utf-8"
    )

    async def selector(system_prompt: str, user_message: str) -> str:
        return '{"selected_memories": ["topic.md"]}'

    result = await find_relevant_memories(
        query="some query",
        user_mem_dir=None,
        project_mem_dir=mem_dir,
        recent_tools=None,
        already_surfaced=None,
        selector=selector,
    )
    assert len(result) == 1
    assert result[0].path.endswith("topic.md")


# 验证 find_relevant_memories 在无候选记忆时返回空列表，不调用选择器。
@pytest.mark.asyncio
async def test_find_relevant_memories_returns_empty_when_no_candidates(
    tmp_path: Path,
) -> None:
    async def selector(system_prompt: str, user_message: str) -> str:
        raise AssertionError("selector should not be called when no candidates")

    result = await find_relevant_memories(
        query="some query",
        user_mem_dir=None,
        project_mem_dir=tmp_path / "nonexistent",
        recent_tools=None,
        already_surfaced=None,
        selector=selector,
    )
    assert result == []


# 验证 find_relevant_memories 在选择器抛异常时静默返回空列表。
@pytest.mark.asyncio
async def test_find_relevant_memories_silently_handles_selector_failure(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "topic.md").write_text(
        "---\nname: topic\ntype: project\n---\n\nbody\n", encoding="utf-8"
    )

    async def selector(system_prompt: str, user_message: str) -> str:
        raise RuntimeError("LLM unavailable")

    result = await find_relevant_memories(
        query="some query",
        user_mem_dir=None,
        project_mem_dir=mem_dir,
        recent_tools=None,
        already_surfaced=None,
        selector=selector,
    )
    assert result == []


# 验证 render_reminder 读取记忆文件全文并附加新鲜度警告。
# 1 天内的记忆不附警告；超过 1 天的附 "days old" 提醒。
def test_render_reminder_includes_content_and_freshness_warning(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh.md"
    fresh.write_text("---\nname: fresh\n---\n\nfresh body\n", encoding="utf-8")
    old = tmp_path / "old.md"
    old.write_text("---\nname: old\n---\n\nold body\n", encoding="utf-8")
    # 显式设置 mtime：old 为 5 天前，fresh 为现在。
    now = time.time()
    os.utime(old, (now - 5 * 86_400, now - 5 * 86_400))
    os.utime(fresh, (now, now))

    memories = [
        RelevantMemory(path=str(fresh), mtime_ms=int(now * 1000)),
        RelevantMemory(
            path=str(old),
            mtime_ms=int((now - 5 * 86_400) * 1000),
        ),
    ]
    reminder = render_reminder(memories)
    assert "fresh body" in reminder
    assert "old body" in reminder
    # old 记忆应附新鲜度警告。
    assert "5 days old" in reminder
    # fresh 记忆不应附警告。
    fresh_section = reminder.split("## Memory: fresh.md")[1].split("## Memory: old.md")[0]
    assert "days old" not in fresh_section


# 验证 render_reminder 空列表返回空字符串。
def test_render_reminder_returns_empty_on_empty_list() -> None:
    assert render_reminder([]) == ""


# =========================================================================
# J. Consolidation 门控与锁
# =========================================================================


# 验证 maybe_run 在记忆目录不存在时直接返回，不触发后续门控。
@pytest.mark.asyncio
async def test_consolidator_maybe_run_skips_when_mem_dir_missing(
    tmp_path: Path,
) -> None:
    consolidator = MemoryConsolidator(str(tmp_path))
    # 不创建 .seacode/memory 目录。
    # 不应抛异常，也不应触发后台整理。
    await consolidator.maybe_run(
        client=None, conversation=None, protocol="anthropic"
    )


# 验证 maybe_run 在距上次整理不足 min_hours 时返回。
@pytest.mark.asyncio
async def test_consolidator_maybe_run_skips_when_within_min_hours(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / ".seacode" / "memory"
    mem_dir.mkdir(parents=True)
    # 写入锁文件，mtime 设为 1 小时前（远小于默认 24h）。
    lock_path = mem_dir / LOCK_FILE
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    recent = time.time() - 3600
    os.utime(lock_path, (recent, recent))

    consolidator = MemoryConsolidator(str(tmp_path))
    await consolidator.maybe_run(
        client=None, conversation=None, protocol="anthropic"
    )
    # 没有抛异常即视为门控正确拦截。


# 验证 _try_acquire_lock 在无锁文件时创建并返回 0（表示整理前无锁）。
def test_try_acquire_lock_creates_new_lock_returning_zero(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    result = _try_acquire_lock(str(mem_dir))
    assert result == 0
    lock_path = mem_dir / LOCK_FILE
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(os.getpid())


# 验证 _try_acquire_lock 在锁被活跃进程持有时返回 None。
def test_try_acquire_lock_returns_none_when_held_by_running_process(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    lock_path = mem_dir / LOCK_FILE
    # 当前进程一定存活，写入自己的 PID。
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    now = time.time()
    os.utime(lock_path, (now, now))
    # 锁被活跃进程持有，应返回 None。
    assert _try_acquire_lock(str(mem_dir)) is None


# 验证 _rollback_lock 在 prior_mtime == 0 时删除锁文件。
def test_rollback_lock_deletes_file_when_prior_mtime_zero(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    lock_path = mem_dir / LOCK_FILE
    lock_path.write_text("123", encoding="utf-8")
    _rollback_lock(str(mem_dir), prior_mtime=0)
    assert not lock_path.exists()


# 验证 _rollback_lock 在 prior_mtime > 0 时清空内容并恢复 mtime。
def test_rollback_lock_restores_mtime_when_prior_positive(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    lock_path = mem_dir / LOCK_FILE
    lock_path.write_text("123", encoding="utf-8")
    prior = int(time.time() * 1000) - 50_000
    _rollback_lock(str(mem_dir), prior_mtime=prior)
    assert lock_path.exists()
    assert lock_path.read_text().strip() == ""
    # mtime 应恢复到 prior_mtime 对应的秒级时间。
    restored = int(os.stat(lock_path).st_mtime * 1000)
    assert abs(restored - prior) < 1000  # 允许 1 秒内的精度误差


# 验证 _read_last_consolidated_at 在锁文件不存在时返回 0。
def test_read_last_consolidated_at_returns_zero_when_no_lock(
    tmp_path: Path,
) -> None:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    assert _read_last_consolidated_at(str(mem_dir)) == 0


# 验证 _is_process_running 对当前进程返回 True。
def test_is_process_running_returns_true_for_current_process() -> None:
    assert _is_process_running(os.getpid()) is True


# 验证 _is_process_running 对不存在的 PID 返回 False。
def test_is_process_running_returns_false_for_dead_pid() -> None:
    # 用一个几乎不可能存在的 PID（2^31-1）。
    assert _is_process_running(2_147_483_647) is False


# 验证 _list_sessions_since 返回 last_active 在 since_ms 之后的会话 ID。
def test_list_sessions_since_filters_by_last_active(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    old_session = manager.create()
    old_session.meta.last_active = datetime.now(UTC) - timedelta(days=10)
    old_session.meta.save(
        tmp_path / ".seacode" / "sessions" / f"{old_session.session_id}.meta"
    )
    old_session.close()

    new_session = manager.create()
    new_session.close()

    # since_ms 设为 5 天前，应只返回 new_session。
    cutoff_ms = int((time.time() - 5 * 86_400) * 1000)
    result = _list_sessions_since(str(tmp_path), cutoff_ms)
    assert new_session.session_id in result
    assert old_session.session_id not in result


# =========================================================================
# K. 集成：SessionManager + MemoryManager 协作
# =========================================================================


# 验证 SessionManager 在工作目录下创建 .seacode/sessions 子目录。
# 多次创建同一路径不抛异常（幂等 mkdir）。
def test_session_manager_creates_sessions_subdir_idempotently(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    SessionManager(str(work_dir))
    assert (work_dir / ".seacode" / "sessions").is_dir()
    # 第二次创建不应抛异常。
    SessionManager(str(work_dir))
    assert (work_dir / ".seacode" / "sessions").is_dir()


# 验证 MemoryManager 的用户级与项目级目录路径符合既定约定。
def test_memory_manager_directory_paths_match_convention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("SEA_REMOTE_MEMORY_DIR", "")

    mgr = MemoryManager(str(tmp_path))
    assert mgr.user_mem_dir == fake_home / ".seacode" / "memory"
    assert mgr.project_mem_dir == tmp_path / ".seacode" / "memory"
