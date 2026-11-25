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
