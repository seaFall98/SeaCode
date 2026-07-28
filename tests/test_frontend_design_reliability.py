"""frontend-design 真实生产工具链的无密钥垂直测试。"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from seacode.agent import Agent, LoopComplete, ToolResultEvent
from seacode.client import (
    LLMClient,
    StreamComplete,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    ToolCallStart,
)
from seacode.conversation import ConversationManager, Message
from seacode.skills import SkillLoader
from seacode.tools import ToolRegistry
from seacode.tools.install_skill import InstallSkill, _InstallSkillParams
from seacode.tools.load_skill import LoadSkill
from seacode.tools.write_file import WriteFile


# 只替换外部模型 Provider，保留 Agent、Skill 与文件工具的真实生产实现。
class _ProviderClient(LLMClient):
    def __init__(self, outcomes: Sequence[Sequence[StreamEvent]]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[tuple[Message, ...]] = []

    # 按预设的 Provider 响应返回事件，记录完整请求用于验证上下文保留。
    async def stream(
        self,
        messages: Sequence[Message],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del system, tools
        self.requests.append(tuple(messages))
        for event in self._outcomes.pop(0):
            yield event


# 将 GitHub Contents API 的文件响应编码为安装器实际消费的 JSON。
def _skill_file_entry(content: bytes) -> dict[str, Any]:
    return {
        "name": "SKILL.md",
        "type": "file",
        "path": "skills/frontend-design/SKILL.md",
        "content": base64.b64encode(content).decode("ascii"),
        "encoding": "base64",
        "size": len(content),
    }


# 收集 Agent 事件，等待真实工具调用链结束。
async def _collect(agent: Agent, conversation: ConversationManager) -> list[Any]:
    return [event async for event in agent.run(conversation)]


# 验证项目级安装、Skill 激活、Agent 上下文与 WriteFile 真实调用链。
# GitHub 与模型是唯一替换的外部边界；最终断言检查真实文件和后续请求上下文。
@pytest.mark.asyncio
async def test_frontend_design_project_install_load_and_write_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_skills = tmp_path / ".seacode" / "skills"
    user_skills = tmp_path / "user-skills"
    loader = SkillLoader(project_dir=project_skills, user_dir=user_skills)
    loader.load_all()
    reload_calls: list[int] = []
    loader.register_reload_callback(lambda: reload_calls.append(1))

    skill_body = (
        b"---\n"
        b"name: frontend-design\n"
        b"description: Build an editorial fashion commerce interface\n"
        b"---\n"
        b"Build a young-trend fashion e-commerce homepage with a minimal "
        b"editorial visual direction.\n"
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/contents/skills/frontend-design")
        return httpx.Response(200, json=[_skill_file_entry(skill_body)])

    real_async_client = httpx.AsyncClient

    def make_http_client(**kwargs: Any) -> httpx.AsyncClient:
        del kwargs
        return real_async_client(transport=httpx.MockTransport(github_handler))

    monkeypatch.setattr("seacode.skills.install.httpx.AsyncClient", make_http_client)

    install_tool = InstallSkill()
    install_tool.set_loader(loader)
    install_result = await install_tool.execute(
        _InstallSkillParams(
            url="https://github.com/acme/design/tree/main/skills/frontend-design"
        )
    )

    installed_dir = project_skills / "frontend-design"
    assert install_result.is_error is False
    assert str(installed_dir) in install_result.content
    assert reload_calls == [1]
    assert loader.get_source_label("frontend-design") == "project"
    assert loader.get("frontend-design") is not None

    user_install_result = await install_tool.execute(
        _InstallSkillParams(
            url="https://github.com/acme/design/tree/main/skills/frontend-design",
            scope="user",
        )
    )
    assert user_install_result.is_error is False
    assert str(user_skills / "frontend-design") in user_install_result.content
    assert (user_skills / "frontend-design" / "SKILL.md").is_file()
    assert reload_calls == [1, 1]
    assert loader.get_source_label("frontend-design") == "project"

    provider = _ProviderClient(
        [
            [
                ToolCallStart(tool_name="LoadSkill", tool_id="load-1"),
                ToolCallComplete(
                    tool_id="load-1",
                    tool_name="LoadSkill",
                    arguments={"name": "frontend-design"},
                ),
                StreamComplete(input_tokens=1, output_tokens=1),
            ],
            [
                ToolCallStart(tool_name="WriteFile", tool_id="write-1"),
                ToolCallComplete(
                    tool_id="write-1",
                    tool_name="WriteFile",
                    arguments={
                        "file_path": str(tmp_path / "testfrontend-design" / "index.html"),
                        "content": "<main>Editorial fashion commerce</main>",
                    },
                ),
                StreamComplete(input_tokens=1, output_tokens=1),
            ],
            [TextDelta("The homepage source is ready."), StreamComplete()],
        ]
    )
    registry = ToolRegistry()
    agent = Agent(
        client=provider,
        registry=registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
    )
    load_tool = LoadSkill()
    load_tool.set_loader(loader)
    load_tool.set_agent(agent)
    registry.register(load_tool)
    registry.register(WriteFile())

    conversation = ConversationManager()
    conversation.add_user_message(
        "Use frontend-design to build a young-trend minimal editorial fashion "
        "e-commerce homepage in testfrontend-design."
    )
    events = await _collect(agent, conversation)

    output_file = tmp_path / "testfrontend-design" / "index.html"
    assert isinstance(events[-1], LoopComplete)
    assert output_file.read_text(encoding="utf-8") == (
        "<main>Editorial fashion commerce</main>"
    )
    assert len([event for event in events if isinstance(event, ToolResultEvent)]) == 2
    assert "frontend-design" in agent.active_skills
    assert any(
        any(
            message.tool_results
            and "# Skill: frontend-design" in message.tool_results[0].content
            for message in request
        )
        for request in provider.requests[1:]
    )
