"""teams/progress.py 单测：SPINNER_VERBS / random_verb / ToolActivity / TeammateProgress。"""

from __future__ import annotations

import threading
from datetime import datetime

from seacode.teams.progress import (
    SPINNER_VERBS,
    TeammateProgress,
    ToolActivity,
    random_verb,
)


# 验证 random_verb 返回 SPINNER_VERBS 中的元素。
# 多次调用 random_verb，断言每次返回值都在 SPINNER_VERBS 集合中。
def test_random_verb_in_spinner_verbs() -> None:
    for _ in range(20):
        v = random_verb()
        assert v in SPINNER_VERBS


# 验证 record_tool_use 计数累加。
# 调用 3 次，断言 tool_use_count == 3。
def test_record_tool_use_increments_count() -> None:
    p = TeammateProgress(name="alice", team_name="demo")
    for i in range(3):
        p.record_tool_use("ReadFile", {"file_path": f"/tmp/{i}"})
    assert p.tool_use_count == 3


# 验证 recent_activities 最多保留 5 条。
# 调用 7 次，断言 recent_activities 长度为 5 且保留最后 5 条。
def test_recent_activities_capped_at_five() -> None:
    p = TeammateProgress(name="alice", team_name="demo")
    for i in range(7):
        p.record_tool_use("ReadFile", {"file_path": f"/tmp/{i}"})
    assert len(p.recent_activities) == 5
    # 保留最后 5 条：索引 2..6。
    paths = [a.args["file_path"] for a in p.recent_activities]
    assert paths == [f"/tmp/{i}" for i in range(2, 7)]


# 验证 record_tool_use 更新 last_activity 含工具名。
# ReadFile 调用后 last_activity 含 "ReadFile"。
def test_record_tool_use_updates_last_activity() -> None:
    p = TeammateProgress(name="alice", team_name="demo")
    p.record_tool_use("ReadFile", {"file_path": "/tmp/x"})
    assert "ReadFile" in p.last_activity
    assert "/tmp/x" in p.last_activity


# 验证 record_tool_use 更新 spinner_verb 非空。
# 调用后 spinner_verb 在 SPINNER_VERBS 集合中。
def test_record_tool_use_updates_spinner_verb() -> None:
    p = TeammateProgress(name="alice", team_name="demo")
    p.record_tool_use("Bash", {"command": "ls"})
    assert p.spinner_verb in SPINNER_VERBS


# 验证 record_tokens 累加。
# 100 + 200 = 300。
def test_record_tokens_accumulates() -> None:
    p = TeammateProgress(name="alice", team_name="demo")
    p.record_tokens(100)
    p.record_tokens(200)
    assert p.token_count == 300


# 验证 activity_summary 返回 last_activity 或 spinner_verb。
# 有 activity 时返回 last_activity；无 activity 时返回 spinner_verb。
def test_activity_summary_prefers_last_activity() -> None:
    p1 = TeammateProgress(name="alice", team_name="demo")
    # 无 activity 时返回空串（spinner_verb 也为空）。
    assert p1.activity_summary() == ""

    p2 = TeammateProgress(name="bob", team_name="demo")
    p2.record_tool_use("ReadFile", {"file_path": "/tmp/x"})
    assert p2.activity_summary() == p2.last_activity


# 验证 format_tokens 1k / 1M 格式化。
# 500 → "500"；1500 → "1.5k"；1_500_000 → "1.5M"。
def test_format_tokens_thresholds() -> None:
    p1 = TeammateProgress(name="a", team_name="t")
    p1.record_tokens(500)
    assert p1.format_tokens() == "500"

    p2 = TeammateProgress(name="b", team_name="t")
    p2.record_tokens(1500)
    assert p2.format_tokens() == "1.5k"

    p3 = TeammateProgress(name="c", team_name="t")
    p3.record_tokens(1_500_000)
    assert p3.format_tokens() == "1.5M"


# 验证 ToolActivity._describe 各工具分支。
# ReadFile/EditFile/WriteFile 显示路径；Bash 截断 40 字；Glob/Grep 显示模式；默认返回 tool_name。
def test_tool_activity_describe_branches() -> None:
    ts = datetime.now()

    a1 = ToolActivity(tool_name="ReadFile", args={"file_path": "/tmp/x"}, timestamp=ts)
    assert a1._describe() == "ReadFile /tmp/x"

    a2 = ToolActivity(tool_name="EditFile", args={"path": "/tmp/y"}, timestamp=ts)
    assert a2._describe() == "EditFile /tmp/y"

    a3 = ToolActivity(tool_name="Bash", args={"command": "echo " + "x" * 60}, timestamp=ts)
    desc3 = a3._describe()
    assert desc3.startswith("Bash ")
    assert len(desc3) <= 5 + 40  # "Bash " + 40 chars

    a4 = ToolActivity(tool_name="Glob", args={"pattern": "*.py"}, timestamp=ts)
    assert a4._describe() == "Glob *.py"

    a5 = ToolActivity(tool_name="Grep", args={"pattern": "TODO"}, timestamp=ts)
    assert a5._describe() == "Grep TODO"

    a6 = ToolActivity(tool_name="CustomTool", args={}, timestamp=ts)
    assert a6._describe() == "CustomTool"


# 验证 TeammateProgress 并发 record_tool_use 不竞争。
# 多线程并发调用 record_tool_use，最终 tool_use_count 等于总调用数。
def test_concurrent_record_tool_use_safe() -> None:
    p = TeammateProgress(name="alice", team_name="demo")
    n_threads = 10
    per_thread = 20
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(per_thread):
                p.record_tool_use("ReadFile", {"file_path": "/tmp/x"})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert p.tool_use_count == n_threads * per_thread
