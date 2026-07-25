"""worktree slug 校验与扁平化的单元测试。"""

from __future__ import annotations

from seacode.worktree.slug import flatten_slug, validate_slug


# 验证 validate_slug 合法单段 name 通过校验。
# 传入 "feat-x" 断言返回 None 表示无错误。
def test_validate_slug_accepts_simple_segment() -> None:
    assert validate_slug("feat-x") is None


# 验证 validate_slug 合法多段 name 通过校验。
# 传入 "feature/auth" 断言返回 None 表示 / 分段合法。
def test_validate_slug_accepts_multi_segment() -> None:
    assert validate_slug("feature/auth") is None


# 验证 validate_slug 空字符串返回错误信息。
# 传入空串断言返回非 None 且消息含 "empty"。
def test_validate_slug_rejects_empty_string() -> None:
    err = validate_slug("")
    assert err is not None
    assert "empty" in err


# 验证 validate_slug 超过 64 字符返回错误信息。
# 构造 65 字符的 name 断言返回非 None 且消息含 "exceeds"。
def test_validate_slug_rejects_too_long_name() -> None:
    err = validate_slug("a" * 65)
    assert err is not None
    assert "exceeds" in err


# 验证 validate_slug 拒绝含 .. 的段。
# 传入 "foo/.." 断言返回非 None。
def test_validate_slug_rejects_double_dot_segment() -> None:
    err = validate_slug("foo/..")
    assert err is not None


# 验证 validate_slug 拒绝含 . 的段。
# 传入 "foo/." 断言返回非 None。
def test_validate_slug_rejects_single_dot_segment() -> None:
    err = validate_slug("foo/.")
    assert err is not None


# 验证 validate_slug 拒绝空段。
# 传入 "foo//bar" 断言返回非 None 且消息含 "empty segment"。
def test_validate_slug_rejects_empty_segment() -> None:
    err = validate_slug("foo//bar")
    assert err is not None
    assert "empty segment" in err


# 验证 validate_slug 拒绝含空格的段。
# 传入 "foo bar" 断言返回非 None。
def test_validate_slug_rejects_space() -> None:
    err = validate_slug("foo bar")
    assert err is not None


# 验证 validate_slug 拒绝含 * 的段。
# 传入 "foo*" 断言返回非 None。
def test_validate_slug_rejects_asterisk() -> None:
    err = validate_slug("foo*")
    assert err is not None


# 验证 validate_slug 拒绝含 + 的段，避免与 flatten_slug 输出冲突。
# 传入 "foo+bar" 断言返回非 None。
def test_validate_slug_rejects_plus_to_avoid_flatten_conflict() -> None:
    err = validate_slug("foo+bar")
    assert err is not None


# 验证 flatten_slug 把多段 name 中的 / 替换为 +。
# 传入 "feature/auth" 断言返回 "feature+auth"。
def test_flatten_slug_replaces_slash_with_plus() -> None:
    assert flatten_slug("feature/auth") == "feature+auth"


# 验证 flatten_slug 对无 / 的 name 原样返回。
# 传入 "simple" 断言返回 "simple"。
def test_flatten_slug_preserves_simple_name() -> None:
    assert flatten_slug("simple") == "simple"
