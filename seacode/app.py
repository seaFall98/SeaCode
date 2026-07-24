"""SeaCode 第 01 步的紧凑 Textual 对话界面。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .client import (
    AuthenticationError,
    LLMClient,
    LLMError,
    NetworkError,
    ProtocolError,
    RateLimitError,
    StreamComplete,
    TextDelta,
    ThinkingDelta,
    create_client,
)
from .config import ProviderConfig
from .conversation import ConversationManager
from .prompts import SYSTEM_PROMPT


class ChatInput(TextArea):
    """提供 Enter 发送与 Shift+Enter 换行的对话输入框。"""

    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("shift+enter", "newline", "New line", priority=True),
    ]

    class Submitted(TextualMessage):
        """携带已确认发送的非空用户文本。"""

        # 保存本次提交的纯文本内容。
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # 发送非空输入，避免主要操作依赖鼠标按钮。
    def action_submit(self) -> None:
        if self.disabled:
            return
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(text))
            self.clear()

    # 在多行提示中保留显式换行行为。
    def action_newline(self) -> None:
        self.insert("\n")


class SeaCodeApp(App[None]):
    """管理 Provider 选择、单活动回合和可恢复流式呈现。"""

    CSS_PATH = "styles.tcss"
    TITLE = "SeaCode"

    # 初始化当前配置、客户端和单回合状态。
    def __init__(
        self,
        providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
        *,
        client_factory: Callable[[ProviderConfig], LLMClient] = create_client,
    ) -> None:
        super().__init__()
        self._providers = tuple(providers)
        self._client_factory = client_factory
        self._client: LLMClient | None = None
        self._conversation = ConversationManager()
        self._selected_provider: ProviderConfig | None = None
        self._streaming = False

    # 生成三行品牌标题，保留终端对话的既定信息层级。
    @staticmethod
    def _make_banner(work_dir: str = "") -> Text:
        banner = Text()
        banner.append(" /\\___/\\   ", style="bold #d9a441")
        banner.append("SeaCode\n", style="#c7d2d5")
        banner.append("( =o.o= )  ", style="bold #d9a441")
        banner.append(f"{work_dir}\n" if work_dir else "\n", style="#9fb2b6")
        banner.append(" /| ||| |\\ ", style="bold #d9a441")
        return banner

    # 构造标题、选择、聊天、输入和横向状态栏五个既定区域。
    def compose(self) -> ComposeResult:
        yield Static(self._make_banner(), id="title-bar")
        if len(self._providers) > 1:
            with Vertical(id="provider-select"):
                yield Static("Select a model profile", id="select-label")
                yield OptionList(
                    *[
                        Option(f"{provider.name}  [{provider.model}]", id=provider.name)
                        for provider in self._providers
                    ],
                    id="provider-list",
                )
        yield VerticalScroll(id="chat-area")
        with Vertical(id="input-area"):
            yield ChatInput(id="chat-input")
            with Horizontal(id="status-bar"):
                yield Static("Preparing configuration", id="turn-status")
                yield Static("", id="model-label")

    # 根据 Provider 数量进入选择状态或直接准备单一配置。
    def on_mount(self) -> None:
        self.query_one(ChatInput).disabled = True
        if len(self._providers) == 1:
            self._select_provider(self._providers[0])
        else:
            self.query_one("#chat-area").display = False
            self.query_one("#input-area").display = False

    # 接收键盘选择并切换到相应模型配置。
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        selected_name = str(event.option.id)
        provider = next(
            (candidate for candidate in self._providers if candidate.name == selected_name),
            None,
        )
        if provider is not None:
            self._select_provider(provider)

    # 建立客户端并让对话界面进入可发送状态。
    def _select_provider(self, provider: ProviderConfig) -> None:
        try:
            self._client = self._client_factory(provider)
        except LLMError as error:
            self._show_startup_error(error)
            return

        self._selected_provider = provider
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        if len(self._providers) > 1:
            self.query_one("#provider-select").display = False
        self.query_one("#title-bar", Static).update(self._make_banner(os.getcwd()))
        self.query_one("#model-label", Static).update(Text(provider.model))
        self._set_status("Ready")
        input_widget = self.query_one(ChatInput)
        input_widget.disabled = False
        input_widget.focus()

    # 在客户端创建失败时展示脱敏启动错误。
    def _show_startup_error(self, error: LLMError) -> None:
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        self._set_status("Configuration error")
        self.call_after_refresh(self._append_error, self._error_message(error))

    # 接收输入消息并把单个活动回合交给 Textual worker 执行。
    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if self._streaming or self._client is None:
            return
        self._streaming = True
        self.run_worker(self._run_turn(event.text), name="conversation", exclusive=True)

    # 执行一条完整流式回合并仅在成功时提交逻辑历史。
    async def _run_turn(self, text: str) -> None:
        client = self._client
        if client is None:
            return

        input_widget = self.query_one(ChatInput)
        input_widget.disabled = True
        self._conversation.begin_turn(text)
        user_message = Text()
        user_message.append("❯ ", style="bold #71b8bc")
        user_message.append(text, style="bold #f2f5f5")
        await self._append_static(user_message, "message user-message")
        live_answer = Static(Text(""), classes="message assistant-message")
        await self.query_one("#chat-area", VerticalScroll).mount(live_answer)
        started = time.monotonic()
        answer = ""
        completed: StreamComplete | None = None
        thinking_widget: Static | None = None
        thinking = ""

        try:
            request_messages = self._conversation.messages_for_request()
            async for event in client.stream(request_messages, SYSTEM_PROMPT):
                if isinstance(event, TextDelta):
                    answer += event.text
                    live_text = Text()
                    live_text.append("● ", style="bold #d9a441")
                    live_text.append(answer)
                    live_answer.update(live_text)
                    self.query_one("#chat-area", VerticalScroll).scroll_end(animate=False)
                elif isinstance(event, ThinkingDelta):
                    thinking += event.text
                    if thinking_widget is None:
                        thinking_widget = Static(
                            Text("Thinking"), classes="message thinking-message"
                        )
                        await self.query_one("#chat-area", VerticalScroll).mount(thinking_widget)
                    thinking_widget.update(Text(f"Thinking\n{thinking}"))
                elif isinstance(event, StreamComplete):
                    completed = event

            if completed is None:
                raise ProtocolError("The provider stream ended without a completion event.")
            self._conversation.complete_turn(answer)
            await live_answer.remove()
            final_answer = answer or "*(The provider completed without text.)*"
            await self.query_one("#chat-area", VerticalScroll).mount(
                Markdown(final_answer, classes="message assistant-markdown")
            )
            elapsed = time.monotonic() - started
            self._set_status(
                f"Ready  {elapsed:.1f}s  "
                f"in {completed.input_tokens} / out {completed.output_tokens}"
            )
        except asyncio.CancelledError:
            self._conversation.abandon_turn()
            await self._append_error(
                "The request was cancelled. You can continue this conversation."
            )
            self._set_status("Ready")
            raise
        except LLMError as error:
            self._conversation.abandon_turn()
            await self._append_error(self._error_message(error))
            self._set_status("Ready")
        except Exception:
            self._conversation.abandon_turn()
            await self._append_error(
                "The request could not be completed. Check the model configuration."
            )
            self._set_status("Ready")
        finally:
            self._streaming = False
            input_widget.disabled = self._client is None
            if not input_widget.disabled:
                input_widget.focus()

    # 在对话区追加一条安全的静态文本消息。
    async def _append_static(self, content: Text, css_class: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.mount(Static(content, classes=css_class))
        chat.scroll_end(animate=False)

    # 在对话区追加不包含原始异常内容的错误消息。
    async def _append_error(self, message: str) -> None:
        await self._append_static(Text(f"✖ {message}"), "message error-message")

    # 将有限错误类别映射成可行动但不泄露细节的文本。
    def _error_message(self, error: LLMError) -> str:
        if isinstance(error, AuthenticationError):
            return "Authentication failed. Check the selected local model configuration."
        if isinstance(error, RateLimitError):
            return "The provider is rate limiting this request. Try again shortly."
        if isinstance(error, NetworkError):
            return "The provider could not be reached. Check the endpoint and network."
        return "The provider returned an unusable response. You can send another message."

    # 在唯一状态栏位置更新当前回合状态、耗时和用量。
    def _set_status(self, text: str) -> None:
        self.query_one("#turn-status", Static).update(Text(text))
