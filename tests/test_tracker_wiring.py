"""C 批 §三：tracker 接进插件之后的**接线**测试。

状态机本身的逻辑在 ``test_tracker.py``，这里只测接下去之后的那条路：
  事件 → Signal → tracker → Notice → push_text → SDK

这条路上每一环都可能悄悄断掉，而断掉的表现是"什么都没发生"——
没有任何异常、没有任何日志，只是真机上永远不说话。那是最难发现的故障，
所以每一环都要有一个"它会响"的测试。

四组：
  A. ``push_text`` —— 红线 1 的唯一出口（显式 source / blind / 永不抛）
  B. ``broadcast_notice`` —— Notice 变成聊天窗里的两行
  C. 配置进状态机 —— 五个阈值真被读、真能换、坏值不崩
  D. ``_poll_once`` 端到端 —— 事件进、通知出；条数上限只推迟不丢弃
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

import plugin.plugins.agent_deskmate as plugin_module
from plugin.plugins.agent_deskmate import (
    MAX_BROADCAST_PER_TICK,
    MAX_PENDING_PUSHES,
    SOURCE,
    AgentDeskmatePlugin,
    _tracker_config,
    _tracker_section,
)
from plugin.plugins.agent_deskmate.broadcast import (
    NOTICE_PRIORITY,
    Broadcaster,
    _light_key,
    broadcast_notice,
    push_text,
)
from plugin.plugins.agent_deskmate.events import (
    EVENT_TOOL_RESULT,
    EVENT_TOOL_USE,
    EVENT_TURN_END,
    UnifiedEvent,
)
from plugin.plugins.agent_deskmate.progress import Light, Role
from plugin.plugins.agent_deskmate.tracker import Notice, ProgressTracker, TrackerConfig

AGENT = "claude-code"


def _now() -> float:
    return time.time()


def _iso(at: float) -> str:
    """epoch → tracker 认的 ISO8601 UTC（毫秒 + Z）。"""
    moment = datetime.fromtimestamp(at, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _step(at: float, *, event: str, ok: bool | None, session: str = "sess-1",
          action_id: str = "tu_1", kind: str = "Bash") -> UnifiedEvent:
    """一步的统一事件。``ok=None`` 表示推不出成败（会被契约层丢掉）。"""
    return UnifiedEvent(
        agent=AGENT, session=session, event=event, tool=kind,
        summary="工具执行失败" if ok else "工具执行完成",
        ts=_iso(at), tool_use_id=action_id, is_error=ok, command=f"{kind} x",
    )


def _failed_step(at: float, **kwargs) -> UnifiedEvent:
    return _step(at, event=EVENT_TOOL_RESULT, ok=True, **kwargs)


def _ok_step(at: float, **kwargs) -> UnifiedEvent:
    return _step(at, event=EVENT_TOOL_RESULT, ok=False, **kwargs)


def _action_step(at: float, **kwargs) -> UnifiedEvent:
    return _step(at, event=EVENT_TOOL_USE, ok=None, **kwargs)


class _ListLogger:
    """把日志收进列表：断言"它真的说了"比断言返回值可靠。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        """插件 logger 用 ``{}`` 占位（``logging`` 的 ``str.format`` 风格），
        不是 ``%``。占位符数量对不上时保留原文——日志断言不该因为
        一条写得马虎的日志就整个测试失败。"""
        try:
            text = message.format(*args) if args else message
        except (IndexError, KeyError, ValueError):
            text = message
        self.lines.append(text)

    def info(self, message, *args): self._log("info", message, *args)   # noqa: E704
    def warning(self, message, *args): self._log("warning", message, *args)  # noqa: E704
    def error(self, message, *args): self._log("error", message, *args)  # noqa: E704
    def debug(self, message, *args): self._log("debug", message, *args)  # noqa: E704


class _StubSource:
    """假观测源：第一次 poll 吐出准备好的事件，之后永远空。"""

    def __init__(self, events) -> None:
        self._events = list(events)

    def poll(self):
        out, self._events = self._events, []
        return out


class _Recorder:
    """一个会记账的推送任务，用来测积压队列。"""

    def __init__(self, tag: str, sink: list[str]) -> None:
        self.tag = tag
        self._sink = sink

    def __call__(self) -> str:
        self._sink.append(self.tag)
        return "sent"


class _TaggingBroadcaster:
    """假规则层播报器：只记"我被叫过几次"，不真播。"""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def broadcast(self, hit) -> str:
        self._sink.append(f"rule:{hit}")
        return "sent"


class _Host:
    """只借 ``AgentDeskmatePlugin`` 的那几个方法，其余手搭。

    不构造真插件：那需要宿主上下文。借方法而不是抄一遍，
    改动会立刻反映到测试上（``_poll_once`` 加一行它就多一行）。
    """

    _ensure_watchers = AgentDeskmatePlugin._ensure_watchers
    _poll_source = AgentDeskmatePlugin._poll_source
    _poll_once = AgentDeskmatePlugin._poll_once
    _sync_tracker_config = AgentDeskmatePlugin._sync_tracker_config
    _feed_tracker = AgentDeskmatePlugin._feed_tracker

    def __init__(self, events=(), progress=None) -> None:
        self.logger = _ListLogger()
        self.pushed: list[dict] = []
        self.push_message = self._push
        self._transcript_adapter = _StubSource(events)
        self._gateway_adapter = None
        self._source_error_at: dict[str, float] = {}
        self._source_state: dict[str, str] = {}
        self._broadcaster = Broadcaster(self.push_message, self.logger,
                                        source=SOURCE, coalescer=None)
        self._tracker = ProgressTracker()
        self._tracker_cfg: TrackerConfig | None = None
        self._pending_pushes: list = []
        self._progress = progress

    def _push(self, **kwargs):
        self.pushed.append(kwargs)
        return {"submitted": True}

    def config(self) -> dict:
        return {} if self._progress is None else {"progress": self._progress}

    def poll(self, times: int = 1) -> list[str]:
        outcomes: list[str] = []
        for _ in range(times):
            outcomes.extend(self._poll_once(self.config()))
        return outcomes


# ── A. push_text：红线 1 的唯一出口 ───────────────────────────────────────

def test_push_text_names_the_source_and_stays_blind() -> None:
    """红线 1：source 显式传、纯播报必须 blind、渠道只走 chat。"""
    calls: list[dict] = []
    outcome = push_text(lambda **kw: calls.append(kw) or {"submitted": True},
                        "第一行\n第二行", source=SOURCE, priority=7,
                        logger=_ListLogger())
    assert outcome == "sent"
    assert len(calls) == 1
    call = calls[0]
    assert call["source"] == SOURCE
    assert call["ai_behavior"] == "blind"
    assert call["visibility"] == ["chat"]
    assert call["parts"] == [{"type": "text", "text": "第一行\n第二行"}]
    assert call["priority"] == 7


def test_push_text_never_raises_even_when_the_host_blows_up() -> None:
    """真机上见过 ``object Ok can't be used in 'await' expression``——
    await 一个入队即返回的 future 就会这样。播报失败绝不能炸掉观测循环。"""

    def boom(**kwargs):
        raise TypeError("object Ok can't be used in 'await' expression")

    assert push_text(boom, "x", source=SOURCE, priority=5,
                     logger=_ListLogger()) == "exception"


@pytest.mark.parametrize("reason", ["backpressure", "transport_error",
                                    "transport_unavailable", "payload_too_large"])
def test_a_refused_push_is_a_normal_branch_not_a_crash(reason: str) -> None:
    """``submitted=False`` 是正常分支：记日志、返回原因，不抛异常。"""
    assert push_text(lambda **kw: {"submitted": False, "reason": reason},
                     "x", source=SOURCE, priority=5,
                     logger=_ListLogger()) == reason


def test_a_non_dict_result_is_reported_rather_than_swallowed() -> None:
    assert push_text(lambda **kw: "nope", "x", source=SOURCE, priority=5,
                     logger=_ListLogger()) == "unexpected"


# ── B. broadcast_notice：Notice → 聊天窗 ──────────────────────────────────

def _notice(light, *, headline="项目 出事了",
            detail="Bash 执行失败，它没自己恢复") -> Notice:
    return Notice(role=Role.OUTCOME, light=light, agent=AGENT, session="sess-1",
                  who="D:/work", headline=headline, detail=detail,
                  at=0.0, fingerprint="fp")


def test_a_notice_reaches_the_chat_as_exactly_two_lines() -> None:
    """B12 P0-2：通知固定两行。多一行就是把最该被看见的那行往下推。"""
    calls: list[dict] = []
    outcome = broadcast_notice(lambda **kw: calls.append(kw) or {"submitted": True},
                               _notice(Light.ERROR), source=SOURCE,
                               logger=_ListLogger())
    assert outcome == "sent"
    assert calls[0]["parts"][0]["text"] == "项目 出事了\nBash 执行失败，它没自己恢复"
    assert calls[0]["source"] == SOURCE
    assert calls[0]["ai_behavior"] == "blind"


def test_the_light_decides_the_priority() -> None:
    """出事的优先级必须高于在等你——一个是它坏了，一个是它在等。"""
    assert NOTICE_PRIORITY[Light.ERROR.value] > NOTICE_PRIORITY[Light.WAITING.value]


@pytest.mark.parametrize("light,expected", [
    (Light.ERROR, "error"),
    (Light.WAITING, "waiting"),
    (Light.RUNNING, "running"),
    ("error", "error"),          # 别处传来的裸字符串
    ("waiting", "waiting"),
])
def test_the_light_key_does_not_gamble_on_the_python_version(light, expected) -> None:
    """``str(Light.ERROR)`` 在不同版本可能是 "error" 也可能是 "Light.ERROR"
    （``__str__`` 的 MRO 在 str 与 Enum 之间争位）。所以取 ``.value``。

    这个坑在本项目付过一次学费：``notify_enabled`` 曾用 ``is`` 比较
    ``str`` 混血 Enum，配置传来的裸字符串永远匹配不上。
    """
    assert _light_key(light) == expected


def test_a_notice_that_cannot_be_pushed_does_not_raise() -> None:
    def boom(**kwargs):
        raise RuntimeError("host gone")

    assert broadcast_notice(boom, _notice(Light.ERROR), source=SOURCE,
                            logger=_ListLogger()) == "exception"


# ── C. 配置进状态机 ───────────────────────────────────────────────────────

def test_all_five_thresholds_are_read_from_the_config() -> None:
    """config.example.toml 的约定：每个键都必须被代码真正读取。"""
    section = {"wait_escalate_seconds": 11, "unclosed_action_seconds": 22,
               "boundary_round_seconds": 33, "fail_coalesce_seconds": 44,
               "failure_confirm_seconds": 55}
    assert _tracker_config(_tracker_section({"progress": section})) == TrackerConfig(
        wait_escalate_seconds=11.0, unclosed_action_seconds=22.0,
        boundary_round_seconds=33.0, fail_coalesce_seconds=44.0,
        failure_confirm_seconds=55.0)


def test_no_config_at_all_gives_the_measured_defaults() -> None:
    assert _tracker_config(_tracker_section({})) == TrackerConfig()


def test_a_bad_value_falls_back_to_the_default_instead_of_crashing() -> None:
    host = _Host(progress={"failure_confirm_seconds": "半小时",
                           "unclosed_action_seconds": None})
    host._sync_tracker_config(host.config())          # noqa: SLF001
    assert host._tracker_cfg == TrackerConfig()       # noqa: SLF001


def test_the_same_config_does_not_swap_the_tracker() -> None:
    """配置没变就别换实例——换了就丢掉在途的未回包与待确认失败。"""
    host = _Host()
    host._sync_tracker_config({})                     # noqa: SLF001
    first = host._tracker
    host._sync_tracker_config({})                     # noqa: SLF001
    assert host._tracker is first                     # noqa: SLF001


def test_a_changed_config_swaps_the_tracker() -> None:
    host = _Host()
    host._sync_tracker_config({})                     # noqa: SLF001
    first = host._tracker
    host._sync_tracker_config({"progress": {"failure_confirm_seconds": 1}})
    assert host._tracker is not first                 # noqa: SLF001


def test_the_config_is_logged_once_when_it_lands() -> None:
    """阈值换一次记一条：用户改了配置却没生效，日志里要能看见用了哪套值。"""
    host = _Host(progress={"failure_confirm_seconds": 30})
    host._sync_tracker_config(host.config())          # noqa: SLF001
    assert any("进度状态机阈值就绪" in line for line in host.logger.lines)


# ── D. _poll_once 端到端 ─────────────────────────────────────────────────

def test_a_failure_that_never_recovers_reaches_the_chat() -> None:
    """端到端：一条 is_error 的 tool_result → Signal → 状态机 → SDK。"""
    host = _Host([_failed_step(_now() - 300)])
    host.poll()
    assert len(host.pushed) == 1
    call = host.pushed[0]
    assert call["source"] == SOURCE
    assert call["ai_behavior"] == "blind"
    assert call["visibility"] == ["chat"]
    text = call["parts"][0]["text"]
    assert "出事" in text
    assert "Bash" in text


def test_a_failure_that_recovers_in_time_says_nothing() -> None:
    """它自己好了——这是 85/93 的那种。精度原则要求闭嘴。"""
    at = _now() - 300
    host = _Host([_failed_step(at, action_id="tu_1"),
                  _ok_step(at + 5, action_id="tu_2")])
    host.poll()
    assert host.pushed == []


def test_the_default_window_stays_quiet_for_a_fresh_failure() -> None:
    """刚失败 5 秒：灯已经红了，但通知要等确认窗口（120s）。"""
    host = _Host([_failed_step(_now() - 5)])
    host.poll()
    assert host.pushed == []


def test_zeroing_the_confirmation_window_announces_at_once() -> None:
    """这一条同时证明配置真的被读到了：默认 120 秒时同一批事件什么都不播。"""
    host = _Host([_failed_step(_now() - 5)], progress={"failure_confirm_seconds": 0})
    host.poll()
    assert len(host.pushed) == 1


def test_an_event_the_contract_layer_drops_finds_no_tracker_state() -> None:
    """推不出成败的 outcome（is_error=None）必须被契约层丢掉——
    猜 True 压掉真失败，猜 False 造假警报，两条都不如丢。"""
    host = _Host([_step(_now() - 300, event=EVENT_TOOL_RESULT, ok=None)])
    host.poll()
    assert host.pushed == []
    # counts() 恒返回四盏灯，全 0 才算"这个会话根本没进状态机"
    assert set(host._tracker.counts().values()) == {0}   # noqa: SLF001


def test_an_action_that_never_comes_back_is_reported() -> None:
    """发了动作、没等到结果，超过 unclosed_action_seconds 就说它没回包。"""
    host = _Host([_action_step(_now() - 400)])
    host.poll()
    assert len(host.pushed) == 1
    text = host.pushed[0]["parts"][0]["text"]
    assert "没有回包" in text
    assert "卡住" in text
    # 指名：没有工作目录时就报 agent，不报 UUID
    assert text.startswith("claude-code")


def test_a_round_that_ends_clears_the_orphans() -> None:
    """一轮结束后还没闭环的动作不许变成"未回包"假警报——那一轮已经翻篇了。

    对照 ``test_an_action_that_never_comes_back_is_reported``：同一个动作，
    没有那条 turn_end 就会响，有了它就不响。两条放在一起才证明"清空"真发生了。
    """
    at = _now() - 10
    host = _Host([_action_step(at, action_id="tu_1"), _boundary(at + 700)])
    host.poll()
    assert host.pushed == []
    assert host._tracker.state_of(AGENT, "sess-1").open_actions == {}  # noqa: SLF001


def _boundary(at: float, session: str = "sess-1"):
    """一轮结束信号（turn_end）。"""
    return UnifiedEvent(agent=AGENT, session=session, event=EVENT_TURN_END,
                        summary="一轮结束", ts=_iso(at))


# ── D2. 条数上限：只推迟，不丢弃 ─────────────────────────────────────────

def test_a_burst_is_deferred_to_the_next_round_not_dropped() -> None:
    """0.3.2 在这里直接 ``return``，而适配器的 offset 在那之前已经推进过了——
    日志写"余下留待下一轮"，实际那条通知**永久消失**。
    规则层丢的就是一条删除播报。这一条钉死修好之后的行为：一条都不许丢。
    """
    at = _now() - 300
    events = [_failed_step(at, session=f"sess-{i}", action_id=f"tu_{i}")
              for i in range(MAX_BROADCAST_PER_TICK + 3)]
    host = _Host(events)
    host.poll()
    assert len(host.pushed) == MAX_BROADCAST_PER_TICK
    assert len(host._pending_pushes) == 3              # noqa: SLF001
    host.poll()                                        # 源已空，只剩队列
    assert len(host.pushed) == MAX_BROADCAST_PER_TICK + 3


def test_the_overflow_queue_has_a_hard_ceiling_and_says_so() -> None:
    """队列不能无限长——那本身就是一种 OOM。超顶丢最旧的，并且要说出来。"""
    sink: list[str] = []
    host = _Host()
    host._pending_pushes = [_Recorder(str(i), sink)             # noqa: SLF001
                            for i in range(MAX_PENDING_PUSHES + 10)]
    host.poll()
    assert len(sink) == MAX_BROADCAST_PER_TICK
    assert len(host._pending_pushes) == MAX_PENDING_PUSHES       # noqa: SLF001
    assert any("超过硬顶" in line for line in host.logger.lines)


def test_rule_hits_go_out_before_progress_notices(monkeypatch) -> None:
    """撞上条数上限时，该被推迟的是进度播报，不是"它刚删了文件"。"""
    sink: list[str] = []
    host = _Host([_failed_step(_now() - 300)])
    host._broadcaster = _TaggingBroadcaster(sink)                # noqa: SLF001
    monkeypatch.setattr(plugin_module, "_hits_for_event",
                        lambda event, roots: ["hit"] * MAX_BROADCAST_PER_TICK)
    host.poll()
    assert sink == ["rule:hit"] * MAX_BROADCAST_PER_TICK
    assert host.pushed == []                                      # 通知整批推迟
    host.poll()
    assert len(host.pushed) == 1                                  # 下一轮补上，没丢
