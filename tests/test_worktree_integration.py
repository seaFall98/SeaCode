"""worktree integration 通知模板与名称生成的单元测试。"""

from __future__ import annotations

import re

from seacode.worktree.cleanup import _is_ephemeral
from seacode.worktree.integration import (
    WORKTREE_NOTICE_TEMPLATE,
    build_worktree_notice,
    generate_worktree_name,
)


# 验证 generate_worktree_name 返回 agent-a + 7 hex 格式。
# 多次调用断言都匹配 ^agent-a[0-9a-f]{7}$。
def test_generate_worktree_name_format() -> None:
    for _ in range(10):
        name = generate_worktree_name()
        assert re.match(r"^agent-a[0-9a-f]{7}$", name)


# 验证 generate_worktree_name 返回的名称匹配 ephemeral 模式。
# 调用一次，断言 _is_ephemeral 返回 True。
def test_generate_worktree_name_is_ephemeral() -> None:
    name = generate_worktree_name()
    assert _is_ephemeral(name) is True


# 验证 build_worktree_notice 包含 [WORKTREE CONTEXT] 标签。
# 调用后断言返回值含起止标签。
def test_build_worktree_notice_contains_tags() -> None:
    notice = build_worktree_notice("/parent", "/wt")
    assert "[WORKTREE CONTEXT]" in notice
    assert "[/WORKTREE CONTEXT]" in notice


# 验证 build_worktree_notice 包含父 cwd 与 worktree 路径。
# 调用后断言返回值含传入的两个路径。
def test_build_worktree_notice_contains_paths() -> None:
    notice = build_worktree_notice("/parent/dir", "/wt/path")
    assert "/parent/dir" in notice
    assert "/wt/path" in notice


# 验证 build_worktree_notice 包含翻译提示。
# 调用后断言返回值含 "translate" 关键字。
def test_build_worktree_notice_contains_translation_hint() -> None:
    notice = build_worktree_notice("/parent", "/wt")
    assert "translate" in notice.lower()


# 验证 WORKTREE_NOTICE_TEMPLATE 含两个占位符。
# 直接读取常量断言含 {parent_cwd} 与 {wt_path}。
def test_worktree_notice_template_contains_placeholders() -> None:
    assert "{parent_cwd}" in WORKTREE_NOTICE_TEMPLATE
    assert "{wt_path}" in WORKTREE_NOTICE_TEMPLATE
