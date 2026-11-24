"""P1c(a)：闸门自己造成的失败，登记簿的单测。

2026-11-24 真机：审批 570s 到期 → Claude 收到 ``tool_result is_error=true``
→ 120 秒后连发三条「出事了 / Bash 执行失败，它没自己恢复」。
失败是插件的闸门造成的，却被报成 Agent 故障。

这本簿子要回答的只有一个问题：**这条失败该记到 Agent 头上吗。**
答错的代价是不对称的——答错"是自伤"会压掉一次真失败（最坏方向），
答错"不是自伤"会制造一条假警报（精度原则禁止的方向）。所以测试的
重心放在"认不出来的时候别乱认"。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.gated import (
    GATED_DEGRADED_WINDOW_SECONDS,
    GATED_FAILURE_LIMIT,
    GATED_FAILURE_WINDOW_SECONDS,
    MODE_DEGRADED,
    MODE_EXACT,
    GatedFailureLog,
)

T0 = 1_700_000_000.0


def _log(**kwargs) -> GatedFailureLog:
    return GatedFailureLog(**kwargs)


# ── 1. 登记 ────────────────────────────────────────────────────────────

def test_recording_needs_a_session() -> None:
    """没有 session 就配不上任何 tracker 桶，登记它没有意义。"""
    assert _log().record(session="", tool_use_id="tu_1", kind="Bash",
                         at=T0) is False


def test_the_same_pair_is_recorded_once() -> None:
    """轮询每 3 秒全扫一遍已了结审批，**不幂等的话 ``at`` 会被不断刷新**，
    窗口永远不过期，簿子跟着无限增长。"""
    log = _log()
    assert log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert not log.record(session="s1", tool_use_id="tu_1", kind="Bash",
                          at=T0 + 500)
    assert len(log) == 1
    # at 没被刷新：T0 + 600（默认窗口）之后它就该过期了
    assert log.claim(session="s1", tool_use_id="tu_1",
                     degraded_window=120, now=T0 + 601) == ""


def test_the_same_id_in_two_sessions_is_two_entries() -> None:
    """tool_use_id 在父子代理间会重复（approval.py 的注释明说），
    配上 session 之后才是唯一的一次调用。"""
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    log.record(session="s2", tool_use_id="tu_1", kind="Bash", at=T0)
    assert len(log) == 2
    assert log.claim(session="s1", tool_use_id="tu_1", degraded_window=120, now=T0)
    assert log.claim(session="s2", tool_use_id="tu_1", degraded_window=120, now=T0)


# ── 2. 精确配对 ────────────────────────────────────────────────────────

def test_a_matching_pair_is_self_inflicted() -> None:
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="tu_1",
                     degraded_window=120, now=T0) == MODE_EXACT


def test_a_different_tool_in_the_same_session_is_not() -> None:
    """**这一条是精度的心脏。** 同会话里另一次调用失败了，不能因为
    "这个会话刚过期过一条审批"就把它一起静音——那是在猜。"""
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="tu_2",
                     degraded_window=120, now=T0) == ""


def test_a_different_session_is_not() -> None:
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session="s2", tool_use_id="tu_1",
                     degraded_window=120, now=T0) == ""


def test_an_id_that_was_never_recorded_is_not() -> None:
    log = _log()
    assert log.claim(session="s1", tool_use_id="tu_1",
                     degraded_window=120, now=T0) == ""


def test_claiming_does_not_consume_the_entry() -> None:
    """命中**不删除**：重复喂同一条 tool_result（适配器重读、测试里手工
    喂两遍）不该让第二次变成假警报。"""
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    for _ in range(3):
        assert log.claim(session="s1", tool_use_id="tu_1",
                         degraded_window=120, now=T0) == MODE_EXACT


# ── 3. 降级路径（审批侧拿不到 tool_use_id）─────────────────────────────

def test_without_an_id_the_first_failure_in_the_window_is_blamed() -> None:
    log = _log()
    log.record(session="s1", tool_use_id="", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0) == MODE_DEGRADED


def test_the_degraded_claim_is_once_per_session() -> None:
    """再宽就是在猜，而猜错的方向是压掉一次真失败（最坏方向）。"""
    log = _log()
    log.record(session="s1", tool_use_id="", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0) == MODE_DEGRADED
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0) == ""


def test_the_degraded_claim_expires_with_the_window() -> None:
    """窗口取 ``failure_confirm_seconds``：再久就等于在猜
    "这次失败和那次审批有关"。"""
    log = _log()
    log.record(session="s1", tool_use_id="", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0) == MODE_DEGRADED
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0) == ""
    # 窗口过了名额还回去，下一条新审批还能用一次
    log.record(session="s1", tool_use_id="", kind="Bash", at=T0 + 5000)
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0 + 5000) == MODE_DEGRADED


def test_the_degraded_path_fires_even_when_the_event_has_an_id() -> None:
    """**这条钉住一个曾经的死代码 bug。** 降级曾经写成"两边都没有 id"，
    而 ``tool_result`` 块没有 id 就在 ``events._parse_tool_results`` 里被
    整块丢掉——事件侧恒有 id，那条分支在生产里永远走不到，
    "hook 负载不带 tool_use_id 的机器上 P1c 静默失效"看起来像已防住。
    """
    log = _log()
    log.record(session="s1", tool_use_id="", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="tu_9",
                     degraded_window=120, now=T0) == MODE_DEGRADED


def test_an_entry_with_an_id_is_not_used_for_degraded_claims() -> None:
    """反过来：审批侧**有** id 时，没有 id 的失败不该被静音——
    那是把两条不相干的链拧在一起。"""
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=120, now=T0) == ""


# ── 4. 有界 ────────────────────────────────────────────────────────────

def test_entries_expire_after_the_window() -> None:
    log = _log(window=100.0)
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="tu_1",
                     degraded_window=120, now=T0) == MODE_EXACT
    # 拨到窗口之后：精确配对也该失效，否则簿子会记住一个小时前的事
    assert log.claim(session="s1", tool_use_id="tu_1",
                     degraded_window=10 ** 9, now=T0 + 10 ** 6) == ""


def test_the_log_has_a_hard_ceiling() -> None:
    """一本只用来配对的簿子不该变成内存泄漏。"""
    log = _log(limit=8)
    for index in range(20):
        log.record(session=f"s{index}", tool_use_id=f"tu_{index}",
                   kind="Bash", at=T0 + index)
    assert len(log) == 8
    # 丢的是最旧的：最新的还在
    assert log.claim(session="s19", tool_use_id="tu_19", degraded_window=120, now=T0)
    assert not log.claim(session="s0", tool_use_id="tu_0", degraded_window=120, now=T0)


def test_the_defaults_are_the_documented_ones() -> None:
    assert GATED_FAILURE_WINDOW_SECONDS == 600.0
    assert GATED_FAILURE_LIMIT == 256


def test_a_negative_window_is_not_a_crash() -> None:
    """配置给歪了不该把轮询线程炸掉——那会连规则层播报一起停。"""
    log = _log(window=-1.0)
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="tu_1", degraded_window=-1, now=T0) == ""


@pytest.mark.parametrize("session,tool_use_id", [("", "tu_1"), ("s1", "")])
def test_claim_without_a_session_is_a_plain_miss(session, tool_use_id) -> None:
    log = _log()
    log.record(session="s1", tool_use_id="tu_1", kind="Bash", at=T0)
    assert log.claim(session=session, tool_use_id=tool_use_id,
                     degraded_window=120, now=T0) == ""


def test_the_degraded_window_is_the_documented_one() -> None:
    """30s，不是 ``failure_confirm_seconds``(120s)。

    要匹配的是「闸门的 fallout 什么时候落进转录」——hook 阻塞着 Agent，
    了结 → 宿主回填 → Claude 写转录 → 下一轮 3 秒轮询读到，各环节都是秒级。
    120s 的代价是闸门了结之后 Agent 继续干活，90 秒后一次**无关的真实失败**
    也会被静音——压掉真失败是最坏方向。
    """
    assert GATED_DEGRADED_WINDOW_SECONDS == 30.0


def test_a_failure_well_after_the_gate_settled_is_not_blamed() -> None:
    """**这一条是窄化窗口的收益。** 120s 窗口会把 90 秒后一次无关的
    真实失败一起静音；30s 不会。"""
    log = _log()
    log.record(session="s1", tool_use_id="", kind="Bash", at=T0)
    assert log.claim(session="s1", tool_use_id="",
                     degraded_window=GATED_DEGRADED_WINDOW_SECONDS,
                     now=T0 + 30) == MODE_DEGRADED
    # 31 秒后：链条早该走完了，这次失败是 Agent 自己后面干出来的
    log.record(session="s2", tool_use_id="", kind="Bash", at=T0)
    assert log.claim(session="s2", tool_use_id="",
                     degraded_window=GATED_DEGRADED_WINDOW_SECONDS,
                     now=T0 + 31) == ""
