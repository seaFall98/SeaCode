"""SubAgent worktree 集成：通知模板与名称生成。"""

from __future__ import annotations

import secrets

# 注入到子 Agent 任务文本前的 worktree 上下文通知模板。
WORKTREE_NOTICE_TEMPLATE = """[WORKTREE CONTEXT]
You are currently operating in an isolated git worktree.
- Parent agent working directory: {parent_cwd}
- Current worktree path: {wt_path}
- File paths in tool results may be relative to the worktree;
  translate them when reporting to the parent.
- Re-read files before editing to ensure you have the latest content.
[/WORKTREE CONTEXT]"""


def generate_worktree_name() -> str:
    """生成 ephemeral worktree 名称：agent-a + 7 hex。"""
    return f"agent-a{secrets.token_hex(4)[:7]}"


def build_worktree_notice(parent_cwd: str, wt_path: str) -> str:
    """构造注入子 Agent 任务前的 worktree 上下文通知。"""
    return WORKTREE_NOTICE_TEMPLATE.format(parent_cwd=parent_cwd, wt_path=wt_path)
