"""补全弹窗 CompletionPopup 的单元测试：覆盖显示、导航、选择与隐藏行为。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot

from seacode.commands.completion import CompletionPopup, Selected


# 测试用 App：挂载补全弹窗并捕获 Selected 消息，供 Pilot 驱动测试。
class _PopupApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.popup: CompletionPopup | None = None
        self.selected: list[str] = []

    def compose(self) -> ComposeResult:
        self.popup = CompletionPopup()
        yield self.popup

    # 捕获弹窗发出的 Selected 消息，记录选中值供断言。
    def on_selected(self, event: Selected) -> None:
        self.selected.append(event.value)


# 通过 Pilot 挂载弹窗，使 update/display/post_message 等操作在测试中可用。
@pytest.fixture
async def popup_pilot():
    app = _PopupApp()
    async with app.run_test() as pilot:
        yield pilot


# 取出已挂载的弹窗实例，断言非空后返回以便调用方法。
def _popup(pilot: Pilot) -> CompletionPopup:
    popup = pilot.app.popup
    assert popup is not None
    return popup


# 验证 DEFAULT_CSS 限制弹窗最大高度为 8 行，避免遮挡聊天区。
# 读取类属性字符串，断言含 max-height: 8。
def test_default_css_limits_max_height() -> None:
    assert "max-height: 8" in CompletionPopup.DEFAULT_CSS


# 验证 show_pairs 设置候选对并重置光标到首项且标记可见。
# mount 后调 show_pairs 传入两对，断言内部状态与可见性。
async def test_show_pairs_sets_pairs_and_cursor(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    assert len(popup._pairs) == 2
    assert popup._cursor == 0
    assert popup.is_visible is True


# 验证 show 兼容纯字符串列表，自动构造 display 与 value 相同的对。
# 调 show 传入字符串列表，断言 _pairs 为对应二元组。
async def test_show_accepts_plain_string_list(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show(["a", "b"])
    assert popup._pairs == [("a", "a"), ("b", "b")]


# 验证 hide 清空候选对、重置光标并标记不可见。
# show_pairs 后调 hide，断言内部状态被清空。
async def test_hide_clears_pairs_and_hides(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a")])
    popup.hide()
    assert popup._pairs == []
    assert popup._cursor == 0
    assert popup.is_visible is False


# 验证 move_up 在顶部时不移动光标。
# show_pairs 后光标为 0，调 move_up 断言光标仍为 0。
async def test_move_up_at_top_does_not_move(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    popup.move_up()
    assert popup._cursor == 0


# 验证 move_up 在中间位置向上移动光标。
# 手动设光标为 1 后调 move_up，断言光标变为 0。
async def test_move_up_in_middle_moves_up(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    popup._cursor = 1
    popup.move_up()
    assert popup._cursor == 0


# 验证 move_down 在底部时不移动光标。
# 光标设为最后一项后调 move_down，断言光标不变。
async def test_move_down_at_bottom_does_not_move(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    popup._cursor = 1
    popup.move_down()
    assert popup._cursor == 1


# 验证 move_down 在中间位置向下移动光标。
# show_pairs 后光标为 0，调 move_down 断言光标变为 1。
async def test_move_down_in_middle_moves_down(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    popup.move_down()
    assert popup._cursor == 1


# 验证 move_up/move_down 在空候选列表时不抛异常。
# 不调 show_pairs 直接调 move_up/move_down，断言不抛异常且光标不变。
async def test_move_up_down_empty_list_no_exception(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.move_up()
    popup.move_down()
    assert popup._cursor == 0
    assert popup._pairs == []


# 验证 get_selected 返回当前光标项的值。
# show_pairs 后设光标为 1，断言 get_selected 返回第二项值。
async def test_get_selected_returns_current_value(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    popup._cursor = 1
    assert popup.get_selected() == "b"


# 验证 get_selected 在空列表时返回 None。
# 不调 show_pairs 直接调 get_selected，断言返回 None。
async def test_get_selected_returns_none_for_empty(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    assert popup.get_selected() is None


# 验证 get_selected 在光标越界时返回 None。
# show_pairs 后设光标为 5，断言 get_selected 返回 None。
async def test_get_selected_returns_none_when_cursor_out_of_bounds(
    popup_pilot: Pilot,
) -> None:
    popup = _popup(popup_pilot)
    popup.show_pairs([("a", "a"), ("b", "b")])
    popup._cursor = 5
    assert popup.get_selected() is None


# 验证 is_visible 默认为 False。
# 构造弹窗不调 show_pairs，断言 is_visible 为 False。
async def test_is_visible_defaults_false(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    assert popup.is_visible is False


# 验证 on_click 点击行号后发出 Selected 消息并隐藏。
# show_pairs 后模拟点击第二行，断言捕获到 Selected(value="b") 且弹窗隐藏。
async def test_on_click_emits_selected_message(popup_pilot: Pilot) -> None:
    popup = _popup(popup_pilot)
    app = popup_pilot.app
    popup.show_pairs([("a", "a"), ("b", "b")])
    # on_click 仅读取 event.y，用 SimpleNamespace 模拟点击事件避免构造完整 Click。
    popup.on_click(SimpleNamespace(y=1))
    await popup_pilot.pause()
    assert app.selected == ["b"]
    assert popup.is_visible is False
