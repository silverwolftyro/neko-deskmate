"""AgentHUD 任务卡（0.6.26）——照着使魔任务 HUD 的形状，按会话分组。

## 这批测试盯的是什么

第一版把待决审批**平铺成一列**，只回答"有几件在等我"。用户在 2026-11-27
拿了官方使魔任务 HUD 的截图（那才是真正的原本），结构是**任务才是单位、
提问挂在任务下面**——同时回答"谁在跑、在跑什么、跑了多久"。
这一版照那个形状重做，所以测试重点从"列表对不对"变成"分组对不对"：

1. **按 (agent, session) 分组**，两边的会话都进来（tracker 知道谁在跑，
   快照知道谁在等）。只显示有待决的会把这个面板退化回待办清单——
   那正是第一版的问题。
2. **每个按钮的 id 带上 request_id。** ``actions`` 是字典，两条审批共用
   "allow_once" 会互相覆盖，屏幕上只有一个能点。
3. **闲着又久没动静的会话不占版面**，但在跑的、在等的、出事的都留。
4. **没有输入框**——官方那个「补一句」是原生 React 组件的能力，
   我们的 html_card 是禁脚本 iframe + 固定 JSON args。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from plugin.plugins.agent_deskmate.waiting_view import (
    IDLE_AFTER_SECONDS,
    MAX_SESSIONS,
    TITLE,
    WaitingState,
    _elapsed,
    build_waiting_actions,
    build_waiting_html,
    build_waiting_summary,
    session_cards,
)

NOW = 1_000.0


def _session(agent="claude-code", session="s1", light="running",
             age=10.0, kinds=None, workspace=r"D:\work\proj"):
    return SimpleNamespace(
        agent=agent, session=session, light=light,
        last_event_at=NOW - age,
        round_kinds=dict(kinds or {"Bash": 3}),
        workspace=workspace,
    )


def _approval(request_id="r1", agent="claude-code", session="s1",
              summary="Bash: rm -rf build/", working=r"D:\work\proj",
              state="pending", age=60.0):
    return {
        "request_id": request_id, "agent": agent, "session_id": session,
        "state": state, "created_at": NOW - age,
        "tool_name": "Bash", "tool_summary": summary,
        "working_directory": working,
    }


def _cards(approvals=(), sessions=(), now=NOW):
    return session_cards(approvals=list(approvals), sessions=list(sessions),
                         now=now)


def _state(cards, notices=()):
    return WaitingState(cards=tuple(cards), notices=notices)


# ── 1. 分组 ────────────────────────────────────────────────────────────

def test_pending_approvals_group_by_session() -> None:
    cards = _cards(
        approvals=[_approval("r1", session="s1"),
                   _approval("r2", session="s1"),
                   _approval("r3", session="s2")],
        sessions=[_session(session="s1"), _session(session="s2")],
    )
    assert len(cards) == 2
    by_session = {c.session: c for c in cards}
    assert len(by_session["s1"].approvals) == 2
    assert len(by_session["s2"].approvals) == 1


def test_a_running_session_with_nothing_pending_still_shows() -> None:
    """在跑但没待决的是大多数。只显示有待决的就把面板退化回待办清单——
    那正是第一版的问题，也是官方那张图不是那样的原因。"""
    cards = _cards(sessions=[_session(session="s1", light="running")])
    assert len(cards) == 1
    assert cards[0].approvals == ()
    assert cards[0].status == "running"


def test_a_session_only_in_the_snapshot_still_shows() -> None:
    """tracker 没见过但它在等：仍要显示，状态按"在等"算，不假装知道更多。"""
    cards = _cards(approvals=[_approval("r1", session="ghost")], sessions=[])
    assert len(cards) == 1
    assert cards[0].session == "ghost"
    assert cards[0].status == "waiting"
    assert len(cards[0].approvals) == 1


def test_idle_and_stale_sessions_drop_off() -> None:
    """闲着又久没动静的会话不占版面。"""
    fresh = _session(session="fresh", light="idle", age=10)
    stale = _session(session="stale", light="idle",
                     age=IDLE_AFTER_SECONDS + 60)
    cards = _cards(sessions=[fresh, stale])
    assert [c.session for c in cards] == ["fresh"]


def test_an_idle_session_with_a_pending_approval_stays() -> None:
    """灯是 IDLE 但有东西在等：等是更硬的事实，覆盖灯。"""
    cards = _cards(
        approvals=[_approval("r1", session="s1")],
        sessions=[_session(session="s1", light="idle", age=10)],
    )
    assert len(cards) == 1
    assert cards[0].status == "waiting"


def test_settled_approvals_are_not_counted() -> None:
    cards = _cards(
        approvals=[_approval("r1", state="settled"),
                   _approval("r2", state="pending")],
        sessions=[_session()],
    )
    assert sum(len(c.approvals) for c in cards) == 1


def test_waiting_sessions_sort_first() -> None:
    """这个面板首先要回答"要不要现在去点"，所以有待决的排前面。"""
    cards = _cards(
        approvals=[_approval("r1", session="waiting")],
        sessions=[_session(session="waiting", light="running"),
                  _session(session="busy", light="running")],
    )
    assert cards[0].session == "waiting"


def test_sessions_are_capped() -> None:
    """同时开十几个会话的人需要的是另一个工具，这个面板只回答"现在什么情况"。"""
    sessions = [_session(session=f"s{i}") for i in range(MAX_SESSIONS + 4)]
    assert len(_cards(sessions=sessions)) == MAX_SESSIONS


# ── 2. 按钮：每个都带自己的 request_id ─────────────────────────────────

def test_every_button_id_carries_its_own_request_id() -> None:
    """``actions`` 是字典——两条审批共用 "allow_once" 会互相覆盖，
    屏幕上只有一个能点。所以 id 必须前缀 request_id。"""
    cards = _cards(
        approvals=[_approval("r1", session="s1"),
                   _approval("r2", session="s1")],
        sessions=[_session(session="s1")],
    )
    actions = build_waiting_actions(_state(cards))
    assert set(actions) == {
        "always_r1", "session_r1", "once_r1", "deny_r1",
        "always_r2", "session_r2", "once_r2", "deny_r2",
    }, sorted(actions)


def test_button_args_point_at_the_right_request_and_scope() -> None:
    cards = _cards(approvals=[_approval("r9")], sessions=[_session()])
    actions = build_waiting_actions(_state(cards))
    assert actions["always_r9"]["args"] == {
        "request_id": "r9", "decision": "allow", "scope": "always"}
    assert actions["session_r9"]["args"]["scope"] == "session"
    assert actions["once_r9"]["args"] == {
        "request_id": "r9", "decision": "allow", "scope": "once"}
    assert actions["deny_r9"]["args"] == {
        "request_id": "r9", "decision": "deny"}
    # 全部指向同一个入口——不是第二个写入方
    assert {a["entry"] for a in actions.values()} == {"approval_decide"}


def test_html_buttons_match_the_actions_dict() -> None:
    """HTML 里的 ``data-neko-action`` 与 actions 的键一一对应。
    少一个 = 有个按钮点了没反应；多一个 = 有个映射永远用不上。"""
    cards = _cards(
        approvals=[_approval("r1", session="s1"),
                   _approval("r2", session="s1")],
        sessions=[_session(session="s1")],
    )
    html_text = build_waiting_html(_state(cards))
    in_html = set(re.findall(r'data-neko-action="([^"]+)"', html_text))
    assert in_html == set(build_waiting_actions(_state(cards)))


def test_the_four_button_labels_are_the_official_wording() -> None:
    """文案照官方那张图：始终允许 / 这件事里都允许 / 只这一次 / 拒绝。"""
    cards = _cards(approvals=[_approval("r1")], sessions=[_session()])
    html_text = build_waiting_html(_state(cards))
    for label in ("始终允许", "这件事里都允许", "只这一次", "拒绝"):
        assert label in html_text


# ── 3. 没有输入框（官方那个我们够不到）──────────────────────────────

def test_no_input_and_no_script_in_any_html() -> None:
    """没有 ``<input>`` / ``<textarea>``：官方那个「补一句」是原生 React
    组件的能力，我们的 html_card 是禁脚本 iframe + 固定 JSON args。
    文档原话：「不提供通用表单、数据绑定或 JavaScript 运行时」。

    写了的后果不是报错，是**静默不工作**——然后所有人去查入口配置，白查一圈。
    """
    cards = _cards(approvals=[_approval("r1")], sessions=[_session()])
    html_text = build_waiting_html(_state(cards, notices=("出事了",)))
    lowered = html_text.lower()
    for banned in ("<input", "<textarea", "<form", "<script",
                   "javascript:", "onclick"):
        assert banned not in lowered, f"出现了 {banned!r}"


def test_everything_dynamic_is_escaped() -> None:
    hostile = "<img src=x onerror=alert(1)>"
    cards = _cards(
        approvals=[_approval("r1", summary=hostile, working=hostile)],
        sessions=[_session()],
    )
    html_text = build_waiting_html(_state(cards, notices=(hostile,)))
    assert "<img" not in html_text
    assert "&lt;img" in html_text


# ── 4. 状态与计时 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("light,expected", [
    ("running", "运行中"), ("waiting", "等你回话"),
    ("error", "出事了"), ("idle", "闲着"),
])
def test_status_wording_comes_from_the_light(light, expected) -> None:
    cards = _cards(sessions=[_session(light=light)])
    assert cards[0].status == light
    assert expected in build_waiting_html(_state(cards))


def test_elapsed_wording() -> None:
    assert _elapsed(0) == "不到 1 分钟"
    assert _elapsed(59) == "不到 1 分钟"
    assert _elapsed(60) == "1 分钟"
    assert _elapsed(3600) == "1 小时 0 分钟"
    assert _elapsed(3780) == "1 小时 3 分钟"


def test_summary_reports_categories_only() -> None:
    """只报类别（bash 7 次），不报内容——和 boundary 通知同一套话。
    ``Signal`` 没有能装内容的字段，那是隐私红线的结构性保证。"""
    cards = _cards(sessions=[_session(kinds={"Bash": 7, "Edit": 4})])
    assert "Bash 7 次" in cards[0].summary
    assert "Edit 4 次" in cards[0].summary


def test_count_and_oldest_age_span_all_cards() -> None:
    state = _state(_cards(
        approvals=[_approval("r1", session="s1", age=30),
                   _approval("r2", session="s2", age=300)],
        sessions=[_session(session="s1"), _session(session="s2")],
    ))
    assert state.count == 2
    assert state.oldest_age == 300


# ── 5. 节流：什么算真变了 ─────────────────────────────────────────────

def test_a_new_notice_line_updates_but_does_not_rebuild() -> None:
    """加了条流水：**该 update，不该重建**。

    这是两件事，故意分开：
    - signature 包含流水 → 变了就走一次 update，日志刷新；
    - 但重建只看计数（``_sync_waiting_view`` 比的是 ``signature()[0]``）——
      每分钟重建一次就是每分钟亮一次圆点，那是另一种打扰。

    官方那个 HUD 也是这么分的：任务状态天天在变，但不会每分钟重新弹一遍。
    """
    cards = _cards(approvals=[_approval("r1")], sessions=[_session()])
    before = _state(cards)
    after = _state(cards, notices=("出事了",))
    # 签名变 → 会 update
    assert before.signature() != after.signature()
    # 但计数没变 → 不重建
    assert before.signature()[0] == after.signature()[0]


def test_signature_changes_when_the_approval_set_changes() -> None:
    """待决集合变了才重建：那才是"有新的要你点"的真事件。"""
    before = _state(_cards(approvals=[_approval("r1")], sessions=[_session()]))
    after = _state(_cards(approvals=[_approval("r1"), _approval("r2")],
                          sessions=[_session()]))
    assert before.signature() != after.signature()


def test_signature_changes_when_a_session_appears() -> None:
    before = _state(_cards(sessions=[_session(session="s1")]))
    after = _state(_cards(sessions=[_session(session="s1"),
                                    _session(session="s2")]))
    assert before.signature() != after.signature()


# ── 6. 空与汇总 ───────────────────────────────────────────────────────

def test_empty_state_renders_a_plain_line() -> None:
    html_text = build_waiting_html(_state([]))
    assert "没有在跑" in html_text
    assert "data-neko-action" not in html_text


def test_summary_wording() -> None:
    waiting = _state(_cards(approvals=[_approval("r1", age=120)],
                            sessions=[_session()]))
    assert build_waiting_summary(waiting) == "1 个审批在等你 · 最久的已等 2 分钟"
    running = _state(_cards(sessions=[_session(session="s1"),
                                      _session(session="s2")]))
    assert build_waiting_summary(running) == "2 个会话在跑"
    assert build_waiting_summary(_state([])) == "没有在跑的会话"


def test_title_is_stable() -> None:
    """标签页用的是标题，测试钉住它别被随手改——用户已经认得了。"""
    assert TITLE == "同桌猫 · 任务卡"
