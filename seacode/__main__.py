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

    # 构造 worker 工具注册表；SendMessageTool 需要 parent_agent 引用。
    # parent_agent 用 worker agent 自己（执行时由 agent 传入覆盖）。
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
    # 注册 SendMessageTool；parent_agent 为 worker 自己。
    registry.register(SendMessageTool(agent, team_manager))

    # spawn in-process teammate；task 为空，worker 启动后等待邮箱消息。
    task = ""
    handle = spawn_inprocess_teammate(
        agent, task, agent_name, team_manager, mailbox=mailbox
    )
    # 等待主循环结束（shutdown 或异常）。
    await handle.task


# 加载本地配置并启动交互式终端应用。
def main() -> None:
    # batch14：teammate worker 入口；--teammate 标志触发 _run_teammate。
    is_teammate, team_name, agent_name = _parse_teammate_flags(sys.argv)
    if is_teammate:
        asyncio.run(_run_teammate(team_name, agent_name))
        return

    try:
        config = load_config()
    except ConfigError as error:
        print(f"SeaCode configuration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error

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

    SeaCodeApp(
        providers=config.providers,
        max_steps=_read_max_steps(),
        hook_engine=hook_engine,
        # batch14：透传团队协调配置；teammate_mode 指定 spawn 后端，
        # enable_coordinator_mode 开启 Lead 工具收敛与协调者提示词。
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
    ).run()


if __name__ == "__main__":
    main()
