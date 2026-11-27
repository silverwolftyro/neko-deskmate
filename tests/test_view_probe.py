"""AgentHUD 视图探针（``ctx.create_view`` 首次接入，2026-11-27）。

## 这批测试的重点不是"能弹出"，是**三件容易静默错掉的事**

1. **按钮 id 与入口 id 对不上。** ``actions`` 的 ``entry``、``@ui.action(id=)``、
   ``@plugin_entry(id=)`` 三处必须完全一致。对不上的表现是"按钮点了没反应"——
   没有异常、没有日志，只是什么都不发生。所以这里直接拿**注册表**对，
   而不是拿一份手写的期望列表对（手写列表会和自己一起错）。
2. **HTML 里混进脚本。** 视图跑在禁脚本的 iframe 里，``onclick`` / ``<script>``
   写了也不执行——于是"按钮没反应"会被误诊成入口问题，其实是脚本没跑。
3. **装饰器跑错地方。** 开发时这三个入口曾插在 ``approval_decide`` 的装饰器与
   它的 ``async def`` 之间，结果那对装饰器落到 ``view_probe`` 头上：
   ``view_probe`` 的 meta.id 变成 ``approval_decide``，真正的 ``approval_decide``
   一个装饰器都没有。第 1 条那个"拿注册表对"的测试当场抓住了它。

## 另一个教训也钉在这里

第一版把 HTML 构造函数叫 ``build_result_html``，和 ``approval.py`` 里已有的
同名函数撞了——``__init__.py`` 早就 import 了那个，我的 import 静默把它遮住。
表现是「按钮点不动时那句人话不出现了」，被
``test_card_advice.py::test_host_error_never_reaches_the_screen`` 抓住。
所以本文件的构造函数一律带 ``probe`` 前缀，并且有一条测试专门盯
``__init__.py`` 里那个原名函数没被换掉。
"""

from __future__ import annotations

import asyncio
import re

import pytest

import plugin.plugins.agent_deskmate as plugin_module
from plugin.plugins.agent_deskmate import AgentDeskmatePlugin
from plugin.plugins.agent_deskmate.approval import build_result_html as approval_result_html  # noqa: F401
from plugin.plugins.agent_deskmate.viewcard import (
    ACTION_ALLOW,
    ACTION_CLOSE,
    ACTION_DENY,
    ENTRY_VIEW_CLOSE,
    ENTRY_VIEW_DECIDE,
    ENTRY_VIEW_PROBE,
    build_probe_result_html,
    build_view_actions,
    build_view_html,
    new_probe_id,
)


class ListLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        try:
            rendered = str(message).format(*args) if args else str(message)
        except (IndexError, KeyError, ValueError):
            rendered = f"{message} | args={args!r}"
        self.lines.append((level, rendered))

    def info(self, message: str, *args: object) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log("warning", message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log("error", message, *args)


class FakeView:
    """记下 create/update/close 被怎么调。"""

    def __init__(self, view_id: str = "view-1") -> None:
        self.id = view_id
        self.updates: list[dict] = []
        self.closes = 0

    async def update(self, **fields) -> None:
        self.updates.append(fields)

    async def close(self) -> None:
        self.closes += 1


class FakeCtx:
    """只实现探针用到的几个宿主方法。

    ``create_error`` 用来模拟 ``CardSubmissionError``——await 只代表本地
    提交成功，失败是抛出来的，不是返回值的。
    """

    def __init__(self, *, create_error: Exception | None = None) -> None:
        self.views: list[dict] = []
        self.created: list[FakeView] = []
        self._create_error = create_error

    async def create_view(self, **fields) -> FakeView:
        if self._create_error is not None:
            raise self._create_error
        self.views.append(fields)
        view = FakeView(f"view-{len(self.created) + 1}")
        self.created.append(view)
        return view

    def get_view(self, view_id: str, **_kwargs) -> FakeView:
        """按 id 找回**同一个**句柄。

        第一版这里每次 new 一个 FakeView，于是 update 落在一个没人记的对象上，
        ``ctx.created[0].updates`` 永远是空的——测试自己造假阴性。
        真机语义就是"拿回同一个视图的发送句柄"，所以这里也必须找回同一个。
        """
        for view in self.created:
            if view.id == view_id:
                return view
        # 没建过就当场炸：静默返回一个新句柄会把"回调打到了一个
        # 不存在的视图上"这件事藏起来。
        raise AssertionError(f"get_view({view_id!r}) 找不到已创建的视图")


def _plugin(ctx: FakeCtx, logger: ListLogger | None = None):
    plugin = object.__new__(AgentDeskmatePlugin)
    plugin.ctx = ctx
    plugin.logger = logger or ListLogger()
    return plugin


def _registered_ids() -> set[str]:
    """宿主注册表里的全部入口 id。**从装饰器元数据读，不手写。"""
    out: set[str] = set()
    for name in dir(AgentDeskmatePlugin):
        obj = getattr(AgentDeskmatePlugin, name, None)
        meta = getattr(obj, "__neko_event_meta__", None) if obj else None
        if meta is not None:
            out.add(meta.id)
    return out


# ── 1. 三处 id 必须对得上（拿注册表对，不拿手写列表对）─────────────────

def test_every_action_entry_is_a_real_registered_entry() -> None:
    """``actions`` 里每个 ``entry`` 都必须是真注册了的入口。

    这是"按钮点了没反应"的唯一防线。**刻意从注册表读**：手写一份期望列表
    的话，列表和自己会一起错——装饰器跑错地方时，手写列表照样绿。
    """
    registered = _registered_ids()
    actions = build_view_actions(new_probe_id())
    assert actions, "一个按钮都没有，探针就废了"
    for button_id, action in actions.items():
        entry = action["entry"]
        assert entry in registered, (
            f"按钮 {button_id!r} 指向的入口 {entry!r} 没注册——"
            "三处 id（actions.entry / @ui.action / @plugin_entry）对不上")


def test_the_three_probe_entries_are_all_registered() -> None:
    """三个入口都真注册了，且都是 plugin_entry（``@ui.action`` 叠在它上面）。"""
    registered = {
        name: getattr(getattr(AgentDeskmatePlugin, name), "__neko_event_meta__")
        for name in dir(AgentDeskmatePlugin)
        if getattr(getattr(AgentDeskmatePlugin, name, None),
                   "__neko_event_meta__", None) is not None
    }
    by_id = {meta.id: meta for meta in registered.values()}
    for entry in (ENTRY_VIEW_PROBE, ENTRY_VIEW_DECIDE, ENTRY_VIEW_CLOSE):
        assert entry in by_id, f"{entry} 没注册"
        assert by_id[entry].event_type == "plugin_entry"


def test_the_decide_and_close_entries_are_not_the_probe_entry() -> None:
    """三个 id 互不相同。

    开发时踩过：插代码插错了位置，``approval_decide`` 的装饰器落到
    ``view_probe`` 头上，于是 ``view_probe`` 的 meta.id 是 ``approval_decide``。
    这条测试在那个 bug 存在时会红（``view_probe`` 会从注册表里消失）。
    """
    assert len({ENTRY_VIEW_PROBE, ENTRY_VIEW_DECIDE, ENTRY_VIEW_CLOSE}) == 3


def test_html_button_ids_match_the_action_keys() -> None:
    """HTML 里的 ``data-neko-action`` 必须和 ``actions`` 的键一一对应。

    少一个 = 有个按钮点了没反应；多一个 = 有个映射永远用不上。
    """
    html = build_view_html("abcd1234")
    in_html = set(re.findall(r'data-neko-action="([^"]+)"', html))
    in_actions = set(build_view_actions("abcd1234"))
    assert in_html == in_actions, (
        f"HTML 有 {sorted(in_html - in_actions)}，actions 有 "
        f"{sorted(in_actions - in_html)}")


# ── 2. 禁脚本 iframe ──────────────────────────────────────────────────

@pytest.mark.parametrize("builder", [
    lambda: build_view_html("abcd1234"),
    lambda: build_probe_result_html(probe_id="abcd1234", decision="allow",
                                    view_id="v", lanlan_name=""),
])
def test_no_script_or_inline_handler_in_any_html(builder) -> None:
    """一段脚本都不许有——视图跑在禁脚本的 iframe 里，写了也不执行。

    写了的后果不是报错，是**静默不工作**：按钮看着在，点了什么都没发生，
    然后所有人去查入口配置，白查一圈。
    """
    html = builder()
    lowered = html.lower()
    for banned in ("<script", "javascript:", "onclick", "onload", "onerror"):
        assert banned not in lowered, f"HTML 里出现了 {banned!r}"


def test_dynamic_content_is_escaped() -> None:
    """探针编号进 HTML 前必须转义。

    它来自 ``uuid4().hex``，理论安全；但这个模块将来会被真数据复用，
    而 ``html.escape`` 是默认动作，不是可选优化。
    """
    hostile = "<img src=x onerror=alert(1)>"
    html = build_probe_result_html(probe_id=hostile, decision="allow",
                                   view_id="v", lanlan_name="")
    assert "<img" not in html
    assert "&lt;img" in html


# ── 3. 入口行为 ───────────────────────────────────────────────────────

def test_view_probe_creates_a_view_with_title_and_three_buttons() -> None:
    ctx = FakeCtx()
    plugin = _plugin(ctx)
    result = asyncio.run(plugin.view_probe({}))

    assert result.is_ok(), result
    assert len(ctx.views) == 1
    fields = ctx.views[0]
    assert fields["title"], "title 是必填的"
    assert fields["html"]
    assert set(fields["actions"]) == {ACTION_ALLOW, ACTION_DENY, ACTION_CLOSE}
    # summary 也要有：复制/导出用它
    assert fields["summary"]
    # view_id 回给调用方，真机上排查靠它
    assert result.value["view_id"] == "view-1"


def test_each_create_gets_a_fresh_probe_id() -> None:
    """两次 create 用不同编号——宿主语义是"替换旧实例"，编号相同就分不清。"""
    ctx = FakeCtx()
    plugin = _plugin(ctx)
    asyncio.run(plugin.view_probe({}))
    asyncio.run(plugin.view_probe({}))
    first, second = ctx.views
    assert first["summary"] != second["summary"]
    assert len(ctx.created) == 2


def test_view_probe_reports_submission_failure_as_err() -> None:
    """提交失败要变成 ``Err`` + 一句人话，不是把异常甩到界面上。"""
    from plugin.sdk.plugin import CardSubmissionError

    ctx = FakeCtx(create_error=CardSubmissionError("本地队列拒了"))
    plugin = _plugin(ctx)
    result = asyncio.run(plugin.view_probe({}))

    assert result.is_err()
    assert "视图没提交成功" in str(result.err())
    # 真实原因进日志
    joined = " ".join(text for _level, text in plugin.logger.lines)
    assert "本地队列拒了" in joined


def test_the_callback_echoes_the_args_it_received() -> None:
    """**用户要验的第三条**：args 到得了，并且原样回显到卡上。"""
    ctx = FakeCtx()
    plugin = _plugin(ctx)
    asyncio.run(plugin.view_probe({}))       # 先建视图，和真机同一条路
    result = asyncio.run(plugin.view_probe_decide(
        "abcd1234", "allow",
        {"view_id": "view-1", "lanlan_name": "Yui"}))

    assert result.is_ok(), result
    view = ctx.created[0]
    assert len(view.updates) == 1
    fields = view.updates[0]
    assert "已允许" in fields["html"]
    assert "abcd1234" in fields["html"]
    assert "decision = allow" in fields["html"]
    assert "view_id  = view-1" in fields["html"]
    assert "lanlan   = Yui" in fields["html"]
    # 决定完了清空按钮——留着一排可点的会让人以为还能改
    assert fields["actions"] == {}


def test_the_callback_denies_too() -> None:
    ctx = FakeCtx()
    plugin = _plugin(ctx)
    asyncio.run(plugin.view_probe({}))
    result = asyncio.run(plugin.view_probe_decide(
        "abcd1234", "deny", {"view_id": "view-1"}))
    assert result.is_ok()
    assert "已拒绝" in ctx.created[0].updates[0]["html"]


def test_missing_view_id_is_an_err_not_a_crash() -> None:
    """宿主没给 ``view_id`` = 契约被破坏，要明确报错。

    不能吞掉换成"成功"——那会让用户以为卡片改了，而它没有。
    """
    ctx = FakeCtx()
    plugin = _plugin(ctx)
    result = asyncio.run(plugin.view_probe_decide("abcd1234", "allow", {}))
    assert result.is_err()
    assert "view_id" in str(result.err())
    joined = " ".join(text for _level, text in plugin.logger.lines)
    assert "没拿到 view_id" in joined


def test_update_failure_still_reports_err() -> None:
    """参数收到了但卡片没改成——这是两件事，都要说清。"""
    from plugin.sdk.plugin import CardSubmissionError

    class FailingCtx(FakeCtx):
        def get_view(self, view_id, **_kw):
            view = FakeView(view_id)

            async def boom(**_fields):
                raise CardSubmissionError("投递失败")

            view.update = boom  # type: ignore[method-assign]
            return view

    plugin = _plugin(FailingCtx())
    result = asyncio.run(plugin.view_probe_decide(
        "abcd1234", "allow", {"view_id": "view-1"}))
    assert result.is_err()
    assert "参数收到了" in str(result.err())


def test_close_then_update_does_not_reopen() -> None:
    """``close()`` 之后**照样**再 update 一次——用户看不到那行字才对。

    这是宿主语义的探针：close 结束的是这次显示实例，后续 update 提交得动
    但不会重新打开、不会抢焦点。如果哪天它重新打开了，这条测试仍然绿
    （它只断言"我们确实这么发了"），要靠真机看——所以真机那一轮
    专门列了"关掉之后还看得见吗"。
    """
    ctx = FakeCtx()
    plugin = _plugin(ctx)
    asyncio.run(plugin.view_probe({}))
    result = asyncio.run(plugin.view_probe_close(
        "abcd1234", {"view_id": "view-1", "lanlan_name": "Yui"}))
    assert result.is_ok()
    view = ctx.created[0]
    assert view.closes == 1
    assert len(view.updates) == 1
    assert "看不到" in view.updates[0]["html"]


# ── 4. 没遮住 approval 那个同名函数 ──────────────────────────────────

def test_the_approval_result_html_is_still_the_positional_one() -> None:
    """``__init__.py`` 里 ``build_result_html`` 必须还是 ``approval`` 那个。

    第一版我把探针的构造函数也叫 ``build_result_html`` 并 import 进
    ``__init__.py``，**静默遮住了**早就 import 好的 ``approval.build_result_html``
    （那个收位置参数，我的是关键字专用）。表现：
    ``_note_button_unavailable()`` 那句「这个按钮暂时不可用」不再出现——
    一个 TypeError 被 ``except Exception`` 吞了。
    是 ``test_card_advice.py::test_host_error_never_reaches_the_screen``
    当场抓住的。这条测试把那个教训钉在这里。
    """
    from plugin.plugins.agent_deskmate import build_result_html as wired

    # approval 那个收一个位置参数、返回包着 <section> 的一句话
    html = wired("这个按钮暂时不可用，请稍后再试。")
    assert "这个按钮暂时不可用" in html
    assert "<section" in html
    # 探针那个是关键字专用，喂位置参数必须炸——炸了才说明没被换掉
    with pytest.raises(TypeError):
        build_probe_result_html("这个按钮暂时不可用")  # type: ignore[arg-type]


def test_probe_and_approval_builders_are_different_objects() -> None:
    assert build_probe_result_html is not approval_result_html
    assert plugin_module.build_result_html is approval_result_html
