"""让 Yui 查问本机编码 Agent 的活动（0.6.27）。

## 这批测试盯的四件事

1. **脱敏。** ``tool_summary`` 里是**命令原文**——``export TOKEN=xxx``
   这种就在里面，而这份摘要是要送给模型的。密钥形状与用户目录路径
   两条都要挡住。
2. **没有活动时明确说"没有"。** 让 Yui 能直接答"没有"，而不是拿着一份
   空摘要编。用户点名的验收项：误报和漏报在这个工具上一样贵。
3. **只报类别，不报内容。** 和 boundary 通知同一套话——``Signal`` 没有
   能装内容的字段，那是隐私红线的结构性保证。
4. **几百字。** 再长模型会开始复述而不是概括，而用户等的是"一句话说清"。

## 一条不在这个文件里但同样重要的

``@llm_tool`` 的注册**不在** ``__neko_event_meta__`` 那套里（它挂
``__neko_llm_tool_meta__``，运行时存成 ``__llm_tool__{name}`` 动态入口）。
所以 test_smoke 那张入口集合表**不会**因为它变——那是对的，
文档原话：「带这个前缀的入口不会进入 Agent 的插件选择」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.plugins.agent_deskmate import AgentDeskmatePlugin
from plugin.plugins.agent_deskmate.agentquery import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_PARAMETERS,
    empty_answer,
    summarize,
)

NOW = 1_000.0
HOME = r"C:\Users\alpha"


def _session(agent="claude-code", session="s1", light="running", age=10.0,
             kinds=None):
    return SimpleNamespace(
        agent=agent, session=session, light=light, last_event_at=NOW - age,
        round_kinds=dict(kinds or {"Bash": 3}), workspace=None,
    )


def _approval(rid="r1", session="s1", summary="Bash: rm -rf build/",
              state="pending", age=120.0, created=NOW):
    return {"request_id": rid, "agent": "claude-code", "session_id": session,
            "state": state, "created_at": created - age,
            "tool_name": "Bash", "tool_summary": summary,
            "working_directory": None}


def _run(sessions=(), approvals=(), notices=(), home=HOME):
    return summarize(sessions=list(sessions), approvals=list(approvals),
                     notices=list(notices), now=NOW, home=home)


# ── 1. 工具的自我描述（防误调的主杠杆）──────────────────────────────

def test_the_tool_registers_with_the_right_shape() -> None:
    """窄描述 + 只收一句 question。**这条是防误调最强的杠杆**——
    ``query_tasks`` 已经在答"在办什么"，两条工具挨得太近就会互相误调。"""
    meta = getattr(AgentDeskmatePlugin.ask_coding_agents,
                   "__neko_llm_tool_meta__", None)
    assert meta is not None, "@llm_tool 没挂上元数据"
    assert meta.name == TOOL_NAME
    # 名字要能直接拼进 callback URL 路径段
    import re
    assert re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", meta.name)
    # 描述里必须把边界说死：不抢 query_tasks 的活，也别说自己全能
    assert "闲聊不调" in TOOL_DESCRIPTION
    assert "只看得到 Claude Code" in TOOL_DESCRIPTION
    assert "query_tasks" in TOOL_DESCRIPTION
    # **还得拦住 dispatch_task，并且点名数据源**。
    # 2026-11-27 真机三轮：问「刚才」用这个工具；问「在干嘛」派任务
    # （0.6.29 加了「不要为此派任务」，没用）；再问「刚才都在干什么」
    # 又派任务，而派的活是读 ~/.claude/projects——**正是这个工具的数据源**。
    # 所以她不是更信任 dispatch_task，是不知道这工具背后已经读了什么。
    # 描述必须把数据源点名，她才知道那够不够。
    assert "不要派任务" in TOOL_DESCRIPTION
    assert ".claude" in TOOL_DESCRIPTION, "没点名数据源"
    # 只收一句话，不收 id——主对话会话几轮就换一次，给它 id 它记不住
    assert set(TOOL_PARAMETERS["properties"]) == {"question"}
    assert TOOL_PARAMETERS["properties"]["question"]["type"] == "string"
    # question 是可选的：留空返回最近在跑的概况
    assert "required" not in TOOL_PARAMETERS


def test_role_is_none_so_surviving_a_character_switch() -> None:
    """``role=None`` = 对所有猫娘可见。换角色不该让这个工具消失。"""
    meta = getattr(AgentDeskmatePlugin.ask_coding_agents,
                   "__neko_llm_tool_meta__", None)
    assert meta.role is None


def test_the_tool_is_not_in_the_plugin_entry_set() -> None:
    """它**不该**进 ``__neko_event_meta__`` 那套。

    文档原话：「带这个前缀的入口不会进入 Agent 的插件选择，也不会提供给
    task agent，两套 API 不会混在一起」。所以 test_smoke 那张入口集合表
    不因为它变——那是设计，不是漏了什么。
    """
    meta = getattr(AgentDeskmatePlugin.ask_coding_agents,
                   "__neko_event_meta__", None)
    assert meta is None


# ── 2. 脱敏 ───────────────────────────────────────────────────────────

def test_key_shapes_in_a_command_are_redacted() -> None:
    """``tool_summary`` 里是命令原文。这份摘要送给模型，
    所以 ``export TOKEN=xxx`` 必须被打掉。"""
    text = _run(approvals=[_approval(summary="Bash: export TOKEN=sk-live-abc123")])
    assert "sk-live-abc123" not in text


def test_bearer_tokens_are_redacted() -> None:
    text = _run(approvals=[_approval(
        summary="Bash: curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1'")])
    assert "eyJhbGciOiJIUzI1" not in text


def test_the_home_directory_is_replaced() -> None:
    """用户目录路径不该出现在送给模型的文本里——
    它又长又占字数，替换后摘要能多说一件事。"""
    text = _run(approvals=[_approval(
        summary=f"Bash: rm -rf {HOME}/proj/build")])
    assert HOME not in text
    assert "~" in text


def test_the_home_directory_is_replaced_in_both_slash_styles() -> None:
    text = _run(approvals=[_approval(
        summary=f"Bash: rm -rf {HOME.replace(chr(92), '/')}/proj/build")])
    assert HOME.replace("\\", "/") not in text


def test_notice_lines_are_scrubbed_too() -> None:
    """流水一样过脱敏——它是通知原文，也可能带命令。"""
    text = _run(sessions=[_session()],
                notices=["跑完了：rm -rf " + HOME + "/x",
                         "export API_KEY=sk-live-zzz"])
    assert HOME not in text
    assert "sk-live-zzz" not in text


# ── 3. 没有活动时明确说"没有" ─────────────────────────────────────────

def test_nothing_at_all_says_so_plainly() -> None:
    """让 Yui 能直接答"没有"，而不是拿着一份空摘要编。"""
    text = _run()
    assert "没有在跑" in text


def test_empty_answer_says_what_it_cannot_see() -> None:
    """空答案必须自带范围。

    2026-11-27 真机：用户问「有没有在等我」，工具答"没有"，
    而当时屏幕上排着 6 个**喵能力**的审批卡——那些走 NekoPower 委托系统，
    我们的网关看不见。答案是"限定范围内正确、作为一般回答是误导的"。

    所以空答案得说清"我看不见什么"，Yui 才不会过度泛化。
    """
    answer = empty_answer()
    assert "没有" in answer
    assert "看不见" in answer
    assert "喵能力" in answer


def test_empty_answer_stays_short() -> None:
    assert len(empty_answer()) < 80, "空答案也要短——它是要被念出来的"


def test_only_settled_approvals_counts_as_nothing() -> None:
    """已了结的不算"在等"。"""
    text = _run(approvals=[_approval(state="settled")])
    assert "没有在跑" in text
    assert "在等你" not in text


# ── 4. 内容：只报类别，不报内容 ───────────────────────────────────────

def test_sessions_carry_a_short_id_so_two_are_tellable_apart() -> None:
    """两个 claude-code 分会话，不带短 id 就读不出谁是谁。

    披露级别和 AgentHUD 卡片一样（那儿也显示 8 位），不是新泄。
    """
    text = _run(sessions=[_session(session="aaaaaaaa-aaaa"),
                          _session(session="6d62979e-bbbb")])
    assert "aaaaaaaa" in text
    assert "6d62979e" in text
    # 只要前 8 位：全串没有辨认价值，还占字数
    assert "aaaaaaaa-aaaa" not in text


def test_sessions_report_categories_only() -> None:
    text = _run(sessions=[_session(kinds={"Bash": 7, "Edit": 4})])
    assert "我这边看得见的 Claude Code" in text
    assert "Bash 7 次" in text
    assert "Edit 4 次" in text
    # 不该出现命令原文
    assert "rm -rf" not in text


def test_a_waiting_session_says_it_is_waiting() -> None:
    text = _run(sessions=[_session(light="waiting")])
    assert "在等人" in text


def test_an_error_session_says_so() -> None:
    text = _run(sessions=[_session(light="error")])
    assert "出事了" in text


def test_the_light_enum_is_accepted_as_well_as_a_string() -> None:
    """真 tracker 给的是 ``Light`` 枚举，测试里给裸串——两种都要认。"""
    from plugin.plugins.agent_deskmate.progress import Light

    text = _run(sessions=[SimpleNamespace(
        agent="claude-code", session="s1", light=Light.RUNNING,
        last_event_at=NOW - 10, round_kinds={"Bash": 1}, workspace=None)])
    assert "在跑" in text


def test_idle_and_stale_sessions_are_not_listed() -> None:
    """闲着又久没动静的不列——全列出来就是浪费字数。"""
    text = _run(sessions=[_session(light="idle", age=9999)])
    assert "没有在跑" in text


def test_an_idle_but_recent_session_is_still_listed() -> None:
    """刚闲下来不久的要列——用户问"刚才在干嘛"时它正是答案。"""
    text = _run(sessions=[_session(light="idle", age=30)])
    assert "闲着" in text


def test_pending_approvals_are_listed_with_how_long() -> None:
    text = _run(sessions=[_session()],
                approvals=[_approval(summary="Bash: rm -rf build/", age=180)])
    # 带范围限定：用户问"有没有在等我"时，答案要说清是哪边的审批
    assert "1 个 Claude Code 的审批在等你" in text
    assert "3 分钟" in text


def test_the_most_recent_session_comes_first() -> None:
    """最近的在上面——用户问"刚才在干嘛"时它正是答案。

    摘要**不含会话 id**（那是给眼睛的，不是给模型的，而且占字数），
    所以按年龄串的先后断言。
    """
    text = _run(sessions=[_session(session="old", age=600),
                          _session(session="new", age=5)])
    lines = [ln for ln in text.splitlines() if ln.startswith("- claude-code")]
    assert len(lines) == 2
    assert "不到 1 分钟" in lines[0], lines[0]      # 新的在前
    assert "10 分钟" in lines[1], lines[1]


def test_the_summary_stays_short() -> None:
    """几百字。再长模型会开始复述而不是概括。"""
    from plugin.plugins.agent_deskmate.agentquery import SUMMARY_MAX

    text = _run(sessions=[_session(session=f"s{i}") for i in range(20)],
                approvals=[_approval(rid=f"r{i}", session=f"s{i}")
                           for i in range(20)],
                notices=[f"notice {i}" for i in range(50)])
    assert len(text) <= SUMMARY_MAX


# ── 5. 入口：读内存，不碰 jsonl ───────────────────────────────────────

def _plugin(*, approval=None, tracker=None, notices=()):
    from collections import deque

    plugin = object.__new__(AgentDeskmatePlugin)
    plugin._approval = approval
    plugin._tracker = tracker
    plugin._recent_notices = deque(notices, maxlen=5)
    return plugin


def test_the_entry_returns_an_answer_without_either_service() -> None:
    """审批和 tracker 都没起来也要答话——不能让人看到一个错。"""
    import asyncio

    plugin = _plugin()
    result = asyncio.run(plugin.ask_coding_agents(question="在干嘛"))
    assert result.is_ok()
    assert "没有" in result.value["answer"]


def test_the_entry_reports_what_the_tracker_knows() -> None:
    import asyncio

    plugin = _plugin(tracker=SimpleNamespace(
        sessions=lambda: [_session(kinds={"Bash": 2})]))
    result = asyncio.run(plugin.ask_coding_agents(question=""))
    assert result.is_ok()
    assert "Bash 2 次" in result.value["answer"]


def test_the_entry_ignores_the_question_text() -> None:
    """``question`` 收下来但不解析——那是模型的事。

    在这儿做意图判断就等于再造一个路由器，而误判的代价是 Yui 答非所问。
    """
    import asyncio

    plugin = _plugin(tracker=SimpleNamespace(
        sessions=lambda: [_session(kinds={"Bash": 2})]))
    a = asyncio.run(plugin.ask_coding_agents(question="我的 CC 刚才在干嘛"))
    b = asyncio.run(plugin.ask_coding_agents(question="喵能力在办什么"))
    assert a.value["answer"] == b.value["answer"]


def test_the_entry_accepts_no_arguments_at_all() -> None:
    """``question`` 是可选的：模型可能直接调一个空参。"""
    import asyncio

    plugin = _plugin(tracker=SimpleNamespace(sessions=lambda: []))
    result = asyncio.run(plugin.ask_coding_agents())
    assert result.is_ok()
    assert "没有" in result.value["answer"]
