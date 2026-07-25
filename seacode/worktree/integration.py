"""SubAgent worktree 集成：通知模板与名称生成。"""

from __future__ import annotations

import secrets

# 注入到子 Agent 任务文本前的 worktree 上下文通知模板。
# 强调三点：继承父对话上下文、入向路径翻译、文件副本可能与父级分叉。
WORKTREE_NOTICE_TEMPLATE = """[WORKTREE CONTEXT]
You have inherited the parent agent's conversation context.
You are currently working in an isolated Git Worktree: {wt_path}
The parent agent's working directory is: {parent_cwd}

IMPORTANT:
- File paths mentioned in the parent conversation refer to the PARENT directory.
- You must translate them to your local worktree path before reading or editing.
- Always re-read files before editing — your copy may differ from the parent's version.
[/WORKTREE CONTEXT]"""


def generate_worktree_name() -> str:
    """生成 ephemeral worktree 名称：agent-a + 7 hex。"""
    return f"agent-a{secrets.token_hex(4)[:7]}"


def build_worktree_notice(parent_cwd: str, wt_path: str) -> str:
    """构造注入子 Agent 任务前的 worktree 上下文通知。"""
    return WORKTREE_NOTICE_TEMPLATE.format(parent_cwd=parent_cwd, wt_path=wt_path)
