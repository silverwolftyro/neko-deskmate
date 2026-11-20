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
from plugin.plugins.agent_deskmate.rules import (
    SEVERITY_CRITICAL,
    SEVERITY_NOTICE,
    SEVERITY_WARN,
    Coalescer,
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
