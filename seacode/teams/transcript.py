# Teammate 对话历史持久化：把 ConversationManager 序列化为 JSON 并按 agent_id 落盘。
"""teams 子包的 transcript 保存与加载。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from seacode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

log = logging.getLogger(__name__)


# 把 ConversationManager.history 序列化为可 JSON 化的 list[dict]。
# 保留 role / content / tool_uses / tool_results / thinking_blocks 完整结构。
def _serialize_conversation(conversation: ConversationManager) -> list[dict]:
    out: list[dict] = []
    for msg in conversation.history:
        out.append(
            {
                "role": msg.role,
                "content": msg.content,
                "tool_uses": [
                    {
                        "tool_use_id": tu.tool_use_id,
                        "tool_name": tu.tool_name,
                        "arguments": tu.arguments,
                    }
                    for tu in msg.tool_uses
                ],
                "tool_results": [
                    {
                        "tool_use_id": tr.tool_use_id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                    for tr in msg.tool_results
                ],
                "thinking_blocks": [
                    {"thinking": tb.thinking, "signature": tb.signature}
                    for tb in msg.thinking_blocks
                ],
            }
        )
    return out


# 把 list[dict] 反序列化为 ConversationManager；标记 env_injected / ltm_injected 已注入避免重复。
def _deserialize_conversation(data: list[dict]) -> ConversationManager:
    cm = ConversationManager()
    for item in data:
        msg = Message(
            role=item["role"],
            content=item.get("content", ""),
            tool_uses=[
                ToolUseBlock(
                    tool_use_id=tu["tool_use_id"],
                    tool_name=tu["tool_name"],
                    arguments=tu.get("arguments", {}),
                )
                for tu in item.get("tool_uses", [])
            ],
            tool_results=[
                ToolResultBlock(
                    tool_use_id=tr["tool_use_id"],
                    content=tr.get("content", ""),
                    is_error=tr.get("is_error", False),
                )
                for tr in item.get("tool_results", [])
            ],
            thinking_blocks=[
                ThinkingBlock(
                    thinking=tb.get("thinking", ""),
                    signature=tb.get("signature", ""),
                )
                for tb in item.get("thinking_blocks", [])
            ],
        )
        cm.history.append(msg)
    # 标记已注入；transcript 恢复时不重新注入环境与长期记忆。
    cm.env_injected = True
    cm.ltm_injected = True
    return cm


# 把对话历史写入 <team_dir>/transcripts/<agent_id>.json。
def save_transcript(
    team_dir: str | Path, agent_id: str, conversation: ConversationManager
) -> None:
    transcript_dir = Path(team_dir) / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{agent_id}.json"
    path.write_text(
        json.dumps(_serialize_conversation(conversation), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# 从 <team_dir>/transcripts/<agent_id>.json 加载对话历史；不存在或损坏返回 None。
def load_transcript(
    team_dir: str | Path, agent_id: str
) -> ConversationManager | None:
    path = Path(team_dir) / "transcripts" / f"{agent_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _deserialize_conversation(data)
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("failed to load transcript: %s", e)
        return None
