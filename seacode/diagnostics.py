"""受限的本地运行时诊断记录，供 TUI 未知异常关联使用。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

# 诊断记录只保留最近一代，避免用户目录中的运行日志无限增长。
MAX_RUNTIME_DIAGNOSTIC_BYTES = 1024 * 1024
_RUNTIME_FILENAME = "runtime.jsonl"
_RUNTIME_BACKUP_FILENAME = "runtime.1.jsonl"
_PACKAGE_ROOT = Path(__file__).resolve().parent


def write_runtime_diagnostic(
    diagnostic_id: str,
    error: BaseException,
    *,
    phase: str,
    tool_activity: bool,
    runtime_dir: Path | None = None,
    max_bytes: int = MAX_RUNTIME_DIAGNOSTIC_BYTES,
) -> bool:
    """写入白名单诊断字段；写入失败时返回 False 而不影响调用方恢复。"""
    try:
        record = _build_record(
            diagnostic_id,
            error,
            phase=phase,
            tool_activity=tool_activity,
        )
        payload = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        base_dir = runtime_dir if runtime_dir is not None else Path.home() / ".seacode"
        diagnostic_dir = base_dir / "diagnostics"
        runtime_path = diagnostic_dir / _RUNTIME_FILENAME
        backup_path = diagnostic_dir / _RUNTIME_BACKUP_FILENAME
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(runtime_path, backup_path, len(payload), max_bytes)
        with runtime_path.open("ab") as handle:
            handle.write(payload)
    except Exception as write_error:
        # 不记录异常内容，避免诊断通道本身泄漏或递归失败。
        log.warning(
            "Runtime diagnostic write failed diagnostic_id=%s error_type=%s",
            diagnostic_id,
            type(write_error).__name__,
        )
        return False
    return True


def _build_record(
    diagnostic_id: str,
    error: BaseException,
    *,
    phase: str,
    tool_activity: bool,
) -> dict[str, object]:
    """构造不含异常文本、路径、局部变量或请求内容的记录。"""
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "diagnostic_id": diagnostic_id,
        "phase": phase,
        "exception_type": type(error).__name__,
        "tool_activity": tool_activity,
        "frames": _sanitize_package_frames(error.__traceback__),
    }


def _sanitize_package_frames(
    traceback_object: TracebackType | None,
) -> list[dict[str, str | int]]:
    """仅导出 SeaCode 包内的模块、函数和行号，排除原始路径与源码行。"""
    frames: list[dict[str, str | int]] = []
    current = traceback_object
    while current is not None:
        filename = Path(current.tb_frame.f_code.co_filename)
        try:
            relative_path = filename.resolve().relative_to(_PACKAGE_ROOT)
        except (OSError, RuntimeError, ValueError):
            current = current.tb_next
            continue
        if relative_path.suffix == ".py":
            module_parts = relative_path.with_suffix("").parts
            module = ".".join(("seacode", *module_parts))
            frames.append(
                {
                    "module": module,
                    "function": current.tb_frame.f_code.co_name,
                    "line": current.tb_lineno,
                }
            )
        current = current.tb_next
    return frames


def _rotate_if_needed(
    runtime_path: Path,
    backup_path: Path,
    incoming_bytes: int,
    max_bytes: int,
) -> None:
    """在写入前按大小轮转，只保留一份历史主记录。"""
    try:
        current_size = runtime_path.stat().st_size
    except FileNotFoundError:
        return
    if current_size + incoming_bytes <= max_bytes:
        return
    if backup_path.exists():
        backup_path.unlink()
    runtime_path.replace(backup_path)
