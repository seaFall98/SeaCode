"""CLI prompt 入口与输出格式回归。"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from seacode.__main__ import _run_prompt, main
from seacode.client import LLMClient, StreamComplete, StreamEvent, TextDelta
from seacode.config import AppConfig, ProviderConfig
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
