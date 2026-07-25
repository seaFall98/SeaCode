"""teams/transcript.py 单测：序列化、反序列化、save/load 全分支。"""

from __future__ import annotations

import json
from pathlib import Path

from seacode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from seacode.teams.transcript import (
    _deserialize_conversation,
    _serialize_conversation,
    load_transcript,
    save_transcript,
)


# 验证 _serialize_conversation 保留 role / content / tool_uses / tool_results / thinking_blocks。
# 构造含工具调用与思考块的对话，断言序列化字段完整。
def test_serialize_preserves_all_fields() -> None:
    cm = ConversationManager()
    cm.history.append(
        Message(
            role="user",
            content="hello",
        )
    )
    cm.history.append(
        Message(
            role="assistant",
            content="thinking...",
            tool_uses=[
                ToolUseBlock(
                    tool_use_id="tu-1", tool_name="ReadFile",
                    arguments={"file_path": "/tmp/x"},
                )
            ],
            thinking_blocks=[
                ThinkingBlock(thinking="inner thought", signature="sig-1")
            ],
        )
    )
    cm.history.append(
        Message(
            role="user",
            content="",
            tool_results=[
                ToolResultBlock(
                    tool_use_id="tu-1", content="file content", is_error=False
                )
            ],
        )
    )

    data = _serialize_conversation(cm)
    assert len(data) == 3
    assert data[0]["role"] == "user"
    assert data[0]["content"] == "hello"
    assert data[1]["tool_uses"][0]["tool_use_id"] == "tu-1"
    assert data[1]["tool_uses"][0]["tool_name"] == "ReadFile"
    assert data[1]["tool_uses"][0]["arguments"] == {"file_path": "/tmp/x"}
    assert data[1]["thinking_blocks"][0]["thinking"] == "inner thought"
    assert data[1]["thinking_blocks"][0]["signature"] == "sig-1"
    assert data[2]["tool_results"][0]["tool_use_id"] == "tu-1"
    assert data[2]["tool_results"][0]["content"] == "file content"
    assert data[2]["tool_results"][0]["is_error"] is False


# 验证 _deserialize_conversation 重建对话并标记 env_injected / ltm_injected。
# 序列化后反序列化，断言 history 一致且 env_injected / ltm_injected 为 True。
def test_deserialize_rebuilds_and_marks_injected() -> None:
    cm = ConversationManager()
    cm.history.append(
        Message(
            role="assistant",
            content="hi",
            tool_uses=[
                ToolUseBlock(
                    tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"}
                )
            ],
        )
    )
    data = _serialize_conversation(cm)
    restored = _deserialize_conversation(data)
    assert restored.env_injected is True
    assert restored.ltm_injected is True
    assert len(restored.history) == 1
    assert restored.history[0].content == "hi"
    assert restored.history[0].tool_uses[0].tool_name == "Bash"


# 验证 save_transcript 写入 <team_dir>/transcripts/<agent_id>.json。
# save 后断言文件存在且内容可解析。
def test_save_transcript_writes_file(tmp_path: Path) -> None:
    cm = ConversationManager()
    cm.history.append(Message(role="user", content="hello"))
    save_transcript(tmp_path, "agent-1", cm)
    path = tmp_path / "transcripts" / "agent-1.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["role"] == "user"
    assert data[0]["content"] == "hello"


# 验证 load_transcript 文件不存在返回 None。
# 传入不存在的路径，断言返回 None。
def test_load_transcript_missing_returns_none(tmp_path: Path) -> None:
    assert load_transcript(tmp_path, "nobody") is None


# 验证 load_transcript 文件损坏返回 None。
# 写入非法 JSON，断言 load 返回 None 而不抛异常。
def test_load_transcript_corrupt_returns_none(tmp_path: Path) -> None:
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True)
    path = transcript_dir / "agent-1.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_transcript(tmp_path, "agent-1") is None


# 验证 save/load 往返一致。
# save 后 load，断言对话历史完整恢复。
def test_save_load_round_trip(tmp_path: Path) -> None:
    cm = ConversationManager()
    cm.history.append(
        Message(
            role="user",
            content="hello",
            tool_results=[
                ToolResultBlock(
                    tool_use_id="tu-1", content="result", is_error=True
                )
            ],
        )
    )
    save_transcript(tmp_path, "agent-1", cm)
    restored = load_transcript(tmp_path, "agent-1")
    assert restored is not None
    assert len(restored.history) == 1
    assert restored.history[0].content == "hello"
    assert restored.history[0].tool_results[0].tool_use_id == "tu-1"
    assert restored.history[0].tool_results[0].content == "result"
    assert restored.history[0].tool_results[0].is_error is True
