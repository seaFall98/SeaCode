from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from seacode.client import LLMClient, StreamComplete, StreamEvent, TextDelta
from seacode.context.manager import (
    AUTO_COMPACT_SAFETY_MARGIN,
    MANUAL_COMPACT_SAFETY_MARGIN,
    PERSISTED_TAG,
    PREVIEW_CHARS,
    SINGLE_RESULT_CHAR_LIMIT,
    SUMMARY_OUTPUT_RESERVE,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementRecord,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    build_compact_messages,
    build_recovery_attachment,
    cleanup_tool_results,
    clone_replacement_state,
    compute_compact_threshold,
    create_replacement_state,
    ensure_session_dir,
    extract_summary,
    load_replacement_records,
    make_persisted_preview,
    persist_tool_result,
    reconstruct_replacement_state,
)
from seacode.conversation import (
    ConversationManager,
    Message,
    ToolResultBlock,
    ToolUseBlock,
    estimate_tokens,
)

# ---------------------------------------------------------------------------
# 工具函数：构造含工具结果的对话历史
# ---------------------------------------------------------------------------


# 构造一条 assistant tool_use + 一条 user tool_results 的回合。
def _make_tool_round(
    tool_use_id: str,
    tool_name: str,
    result_content: str,
    *,
    arguments: dict[str, Any] | None = None,
    is_error: bool = False,
) -> list[Message]:
    return [
        Message(
            role="assistant",
            content="",
            tool_uses=[
                ToolUseBlock(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    arguments=arguments or {},
                )
            ],
        ),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=result_content,
                    is_error=is_error,
                )
            ],
        ),
    ]


# ===========================================================================
# Layer 1：persist_tool_result
# ===========================================================================


# 验证 persist_tool_result 用 O_EXCL 独占创建文件，内容完整写入。
# 调用后断言文件存在、内容与输入一致。
def test_persist_tool_result_creates_file_with_content(tmp_path: Path) -> None:
    content = "line1\nline2\n"
    fp = persist_tool_result("tu-1", content, tmp_path)

    assert fp.exists()
    assert fp.read_text(encoding="utf-8") == content


# 验证 persist_tool_result 对同一 tool_use_id 幂等：文件已存在时不覆盖。
# 第一次写入后修改文件内容，第二次调用后内容保持第一次的版本。
def test_persist_tool_result_is_idempotent(tmp_path: Path) -> None:
    fp = persist_tool_result("tu-1", "first", tmp_path)
    # 手动改写文件以验证第二次调用不覆盖。
    fp.write_text("modified", encoding="utf-8")

    persist_tool_result("tu-1", "second", tmp_path)

    assert fp.read_text(encoding="utf-8") == "modified"


# ===========================================================================
# Layer 1：make_persisted_preview
# ===========================================================================


# 验证 make_persisted_preview 生成含大小、路径与前 2KB 预览的标签包。
# 构造超长内容，断言输出含 PERSISTED_TAG、路径、截断预览。
def test_make_persisted_preview_contains_size_path_and_preview(
    tmp_path: Path,
) -> None:
    content = "x" * (PREVIEW_CHARS + 500)
    fp = tmp_path / "tu-1.txt"

    preview = make_persisted_preview(content, fp)

    assert preview.startswith(PERSISTED_TAG)
    assert str(fp) in preview
    assert preview.endswith("</persisted-output>")
    # 预览段只包含前 PREVIEW_CHARS 个字符，不含超出的部分。
    assert "x" * PREVIEW_CHARS in preview
    assert "x" * (PREVIEW_CHARS + 1) not in preview


# ===========================================================================
# Layer 1：_is_spill_readback
# ===========================================================================


# 验证 _is_spill_readback 对 ReadFile 读取落盘目录下文件返回 True。
# 构造 ReadFile 工具调用且 file_path 指向 session_dir 下的文件。
def test_is_spill_readback_true_for_readfile_in_spill_dir(tmp_path: Path) -> None:
    from seacode.context.manager import _is_spill_readback

    spill_dir = os.path.abspath(str(tmp_path))
    tool_use_index = {
        "tu-1": ToolUseBlock(
            tool_use_id="tu-1",
            tool_name="ReadFile",
            arguments={"file_path": str(tmp_path / "tu-1.txt")},
        )
    }

    assert _is_spill_readback("tu-1", tool_use_index, spill_dir) is True


# 验证 _is_spill_readback 对非 ReadFile 工具返回 False。
# 构造 Bash 工具调用，即便路径在落盘目录下也不豁免。
def test_is_spill_readback_false_for_non_readfile_tool(tmp_path: Path) -> None:
    from seacode.context.manager import _is_spill_readback

    spill_dir = os.path.abspath(str(tmp_path))
    tool_use_index = {
        "tu-1": ToolUseBlock(
            tool_use_id="tu-1",
            tool_name="Bash",
            arguments={"command": "cat file"},
        )
    }

    assert _is_spill_readback("tu-1", tool_use_index, spill_dir) is False


# 验证 _is_spill_readback 对 ReadFile 读取落盘目录外文件返回 False。
# 构造 ReadFile 但 file_path 指向其它目录。
def test_is_spill_readback_false_for_readfile_outside_spill_dir(
    tmp_path: Path,
) -> None:
    from seacode.context.manager import _is_spill_readback

    spill_dir = os.path.abspath(str(tmp_path / "spill"))
    os.makedirs(spill_dir, exist_ok=True)
    other_path = tmp_path / "other.txt"
    tool_use_index = {
        "tu-1": ToolUseBlock(
            tool_use_id="tu-1",
            tool_name="ReadFile",
            arguments={"file_path": str(other_path)},
        )
    }

    assert _is_spill_readback("tu-1", tool_use_index, spill_dir) is False


# ===========================================================================
# Layer 1：apply_tool_result_budget
# ===========================================================================


# 验证单条工具结果超 SINGLE_RESULT_CHAR_LIMIT 时被落盘替换为预览。
# 构造一条超大 tool_result，调用后断言 content 被替换为预览、记录产生。
def test_apply_budget_replaces_single_oversized_result(tmp_path: Path) -> None:
    conv = ConversationManager()
    big_content = "x" * (SINGLE_RESULT_CHAR_LIMIT + 100)
    conv.history.extend(_make_tool_round("tu-1", "Bash", big_content))
    state = create_replacement_state()

    records = apply_tool_result_budget(conv, tmp_path, state)

    assert len(records) == 1
    assert records[0].tool_use_id == "tu-1"
    tr = conv.history[1].tool_results[0]
    assert tr.content.startswith(PERSISTED_TAG)
    # 落盘文件存在且含原始内容。
    assert (tmp_path / "tu-1.txt").exists()
    assert (tmp_path / "tu-1.txt").read_text(encoding="utf-8") == big_content


# 验证单条未超限的工具结果保持原样不替换。
# 构造一条小 tool_result，调用后断言 content 不变、无记录产生。
def test_apply_budget_keeps_small_result(tmp_path: Path) -> None:
    conv = ConversationManager()
    small_content = "small output"
    conv.history.extend(_make_tool_round("tu-1", "Bash", small_content))
    state = create_replacement_state()

    records = apply_tool_result_budget(conv, tmp_path, state)

    assert records == []
    assert conv.history[1].tool_results[0].content == small_content


# 验证聚合超 AGGREGATE_CHAR_LIMIT 时按内容长度降序替换非豁免项。
# 构造多条中等大小的 tool_result 使总和超限，断言最大的先被替换。
def test_apply_budget_replaces_largest_when_aggregate_exceeds(
    tmp_path: Path,
) -> None:
    conv = ConversationManager()
    # 构造 3 条 tool_result，每条 < SINGLE_RESULT_CHAR_LIMIT 但总和 > AGGREGATE_CHAR_LIMIT。
    # 单条需要 < SINGLE_RESULT_CHAR_LIMIT 避免走 Pass 1；总和需要 > AGGREGATE_CHAR_LIMIT。
    # 由于 AGGREGATE_CHAR_LIMIT=200000，需要构造足够大的内容。
    # 使用 3 条 70000 字符的 content，总 210000 > 200000，每条 < 50000 不对——70000 > 50000。
    # 调整：让每条 > SINGLE 但 Pass 1 已落盘替换，替换后预览约 2KB+标签，
    # 总和不再超限。因此此测试需要每条 < SINGLE 才能进入 Pass 2。
    # 重新构造：5 条 45000 字符，总 225000 > 200000，每条 < 50000。
    per_size = 45_000
    count = 5
    for i in range(count):
        conv.history.extend(
            _make_tool_round(f"tu-{i}", "Bash", "y" * per_size)
        )
    state = create_replacement_state()

    records = apply_tool_result_budget(conv, tmp_path, state)

    # 至少替换了最大的若干条直到总和不超限。
    assert len(records) >= 1
    # 被替换的 content 都应该是预览格式。
    for r in records:
        tr = next(
            msg.tool_results[0]
            for msg in conv.history
            if msg.tool_results and msg.tool_results[0].tool_use_id == r.tool_use_id
        )
        assert tr.content.startswith(PERSISTED_TAG)


# 验证 ReadFile 读取落盘目录下文件时不再次落盘（豁免）。
# 构造 ReadFile 指向 session_dir 下文件且内容超限，断言不被替换。
def test_apply_budget_exempts_readfile_of_spill_dir(tmp_path: Path) -> None:
    conv = ConversationManager()
    # 先在 session_dir 下创建一个文件。
    spill_file = tmp_path / "tu-spilled.txt"
    spill_file.write_text("x" * (SINGLE_RESULT_CHAR_LIMIT + 100), encoding="utf-8")
    # 构造 ReadFile 读取该文件。
    conv.history.extend(
        _make_tool_round(
            "tu-read",
            "ReadFile",
            "x" * (SINGLE_RESULT_CHAR_LIMIT + 100),
            arguments={"file_path": str(spill_file)},
        )
    )
    state = create_replacement_state()

    records = apply_tool_result_budget(conv, tmp_path, state)

    assert records == []
    # content 保持原样。
    assert conv.history[1].tool_results[0].content.startswith("x")


# 验证 ContentReplacementState 已替换的 id 在后续 apply 中直接应用替换文本。
# 第一次替换后，第二次调用应直接应用已有替换、不产生新记录。
def test_apply_budget_applies_existing_replacement_on_subsequent_calls(
    tmp_path: Path,
) -> None:
    conv = ConversationManager()
    big_content = "x" * (SINGLE_RESULT_CHAR_LIMIT + 100)
    conv.history.extend(_make_tool_round("tu-1", "Bash", big_content))
    state = create_replacement_state()

    first_records = apply_tool_result_budget(conv, tmp_path, state)
    assert len(first_records) == 1
    replaced_content = conv.history[1].tool_results[0].content

    # 第二次调用：已替换的 id 应直接应用，不产生新记录。
    second_records = apply_tool_result_budget(conv, tmp_path, state)
    assert second_records == []
    assert conv.history[1].tool_results[0].content == replaced_content


# ===========================================================================
# Layer 1：替换记录持久化与重建
# ===========================================================================


# 验证 append_replacement_records 追加到 jsonl 文件。
# 调用后断言文件存在、每行是合法 JSON。
def test_append_replacement_records_writes_jsonl(tmp_path: Path) -> None:
    import json

    records = [
        ContentReplacementRecord(tool_use_id="tu-1", replacement="preview-1"),
        ContentReplacementRecord(tool_use_id="tu-2", replacement="preview-2"),
    ]

    append_replacement_records(tmp_path, records)

    jsonl = tmp_path / "replacement_records.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    obj1 = json.loads(lines[0])
    assert obj1["tool_use_id"] == "tu-1"
    assert obj1["replacement"] == "preview-1"
    assert obj1["kind"] == "tool-result"


# 验证 append_replacement_records 空列表不写文件。
def test_append_replacement_records_skips_empty(tmp_path: Path) -> None:
    append_replacement_records(tmp_path, [])
    assert not (tmp_path / "replacement_records.jsonl").exists()


# 验证 load_replacement_records 读取 jsonl 并返回记录列表。
# 先 append 再 load，断言内容一致。
def test_load_replacement_records_round_trip(tmp_path: Path) -> None:
    records = [
        ContentReplacementRecord(tool_use_id="tu-1", replacement="preview-1"),
    ]
    append_replacement_records(tmp_path, records)

    loaded = load_replacement_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].tool_use_id == "tu-1"
    assert loaded[0].replacement == "preview-1"


# 验证 load_replacement_records 文件不存在时返回空列表。
def test_load_replacement_records_returns_empty_when_missing(
    tmp_path: Path,
) -> None:
    loaded = load_replacement_records(tmp_path)
    assert loaded == []


# 验证 reconstruct_replacement_state 从记录重建状态。
# 构造含 tool_result 的消息与替换记录，断言重建后 replacements 含对应 id。
def test_reconstruct_replacement_state_builds_from_records() -> None:
    messages = _make_tool_round("tu-1", "Bash", "big")
    records = [
        ContentReplacementRecord(tool_use_id="tu-1", replacement="preview-1"),
        # 不在 messages 中的 id 应被忽略。
        ContentReplacementRecord(tool_use_id="tu-2", replacement="preview-2"),
    ]

    state = reconstruct_replacement_state(messages, records)

    assert "tu-1" in state.replacements
    assert state.replacements["tu-1"] == "preview-1"
    assert "tu-2" not in state.replacements
    assert "tu-1" in state.seen_ids


# ===========================================================================
# Layer 1：clone_replacement_state
# ===========================================================================


# 验证 clone_replacement_state 深拷贝状态，修改副本不影响原状态。
def test_clone_replacement_state_is_deep_copy() -> None:
    src = create_replacement_state()
    src.seen_ids.add("tu-1")
    src.replacements["tu-1"] = "preview"

    cloned = clone_replacement_state(src)
    cloned.seen_ids.add("tu-2")
    cloned.replacements["tu-2"] = "preview-2"

    assert "tu-2" not in src.seen_ids
    assert "tu-2" not in src.replacements


# ===========================================================================
# Layer 2：compute_compact_threshold
# ===========================================================================


# 验证自动模式 margin 为 AUTO_COMPACT_SAFETY_MARGIN。
# 断言阈值 = context_window - SUMMARY_OUTPUT_RESERVE - AUTO_COMPACT_SAFETY_MARGIN。
def test_compute_threshold_auto_uses_auto_margin() -> None:
    cw = 200_000
    expected = cw - SUMMARY_OUTPUT_RESERVE - AUTO_COMPACT_SAFETY_MARGIN
    assert compute_compact_threshold(cw, manual=False) == expected


# 验证手动模式 margin 为 MANUAL_COMPACT_SAFETY_MARGIN。
# 断言阈值 = context_window - SUMMARY_OUTPUT_RESERVE - MANUAL_COMPACT_SAFETY_MARGIN。
def test_compute_threshold_manual_uses_manual_margin() -> None:
    cw = 200_000
    expected = cw - SUMMARY_OUTPUT_RESERVE - MANUAL_COMPACT_SAFETY_MARGIN
    assert compute_compact_threshold(cw, manual=True) == expected


# ===========================================================================
# Layer 2：CompactCircuitBreaker
# ===========================================================================


# 验证熔断器在连续 3 次失败后打开。
# 前 2 次 is_open 为 False，第 3 次失败后为 True。
def test_circuit_breaker_opens_after_three_failures() -> None:
    breaker = CompactCircuitBreaker(max_failures=3)
    assert breaker.is_open() is False

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False

    breaker.record_failure()
    assert breaker.is_open() is True


# 验证熔断器在成功后重置。
# 失败 2 次后成功，断言 consecutive_failures 归零、is_open 为 False。
def test_circuit_breaker_resets_on_success() -> None:
    breaker = CompactCircuitBreaker(max_failures=3)
    breaker.record_failure()
    breaker.record_failure()

    breaker.record_success()

    assert breaker.consecutive_failures == 0
    assert breaker.is_open() is False


# ===========================================================================
# Layer 2：extract_summary
# ===========================================================================


# 验证 extract_summary 从 <summary> 标签提取正文。
# 构造含 <analysis> 与 <summary> 的 LLM 输出，断言只返回 summary 内容。
def test_extract_summary_extracts_tag_content() -> None:
    llm_output = (
        "<analysis>thinking process</analysis>\n"
        "<summary>\n1. Primary: do thing\n2. Concepts: a, b\n</summary>"
    )

    summary = extract_summary(llm_output)

    assert "Primary: do thing" in summary
    assert "Concepts: a, b" in summary
    assert "thinking process" not in summary


# 验证 extract_summary 找不到 <summary> 标签时退化为整段原文。
def test_extract_summary_falls_back_to_full_text_when_tag_missing() -> None:
    llm_output = "just plain text without tags"

    summary = extract_summary(llm_output)

    assert summary == llm_output


# ===========================================================================
# Layer 2：_compute_keep_start_index / _align_keep_start_to_tool_pair
# ===========================================================================


# 验证 _compute_keep_start_index 满足 MIN_KEEP_MESSAGES 时停止回溯。
# 构造 3 条小消息，断言 keep_start 回溯到第 0 条（消息数下限未达 5 时全保留）。
def test_keep_start_index_keeps_min_messages() -> None:
    from seacode.context.manager import _compute_keep_start_index

    messages = [
        Message(role="user", content="m1"),
        Message(role="assistant", content="m2"),
        Message(role="user", content="m3"),
    ]

    keep_start = _compute_keep_start_index(messages)

    # 消息数 < MIN_KEEP_MESSAGES，全部保留。
    assert keep_start == 0


# 验证 _compute_keep_start_index 满足 KEEP_RECENT_TOKENS 时停止回溯。
# 构造足够多消息使 token 累计达 KEEP_RECENT_TOKENS，断言只保留尾部若干条。
def test_keep_start_index_stops_at_token_budget() -> None:
    from seacode.context.manager import _compute_keep_start_index

    # 构造 10 条大消息，每条约 5000 token（17500 字符）。
    # KEEP_RECENT_TOKENS=10000，约 2 条即达上限。
    big_content = "x" * 17_500
    messages = [
        Message(role="user", content=big_content),
        Message(role="assistant", content=big_content),
        Message(role="user", content=big_content),
        Message(role="assistant", content=big_content),
        Message(role="user", content=big_content),
        Message(role="assistant", content=big_content),
        Message(role="user", content=big_content),
        Message(role="assistant", content=big_content),
        Message(role="user", content=big_content),
        Message(role="assistant", content=big_content),
    ]

    keep_start = _compute_keep_start_index(messages)

    # 应保留尾部约 2 条（满足 token 下限）。
    assert keep_start >= 8


# 验证 _align_keep_start_to_tool_pair 把切割点回退到配对的 assistant tool_use。
# 构造 assistant(tool_use) + user(tool_result)，keep_start 落在 user 上时回退。
def test_align_keep_start_to_tool_pair() -> None:
    from seacode.context.manager import _align_keep_start_to_tool_pair

    messages = [
        Message(role="user", content="early"),
        Message(
            role="assistant",
            content="",
            tool_uses=[
                ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={})
            ],
        ),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="tu-1", content="result")
            ],
        ),
        Message(role="assistant", content="final"),
    ]

    # keep_start 落在 user(tool_results) 上（下标 2），应回退到 assistant tool_use（下标 1）。
    aligned = _align_keep_start_to_tool_pair(messages, 2)
    assert aligned == 1


# ===========================================================================
# Layer 2：_prefix_too_small_to_compact
# ===========================================================================


# 验证 _prefix_too_small_to_compact 对空前缀返回 True。
def test_prefix_too_small_empty_returns_true() -> None:
    from seacode.context.manager import _prefix_too_small_to_compact

    assert _prefix_too_small_to_compact([]) is True


# 验证 _prefix_too_small_to_compact 对小于 MIN_SUMMARIZE_PREFIX_TOKENS 的前缀返回 True。
def test_prefix_too_small_short_prefix_returns_true() -> None:
    from seacode.context.manager import _prefix_too_small_to_compact

    # MIN_SUMMARIZE_PREFIX_TOKENS=2000，约 7000 字符。
    small_prefix = [Message(role="user", content="x" * 100)]
    assert _prefix_too_small_to_compact(small_prefix) is True


# 验证 _prefix_too_small_to_compact 对大于阈值的前缀返回 False。
def test_prefix_too_small_large_prefix_returns_false() -> None:
    from seacode.context.manager import _prefix_too_small_to_compact

    # 构造 > 2000 token 的前缀（约 7000 字符）。
    big_prefix = [Message(role="user", content="x" * 8000)]
    assert _prefix_too_small_to_compact(big_prefix) is False


# ===========================================================================
# Layer 2：_group_messages_by_turn
# ===========================================================================


# 验证 _group_messages_by_turn 按 assistant 无 tool_uses 分组。
# 构造两轮对话，断言分成两组。
def test_group_messages_by_turn_splits_at_assistant_without_tool_uses() -> None:
    from seacode.context.manager import _group_messages_by_turn

    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
        Message(role="assistant", content="a2"),
    ]

    groups = _group_messages_by_turn(messages)

    assert len(groups) == 2
    assert [m.content for m in groups[0]] == ["q1", "a1"]
    assert [m.content for m in groups[1]] == ["q2", "a2"]


# 验证 _group_messages_by_turn 不在 assistant 含 tool_uses 时分组。
# 构造 assistant(tool_use) + user(tool_result) + assistant(text)，断言成一组的延续。
def test_group_messages_by_turn_keeps_tool_round_together() -> None:
    from seacode.context.manager import _group_messages_by_turn

    messages = [
        Message(role="user", content="q1"),
        Message(
            role="assistant",
            tool_uses=[
                ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={})
            ],
        ),
        Message(
            role="user",
            tool_results=[
                ToolResultBlock(tool_use_id="tu-1", content="r")
            ],
        ),
        Message(role="assistant", content="final answer"),
    ]

    groups = _group_messages_by_turn(messages)

    assert len(groups) == 1


# ===========================================================================
# Layer 2：build_compact_messages
# ===========================================================================


# 验证 build_compact_messages 生成含摘要与恢复附件的 user 消息。
# 调用后断言首条消息 role=user 且含摘要与附件文本。
def test_build_compact_messages_contains_summary_and_attachment() -> None:
    summary = "Task: do thing"
    attachment = "## 最近读过的文件\nfile.py"

    messages = build_compact_messages(
        summary, attachment=attachment, has_keep_tail=True, transcript_path="/tmp/t.jsonl"
    )

    assert len(messages) == 1
    assert messages[0].role == "user"
    assert summary in messages[0].content
    assert attachment in messages[0].content
    assert "/tmp/t.jsonl" in messages[0].content


# 验证 build_compact_messages 无附件时不含分隔线。
def test_build_compact_messages_without_attachment() -> None:
    messages = build_compact_messages("summary", attachment="")

    assert "---" not in messages[0].content
    assert "summary" in messages[0].content


# ===========================================================================
# 恢复附件：RecoveryState
# ===========================================================================


# 验证 record_file_read 按 path 去重保留最新。
# 同一 path 调用两次，断言只保留最新快照。
def test_record_file_read_deduplicates_by_path() -> None:
    state = RecoveryState()
    state.record_file_read("/a.py", "old content")
    state.record_file_read("/a.py", "new content")

    files = state.snapshot_files(10)
    assert len(files) == 1
    assert files[0].content == "new content"


# 验证 record_file_read 空 path 时忽略。
def test_record_file_read_ignores_empty_path() -> None:
    state = RecoveryState()
    state.record_file_read("", "content")

    assert state.snapshot_files(10) == []


# 验证 record_skill_invocation 按 name 去重保留最新。
def test_record_skill_invocation_deduplicates_by_name() -> None:
    state = RecoveryState()
    state.record_skill_invocation("skill-a", "old body")
    state.record_skill_invocation("skill-a", "new body")

    skills = state.snapshot_skills()
    assert len(skills) == 1
    assert skills[0].body == "new body"


# 验证 snapshot_files 按 limit 截断。
# 记录 7 个文件，limit=5，断言只返回 5 个。
def test_snapshot_files_respects_limit() -> None:
    state = RecoveryState()
    for i in range(7):
        state.record_file_read(f"/f{i}.py", f"content-{i}")

    files = state.snapshot_files(5)
    assert len(files) == 5


# ===========================================================================
# 恢复附件：build_recovery_attachment
# ===========================================================================


# 验证 build_recovery_attachment 渲染文件、工具与提示段。
# 构造含文件快照与工具 schema 的输入，断言输出含三段标题。
def test_build_recovery_attachment_renders_all_sections() -> None:
    state = RecoveryState()
    state.record_file_read("/a.py", "print('hello')")
    tool_schemas = [
        {"name": "Bash", "description": "Execute shell command"},
        {"name": "ReadFile", "description": "Read a file"},
    ]

    attachment = build_recovery_attachment(state, tool_schemas)

    assert "## 最近读过的文件" in attachment
    assert "/a.py" in attachment
    assert "print('hello')" in attachment
    assert "## 可用工具" in attachment
    assert "Bash" in attachment
    assert "ReadFile" in attachment
    assert "## 提示" in attachment


# 验证 build_recovery_attachment 无内容时返回空串。
def test_build_recovery_attachment_returns_empty_when_nothing_to_attach() -> None:
    state = RecoveryState()

    attachment = build_recovery_attachment(state, None)

    assert attachment == ""


# 验证 build_recovery_attachment 文件数超过 5 时只渲染前 5 个。
# 显式构造 FileReadRecord 并赋予单调递增的 timestamp，避免依赖 time.time()
# 在同一时钟滴答内的精度差异——Windows 上 7 次连续调用常返回相同值使排序
# 退化为插入顺序，而 CI（Linux）精度更高每次都不同，导致断言跨环境不一致。
def test_build_recovery_attachment_truncates_files_at_limit() -> None:
    from seacode.context.manager import FileReadRecord

    state = RecoveryState()
    # f0 最早（timestamp 最小）、f6 最近（timestamp 最大）。
    # snapshot_files 按时间倒序取最近 5 个，应保留 [f6, f5, f4, f3, f2]、丢弃 [f0, f1]。
    base_ts = 1_700_000_000.0
    for i in range(7):
        state._files[f"/f{i}.py"] = FileReadRecord(
            path=f"/f{i}.py", content=f"content-{i}", timestamp=base_ts + i
        )

    attachment = build_recovery_attachment(state, None)

    # 最早的 f0、f1 不应被渲染（snapshot_files 按时间倒序截断到 5 个）。
    for i in range(0, 2):
        assert f"/f{i}.py" not in attachment
    # 最近 5 个都被渲染为文件标题。
    assert attachment.count("### /f") == 5


# ===========================================================================
# 用量锚点：ConversationManager
# ===========================================================================


# 验证 record_usage_anchor 计算 baseline = input + cache_read + cache_creation + output。
# 调用后断言 baseline_tokens 与 anchor_count 对齐。
def test_record_usage_anchor_calculates_baseline() -> None:
    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_assistant_message("hi")

    conv.record_usage_anchor(
        input_tokens=100, output_tokens=50, cache_read=30, cache_creation=20
    )

    assert conv.baseline_tokens == 200  # 100 + 50 + 30 + 20
    assert conv.anchor_count == 2
    assert conv.last_input_tokens == 200


# 验证 current_tokens 有锚点时返回 baseline + 尾部估算。
# 锚定后追加一条消息，断言 current_tokens > baseline。
def test_current_tokens_with_anchor_returns_baseline_plus_tail() -> None:
    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_assistant_message("hi")
    conv.record_usage_anchor(input_tokens=1000, output_tokens=100)

    baseline = conv.baseline_tokens
    conv.add_user_message("a new follow-up message")

    current = conv.current_tokens()
    assert current > baseline


# 验证 current_tokens 无锚点时退化为全量字符估算。
def test_current_tokens_without_anchor_returns_full_estimate() -> None:
    conv = ConversationManager()
    conv.add_user_message("hello world")

    current = conv.current_tokens()
    expected = estimate_tokens(list(conv.messages))
    assert current == expected


# 验证 replace_history 清零用量锚点。
# 锚定后 replace_history，断言三个字段归零。
def test_replace_history_resets_anchor() -> None:
    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.record_usage_anchor(input_tokens=100, output_tokens=50)
    assert conv.baseline_tokens > 0

    conv.replace_history([Message(role="user", content="new")])

    assert conv.baseline_tokens == 0
    assert conv.anchor_count == 0
    assert conv.last_input_tokens == 0
    assert conv.env_injected is False


# 验证 estimate_tokens 使用 3.5 字符/token 启发式。
# 构造 3500 字符的消息，断言估算约 1000 token。
def test_estimate_tokens_uses_chars_per_token_heuristic() -> None:
    messages = [Message(role="user", content="x" * 3500)]
    assert estimate_tokens(messages) == 1000


# ===========================================================================
# auto_compact 集成测试（使用 fake client）
# ===========================================================================


# 假客户端：返回预设文本流，记录传入的 messages。
class _FakeSummaryClient(LLMClient):
    def __init__(self, summary_text: str) -> None:
        self._summary_text = summary_text
        self.requests: list[list[Message]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        yield TextDelta(self._summary_text)
        yield StreamComplete(input_tokens=10, output_tokens=10)


# 验证 auto_compact 在未达软阈值时返回 None 不压缩。
# 构造小对话与较大 context_window，断言返回 None。
@pytest.mark.asyncio
async def test_auto_compact_returns_none_when_below_threshold(
    tmp_path: Path,
) -> None:
    conv = ConversationManager()
    conv.add_user_message("hi")
    conv.add_assistant_message("hello")
    client = _FakeSummaryClient("summary text")

    # context_window=200000，current_tokens 远低于软阈值。
    result = await auto_compact(
        conv, client, context_window=200_000, session_dir=tmp_path
    )

    assert result is None
    assert client.requests == []


# 验证 auto_compact 手动模式跳过阈值检查直接压缩。
# 构造小对话但 manual=True，断言触发压缩。
@pytest.mark.asyncio
async def test_auto_compact_manual_bypasses_threshold(tmp_path: Path) -> None:
    conv = ConversationManager()
    # 构造足够大的前缀使 _prefix_too_small_to_compact 返回 False。
    big_content = "x" * 10_000
    conv.add_user_message(big_content)
    conv.add_assistant_message(big_content)
    # 需要超过 MIN_KEEP_MESSAGES(5) 条消息让 keep_start > 0，否则全部消息落入
    # 保留窗口、无前缀可摘要，manual 模式也会返回 None。
    conv.add_user_message("recent1")
    conv.add_assistant_message("recent2")
    conv.add_user_message("recent3")
    conv.add_assistant_message("recent4")
    client = _FakeSummaryClient("<summary>manual compact</summary>")

    result = await auto_compact(
        conv, client, context_window=200_000, session_dir=tmp_path, manual=True
    )

    assert isinstance(result, CompactEvent)
    assert result.boundary is not None
    assert "manual compact" in result.boundary.summary
    # 压缩后 history 被重建，首条消息含摘要文本。
    assert "manual compact" in conv.history[0].content


# 验证 auto_compact 成功后清零用量锚点并清理 tool-results 目录。
@pytest.mark.asyncio
async def test_auto_compact_clears_anchor_and_cleans_tool_results(
    tmp_path: Path,
) -> None:
    conv = ConversationManager()
    big_content = "x" * 10_000
    conv.add_user_message(big_content)
    conv.add_assistant_message(big_content)
    # 需要超过 MIN_KEEP_MESSAGES(5) 条消息让 keep_start > 0，否则无前缀可摘要、
    # auto_compact 返回 None，不会触发 replace_history 与 cleanup。
    conv.add_user_message("recent1")
    conv.add_assistant_message("recent2")
    conv.add_user_message("recent3")
    conv.add_assistant_message("recent4")
    conv.record_usage_anchor(input_tokens=500, output_tokens=100)
    # 在 session_dir 下创建一个 tool-result 文件。
    (tmp_path / "tu-old.txt").write_text("old spill", encoding="utf-8")
    client = _FakeSummaryClient("<summary>cleaned</summary>")

    await auto_compact(
        conv, client, context_window=200_000, session_dir=tmp_path, manual=True
    )

    assert conv.baseline_tokens == 0
    assert conv.anchor_count == 0
    # tool-results 目录被清理。
    assert not (tmp_path / "tu-old.txt").exists()


# 验证 auto_compact 在熔断器打开且处于软硬阈值之间时返回错误字符串。
@pytest.mark.asyncio
async def test_auto_compact_returns_error_when_breaker_open(tmp_path: Path) -> None:
    conv = ConversationManager()
    # 构造使 current_tokens 落在软硬阈值之间的对话。
    # 软阈值 = 200000 - 20000 - 13000 = 167000
    # 硬阈值 = 200000 - 20000 - 3000 = 177000
    # 需要 current_tokens 在 [167000, 177000) 之间。
    # 用锚点 + 尾部估算控制。
    conv.record_usage_anchor(input_tokens=170_000, output_tokens=0)
    # anchor_count=0，current_tokens = 170000，落在软硬之间。
    breaker = CompactCircuitBreaker(max_failures=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    client = _FakeSummaryClient("should not be called")

    result = await auto_compact(
        conv,
        client,
        context_window=200_000,
        session_dir=tmp_path,
        breaker=breaker,
    )

    assert isinstance(result, str)
    assert "熔断" in result
    assert client.requests == []


# 验证 auto_compact 前缀太小时返回 None。
@pytest.mark.asyncio
async def test_auto_compact_returns_none_when_prefix_too_small(
    tmp_path: Path,
) -> None:
    conv = ConversationManager()
    # 构造小对话，手动模式但前缀 < MIN_SUMMARIZE_PREFIX_TOKENS。
    conv.add_user_message("hi")
    conv.add_assistant_message("hello")
    client = _FakeSummaryClient("summary")

    result = await auto_compact(
        conv, client, context_window=200_000, session_dir=tmp_path, manual=True
    )

    assert result is None


# ===========================================================================
# ensure_session_dir / cleanup_tool_results
# ===========================================================================


# 验证 ensure_session_dir 创建嵌套目录并返回路径。
def test_ensure_session_dir_creates_nested_dirs(tmp_path: Path) -> None:
    work_dir = tmp_path / "project"
    session_dir = ensure_session_dir(str(work_dir))

    assert session_dir.exists()
    assert session_dir.name == "tool-results"
    assert session_dir.parent.name == "session"
    assert session_dir.parent.parent.name == ".seacode"


# 验证 cleanup_tool_results 清空目录内容但保留目录本身。
def test_cleanup_tool_results_clears_contents(tmp_path: Path) -> None:
    (tmp_path / "tu-1.txt").write_text("spill", encoding="utf-8")
    (tmp_path / "replacement_records.jsonl").write_text("{}", encoding="utf-8")

    cleanup_tool_results(tmp_path)

    assert tmp_path.exists()
    assert not (tmp_path / "tu-1.txt").exists()
    assert not (tmp_path / "replacement_records.jsonl").exists()
