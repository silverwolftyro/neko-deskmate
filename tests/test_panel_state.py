"""面板状态计算的测试。

重点钉两件事：

1. **「需要你注意」置顶，不是时间序第一条。** 纯时间序会把待处理项埋掉——
   那是"沉默的前置条件"这个病的又一个变体。
2. **宁少不猜。** 取不到的值不进 state；这里没有"看起来像数据的占位符"。
"""

from __future__ import annotations

import time

from plugin.plugins.agent_deskmate.panel_state import (
    APPROVAL_LIMIT,
    SESSION_LIMIT,
    build_panel_state,
)

NOW = 1_800_000_000.0


def _base(**kw):
    args = dict(
        running=True,
        hook={"state": "ok", "tone": "success", "title": "就绪", "detail": "",
              "action": None},
        roots=["D:\\work"],
        api_key_set=True,
        auto_review_enabled=False,
        authorized_scopes=[],
        explain_verbosity="brief",
        warning_emphasis="first_n",
        warning_first_n=3,
        sessions=[],
        approvals=[],
        now=NOW,
    )
    args.update(kw)
    return build_panel_state(**args)


# ── attention ────────────────────────────────────────────────────────────


def test_stopped_plugin_is_the_first_attention_item() -> None:
    state = _base(running=False)
    assert state["attention"][0]["title"] == "插件已停止"
    assert state["attention"][0]["action"] is None  # 没有一键修复，只能人去启动


def test_hook_problem_outranks_roots() -> None:
    """hook 让整个审批功能失效，后果比"主语说未知"硬，所以排前面。"""
    state = _base(
        roots=[],
        hook={"state": "missing", "tone": "danger", "title": "没装",
              "detail": "装上", "action": "hook_install"},
    )
    assert [a["title"] for a in state["attention"]] == ["没装", "没声明你自己的目录"]


def test_empty_roots_warns_but_is_not_danger() -> None:
    """roots 没配是降级，不是坏——功能在跑，只是主语说未知。"""
    state = _base(roots=[])
    item = state["attention"][0]
    assert item["tone"] == "warning"
    assert item["action"] is None  # 面板上靠切到设置页解决，不是一键


def test_auto_review_enabled_without_signature_is_danger() -> None:
    """配置开着但没签字 = 功能静默不生效。这个状态必须显式。"""
    state = _base(auto_review_enabled=True, authorized_scopes=[])
    titles = [a["title"] for a in state["attention"]]
    assert "自动审查已启用但未确认授权" in titles
    item = next(a for a in state["attention"] if "自动审查" in a["title"])
    assert item["tone"] == "danger"
    assert item["action"] == "approval_authorize"


def test_signed_auto_review_produces_no_attention() -> None:
    state = _base(auto_review_enabled=True, authorized_scopes=["auto_review"])
    assert state["attention"] == []


def test_everything_fine_means_empty_attention() -> None:
    state = _base()
    assert state["attention"] == []


# ── log ──────────────────────────────────────────────────────────────────


def test_attention_and_recent_are_separate_lists() -> None:
    state = _base(
        roots=[],
        approvals=[{
            "created_at": NOW + 100,
            "decision": "approve",
            "tool_name": "Bash",
            "tool_summary": "ls",
            "session_id": "abcdef1234567890",
        }],
    )
    # 2026-11-25 真机截图：roots 提示在「需要你注意」和「最近发生」各出现一次。
    # 修法是两份独立列表——attention 不掺进 recent。
    assert state["recent"][0]["text"].startswith("你允许了")
    assert all("没声明你自己的目录" not in r["text"] for r in state["recent"])
    assert state["attention"][0]["title"] == "没声明你自己的目录"


def test_recent_is_capped() -> None:
    approvals = [
        {"created_at": NOW + i, "decision": "approve", "tool_name": "Bash",
         "tool_summary": "x", "session_id": "s"}
        for i in range(APPROVAL_LIMIT * 4)
    ]
    state = _base(approvals=approvals)
    assert len(state["recent"]) <= APPROVAL_LIMIT


def test_approval_text_names_tool_and_session() -> None:
    long_summary = "rm -rf ./build && git reset --hard && npm ci && npm run build -- --verbose"
    state = _base(approvals=[{
        "created_at": NOW,
        "decision": "deny",
        "tool_name": "Bash",
        "tool_summary": long_summary,
        "session_id": "abcdef1234567890",
    }])
    text = state["recent"][0]["text"]
    assert "拒绝" in text
    assert "Bash" in text
    assert "abcdef12" in text  # 只取前 8 位
    assert "…" in text        # 长摘要被截断（>40 字）


def test_approval_without_summary_still_reads() -> None:
    state = _base(approvals=[{
        "created_at": NOW, "decision": "expire", "tool_name": "Bash",
        "tool_summary": "", "session_id": "",
    }])
    assert "Bash" in state["recent"][0]["text"]


# ── sessions ─────────────────────────────────────────────────────────────


def test_sessions_sorted_by_recency_and_capped() -> None:
    sessions = [
        {"agent": "a", "session": "s%d" % i, "workspace": "",
         "last_event_at": NOW + i, "rounds": 1}
        for i in range(SESSION_LIMIT * 3)
    ]
    state = _base(sessions=sessions)
    assert len(state["sessions"]) == SESSION_LIMIT
    # 最新的在前
    assert state["sessions"][0]["last_event_at"] == NOW + SESSION_LIMIT * 3 - 1
    assert state["session_count"] == SESSION_LIMIT * 3  # 计数不裁，列表才裁


def test_session_short_id_is_eight_chars() -> None:
    state = _base(sessions=[{
        "agent": "claude-code", "session": "547ea690-abd2-47e3",
        "workspace": "D:\\w", "last_event_at": NOW, "rounds": 3,
    }])
    assert state["sessions"][0]["short"] == "547ea690"


# ── 直通字段 ─────────────────────────────────────────────────────────────


def test_settings_fields_pass_through_unchanged() -> None:
    state = _base(
        explain_verbosity="detailed",
        warning_emphasis="always",
        warning_first_n=7,
    )
    assert state["explain_verbosity"] == "detailed"
    assert state["warning_emphasis"] == "always"
    assert state["warning_first_n"] == 7


def test_auto_review_reports_all_three_scopes() -> None:
    state = _base(
        auto_review_enabled=True,
        authorized_scopes=["auto_review", "ai_judgement"],
    )
    assert state["auto_review"]["signed"] is True
    assert state["auto_review"]["advice_signed"] is True
    assert state["auto_review"]["auto_signed"] is False


def test_roots_are_copied_not_aliased() -> None:
    """调用方改自己那份列表不该影响到已经产出的 state。"""
    roots = ["D:\\a"]
    state = _base(roots=roots)
    roots.append("D:\\b")
    assert state["roots"] == ["D:\\a"]


def test_attention_items_carry_when_they_were_noticed() -> None:
    """attention 项要带时间戳，否则日志页只能显示文字不能显示时间。"""
    state = _base(roots=[], now=NOW)
    assert state["attention"][0]["ts"] == NOW
    # 但不该因此混进 recent——两份列表各有用途（2026-11-25 真机截图去过重）。
    assert state["recent"] == []


def test_timestamps_are_floats_not_strings() -> None:
    """面板要能直接 new Date(ts * 1000)；给字符串会静默显示 Invalid Date。"""
    state = _base(sessions=[{
        "agent": "a", "session": "s", "workspace": "",
        "last_event_at": NOW, "rounds": 1,
    }])
    assert isinstance(state["sessions"][0]["last_event_at"], float)


def test_build_is_deterministic_enough_for_a_refresh() -> None:
    """连点两次刷新不该让面板内容跳。"""
    a = _base(sessions=[{
        "agent": "a", "session": "s", "workspace": "",
        "last_event_at": NOW, "rounds": 1,
    }])
    b = _base(sessions=[{
        "agent": "a", "session": "s", "workspace": "",
        "last_event_at": NOW, "rounds": 1,
    }])
    assert a == b


def test_time_is_monotonic_source() -> None:
    """确认 time.time() 量级，别哪天传了毫秒进来。"""
    assert time.time() > 1_700_000_000


# ── last_broadcast_at ────────────────────────────────────────────────────


def test_last_broadcast_is_the_max_over_sessions() -> None:
    state = _base(sessions=[
        {"agent": "a", "session": "s1", "workspace": "",
         "last_event_at": NOW, "last_broadcast_at": NOW + 10},
        {"agent": "a", "session": "s2", "workspace": "",
         "last_event_at": NOW + 5, "last_broadcast_at": NOW + 99},
    ])
    assert state["last_broadcast_at"] == NOW + 99


def test_no_broadcast_yet_stays_zero_not_a_guess() -> None:
    """从没播过就是 0，面板显示「—」。不拿 last_event_at 冒充。"""
    state = _base(sessions=[
        {"agent": "a", "session": "s1", "workspace": "",
         "last_event_at": NOW + 500, "last_broadcast_at": 0.0},
    ])
    assert state["last_broadcast_at"] == 0.0


def test_garbage_broadcast_timestamp_is_ignored() -> None:
    state = _base(sessions=[
        {"agent": "a", "session": "s1", "workspace": "",
         "last_event_at": NOW, "last_broadcast_at": "not-a-number"},
    ])
    assert state["last_broadcast_at"] == 0.0


# ── Agent 卡：会话来自磁盘，不来自事件流水线 ──────────────────────────────


def _agent_row(**kw):
    row = {"id": "claude", "label": "Claude Code", "state": "observable",
           "badge": "full", "detail": "d", "reported": True}
    row.update(kw)
    return row


def _sess(sid, live=False):
    return {"session": sid, "last_seen": NOW, "size_bytes": 100,
            "where": "proj", "live": live}


def test_agents_carry_their_own_sessions():
    state = _base(
        agents=[_agent_row()],
        agent_sessions={"claude": [_sess("a", live=True), _sess("b")]},
    )
    row = state["agents"][0]
    assert row["session_total"] == 2
    assert row["session_live"] == 1
    assert [s["session"] for s in row["sessions"]] == ["a", "b"]


def test_session_list_truncated_but_count_is_full():
    """数字要真，列表要短——3 条以后该有专门一页，不是把状态卡变成文件管理器。"""
    state = _base(
        agents=[_agent_row()],
        agent_sessions={"claude": [_sess("s%d" % i) for i in range(9)]},
    )
    row = state["agents"][0]
    assert row["session_total"] == 9
    assert len(row["sessions"]) == 3


def test_agent_without_sessions_reports_zero():
    state = _base(agents=[_agent_row()], agent_sessions={})
    assert state["agents"][0]["session_total"] == 0
    assert state["agents"][0]["sessions"] == []


def test_pipeline_sessions_never_fill_agent_sessions():
    """**这条是 2026-11-25 真机那个"没在跑？"的回归测试。**

    事件流水线的 ``sessions`` 与磁盘盘的 ``agent_sessions`` 是两条独立的事实。
    老实现只有前者，于是"插件启动后没来新事件"被显示成"还没看到任何会话"，
    用户明明开着 agent 却被告诉没有。两边不许互相冒充。
    """
    state = _base(
        agents=[_agent_row()],
        sessions=[{"agent": "claude", "session": "x" * 8,
                   "last_event_at": NOW, "last_broadcast_at": 0.0}],
        agent_sessions={"claude": []},
    )
    # 流水线有 1 条，磁盘盘出 0 条——Agent 卡必须照实说 0
    assert state["session_count"] == 1
    assert state["agents"][0]["session_total"] == 0


def test_absent_agents_are_dropped_from_panel():
    state = _base(agents=[
        _agent_row(),
        _agent_row(id="workbuddy", label="WorkBuddy", state="absent",
                   badge="absent", reported=False),
    ])
    assert [a["label"] for a in state["agents"]] == ["Claude Code"]


def test_agent_summary_counts_by_badge_not_state():
    """observable 内部还分 full / metadata，合成一个数就比徽标说得漂亮。"""
    state = _base(agents=[
        _agent_row(id="a", badge="full", state="observable"),
        _agent_row(id="b", badge="metadata", state="observable"),
        _agent_row(id="c", badge="blind", state="blind"),
    ])
    assert state["agent_summary"] == {"full": 1, "metadata": 1,
                                      "conditional": 0, "feasible": 0,
                                      "blind": 1}


# ── 播报方式：speak 与 speak_available ───────────────────────────────────


def test_speak_defaults_off():
    """默认只出文字——这是插件一直的行为，不能因为加了开关就变。"""
    assert _base()["speak"] is False


def test_speak_available_when_key_present():
    state = _base(api_key_set=True, speak=False)
    assert state["speak_available"] is True


def test_speak_unavailable_without_key():
    """没 key 时 respond 走不了（红线 3）。控件必须置灰并说清原因，
    而不是"能点、点了不生效"——那正是假控件的定义。"""
    state = _base(api_key_set=False, speak=False)
    assert state["speak_available"] is False


def test_speak_stays_available_once_on():
    """已经开着的不因为后来 key 被撤掉就变成不可用——
    那会让控件显示成"你现在这个设置不合法"，而用户只是想改回去。"""
    state = _base(api_key_set=False, speak=True)
    assert state["speak"] is True
    assert state["speak_available"] is True


# ── B11：被压掉的播报要能在「日志」页看见 ──────────────────────────────
#
# 2026-11-26 真机：用户打开「日志」页看到「还没有记录。」，而插件刚刚
# 静默吞了两条删除播报。根因是 recent 只从 approvals 取——没弹卡就空着，
# 而 B11 压掉的东西既不在这个列表、计数器又只在设置页。
#
# 压的时候静默是对的（B11 的目的就是少打扰），静默到无迹可查是另一回事。


def _approval(ts: float, **kw):
    # 字段名照 ``_approval_text`` 真正读的那几个来：tool_name / tool_summary /
    # session_id。写成 tool / summary 会被静默忽略，文本变成"你未决定了 某个工具"
    # ——那种桩看起来在工作，其实什么都没验。
    row = {"created_at": ts, "state": "pending", "tool_name": "Bash",
           "tool_summary": "审批甲", "session_id": "s", "agent": "a",
           "request_id": "r"}
    row.update(kw)
    return row


def test_suppressed_broadcasts_show_up_in_recent() -> None:
    """压掉的播报必须出现在「最近发生」里，否则用户无从判断该不该开着它。"""
    state = _base(review_log=[{
        "ts": NOW - 10, "kind": "suppressed",
        "text": "没播 · 删除文件：rm -rf build", "severity": "notice",
        "attempts": 1,
    }])
    assert [r["text"] for r in state["recent"]] == ["没播 · 删除文件：rm -rf build"]


def test_a_suppressed_entry_is_not_coloured_like_a_problem() -> None:
    """tone 用 ``info`` 不是 warning/danger。

    这条记录说的是"没有打扰你"，不是"出事了"。给它警告色，用户会以为
    漏了什么——那就把 B11 省下来的注意力又赔进去了。
    """
    state = _base(review_log=[{
        "ts": NOW, "kind": "suppressed", "text": "没播 · x", "severity": "warn",
    }])
    assert state["recent"][0]["tone"] == "info"


def test_recent_mixes_approvals_and_suppressed_by_time() -> None:
    """审批与被压掉的播报**按时间混排**，不是审批永远在上。

    用户要的是"刚才发生了什么"，不是"刚才弹了几张卡"。
    """
    state = _base(
        approvals=[_approval(NOW - 100)],
        review_log=[{"ts": NOW - 1, "kind": "suppressed", "text": "没播 · 新的"},
                    {"ts": NOW - 200, "kind": "suppressed", "text": "没播 · 旧的"}],
    )
    texts = [r["text"] for r in state["recent"]]
    # 三条按时间交错：压制(NOW-1) → 审批(NOW-100) → 压制(NOW-200)。
    # 断言写成"压制总在前面"就验不出混排了。
    assert texts[0] == "没播 · 新的"
    assert "审批甲" in texts[1]
    assert texts[2] == "没播 · 旧的"


def test_recent_is_capped_as_a_whole_list() -> None:
    """上限管的是整份列表，不是分别管两边。

    分别截断的后果：审批 5 条 + 压制 20 条 = 25 条，而这一页本该是扫一眼的。
    """
    from plugin.plugins.agent_deskmate.panel_state import LOG_LIMIT

    state = _base(
        approvals=[_approval(NOW - i) for i in range(APPROVAL_LIMIT)],
        review_log=[{"ts": NOW - 1000 - i, "kind": "suppressed",
                     "text": f"没播 · {i}"} for i in range(LOG_LIMIT)],
    )
    assert len(state["recent"]) == LOG_LIMIT


def test_review_log_entries_without_text_are_dropped() -> None:
    """空文本不进列表——它占一行却什么也不说。"""
    state = _base(review_log=[{"ts": NOW, "kind": "suppressed", "text": ""},
                              {"ts": NOW, "kind": "suppressed", "text": "   "}])
    assert state["recent"] == []


def test_no_review_log_leaves_recent_untouched() -> None:
    """没传 review_log（老调用方）时行为不变，只是没有压制痕迹。"""
    state = _base(approvals=[_approval(NOW)])
    assert len(state["recent"]) == 1
    assert "审批甲" in state["recent"][0]["text"]
