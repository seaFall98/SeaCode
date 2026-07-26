"""SeaCode 终端入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from uuid import uuid4

from .config import ConfigError, ProviderConfig, load_config
from .hooks import HookConfigError, HookEngine, load_hooks

# Agent Loop 默认最大迭代次数，与 SEA_MAX_STEPS 未设置时的回退值一致。
_DEFAULT_MAX_STEPS: int = 100

log = logging.getLogger(__name__)

# teammate worker 的系统提示词附加段；告知 worker 它是团队成员，
# 文本回复不可见，需用 SendMessage 与其它成员通信，且工作在隔离 worktree 中。
_TEAMMATE_ADDENDUM = (
    "\n[TEAMMATE CONTEXT]\n"
    "You are a teammate in a team. Your text replies are NOT visible to other "
    "teammates — use the SendMessage tool to communicate. You are working in an "
    "isolated worktree; use relative paths for all file operations.\n"
    "[/TEAMMATE CONTEXT]"
)


# 读取 SEA_MAX_STEPS 环境变量，非正整数或缺失时回退到默认值。
def _read_max_steps() -> int:
    raw = os.environ.get("SEA_MAX_STEPS")
    if not raw:
        return _DEFAULT_MAX_STEPS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_STEPS
    return value if value > 0 else _DEFAULT_MAX_STEPS


# 对每个 Provider 尝试拉取 context window 并缓存到 ProviderConfig 上。
# 完全尽力而为：非 anthropic、网络失败或超时都静默降级，由 get_context_window()
# 走内置映射表或默认值。拉取是并发的，避免多 Provider 时串行等待。
async def _resolve_context_windows_async(
    providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
) -> None:
    from .client import resolve_context_window

    await asyncio.gather(
        *(resolve_context_window(provider) for provider in providers),
        return_exceptions=True,
    )


# batch14：解析 --teammate / --team-name / --agent-name 命令行标志。
# 返回 (is_teammate, team_name, agent_name)；缺 --team-name / --agent-name 时对应字段为空。
def _parse_teammate_flags(argv: list[str]) -> tuple[bool, str, str]:
    is_teammate = "--teammate" in argv
    team_name = ""
    agent_name = ""
    for i, arg in enumerate(argv):
        if arg == "--team-name" and i + 1 < len(argv):
            team_name = argv[i + 1]
        elif arg == "--agent-name" and i + 1 < len(argv):
            agent_name = argv[i + 1]
    return (is_teammate, team_name, agent_name)


# 非交互模式：直接执行 prompt 并把结果输出到 stdout，用于脚本化调用与 CI 集成。
# 不启动 TUI、不连接 MCP、不加载 Hook；保留权限检查与默认工具集，
# 让 LLM 仍可调用 ReadFile / Bash 等完成实际任务。
# -p 模式默认 DEFAULT 权限模式，PermissionRequest 事件自动批准避免阻塞；
# 用户可通过 --mode bypassPermissions 完全跳过权限确认，或 --mode acceptEdits 放行写操作。
# output_format 支持 text（默认最终文本）、json（单个最终结果）与
# stream-json（NDJSON 事件流）。
async def _run_prompt(
    prompt: str, output_format: str, mode_str: str | None
) -> None:
    import json
    import time

    from .agent import (
        Agent,
        CompactNotification,
        ErrorEvent,
        LoopComplete,
        PermissionRequest,
        PermissionResponse,
        RetryEvent,
        StreamText,
        ThinkingText,
        ToolResultEvent,
        ToolUseEvent,
        TurnComplete,
        UsageEvent,
    )
    from .client import create_client
    from .conversation import ConversationManager
    from .memory.instructions import load_instructions
    from .permissions import PermissionChecker, PermissionMode
    from .permissions.dangerous import DangerousCommandDetector
    from .permissions.rules import RuleEngine
    from .permissions.sandbox import PathSandbox
    from .tools import create_default_registry

    try:
        config = load_config()
    except ConfigError as error:
        print(f"SeaCode configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    if not config.providers:
        print("SeaCode configuration error: no provider configured", file=sys.stderr)
        raise SystemExit(1)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as error:
        print(f"SeaCode hook configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    hook_engine = HookEngine(hooks) if hooks else None

    provider = config.providers[0]
    try:
        client = create_client(provider)
    except Exception as error:
        print(f"SeaCode client error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    # 解析权限模式：--mode 优先，否则采用配置中的默认权限模式。
    permission_mode = PermissionMode(mode_str or config.permission_mode)

    # 装配默认工具注册表与权限检查器；sandbox_enabled 关闭让 Bash 走常规确认。
    # -p 模式下无法弹 HITL 对话框，PermissionRequest 事件在事件循环中自动批准。
    registry = create_default_registry()
    cwd = os.getcwd()
    sandbox = PathSandbox(project_root=cwd)
    detector = DangerousCommandDetector()
    # 三层规则路径：用户级 ~/.seacode/permissions.yaml、项目级 .seacode/permissions.yaml、
    # 本地级 .seacode/permissions.local.yaml；不存在时该层为空。
    from pathlib import Path

    home = str(Path.home())
    rule_engine = RuleEngine(
        user_rules_path=Path(home) / ".seacode" / "permissions.yaml",
        project_rules_path=Path(cwd) / ".seacode" / "permissions.yaml",
        local_rules_path=Path(cwd) / ".seacode" / "permissions.local.yaml",
    )
    checker = PermissionChecker(
        detector=detector,
        sandbox=sandbox,
        rule_engine=rule_engine,
        mode=permission_mode,
        sandbox_enabled=False,
    )

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=os.getcwd(),
        max_iterations=_read_max_steps(),
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=load_instructions(cwd),
        hook_engine=hook_engine,
    )

    # 注册高级工具：ToolSearch / AgentTool / TeamCreate / TeamDelete。
    # 非交互运行同样需要完整 worktree 装配，团队成员才能在隔离目录中启动。
    from types import SimpleNamespace

    from .agents.loader import AgentLoader
    from .agents.task_manager import TaskManager
    from .agents.trace import TraceManager
    from .teams.manager import TeamManager
    from .tools.agent_tool import AgentTool
    from .tools.team_create import TeamCreateTool
    from .tools.team_delete import TeamDeleteTool
    from .tools.tool_search import ToolSearchTool
    from .worktree import WorktreeManager

    trace_manager = TraceManager()
    task_manager = TaskManager()
    agent_loader = AgentLoader(Path(cwd))
    agent_loader.load_all()
    worktree_manager = WorktreeManager(
        repo_root=cwd,
        symlink_directories=list(config.worktree.symlink_directories),
    )
    team_manager = TeamManager(
        worktree_manager=worktree_manager, trace_manager=trace_manager
    )
    teams_config = SimpleNamespace(
        teammate_mode="in-process",
        enable_coordinator_mode=False,
    )
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))
    agent_tool = AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=False,
        provider_config=provider,
        worktree_manager=worktree_manager,
        team_manager=team_manager,
    )
    registry.register(agent_tool)
    registry.register(TeamCreateTool(agent, team_manager, teams_config))
    registry.register(TeamDeleteTool(agent, team_manager))

    # Lead 邮箱 draining：teammate 完成/空闲时通知会写入 lead mailbox，
    # agent 在每轮开始时通过 notification_fn 取出并注入为 system-reminder。
    def drain_mailbox_only() -> list[str]:
        return team_manager.drain_lead_mailbox()

    agent.notification_fn = drain_mailbox_only

    # Windows 默认 GBK 无法编码 emoji 等 Unicode 字符，强制 stdout 用 UTF-8。
    try:
        # TextIO 在类型存根中没有 reconfigure，但 CPython 运行时存在该方法。
        _reconfigure = getattr(sys.stdout, "reconfigure", None)
        if _reconfigure is not None:
            _reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    is_stream_json = output_format == "stream-json"
    final_text = ""

    def emit_json(obj: dict) -> None:
        """输出一行 NDJSON 到 stdout。"""
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    conv = ConversationManager()
    conv.add_user_message(prompt)

    start = time.monotonic()
    text_buf = ""
    total_input = 0
    total_output = 0
    tool_calls: list[dict] = []

    try:
        # 消费 agent.run() 事件流；text 模式累积最终文本，stream-json 模式逐事件 emit。
        async for event in agent.run(conv):
            if isinstance(event, StreamText):
                text_buf += event.text
                if is_stream_json:
                    emit_json({"type": "assistant", "text": event.text})
            elif isinstance(event, ThinkingText):
                if is_stream_json:
                    emit_json({"type": "thinking", "text": event.text})
            elif isinstance(event, ToolUseEvent):
                tool_calls.append({"name": event.tool_name, "is_error": False})
                if is_stream_json:
                    emit_json({
                        "type": "tool_use",
                        "tool_name": event.tool_name,
                        "tool_id": event.tool_id,
                        "args": event.arguments,
                    })
            elif isinstance(event, ToolResultEvent):
                # 回填最后一个同名 tool_call 的 is_error 状态。
                if tool_calls:
                    tool_calls[-1]["is_error"] = event.is_error
                if is_stream_json:
                    emit_json({
                        "type": "tool_result",
                        "tool_name": event.tool_name,
                        "tool_id": event.tool_id,
                        "output": event.output,
                        "is_error": event.is_error,
                        "elapsed": round(event.elapsed, 3),
                    })
            elif isinstance(event, UsageEvent):
                total_input = event.input_tokens
                total_output = event.output_tokens
                if is_stream_json:
                    emit_json({
                        "type": "usage",
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                    })
            elif isinstance(event, TurnComplete):
                if is_stream_json:
                    emit_json({"type": "turn_complete", "turn": event.turn})
            elif isinstance(event, LoopComplete):
                # 最终结果：stream-json 输出 result 行；json 在轮询完成后输出单对象。
                elapsed_ms = int((time.monotonic() - start) * 1000)
                final_text = text_buf
                if is_stream_json:
                    emit_json({
                        "type": "result",
                        "result": text_buf,
                        "duration_ms": elapsed_ms,
                        "num_turns": event.total_turns,
                        "tool_calls": tool_calls,
                        "usage": {
                            "input_tokens": total_input,
                            "output_tokens": total_output,
                        },
                        "stop_reason": "end_turn",
                    })
                elif output_format == "text":
                    sys.stdout.write(text_buf)
                    if not text_buf.endswith("\n"):
                        sys.stdout.write("\n")
                break
            elif isinstance(event, ErrorEvent):
                if is_stream_json:
                    emit_json({"type": "error", "message": event.message})
                else:
                    print(f"Error: {event.message}", file=sys.stderr, flush=True)
            elif isinstance(event, CompactNotification):
                if is_stream_json:
                    emit_json({"type": "compact", "message": event.message})
            elif isinstance(event, RetryEvent):
                if is_stream_json:
                    emit_json({"type": "retry", "reason": event.reason})
            elif isinstance(event, PermissionRequest):
                # -p 非交互模式：自动批准所有权限请求，避免事件循环阻塞。
                event.future.set_result(PermissionResponse.ALLOW)
    except Exception as error:
        if output_format in {"json", "stream-json"}:
            emit_json({"type": "error", "message": str(error)})
        else:
            print(f"SeaCode run error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    # 团队轮询：如果 -p 模式创建了团队，LoopComplete 后 teammate 可能仍在运行。
    # 轮询收取 teammate 完成通知与 lead 邮箱消息，注入为 system-reminder 后
    # 用 run_to_completion 让 Lead 处理通知并继续，直到所有 teammate 完成。
    if team_manager._teams:
        for _ in range(90):
            await asyncio.sleep(2)
            running = any(not t.done() for t in task_manager._async_tasks.values())
            notes: list[str] = []
            for t in task_manager.poll_completed():
                notes.append(
                    f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                    f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                    f"</task-notification>"
                )
            notes.extend(team_manager.drain_lead_mailbox())
            if not notes:
                if not running:
                    break
                continue
            for note in notes:
                conv.add_system_reminder(note)
            # 后续 team 轮询用 run_to_completion，避免重复事件流输出。
            last_result = await agent.run_to_completion(
                "Teammate notifications received. Process them and continue.", conv
            )
            final_text = last_result
            if is_stream_json:
                emit_json({"type": "assistant", "text": last_result})
            elif output_format == "text":
                print(last_result, flush=True)

    if output_format == "json":
        print(json.dumps({"text": final_text}, ensure_ascii=False), flush=True)


# batch14：teammate worker 入口；加载 config → 注册 self/lead 名字 → 构造 Agent → spawn。
# 团队不存在时记 error 退出；任一步失败不抛异常到上层，避免 worker 进程崩溃。
async def _run_teammate(team_name: str, agent_name: str) -> None:
    from .agent import Agent
    from .client import create_client
    from .teams.manager import TeamManager
    from .teams.registry import AgentNameRegistry
    from .teams.spawn_inprocess import LEAD_NAME, spawn_inprocess_teammate
    from .tools import create_default_registry
    from .tools.send_message import SendMessageTool

    try:
        config = load_config()
    except ConfigError as error:
        log.error("SeaCode configuration error: %s", error)
        return

    # 默认用第一个 Provider；无 Provider 时退出。
    if not config.providers:
        log.error("no provider configured for teammate worker")
        return
    provider = config.providers[0]
    try:
        client = create_client(provider)
    except Exception as error:
        log.error("create client failed: %s", error)
        return

    team_manager = TeamManager(worktree_manager=None, trace_manager=None)
    team = team_manager.get_team(team_name)
    if team is None:
        log.error("team %s not found", team_name)
        return

    mailbox = team_manager.get_mailbox(team_name)
    # 生成 worker agent_id；注册 self 与 lead 名字到进程级单例。
    self_agent_id = f"{agent_name}-{uuid4().hex[:8]}"
    registry_inst = AgentNameRegistry.instance()
    registry_inst.register(agent_name, self_agent_id)
    registry_inst.register(LEAD_NAME, team.lead_agent_id)

    # 构造 worker 工具注册表；SendMessageTool 绑定 team_name / from_agent_id / from_agent_name。
    registry = create_default_registry()
    # 先创建 agent 占位，再注册 SendMessageTool 引用 agent。
    # permission_checker=None 等价于 BYPASS（跳过权限检查），worker 在隔离环境不阻塞。
    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=os.getcwd(),
        max_iterations=_DEFAULT_MAX_STEPS,
        context_window=provider.get_context_window(),
        permission_checker=None,
        agent_id=self_agent_id,
        team_name=team_name,
        team_manager=team_manager,
    )
    # 注入 teammate 上下文到系统提示词；通过 _current_definition.system_prompt 传递。
    from .agents.parser import AgentDef

    teammate_def = AgentDef(
        agent_type="teammate",
        when_to_use="default teammate",
        system_prompt=_TEAMMATE_ADDENDUM,
        permission_mode="bypassPermissions",
    )
    agent._current_definition = teammate_def
    # 注册 SendMessageTool；team_name / from_agent_id / from_agent_name 直接传入。
    registry.register(
        SendMessageTool(team_manager, team_name, self_agent_id, agent_name)
    )

    # spawn in-process teammate；task 为空，worker 启动后等待邮箱消息。
    task = ""
    handle = spawn_inprocess_teammate(
        agent,
        task,
        agent_name,
        team_manager,
        mailbox=mailbox,
        lead_agent_id=team.lead_agent_id,
    )
    # 等待主循环结束（shutdown 或异常）。
    await handle.task


# 加载本地配置并启动交互式终端应用。
def main() -> None:
    import argparse

    # batch14：teammate worker 入口；--teammate 标志触发 _run_teammate。
    # 必须在 argparse 之前拦截，走独立的 worker 分支而不是正常 TUI。
    is_teammate, team_name, agent_name = _parse_teammate_flags(sys.argv)
    if is_teammate:
        asyncio.run(_run_teammate(team_name, agent_name))
        return

    from .permissions import PermissionMode

    parser = argparse.ArgumentParser(
        prog="sea", description="SeaCode AI coding assistant"
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        dest="prompt",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Output format for -p mode: 'text' (default) prints final text, "
        "'json' prints one final result object, and 'stream-json' emits NDJSON events",
    )
    args = parser.parse_args()

    # 非交互模式优先：-p / --prompt 触发 _run_prompt，直接执行并输出到 stdout。
    if args.prompt is not None:
        asyncio.run(_run_prompt(args.prompt, args.output_format, args.mode))
        return

    try:
        config = load_config()
    except ConfigError as error:
        print(f"SeaCode configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    permission_mode = PermissionMode(args.mode or config.permission_mode)

    # 加载 Hook 配置；字段级校验失败打印错误并退出，不让 SeaCode 带着错误配置启动。
    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as error:
        print(f"SeaCode hook configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    # 无 Hook 配置时 hook_engine 为 None，Agent 与 App 的注入点都跳过零开销。
    hook_engine = HookEngine(hooks) if hooks else None

    # 启动前尽力拉取 anthropic Provider 的 context window；失败静默降级。
    # 同步入口里跑一次事件循环，拉取超时由 ANTHROPIC_MODEL_FETCH_TIMEOUT 兜底。
    try:
        asyncio.run(_resolve_context_windows_async(config.providers))
    except Exception:
        pass

    from .app import SeaCodeApp
    from .driver import NoAltScreenDriver

    app = SeaCodeApp(
        providers=config.providers,
        max_steps=_read_max_steps(),
        permission_mode=permission_mode,
        hook_engine=hook_engine,
        # batch14：透传团队协调配置；teammate_mode 指定 spawn 后端，
        # enable_coordinator_mode 开启 Lead 工具收敛与协调者提示词。
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
    )
    # 使用自定义 driver 跳过 alternate screen，让 TUI 输出保留在主终端 scrollback。
    app.driver_class = NoAltScreenDriver
    app.run()


if __name__ == "__main__":
    main()
