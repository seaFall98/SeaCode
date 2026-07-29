"""CLI prompt 入口与输出格式回归。"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from seacode.__main__ import _run_prompt, main
from seacode.agent import LoopComplete
from seacode.client import LLMClient, StreamComplete, StreamEvent, TextDelta
from seacode.config import AppConfig, MCPServerConfig, ProviderConfig
from seacode.conversation import Message


class _FakeClient(LLMClient):
    """提供单次文本结果，不连接外部服务。"""

    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del messages, system, tools
        yield TextDelta("completed")
        yield StreamComplete(input_tokens=3, output_tokens=2)


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol="openai-compat",
        model="test-model",
        base_url="https://example.invalid",
        api_key="test-key",
    )


# 验证已有 --prompt 别名和 json 输出格式可被 CLI 入口接受。
# 替换运行协程，仅检查参数解析与参数传递，不触发真实模型请求。
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["sea", "-p", "verify", "--output-format", "text"], ("verify", "text", None)),
        (["sea", "--prompt", "verify", "--output-format", "json"], ("verify", "json", None)),
        (
            ["sea", "--prompt", "verify", "--output-format", "stream-json"],
            ("verify", "stream-json", None),
        ),
    ],
)
def test_main_accepts_prompt_alias_and_all_output_formats(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: tuple[str, str, str | None],
) -> None:
    received: list[tuple[str, str, str | None]] = []

    async def fake_run_prompt(
        prompt: str, output_format: str, mode: str | None
    ) -> None:
        received.append((prompt, output_format, mode))

    monkeypatch.setattr("seacode.__main__._run_prompt", fake_run_prompt)
    monkeypatch.setattr(sys, "argv", argv)

    main()

    assert received == [expected]


# 验证 json 格式返回一份可供脚本读取的最终结果对象。
# 使用真实 Agent Loop 与本地假客户端，避免仅断言分支文本而遗漏运行时输出。
@pytest.mark.asyncio
async def test_prompt_json_output_is_single_final_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    provider = _provider()
    config = AppConfig(providers=(provider,))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)
    monkeypatch.setattr("seacode.client.create_client", lambda _: _FakeClient())

    await _run_prompt("verify", "json", None)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"text": "completed"}


# 验证 -p 装配的团队工具可创建成员并为成员提供 worktree 管理器。
# 替换模型与 worktree 外部边界，执行 TeamCreate 和带 team_name 的 Agent 调用验证真实装配链路。
@pytest.mark.asyncio
async def test_prompt_runtime_starts_team_member_with_worktree_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    provider = _provider()
    config = AppConfig(providers=(provider,))
    agents: list[Any] = []

    class _PromptAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.client = kwargs["client"]
            self.registry = kwargs["registry"]
            self.protocol = kwargs["protocol"]
            self.context_window = kwargs["context_window"]
            self.agent_id = f"agent-{len(agents)}"
            self._full_registry: Any = None
            self._team_manager: Any = None
            self.coordinator_mode = False
            self.team_result: Any = None
            agents.append(self)

        def set_full_registry(self, registry: Any) -> None:
            self._full_registry = registry

        async def run(self, conversation: Any) -> AsyncIterator[Any]:
            del conversation
            create = self.registry.get("TeamCreate")
            created = await create.execute(
                SimpleNamespace(team_name="demo", description="test")
            )
            assert not created.is_error
            member = self.registry.get("Agent")
            self.team_result = await member.execute(
                SimpleNamespace(
                    team_name="demo",
                    name="worker",
                    prompt="inspect",
                    description="inspect files",
                    subagent_type="",
                    run_in_background=False,
                    model=None,
                ),
                conversation=None,
                parent_agent=self,
            )
            yield LoopComplete(total_turns=1)

    class _WorktreeManager:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def create(self, name: str, base_branch: str) -> Any:
            return SimpleNamespace(
                name=name,
                path=str(tmp_path / "worktree"),
                branch="worktree-test",
                based_on=base_branch,
                head_commit="abc123",
                created=datetime.now(),
            )

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)
    monkeypatch.setattr("seacode.client.create_client", lambda _: object())
    monkeypatch.setattr("seacode.agent.Agent", _PromptAgent)
    monkeypatch.setattr("seacode.worktree.WorktreeManager", _WorktreeManager)
    monkeypatch.setattr("seacode.__main__.asyncio.sleep", no_sleep)
    monkeypatch.setattr(
        "seacode.teams.spawn_inprocess.spawn_inprocess_teammate",
        lambda *args, **kwargs: SimpleNamespace(task=None, progress=None),
    )

    await _run_prompt("start a team", "text", None)

    lead = agents[0]
    assert lead.team_result is not None
    assert not lead.team_result.is_error
    member_tool = lead.registry.get("Agent")
    assert member_tool.worktree_manager is not None
    team = member_tool.team_manager.get_team("demo")
    assert team is not None
    assert [member.name for member in team.members] == ["worker"]


# 验证 -p 未传 --mode 时采用配置权限模式，并将项目指令和 Hook 注入 Agent。
# 使用捕获 Agent 替代模型调用，只断言运行时装配的三个输入，避免依赖外部服务。
@pytest.mark.asyncio
async def test_prompt_runtime_uses_config_mode_instructions_and_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    provider = _provider()
    config = AppConfig(
        providers=(provider,),
        permission_mode="acceptEdits",
        raw_hooks=[
            {
                "event": "pre_send",
                "action": {"type": "prompt", "message": "Apply local rules."},
            }
        ],
    )
    created: list[Any] = []

    class _CaptureAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.agent_id = "lead"
            self.registry = kwargs["registry"]
            self._full_registry: Any = None
            self.coordinator_mode = False
            created.append(self)

        def set_full_registry(self, registry: Any) -> None:
            self._full_registry = registry

        async def run(self, conversation: Any) -> AsyncIterator[Any]:
            del conversation
            yield LoopComplete(total_turns=1)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)
    monkeypatch.setattr("seacode.client.create_client", lambda _: object())
    monkeypatch.setattr("seacode.agent.Agent", _CaptureAgent)
    monkeypatch.setattr(
        "seacode.memory.instructions.load_instructions",
        lambda _: "Project instructions",
    )

    await _run_prompt("verify", "text", None)

    agent = created[0]
    assert agent.kwargs["permission_checker"].mode.value == "acceptEdits"
    assert agent.kwargs["instructions_content"] == "Project instructions"
    assert agent.kwargs["hook_engine"] is not None
    assert len(agent.kwargs["hook_engine"].hooks) == 1


# 验证非法 Hook 会在 -p 启动前以明确配置错误终止。
# 构造缺失 action 的 Hook，断言不会在带错误运行配置下继续执行任务。
@pytest.mark.asyncio
async def test_prompt_runtime_rejects_invalid_hook_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = AppConfig(
        providers=(_provider(),), raw_hooks=[{"event": "pre_send"}]
    )
    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)

    with pytest.raises(SystemExit, match="1"):
        await _run_prompt("verify", "text", None)

    assert "hook configuration error" in capsys.readouterr().err.lower()


# 验证普通 TUI 将配置权限模式、可选子 Agent 开关与显式 --mode 一并传入应用。
# 替换应用实例和上下文窗口解析，只检查入口装配结果，不启动真实终端界面。
@pytest.mark.parametrize(
    ("argv", "config_mode", "expected_mode"),
    [
        (["sea"], "acceptEdits", "acceptEdits"),
        (["sea", "--mode", "default"], "acceptEdits", "default"),
        (["sea", "--mode", "acceptEdits"], "default", "acceptEdits"),
        (["sea", "--mode", "plan"], "default", "plan"),
        (["sea", "--mode", "bypassPermissions"], "default", "bypassPermissions"),
    ],
)
def test_main_passes_effective_permission_mode_to_tui(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    config_mode: str,
    expected_mode: str,
) -> None:
    created: list[Any] = []

    class _FakeApp:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.driver_class: Any = None
            created.append(self)

        def run(self) -> None:
            return None

    async def no_context_window_lookup(_: Any) -> None:
        return None

    config = AppConfig(
        providers=(_provider(),),
        permission_mode=config_mode,
        mcp_servers=(
            MCPServerConfig(
                name="codegraph",
                command="codegraph",
                args=("serve", "--mcp"),
            ),
        ),
        enable_fork=True,
        enable_verification_agent=True,
    )
    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)
    monkeypatch.setattr("seacode.__main__._resolve_context_windows_async", no_context_window_lookup)
    monkeypatch.setattr("seacode.app.SeaCodeApp", _FakeApp)
    monkeypatch.setattr(sys, "argv", argv)

    main()

    assert created[0].kwargs["permission_mode"].value == expected_mode
    assert created[0].kwargs["mcp_servers"] == config.mcp_servers
    assert created[0].kwargs["enable_fork"] is True
    assert created[0].kwargs["enable_verification_agent"] is True


# 验证 -p 入口将配置开关传给实际使用的子 Agent 装配。
# 替换模型和加载器，仅检查 Fork 与 Verification 的入口参数，不连接外部服务。
@pytest.mark.asyncio
async def test_prompt_runtime_passes_subagent_feature_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _provider()
    config = AppConfig(
        providers=(provider,),
        enable_fork=True,
        enable_verification_agent=True,
    )
    loaders: list[Any] = []
    agents: list[Any] = []

    class _CaptureLoader:
        def __init__(self, work_dir: Path, *, enable_verification: bool = False) -> None:
            self.work_dir = work_dir
            self.enable_verification = enable_verification
            loaders.append(self)

        def load_all(self) -> None:
            return None

    class _CaptureAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.registry = kwargs["registry"]
            self._full_registry: Any = None
            self.agent_id = "lead"
            self.coordinator_mode = False
            agents.append(self)

        def set_full_registry(self, registry: Any) -> None:
            self._full_registry = registry

        async def run(self, conversation: Any) -> AsyncIterator[Any]:
            del conversation
            yield LoopComplete(total_turns=1)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)
    monkeypatch.setattr("seacode.client.create_client", lambda _: object())
    monkeypatch.setattr("seacode.agent.Agent", _CaptureAgent)
    monkeypatch.setattr("seacode.agents.loader.AgentLoader", _CaptureLoader)

    await _run_prompt("verify", "text", None)

    assert loaders[0].enable_verification is True
    assert agents[0].registry.get("Agent").enable_fork is True
    assert agents[0]._full_registry is agents[0].registry


# 验证非法 --mode 仍由 argparse 在启动前拒绝。
# 传入未定义模式并断言入口不构造 TUI，保留命令行错误语义。
def test_main_rejects_invalid_permission_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["sea", "--mode", "unsupported"])

    with pytest.raises(SystemExit, match="2"):
        main()


# 验证 --remote 在非交互入口启动浏览器服务而不是终端 TUI。
# 替换远程服务和上下文查询，只断言 CLI 完成了正确的运行时装配。
def test_main_starts_remote_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    config = AppConfig(providers=(provider,))
    created: list[Any] = []

    class _Remote:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            created.append(self)

        async def run(self) -> None:
            return None

    async def no_context_window_lookup(_: Any) -> None:
        return None

    monkeypatch.setattr("seacode.__main__.load_config", lambda: config)
    monkeypatch.setattr("seacode.__main__._resolve_context_windows_async", no_context_window_lookup)
    monkeypatch.setattr("seacode.remote.RemoteServer", _Remote)
    monkeypatch.setattr(sys, "argv", ["sea", "--remote"])

    main()

    assert created[0].kwargs["providers"] == config.providers
    assert created[0].kwargs["mcp_servers"] == config.mcp_servers


# 验证 -p 与 --remote 同时出现时仍优先执行非交互任务。
# 替换两条入口，断言远程服务没有被构造。
def test_main_prompt_takes_priority_over_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, str, str | None]] = []

    async def capture_prompt(prompt: str, output_format: str, mode: str | None) -> None:
        received.append((prompt, output_format, mode))

    monkeypatch.setattr("seacode.__main__._run_prompt", capture_prompt)
    monkeypatch.setattr(sys, "argv", ["sea", "--remote", "-p", "inspect"])

    main()

    assert received == [("inspect", "text", None)]
