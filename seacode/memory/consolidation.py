"""后台记忆整理：5 级门控 + PID+mtime 锁 + 受限子 Agent。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from seacode.memory.auto_memory import ENTRYPOINT_NAME, MAX_ENTRYPOINT_LINES
from seacode.memory.session import SessionManager

logger = logging.getLogger(__name__)

# 默认门控阈值：上次整理至少 24h 前、至少 5 个新会话才触发整理。
DEFAULT_MIN_HOURS = 24
DEFAULT_MIN_SESSIONS = 5
# 会话扫描节流：10min 内不重复扫描，避免每轮 Loop 都遍历 .meta。
SCAN_THROTTLE_MS = 10 * 60 * 1000
# 整理锁文件名（保留 v1 既定名）。
LOCK_FILE = ".consolidate-lock"
# 锁超时：1h 内有活跃持有者进程则不抢锁；超时且进程不存活才接管。
HOLDER_STALE_MS = 60 * 60 * 1000


class MemoryConsolidator:
    """管理后台记忆整理的状态和执行。

    门控顺序：目录存在 → 24h 时间 → 10min 节流 → ≥5 新会话 → 文件锁；
    全通过后 fork 受限子 Agent 执行 4 阶段 prompt 整理。
    """

    def __init__(
        self,
        work_dir: str,
        *,
        min_hours: int = DEFAULT_MIN_HOURS,
        min_sessions: int = DEFAULT_MIN_SESSIONS,
    ) -> None:
        self._work_dir = work_dir
        self._mem_dir = os.path.join(work_dir, ".seacode", "memory")
        self._user_mem_dir = os.path.join(Path.home(), ".seacode", "memory")
        self._min_hours = min_hours
        self._min_sessions = min_sessions
        self._last_scan_at = 0

    # 检查门控条件，满足则后台执行一次整理。
    async def maybe_run(
        self,
        client: Any,
        conversation: Any,
        protocol: str,
    ) -> None:
        # 门控 1：记忆目录不存在直接返回。
        if not os.path.isdir(self._mem_dir):
            return

        # 门控 2：距上次整理不足 min_hours 返回。
        last_at = _read_last_consolidated_at(self._mem_dir)
        hours_since = (time.time() * 1000 - last_at) / 3_600_000
        if hours_since < self._min_hours:
            return

        # 门控 3：10min 内扫过会话统计返回（节流）。
        now = int(time.time() * 1000)
        if now - self._last_scan_at < SCAN_THROTTLE_MS:
            return
        self._last_scan_at = now

        # 门控 4：新会话数不足 min_sessions 返回。
        session_ids = _list_sessions_since(self._work_dir, last_at)
        if len(session_ids) < self._min_sessions:
            return

        # 门控 5：文件锁获取失败返回。
        prior_mtime = _try_acquire_lock(self._mem_dir)
        if prior_mtime is None:
            return

        logger.debug(
            "[consolidation] firing — %.1fh since last, %d sessions",
            hours_since,
            len(session_ids),
        )

        # 后台执行：失败 _rollback_lock 恢复 mtime，避免一次失败导致 24h 内不再重试。
        asyncio.ensure_future(
            self._run(client, conversation, protocol, session_ids, prior_mtime)
        )

    # 后台执行 wrapper：异常时回滚锁 mtime。
    async def _run(
        self,
        client: Any,
        conversation: Any,
        protocol: str,
        session_ids: list[str],
        prior_mtime: int,
    ) -> None:
        try:
            await self._do_consolidation(
                client, conversation, protocol, session_ids
            )
        except Exception:
            logger.debug("[consolidation] failed, rolling back lock")
            _rollback_lock(self._mem_dir, prior_mtime)

    # 实际整理：fork 受限子 Agent（6 个基础工具 + bypass 权限 + 15 轮上限），
    # 4 阶段 prompt（Orient / Gather / Consolidate / Prune and index）。
    async def _do_consolidation(
        self,
        client: Any,
        conversation: Any,
        protocol: str,
        session_ids: list[str],
    ) -> None:
        # 局部 import 避免模块加载循环。
        from seacode.agent import Agent
        from seacode.conversation import ConversationManager
        from seacode.permissions import PermissionChecker, PermissionMode
        from seacode.tools import ToolRegistry
        from seacode.tools.bash import Bash
        from seacode.tools.edit_file import EditFile
        from seacode.tools.glob import Glob
        from seacode.tools.grep import Grep
        from seacode.tools.read_file import ReadFile
        from seacode.tools.write_file import WriteFile

        transcript_dir = os.path.join(self._work_dir, ".seacode", "sessions")
        prompt = _build_consolidation_prompt(
            self._mem_dir, self._user_mem_dir, transcript_dir, session_ids
        )

        # 构建子 Agent 的工具注册表：6 个基础工具。
        registry = ToolRegistry()
        for tool_cls in [
            ReadFile, WriteFile, EditFile,
            Glob, Grep, Bash,
        ]:
            registry.register(tool_cls())

        # bypass 权限避免整理场景下弹审批；max_iterations=15 硬上限兜底。
        # 整理子 Agent 不需要 OS 沙箱与三层规则文件，仅以 bypass 模式跳过 HITL。
        from seacode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            RuleEngine,
        )
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(project_root=self._work_dir),
            rule_engine=RuleEngine(),
            mode=PermissionMode.BYPASS,
        )

        conv = ConversationManager()
        conv.add_user_message(prompt)

        sub_agent = Agent(
            client=client,
            registry=registry,
            protocol=protocol,
            work_dir=self._work_dir,
            max_iterations=15,
            permission_checker=checker,
        )

        async for _event in sub_agent.run(conv):
            pass  # drain

        logger.debug("[consolidation] completed")


# ---------------------------------------------------------------------------
# 锁文件管理
# ---------------------------------------------------------------------------


def _lock_path(mem_dir: str) -> str:
    return os.path.join(mem_dir, LOCK_FILE)


# 返回上次整理时间戳（ms）。锁文件不存在返回 0。
def _read_last_consolidated_at(mem_dir: str) -> int:
    path = _lock_path(mem_dir)
    try:
        return int(os.stat(path).st_mtime * 1000)
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


# 获取锁。成功返回旧 mtime（ms），失败返回 None。
# 双语义锁：内容是持有者 PID（检测进程存活），mtime 是上次整理完成时间。
# 锁超时 HOLDER_STALE_MS 且持有者进程不存活才接管。写入 PID 后回读验证解决多进程竞争。
def _try_acquire_lock(mem_dir: str) -> int | None:
    path = _lock_path(mem_dir)
    mtime_ms: int | None = None
    holder_pid: int | None = None

    if os.path.exists(path):
        try:
            mtime_ms = int(os.stat(path).st_mtime * 1000)
            raw = Path(path).read_text().strip()
            holder_pid = int(raw) if raw else None
        except (ValueError, OSError):
            pass

    if mtime_ms is not None and time.time() * 1000 - mtime_ms < HOLDER_STALE_MS:
        if holder_pid is not None and _is_process_running(holder_pid):
            return None

    os.makedirs(mem_dir, exist_ok=True)
    Path(path).write_text(str(os.getpid()))

    # 回读验证：避免与并发进程竞争写入。
    try:
        verify = Path(path).read_text().strip()
        if int(verify) != os.getpid():
            return None
    except (ValueError, OSError):
        return None

    return mtime_ms if mtime_ms is not None else 0


# 整理失败时恢复锁 mtime 到整理前值，避免一次失败导致 24h 内不再重试。
# prior_mtime == 0 时删除锁文件（整理前无锁）；否则清空内容并恢复 mtime。
def _rollback_lock(mem_dir: str, prior_mtime: int) -> None:
    path = _lock_path(mem_dir)
    try:
        if prior_mtime == 0:
            os.unlink(path)
            return
        Path(path).write_text("")
        t = prior_mtime / 1000
        os.utime(path, (t, t))
    except OSError:
        pass


# 检测进程是否存活。Windows 用 OpenProcess，Unix 用 os.kill(pid, 0)。
# PermissionError 也算"不存活"——保守判定，宁可错抢锁也不永久卡住。
def _is_process_running(pid: int) -> bool:
    # Windows 用 OpenProcess 查询进程存活；Unix 用 os.kill(pid, 0) 探测。
    # getattr 兼容 mypy 在 Linux 上的类型检查（windll 仅 Windows 存在）。
    if os.name == "nt":
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return True
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if h:
            windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------


# 返回 since_ms 之后被修改过的会话 ID 列表。
def _list_sessions_since(work_dir: str, since_ms: int) -> list[str]:
    mgr = SessionManager(work_dir)
    since_ts = since_ms / 1000
    # last_active 是 datetime，转成秒级 epoch 再与阈值比较
    return [
        s.id
        for s in mgr.list()
        if s.last_active.timestamp() > since_ts
    ]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


# 4 阶段整理 prompt：Orient / Gather / Consolidate / Prune and index。
# Bash 限制只读命令（ls / find / grep / cat / stat / wc / head / tail）。
def _build_consolidation_prompt(
    mem_dir: str,
    user_mem_dir: str,
    transcript_dir: str,
    session_ids: list[str],
) -> str:
    lines = [
        "# Dream: Memory Consolidation",
        "",
        "You are performing a dream — a reflective pass over your memory files. "
        "Synthesize what you've learned recently into durable, well-organized "
        "memories so that future sessions can orient quickly.",
        "",
        f"Project memory directory: `{mem_dir}`",
        f"User memory directory: `{user_mem_dir}`",
        "The memory directory already exists — write to it directly.",
        "",
        f"Session transcripts: `{transcript_dir}` (large JSONL files — grep narrowly, don't read whole files)",
        "",
        "---",
        "",
        "## Phase 1 — Orient",
        "",
        "- `ls` the memory directory to see what already exists",
        f"- Read `{ENTRYPOINT_NAME}` to understand the current index",
        "- Skim existing topic files so you improve them rather than creating duplicates",
        "",
        "## Phase 2 — Gather recent signal",
        "",
        "Look for new information worth persisting:",
        "",
        "1. **Existing memories that drifted** — facts that contradict something you see in the codebase now",
        "2. **Transcript search** — if you need specific context, grep the JSONL transcripts for narrow terms",
        "",
        "Don't exhaustively read transcripts. Look only for things you already suspect matter.",
        "",
        "## Phase 3 — Consolidate",
        "",
        "For each thing worth remembering, write or update a memory file. "
        "Each memory file uses YAML frontmatter with name, description, and metadata.type fields, "
        "followed by a Markdown body.",
        "",
        "Focus on:",
        "- Merging new signal into existing topic files rather than creating near-duplicates",
        '- Converting relative dates ("yesterday", "last week") to absolute dates',
        "- Deleting contradicted facts — if today's investigation disproves an old memory, fix it at the source",
        "",
        "## Phase 4 — Prune and index",
        "",
        f"Update `{ENTRYPOINT_NAME}` so it stays under {MAX_ENTRYPOINT_LINES} lines AND under ~25KB. "
        "It's an **index**, not a dump — each entry should be one line under ~150 characters: "
        "`- [Title](file.md) — one-line hook`. Never write memory content directly into it.",
        "",
        "- Remove pointers to memories that are now stale, wrong, or superseded",
        "- Demote verbose entries: if an index line is over ~200 chars, shorten the line, move the detail",
        "- Add pointers to newly important memories",
        "- Resolve contradictions — if two files disagree, fix the wrong one",
        "",
        "---",
        "",
        "**Tool constraints for this run:** Bash is restricted to read-only commands "
        "(`ls`, `find`, `grep`, `cat`, `stat`, `wc`, `head`, `tail`, and similar). "
        "Anything that writes, redirects to a file, or modifies state will be denied.",
        "",
    ]

    if session_ids:
        lines.append(f"Sessions since last consolidation ({len(session_ids)}):")
        for sid in session_ids:
            lines.append(f"- {sid}")

    lines.extend([
        "",
        "Return a brief summary of what you consolidated, updated, or pruned. "
        "If nothing changed (memories are already tight), say so.",
    ])

    return "\n".join(lines)
