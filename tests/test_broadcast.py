"""broadcast.py 播报层单测（M0-lite-4）。

核心是红线 1 与任务书要求：
  - source 必须显式、ai_behavior 必须 blind；
  - ``submitted=False`` 是正常分支，不抛异常；
  - 永不 await push_message（播报器是同步函数，测试里用同步桩就能证明）。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.broadcast import (
    Broadcaster,
    priority_for,
    render_text,
)
from plugin.plugins.agent_deskmate.events import UnifiedEvent, attribute
from plugin.plugins.agent_deskmate.rules import (
    SEVERITY_CRITICAL,
    SEVERITY_NOTICE,
    SEVERITY_WARN,
    Coalescer,
    RuleHit,
    _attribution_of,
    evaluate_delete,
    evaluate_failure,
)

SOURCE = "agent_deskmate"


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        self.lines.append((level, message))

    def info(self, message: str, *args: object) -> None:
        self._log("info", message)

    def warning(self, message: str, *args: object) -> None:
        self._log("warning", message)

    def error(self, message: str, *args: object) -> None:
        self._log("error", message)


class RecordingPush:
    """同步记录桩。若被测代码 await 它，这里会直接炸——正好守住"永不 await"。"""

    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"submitted": True}
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.result


def _delete_hit(command: str = "rm -rf build/"):
    return evaluate_delete(command, agent="claude-code", session="s1", ts="t")


# ── 严重度 → priority ─────────────────────────────────────────────────────

def test_priority_ladder() -> None:
    assert priority_for(SEVERITY_CRITICAL) == 9
    assert priority_for(SEVERITY_WARN) == 6
    assert priority_for(SEVERITY_NOTICE) == 4
    assert priority_for("info") == 2
    assert priority_for("没见过的严重度") == 5  # 不猜，给中间值


def test_render_text_has_severity_and_agent() -> None:
    text = render_text(_delete_hit())
    assert text.startswith("【严重】")
    assert "claude-code" in text
    assert "rm -rf build/" in text
    # 不用 emoji：这是状态播报不是装饰
    assert not any(ord(ch) > 0x1F000 for ch in text)


# ── 红线 1：push_message 的调用形状 ───────────────────────────────────────

def test_push_carries_explicit_source_and_blind() -> None:
    push = RecordingPush()
    Broadcaster(push, FakeLogger(), source=SOURCE).broadcast(_delete_hit())
    assert len(push.calls) == 1
    call = push.calls[0]
    assert call["source"] == SOURCE          # 必须显式，不能是默认空串
    assert call["ai_behavior"] == "blind"    # 纯播报，不让角色开口
    assert call["visibility"] == ["chat"]
    assert call["priority"] == 9             # critical
    assert call["parts"][0]["type"] == "text"
    assert "rm -rf build/" in call["parts"][0]["text"]


def test_broadcaster_is_synchronous() -> None:
    """播报器是同步函数：push 被同步调用，不产生协程。"""
    import inspect

    assert not inspect.iscoroutinefunction(Broadcaster.broadcast)


# ── submitted=False 是正常分支 ────────────────────────────────────────────

@pytest.mark.parametrize("reason,expect_warning", [
    ("backpressure", False),
    ("transport_error", False),
    ("transport_unavailable", False),
    ("payload_too_large", True),
])
def test_rejection_is_a_normal_branch(reason: str, expect_warning: bool) -> None:
    """四种拒绝原因都不抛异常；只有永久性问题记 warning，瞬时问题记 info。"""
    push = RecordingPush({"ok": False, "submitted": False, "reason": reason})
    logger = FakeLogger()
    broadcaster = Broadcaster(push, logger, source=SOURCE)

    outcome = broadcaster.broadcast(_delete_hit())

    assert outcome == reason           # 原样返回原因，便于调用方判断
    assert broadcaster.rejected == 1
    assert broadcaster.sent == 0
    levels = [level for level, _ in logger.lines]
    assert ("warning" in levels) is expect_warning


def test_submitted_true_counts_as_sent() -> None:
    push = RecordingPush({"submitted": True})
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "sent"
    assert broadcaster.sent == 1
    assert broadcaster.rejected == 0


def test_push_exception_does_not_propagate() -> None:
    """播报抛异常绝不能炸掉观测循环。"""

    def exploding(**_: object) -> dict:
        raise RuntimeError("transport gone")

    broadcaster = Broadcaster(exploding, FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "exception"
    assert broadcaster.rejected == 1


def test_unexpected_result_type() -> None:
    broadcaster = Broadcaster(RecordingPush("不是dict"), FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "unexpected"


def test_missing_reason_field_is_unknown() -> None:
    push = RecordingPush({"submitted": False})
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "unknown"


# ── 合并 ──────────────────────────────────────────────────────────────────

def test_coalesced_hit_never_reaches_push() -> None:
    push = RecordingPush()
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE,
                              coalescer=Coalescer(window_seconds=60.0))
    # 同一条命令重复 → 第二条被合并，推不到 SDK
    assert broadcaster.broadcast(_delete_hit("rm -rf a")) == "sent"
    assert broadcaster.broadcast(_delete_hit("rm -rf a")) == "coalesced"
    assert len(push.calls) == 1
    assert broadcaster.coalesced == 1


def test_distinct_deletions_each_reach_push() -> None:
    """不同命令必须各自播——否则用户一分钟内漏掉多次删除。"""
    push = RecordingPush()
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE,
                              coalescer=Coalescer(window_seconds=60.0))
    assert broadcaster.broadcast(_delete_hit("rm -rf a")) == "sent"
    assert broadcaster.broadcast(_delete_hit("rm -rf b")) == "sent"
    assert len(push.calls) == 2
    assert broadcaster.coalesced == 0


# ── 规则 → 播报 的接线（M0-lite-4 把 3a 的规则接到 SDK 上） ────────────────

def test_failure_severity_reaches_priority() -> None:
    """5xx 必须用 critical 的 priority，否则用户在聊天窗里看不出严重程度。"""
    push = RecordingPush()
    hit = evaluate_failure(status_code=503, error_message="overloaded",
                           agent="claude", session="s1")
    Broadcaster(push, FakeLogger(), source=SOURCE).broadcast(hit)
    assert push.calls[0]["priority"] == 9
    assert "503" in push.calls[0]["parts"][0]["text"]

    push2 = RecordingPush()
    hit2 = evaluate_failure(status_code=429, error_message="rate limited",
                            agent="claude", session="s1")
    Broadcaster(push2, FakeLogger(), source=SOURCE).broadcast(hit2)
    assert push2.calls[0]["priority"] == 4        # notice，不按 critical 打扰人
    assert push2.calls[0]["ai_behavior"] == "blind"


# ── 归因文案：四种模版（2026-11-21 决策记录 §1/§2） ─────────────────────────
# 核心原则：主语位只放「可证的请求源」；执行者降级到括号里；拿不准就写未知。
# 用户会把主语读成「发起者」——主语位放执行者，就等于把锅扣给手。
# 真实事故：DSH spawn 的 claude -p 删了文件，播报说「claude-code 删除」，
# 用户第一反应是"我什么时候让 Claude 删东西了？"

CWD = r"C:\Users\alpha\AppData\Local\Temp"


def _hit(**overrides):
    base = dict(rule="delete", severity=SEVERITY_CRITICAL,
                summary="删除文件：rm -f deskmate-probe.docx",
                matched="rm", agent="claude-code", session="800257f3-aaaa-bbbb",
                ts="t", executor="claude-code", working_directory=CWD)
    base.update(overrides)
    return RuleHit(**base)


def _source(**overrides) -> UnifiedEvent:
    base = dict(agent="claude-code", session="800257f3-aaaa-bbbb", event="tool_use",
                tool="Bash", command="rm -f x", ts="t", executor="claude-code",
                working_directory=CWD)
    base.update(overrides)
    return UnifiedEvent(**base)


# 1) 单 Agent：用户声明过这个目录 → 主语是"你自己开的"

def test_template_single_agent_user_declared() -> None:
    source = attribute(_source(), mine_roots=[CWD])
    hit = evaluate_delete("rm -f deskmate-probe.docx", agent=source.agent,
                          session=source.session, ts=source.ts, source=source)
    text = render_text(hit)
    assert text.startswith("【严重】你自己开的 claude-code 删除文件：")
    assert "rm -f deskmate-probe.docx" in text
    # 定位三件套：会话短 id + cwd，让用户 3 秒内判断"这是我开的吗"
    assert "会话 800257f3" in text
    assert CWD in text


# 2) 父子代理

def test_template_subagent() -> None:
    source = attribute(_source(sidechain=True))
    hit = evaluate_delete("rm -f deskmate-probe.docx", source=source)
    text = render_text(hit)
    assert "（经子代理）" in text
    assert text.startswith("【严重】claude-code（经子代理）删除文件：")
    # 主语是父会话，执行者降级到括号里
    assert "你自己开的" not in text


# 3) 贵脑 + 廉价手

def test_template_delegated_to_worker() -> None:
    source = attribute(_source(tool="mcp__deepseek-worker__deepseek_task", command=None))
    hit = RuleHit(rule="delete", severity=SEVERITY_CRITICAL,
                  summary="删除文件：rm -f x", matched="rm",
                  agent="claude-code", session=source.session, ts="t",
                  **_attribution_of(source))
    text = render_text(hit)
    # 主语是大脑（用户对话的对象），worker 降级成"手"
    assert "让 deepseek-worker 执行" in text
    assert text.startswith("【严重】claude-code 让 deepseek-worker 执行删除文件：")
    # 绝不把 worker 说成主语——那正是用户那句"我让 DeepSeek 执行任务了？"
    assert not text.startswith("【严重】deepseek-worker")


# 4) 未知发起者（critical 的默认）

def test_template_unknown_requester_is_the_critical_default() -> None:
    hit = _hit()   # 不传任何归因 → unknown
    assert hit.attribution == "unknown"
    text = render_text(hit)
    assert "发起者未知" in text
    # 陈述句 + 线索，不是免责声明腔
    assert "无法确定" not in text and "请自行核实" not in text
    # 第二行给出执行者 + 会话 + cwd，用户自己就能补上最后一跳
    assert text.splitlines()[1] == f"由 claude-code 会话 800257f3 执行 @ {CWD}"


def test_unknown_template_matches_the_real_incident() -> None:
    """锁住真实事故的期望输出：DSH spawn 的 claude -p 在 %TEMP% 删文件。

    这条播报必须让用户 3 秒内明白"我没直接让 Claude 干这件事"，
    同时给足线索让他自己想起来"哦对，是我让 DSH 清理临时文件"。
    """
    hit = _hit(summary="删除文件：rm -f deskmate-probe.docx")
    text = render_text(hit)
    assert text == (
        "【严重】删除文件：rm -f deskmate-probe.docx，发起者未知\n"
        f"由 claude-code 会话 800257f3 执行 @ {CWD}"
    )


# ── 按后果分级 ──────────────────────────────────────────────────────────────

def test_notice_severity_may_name_the_visible_agent() -> None:
    """notice/warn 可以直接写可见的 agent——没人会为一条 429 质疑授权。"""
    hit = _hit(rule="failure", severity=SEVERITY_NOTICE,
               summary="请求失败（状态 429）：rate limited", matched="429")
    text = render_text(hit)
    assert "发起者未知" not in text
    assert text.startswith("【提示】claude-code 请求失败（状态 429）")


def test_warn_severity_may_name_the_visible_agent() -> None:
    hit = _hit(rule="failure", severity=SEVERITY_WARN,
               summary="转录报错：api_error", matched="transcript-error")
    assert "发起者未知" not in render_text(hit)


def test_critical_never_guesses_even_for_failures() -> None:
    """5xx 是 critical：同样不把主语交给推测。"""
    hit = _hit(rule="failure", severity=SEVERITY_CRITICAL,
               summary="请求失败（状态 503）：overloaded", matched="503")
    assert "发起者未知" in render_text(hit)


def test_known_attribution_escapes_the_hedge_at_any_severity() -> None:
    """有证据就不加"未知"——分级只管"没证据时要不要坦白"，不管有证据的。"""
    source = attribute(_source(), mine_roots=[CWD])
    hit = evaluate_delete("rm -f x", source=source)
    assert hit.severity == SEVERITY_CRITICAL
    assert "发起者未知" not in render_text(hit)


# ── 边界 ────────────────────────────────────────────────────────────────────

def test_no_locator_when_session_and_cwd_missing() -> None:
    hit = _hit(session="", working_directory=None)
    text = render_text(hit)
    assert "（）" not in text
    assert "会话" not in text


def test_hedged_line_handles_missing_session() -> None:
    hit = _hit(session="", working_directory=None)
    text = render_text(hit)
    assert text.splitlines()[1] == "由 claude-code 执行"


def test_executor_falls_back_to_agent() -> None:
    """executor 空串时用 agent——不留"未知执行者"这种噪音。"""
    hit = _hit(executor="")
    assert "claude-code" in render_text(hit)
    assert "未知执行者" not in render_text(hit)


def test_summary_without_colon_still_renders() -> None:
    hit = _hit(summary="删除文件")
    assert render_text(hit).startswith("【严重】")


def test_severity_and_attribution_stay_orthogonal() -> None:
    """严重度衡量动作危险度，归因衡量谁负责——两者不许互相编码。

    把"未知"降成 notice 会让用户忽略真正危险的删除；升成 critical 会让 429 也变严重。
    """
    unknown = _hit()
    known = evaluate_delete("rm -f x", session="s",
                            source=attribute(_source(session="s"), mine_roots=[CWD]))
    assert unknown.severity == known.severity == SEVERITY_CRITICAL
    assert priority_for(unknown.severity) == priority_for(known.severity)
