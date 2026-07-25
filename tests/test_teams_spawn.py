"""teams/spawn.py 单测：shell_quote 与 build_teammate_cli。"""

from __future__ import annotations

from seacode.teams.spawn import build_teammate_cli, shell_quote


# 验证 shell_quote 对空串 / 安全字符 / 含单引号 / 含空格 的处理。
# 四种输入分别断言：空串返回 ''；安全字符原样；单引号转义；空格包单引号。
def test_shell_quote_branches() -> None:
    assert shell_quote("") == "''"
    assert shell_quote("simple") == "simple"
    assert shell_quote("path/to/file.py") == "path/to/file.py"
    assert shell_quote("abc-123_def") == "abc-123_def"
    # 含单引号：包单引号并替换内部单引号。
    assert shell_quote("it's") == "'it'\"'\"'s'"
    # 含空格：包单引号。
    assert shell_quote("my path") == "'my path'"
    # 含特殊字符 $：包单引号。
    assert shell_quote("a$b") == "'a$b'"


# 验证 build_teammate_cli 产出 cd <workdir> && <python> -m seacode
# --teammate --team-name <t> --agent-name <n>。
# 断言关键字段全部出现，且 cd 与命令之间用 && 连接。
def test_build_teammate_cli_format() -> None:
    cli = build_teammate_cli("demo", "alice", "/tmp/work")
    assert "cd /tmp/work" in cli
    assert "-m seacode" in cli
    assert "--teammate" in cli
    assert "--team-name demo" in cli
    assert "--agent-name alice" in cli
    assert " && " in cli


# 验证 build_teammate_cli 对含特殊字符的 workdir / team_name / member_name 正确转义。
# 含空格的 workdir 包单引号；含 $ 的 team_name 包单引号。
def test_build_teammate_cli_escapes_special_chars() -> None:
    cli = build_teammate_cli("my team", "alice", "/tmp/my work")
    assert "cd '/tmp/my work'" in cli
    assert "--team-name 'my team'" in cli
    assert "--agent-name alice" in cli
