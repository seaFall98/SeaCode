"""运行时诊断记录测试：验证白名单字段、轮转和失败隔离。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seacode import diagnostics


def _raise_secret_error() -> None:
    raise RuntimeError("secret provider response with api_key=hidden")


# 验证运行时记录只写白名单字段，异常消息和绝对路径不会进入 JSONL。
# 将包根定向到测试文件以覆盖净化帧提取，同时断言记录可由诊断 ID 关联。
def test_runtime_diagnostic_record_is_safe_and_correlated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_PACKAGE_ROOT", Path(__file__).resolve().parent)
    try:
        _raise_secret_error()
    except RuntimeError as error:
        written = diagnostics.write_runtime_diagnostic(
            "SC-safe123",
            error,
            phase="tui_turn",
            tool_activity=True,
            runtime_dir=tmp_path,
        )

    runtime_path = tmp_path / "diagnostics" / "runtime.jsonl"
    line = runtime_path.read_text(encoding="utf-8")
    record = json.loads(line)

    assert written is True
    assert set(record) == {
        "timestamp",
        "diagnostic_id",
        "phase",
        "exception_type",
        "tool_activity",
        "frames",
    }
    assert record["diagnostic_id"] == "SC-safe123"
    assert record["phase"] == "tui_turn"
    assert record["exception_type"] == "RuntimeError"
    assert record["tool_activity"] is True
    assert record["frames"]
    assert "secret provider response" not in line
    assert "api_key" not in line
    assert str(tmp_path) not in line


# 验证记录超过配置大小时先轮转主文件，最多保留当前文件和一代备份。
# 用极小阈值连续写入两条记录，断言第一条进入备份、第二条保留在主文件。
def test_runtime_diagnostic_rotates_single_backup(tmp_path: Path) -> None:
    first_error = RuntimeError("first")
    second_error = RuntimeError("second")

    assert diagnostics.write_runtime_diagnostic(
        "SC-first", first_error, phase="tui_turn", tool_activity=False,
        runtime_dir=tmp_path, max_bytes=1,
    )
    assert diagnostics.write_runtime_diagnostic(
        "SC-second", second_error, phase="tui_turn", tool_activity=False,
        runtime_dir=tmp_path, max_bytes=1,
    )

    diagnostic_dir = tmp_path / "diagnostics"
    assert "SC-first" in (diagnostic_dir / "runtime.1.jsonl").read_text(encoding="utf-8")
    assert "SC-second" in (diagnostic_dir / "runtime.jsonl").read_text(encoding="utf-8")
    assert len(list(diagnostic_dir.glob("runtime*.jsonl"))) == 2


# 验证诊断目录不可写时记录器安静失败，不把异常扩散回 TUI 调用方。
# 将运行目录指定为普通文件，使 mkdir 失败并断言返回 False。
def test_runtime_diagnostic_write_failure_is_isolated(tmp_path: Path) -> None:
    blocked_path = tmp_path / "blocked"
    blocked_path.write_text("not a directory", encoding="utf-8")

    written = diagnostics.write_runtime_diagnostic(
        "SC-blocked",
        RuntimeError("secret"),
        phase="tui_turn",
        tool_activity=False,
        runtime_dir=blocked_path,
    )

    assert written is False
