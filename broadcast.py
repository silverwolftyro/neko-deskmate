"""L0 播报：把规则命中推进 N.E.K.O 聊天窗口（红线 1）。

铁律（AGENTS.md + 任务书）：
  - ``source`` 必须显式传，且等于 plugin_id；
  - 纯播报必须 ``ai_behavior="blind"``——默认的 ``"respond"`` 会让角色开口，
    那是把插件的观测结果变成角色发言，属于骗人；
  - **永不 await push_message**：它入队后就会抛 TypeError，重试等于发两条。
    ``PushMessageResult`` 只是「本地已收」的回执，不代表用户看到、模型生成或播放；
  - ``submitted=False`` 是**正常分支**：记日志、返回原因，不抛异常。

四种拒绝原因都是可恢复的，处置不同：
  ``payload_too_large``  永久，得把内容改小（记 warning）
  其余三种               瞬时，下一轮自然恢复（记 info，避免刷日志）
"""

from __future__ import annotations

from typing import Any, Callable

from .rules import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_NOTICE, SEVERITY_WARN, RuleHit

# 严重度 → N.E.K.O priority（0-10，越大越重要）
SEVERITY_PRIORITY = {
    SEVERITY_CRITICAL: 9,
    SEVERITY_WARN: 6,
    SEVERITY_NOTICE: 4,
    SEVERITY_INFO: 2,
}

# 人眼前缀。刻意不用 emoji：这是状态播报，不是装饰。
SEVERITY_LABEL = {
    SEVERITY_CRITICAL: "【严重】",
    SEVERITY_WARN: "【警告】",
    SEVERITY_NOTICE: "【提示】",
    SEVERITY_INFO: "【信息】",
}

_PUSH_REJECTION_INFO = frozenset({"backpressure", "transport_error", "transport_unavailable"})


def priority_for(severity: str) -> int:
    return SEVERITY_PRIORITY.get(severity, 5)


def render_text(hit: RuleHit) -> str:
    """一行给人看的文本。带严重度前缀与来源 Agent，便于判断是谁的事。"""
    label = SEVERITY_LABEL.get(hit.severity, "【信息】")
    agent = f"{hit.agent} " if hit.agent else ""
    return f"{label}{agent}{hit.summary}"


class Broadcaster:
    """把 RuleHit 播出去。``push`` 由插件注入，便于单测时换成记录桩。"""

    def __init__(
        self,
        push: Callable[..., dict],
        logger: Any,
        *,
        source: str,
        coalescer: Any = None,
    ) -> None:
        self._push = push
        self._logger = logger
        self._source = source
        self._coalescer = coalescer
        self.sent = 0
        self.rejected = 0
        self.coalesced = 0

    def broadcast(self, hit: RuleHit) -> str:
        """播一条。返回 ``sent`` / ``coalesced`` / 拒绝原因，永不抛异常。"""
        if self._coalescer is not None:
            kept = self._coalescer.offer(hit)
            if kept is None:
                self.coalesced += 1
                return "coalesced"
            hit = kept

        text = render_text(hit)
        try:
            result = self._push(
                source=self._source,
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": text}],
                priority=priority_for(hit.severity),
            )
        except Exception as exc:  # noqa: BLE001  播报失败绝不能炸掉观测循环
            self.rejected += 1
            self._logger.warning("播报抛出异常（{}）：{}", type(exc).__name__, text)
            return "exception"

        return self._handle_result(result, hit, text)

    def _handle_result(self, result: object, hit: RuleHit, text: str) -> str:
        if not isinstance(result, dict):
            self.rejected += 1
            self._logger.warning("播报返回了非预期类型 {}：{}", type(result).__name__, text)
            return "unexpected"

        if result.get("submitted") is True:
            self.sent += 1
            self._logger.info("已播报 [{}] {}（累计 {} 条）", hit.severity, text, self.sent)
            return "sent"

        reason = str(result.get("reason") or "unknown")
        self.rejected += 1
        # payload_too_large 是永久性问题，值得 warning；其余是瞬时拥塞，info 就够
        level = self._logger.warning if reason not in _PUSH_REJECTION_INFO else self._logger.info
        level("播报未被接受（{}）：[{}] {}", reason, hit.severity, text)
        return reason
