"""「在等你」AgentHUD 常驻面板（0.6.25）。

## 这批测试盯的四件事

1. **N 归零要自己关掉。** 常驻面板最怕变成又一块永远亮着的装饰——
   那正是用户说的「麻木」。
2. **不每秒刷新。** 年龄每秒都在变，跟着刷就是每秒一次 host 投递。
   ``signature()`` 按分钟取整，这条测试钉住"同一分钟内不算变动"。
3. **计数变化要重新 create。** 宿主语义是「用户关掉之后 update 不会
   重新打开」，所以嫌烦关掉面板的用户，必须在真来新事件时再看见它。
4. **没有按钮。** 面板不是审批状态机的第二个写入方——一旦它有按钮，
   就出现了两个能改变审批结果的地方，而两个真源必然漂移。

## 为什么不需要 key

``create_view`` 底层是 ``push_message`` 的 blind 分支，不走模型。
所以零 key 用户也看得见这一栏——它不是"解释类功能"。
"""

from __future__ import annotations

import asyncio

import pytest

from plugin.plugins.agent_deskmate import AgentDeskmatePlugin
from plugin.plugins.agent_deskmate.waiting_view import (
    MAX_ROWS,
    TITLE,
    build_waiting_html,
    build_waiting_summary,
    waiting_state,
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
    def __init__(self, view_id: str) -> None:
        self.id = view_id
        self.updates: list[dict] = []
        self.closes = 0

    async def update(self, **fields) -> None:
        self.updates.append(fields)

    async def close(self) -> None:
        self.closes += 1


class FakeCtx:
    """按 id 记住已创建的视图——``get_view`` 必须找回**同一个**句柄。

    0.6.24 踩过：每次 new 一个新对象，update 落在没人记的对象上，
    断言永远空，测试自己造假阴性。
    """

    def __init__(self) -> None:
        self.views: list[dict] = []
        self.created: list[FakeView] = []

    async def create_view(self, **fields) -> FakeView:
        self.views.append(fields)
        view = FakeView(f"view-{len(self.created) + 1}")
        self.created.append(view)
        return view

    def get_view(self, view_id: str, **_kwargs) -> FakeView:
        for view in self.created:
            if view.id == view_id:
                return view
        raise AssertionError(f"get_view({view_id!r}) 找不到已创建的视图")


class FakeApproval:
    """只实现面板用到的两个方法。"""

    def __init__(self, snapshot: list[dict]) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> list[dict]:
        return self._snapshot


def _row(*, age: float = 0.0, state: str = "pending",
         summary: str = "Bash: rm -rf build/", now: float = 1_000.0):
    return {
        "request_id": f"r{age}",
        "state": state,
        "created_at": now - age,
        "tool_name": "Bash",
        "tool_summary": summary,
        "working_directory": None,
        "session_id": "s1",
        "agent": "claude-code",
    }


def _plugin(snapshot: list[dict], *, on: bool = True):
    from types import SimpleNamespace

    ctx = FakeCtx()
    plugin = object.__new__(AgentDeskmatePlugin)
    plugin.ctx = ctx
    plugin.logger = ListLogger()
    plugin._approval = FakeApproval(snapshot)  # noqa: SLF001
    plugin._waiting_view_on = on  # noqa: SLF001
    plugin._waiting_signature = None  # noqa: SLF001
    plugin._waiting_view_id = None  # noqa: SLF001
    plugin._config_section = SimpleNamespace()
    return plugin, ctx


NOW = 1_000.0


# ── 1. 纯函数层 ────────────────────────────────────────────────────────

def test_no_pending_means_no_state() -> None:
    state = waiting_state([_row(state="settled")], NOW)
    assert state.count == 0
    assert state.rows == ()


def test_empty_snapshot_is_fine() -> None:
    assert waiting_state([], NOW).count == 0


def test_count_and_oldest_age() -> None:
    state = waiting_state([_row(age=10), _row(age=300), _row(age=60)], NOW)
    assert state.count == 3
    assert state.oldest_age == 300


def test_settled_rows_are_not_counted() -> None:
    """已了结的不该再算"在等你"——但它们仍要留在快照里供事后查，
    所以过滤在这儿做，而不是改 gateway。"""
    state = waiting_state([_row(age=5), _row(age=999, state="settled")], NOW)
    assert state.count == 1
    assert state.oldest_age == 5


def test_rows_are_capped() -> None:
    """列长了没人读，而读这个面板的人只想知道"要不要现在去点"。"""
    state = waiting_state([_row(age=i) for i in range(10)], NOW)
    assert len(state.rows) == MAX_ROWS


def test_signature_ignores_sub_minute_age_changes() -> None:
    """同一分钟内不算变动——年龄每秒在变，跟着刷就是每秒一次投递。"""
    a = waiting_state([_row(age=30)], NOW)
    b = waiting_state([_row(age=59)], NOW)   # 30 和 59 都在第 0 分钟
    assert a.signature() == b.signature(), "同一分钟被算成两次变动了"


def test_signature_changes_on_a_new_minute() -> None:
    a = waiting_state([_row(age=30)], NOW)
    b = waiting_state([_row(age=91)], NOW)
    assert a.signature() != b.signature()


def test_signature_changes_on_count() -> None:
    """计数单独一项：它变了就是真事件，该重新 create 让面板再亮一次。"""
    a = waiting_state([_row(age=30)], NOW)
    b = waiting_state([_row(age=30), _row(age=31)], NOW)
    assert a.signature() != b.signature()


def test_signature_changes_when_the_rows_change() -> None:
    """内容换了也要刷——不然面板会说着一批已经点了的事。"""
    a = waiting_state([_row(age=30, summary="Bash: rm -rf build/")], NOW)
    b = waiting_state([_row(age=30, summary="Bash: git push --force")], NOW)
    assert a.signature() != b.signature()


def test_html_has_no_buttons_and_no_scripts() -> None:
    """**没有按钮**：面板不是审批状态机的第二个写入方。

    也不许有脚本——视图跑在禁脚本 iframe 里，写了静默不执行，
    然后所有人去查入口配置，白查一圈。
    """
    html_text = build_waiting_html(waiting_state([_row(age=90)], NOW))
    assert "data-neko-action" not in html_text
    assert "<button" not in html_text
    lowered = html_text.lower()
    for banned in ("<script", "javascript:", "onclick"):
        assert banned not in lowered


def test_html_escapes_the_command_text() -> None:
    """工具摘要里有命令原文；转义是默认动作，不是可选优化。"""
    hostile = "<img src=x onerror=alert(1)>"
    html_text = build_waiting_html(
        waiting_state([_row(age=90, summary=hostile)], NOW))
    assert "<img" not in html_text
    assert "&lt;img" in html_text


def test_rows_show_the_tool_summary_verbatim() -> None:
    """原样显示 ``tool_summary``，**不过 ``fact_line``**。

    0.6.25 开发时踩过：``fact_line`` 吃的是原始命令，喂它 ``tool_summary``
    （"Bash: rm -rf build/" 这种）会解析不出动作，退回
    「未识别的命令：…（看不出它会影响什么）」——又长又全是空话，
    而那句话正是 ``build_card_html`` 自己从卡片上删掉的那类内容
    （注释原话：「同一件事说两遍、其中一遍还是空话，正是 B7 删掉的」）。
    这条测试钉住别再把它请回来。
    """
    state = waiting_state([_row(age=90, summary="Bash: rm -rf build/")], NOW)
    assert state.rows == ("Bash: rm -rf build/",)
    html_text = build_waiting_html(state)
    assert "未识别" not in html_text
    assert "看不出它会影响什么" not in html_text


def test_rows_are_truncated_not_dropped() -> None:
    """超长的摘要截断，不是整行丢掉——丢了这一栏就不知道它在等什么。"""
    long_summary = "Bash: " + "x" * 500
    state = waiting_state([_row(age=90, summary=long_summary)], NOW)
    assert state.rows
    assert len(state.rows[0]) <= 80


def test_html_says_where_to_actually_decide() -> None:
    """面板必须说清"批还是不批回聊天窗点卡片"——不然用户会在这儿找按钮。"""
    html_text = build_waiting_html(waiting_state([_row(age=90)], NOW))
    assert "聊天窗" in html_text


def test_elapsed_wording() -> None:
    from plugin.plugins.agent_deskmate.waiting_view import _elapsed

    assert _elapsed(0) == "不到 1 分钟"
    assert _elapsed(59) == "不到 1 分钟"
    assert _elapsed(60) == "1 分钟"
    assert _elapsed(599) == "9 分钟"
    assert _elapsed(3600) == "1 小时 0 分钟"
    assert _elapsed(3780) == "1 小时 3 分钟"


# ── 2. 三级动作 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_pending_creates_the_view() -> None:
    plugin, ctx = _plugin([_row(age=90)])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001

    assert len(ctx.views) == 1
    fields = ctx.views[0]
    assert fields["title"] == TITLE
    assert "1 个审批在等你" in fields["summary"]
    assert plugin._waiting_view_id == "view-1"  # noqa: SLF001


@pytest.mark.asyncio
async def test_nothing_pending_creates_nothing() -> None:
    plugin, ctx = _plugin([])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    assert ctx.views == []


@pytest.mark.asyncio
async def test_count_dropping_to_zero_closes_the_view() -> None:
    """N 归零**必须**自己关掉——常驻面板最怕变成永远亮着的装饰。"""
    plugin, ctx = _plugin([_row(age=90)])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    assert len(ctx.created) == 1

    # 换成空快照：审批被点了
    plugin._approval = FakeApproval([])  # noqa: SLF001
    await plugin._sync_waiting_view(NOW + 10)  # noqa: SLF001

    assert ctx.created[0].closes == 1
    assert plugin._waiting_view_id is None  # noqa: SLF001
    joined = " ".join(text for _level, text in plugin.logger.lines)
    assert "已收起" in joined


@pytest.mark.asyncio
async def test_closing_an_already_gone_view_is_harmless() -> None:
    """从没建过就别关——``close()`` 对不存在的视图是白投一次。"""
    plugin, ctx = _plugin([])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    assert ctx.created == []


@pytest.mark.asyncio
async def test_sub_minute_age_change_updates_instead_of_recreating() -> None:
    """只有分钟数变了 → update，不新建。

    每分钟都新建会每分钟亮一次圆点，那是另一种打扰。
    """
    plugin, ctx = _plugin([_row(age=90)])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001

    plugin._approval = FakeApproval([_row(age=119)])  # noqa: SLF001
    await plugin._sync_waiting_view(NOW + 1)  # noqa: SLF001

    assert len(ctx.views) == 1, "同一分钟内不该重建"
    assert len(ctx.created[0].updates) == 1


@pytest.mark.asyncio
async def test_a_new_minute_updates_the_text() -> None:
    plugin, ctx = _plugin([_row(age=90)])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    plugin._approval = FakeApproval([_row(age=200)])  # noqa: SLF001
    await plugin._sync_waiting_view(NOW + 110)  # noqa: SLF001

    assert len(ctx.views) == 1
    # now 推后 110、created_at 是 NOW-200 → 实际等了 310 秒 = 5 分钟
    assert "5 分钟" in ctx.created[0].updates[0]["summary"]


@pytest.mark.asyncio
async def test_count_change_recreates_so_a_closed_view_comes_back() -> None:
    """计数变了就重建。

    宿主语义：用户手动关掉之后 ``update()`` 不会重新打开。所以嫌烦关掉
    面板的人，必须在真来新事件时再看见它——重建会亮圆点、不抢焦点
    （0.6.24 真机实测过重复 create 不抢焦点）。
    """
    plugin, ctx = _plugin([_row(age=90)])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001

    plugin._approval = FakeApproval([_row(age=90), _row(age=95)])  # noqa: SLF001
    await plugin._sync_waiting_view(NOW + 5)  # noqa: SLF001

    assert len(ctx.views) == 2, "计数变了必须重建，否则关掉的面版回不来"
    assert plugin._waiting_view_id == "view-2"  # noqa: SLF001


@pytest.mark.asyncio
async def test_unchanged_state_does_nothing() -> None:
    """完全没变就一次投递都不发。"""
    plugin, ctx = _plugin([_row(age=90)])
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    await plugin._sync_waiting_view(NOW + 1)  # noqa: SLF001
    await plugin._sync_waiting_view(NOW + 2)  # noqa: SLF001

    assert len(ctx.views) == 1
    assert ctx.created[0].updates == []


@pytest.mark.asyncio
async def test_the_switch_off_means_nothing_at_all() -> None:
    """``[agent_deskmate.progress].waiting_view = false`` 一票关掉。"""
    plugin, ctx = _plugin([_row(age=90)], on=False)
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    assert ctx.views == []
    assert plugin._waiting_view_id is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_submission_failure_does_not_break_the_main_line() -> None:
    """面板发不出去≠审批不存在：卡片照旧弹、照旧能点。

    这一栏是附赠，不能因为它发不出去就搅了主线——所以异常必须被吃掉，
    且要把 view_id 清掉（留着下次会 update 一个从没建成的视图）。
    """
    from plugin.sdk.plugin import CardSubmissionError

    class FailingCtx(FakeCtx):
        async def create_view(self, **_fields):
            raise CardSubmissionError("本地队列拒了")

    plugin, _ctx = _plugin([_row(age=90)])
    plugin.ctx = FailingCtx()
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001

    assert plugin._waiting_view_id is None  # noqa: SLF001
    joined = " ".join(text for _level, text in plugin.logger.lines)
    assert "本地队列拒了" in joined


@pytest.mark.asyncio
async def test_it_works_without_a_key() -> None:
    """不需要 key：``create_view`` 底层是 blind 投递，不走模型。

    红线 3 管的是"解释/建议类功能"，这一栏只是把已有的事实显示出来，
    所以零 key 用户也看得见。
    """
    plugin, ctx = _plugin([_row(age=90)])
    # 没有 _advisor、没有 key——面板照样该建
    await plugin._sync_waiting_view(NOW)  # noqa: SLF001
    assert len(ctx.views) == 1


def test_summary_is_short_and_plain() -> None:
    """复制/导出用的那一行要短。"""
    state = waiting_state([_row(age=90)], NOW)
    summary = build_waiting_summary(state)
    assert summary == "1 个审批在等你 · 最久的已等 1 分钟"
