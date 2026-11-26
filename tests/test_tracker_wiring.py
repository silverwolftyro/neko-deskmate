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

import queue
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
    NOTICE_MERGE_SECONDS,
    NOTICE_PRIORITY,
    Broadcaster,
    NoticeMerger,
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
from plugin.plugins.agent_deskmate.gated import GatedFailureLog
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
          action_id: str = "tu_1", kind: str = "Bash",
          working_directory: str | None = None) -> UnifiedEvent:
    """一步的统一事件。``ok=None`` 表示推不出成败（会被契约层丢掉）。"""
    return UnifiedEvent(
        agent=AGENT, session=session, event=event, tool=kind,
        summary="工具执行失败" if ok else "工具执行完成",
        ts=_iso(at), tool_use_id=action_id, is_error=ok, command=f"{kind} x",
        working_directory=working_directory,
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

    def resolve_ai_behavior(self, now=None) -> str:
        # 真 Broadcaster 才有冷却；桩只需要别让调用方炸。
        return "blind"


class _Host:
    """只借 ``AgentDeskmatePlugin`` 的那几个方法，其余手搭。

    不构造真插件：那需要宿主上下文。借方法而不是抄一遍，
    改动会立刻反映到测试上（``_poll_once`` 加一行它就多一行）。
    """

    _ensure_watchers = AgentDeskmatePlugin._ensure_watchers
    _poll_source = AgentDeskmatePlugin._poll_source
    _poll_once = AgentDeskmatePlugin._poll_once
    _sync_tracker_config = AgentDeskmatePlugin._sync_tracker_config
    _sync_merge_window = AgentDeskmatePlugin._sync_merge_window
    _feed_tracker = AgentDeskmatePlugin._feed_tracker
    _approval_blocked_sessions = AgentDeskmatePlugin._approval_blocked_sessions
    _released_gate_sessions = AgentDeskmatePlugin._released_gate_sessions
    _record_gate_stopped = AgentDeskmatePlugin._record_gate_stopped
    _is_gate_caused = AgentDeskmatePlugin._is_gate_caused
    # B11：播报压制审查。真插件 _reset_watch_state 建状态，这里手搭一份。
    _drain_review_survivors = AgentDeskmatePlugin._drain_review_survivors
    _maybe_submit_review = AgentDeskmatePlugin._maybe_submit_review
    _run_review_batch = AgentDeskmatePlugin._run_review_batch

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
        # 审批在等人的会话。None = 审批网关没起来（真插件 _approval 的初值），
        # 走 "没有阻塞" 分支——这条路径本身也要被测到。
        self._approval = None
        # P1c：自伤失败登记簿。真插件 _reset_watch_state 建，这里手搭一份。
        self._gated = GatedFailureLog()
        # P1b：已经释放过的审批 request_id。同上，真插件建、这里手搭。
        self._gate_released: dict[str, float] = {}
        # P1c(b)：同目录通知的跨批次合并器。默认窗口 20s 会把通知压到下一轮，
        # 所以**除 §H 之外的所有测试都通过 config() 关掉它**——那些测试要断言
        # 的是别的东西（状态机、闸门、静音），不该被合并窗口拖着。
        self._merger = NoticeMerger()
        self._merge_window = NOTICE_MERGE_SECONDS
        # B11：三个容器 + 三个计数器。真插件建在 _reset_watch_state 里。
        self._review_queue: list = []
        self._review_future = None
        self._review_survivors = queue.SimpleQueue()
        self._review_submitted = 0
        self._review_suppressed = 0
        self._review_failed = 0
        self._review_attempts = 0
        # 这些测试不送审：没有 reviewer（_broadcast_reviewer 返回 None），
        # 所以 _maybe_submit_review 直接返回。_advice_pool 留着 None 即可。
        self._advice_pool = None

    def _push(self, **kwargs):
        self.pushed.append(kwargs)
        return {"submitted": True}

    def config(self) -> dict:
        section = {} if self._progress is None else dict(self._progress)
        # 不显式关掉跨批合并的话，通知会被压一个窗口（默认 20s），
        # 当轮就看不到它了。想测合并窗口自己的，传 progress 时覆盖这个键。
        section.setdefault("notice_merge_seconds", 0)
        return {"progress": section}

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


# ── E. P1b：审批挂起的会话不进「未回包」评估 ─────────────────────────────
#
# 2026-11-24 真机：三张审批卡挂着等人，300 秒后连发三条
# 「有一步没有回包 / 它可能卡住了」。Agent 是被 hook 阻塞，不是卡死。
#
# 关键在"跳过评估"而不是"过滤通知"——评估跑过一次，
# action_id 就进 ``unclosed_reported``，从此永远不再报。
# 这一组同时钉住接线两头的形状：``_approval_blocked_sessions`` 的产出
# 必须正好是 tracker 认的 ``(agent, session)`` 桶键。


class _FakeApproval:
    """只实现 ``snapshot()``——``_approval_blocked_sessions`` 与
    ``_record_gate_stopped`` 都只调它。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def snapshot(self) -> list[dict]:
        return self._rows


def _pending(session: str, agent: str = AGENT) -> dict:
    return {"session_id": session, "agent": agent, "state": "pending"}


def _settled(session: str, agent: str = AGENT) -> dict:
    return {"session_id": session, "agent": agent, "state": "settled"}


def _gate_stopped(session: str, *, tool_use_id: str = "tu_1",
                  tool_name: str = "Bash", request_id: str = "") -> dict:
    """一条「工具没跑成」的已了结审批（到期 / 拒绝 / 回填断开）。"""
    return {"request_id": request_id or f"req-{session}",
            "session_id": session, "agent": AGENT, "state": "settled",
            "tool_use_id": tool_use_id, "tool_did_not_run": True,
            "tool_name": tool_name}


def _gate_allowed(session: str, *, tool_use_id: str = "tu_1") -> dict:
    """一条放行了、工具真的跑了的审批。它的失败是真实失败。"""
    return {"session_id": session, "agent": AGENT, "state": "settled",
            "tool_use_id": tool_use_id, "tool_did_not_run": False,
            "tool_name": "Bash"}


def test_no_approval_gateway_blocks_nothing() -> None:
    """插件刚起来、审批中继还没建 → 空集合。不是异常，是正常路径。"""
    host = _Host()
    assert host._approval is None                    # noqa: SLF001
    assert host._approval_blocked_sessions() == set()   # noqa: SLF001


def test_only_pending_approvals_block() -> None:
    """已了结的（允许/拒绝/过期）不阻塞——它已经不再拦着 Agent 了。"""
    host = _Host()
    host._approval = _FakeApproval([_pending("s1"), _settled("s2")])  # noqa: SLF001
    assert host._approval_blocked_sessions() == {(AGENT, "s1")}       # noqa: SLF001


def test_a_row_without_a_session_id_is_skipped() -> None:
    """没有 session_id 就对不上桶。硬塞一个空串进去会屏蔽掉一个假会话，
    更糟的是形状漂移之后没人发现。"""
    host = _Host()
    host._approval = _FakeApproval([_pending("")])                    # noqa: SLF001
    assert host._approval_blocked_sessions() == set()                 # noqa: SLF001


def test_an_empty_agent_falls_back_to_claude_code() -> None:
    """hook 负载没带 agent 时的兜底。与 ``ApprovalRequest.agent`` 的默认值
    同源，不是两处各写一个字符串。"""
    host = _Host()
    host._approval = _FakeApproval([_pending("s1", agent="")])        # noqa: SLF001
    assert host._approval_blocked_sessions() == {("claude-code", "s1")}  # noqa: SLF001


def test_the_blocked_set_reaches_the_tracker_as_bucket_keys() -> None:
    """端到端：挂着审批的会话不播「没有回包」，另一个会话照播。

    ``SessionState.key()`` 是 ``(agent, session)`` 二元组；接线断了
    （比如传了裸 session id）表现是"什么都没发生"——没有任何异常，
    只是假警报照旧。所以这条必须存在。
    """
    at = _now() - 400
    host = _Host([_action_step(at, session="s1", action_id="tu_1"),
                  _action_step(at, session="s2", action_id="tu_2")])
    host._approval = _FakeApproval([_pending("s1")])                  # noqa: SLF001
    host.poll()
    assert len(host.pushed) == 1
    assert "没有回包" in host.pushed[0]["parts"][0]["text"]
    # 指名落到没被屏蔽的那个会话上
    assert host.pushed[0]["parts"][0]["text"].startswith(AGENT)


def test_the_action_survives_the_block_and_reports_afterwards() -> None:
    """**P1b 的核心不变量。** 审批挂着的那几分钟不许把 action 烧掉。

    用户点完卡、Agent 真的卡死了 → 该报的那一次必须还能报。
    否则"等审批"被误判成"卡死"之后，真卡死反而不报了。
    """
    at = _now() - 400
    host = _Host([_action_step(at, session="s1", action_id="tu_1")])
    host._approval = _FakeApproval([_pending("s1")])                  # noqa: SLF001

    host.poll()                                   # 屏蔽中：什么都不该播
    assert host.pushed == []
    for _ in range(3):                            # 再转几圈，模拟等了 15 秒
        assert host.poll() == []
        assert host.pushed == []

    host._approval = None                          # noqa: SLF001  用户点完卡
    host.poll()
    assert len(host.pushed) == 1
    assert "没有回包" in host.pushed[0]["parts"][0]["text"]


# ── E2. P1b 收尾：挂起解除时作废被扣住的动作 ─────────────────────────────
#
# 2026-11-24 真机（0.6.0 验收 §3）：卡片 12:33 弹出、12:42:53 到期，
# 12:44:09 宿主张口就是一条「有一步没有回包 / Bash 执行了 10 分 46 秒」。
# 那个动作 12:33 就发出去了，没有结果是因为**插件扣着它等审批**。
# 而到期按设计不回填（approval.DECIDED_BY_EXPIRED），那个 outcome
# 永远不会来——**每一张没人点的卡，到期后都必然产出这么一条假警报**。
#
# 上面 E 组钉的是"挂着的时候别评估"；这一组钉的是"解除的那一秒"——
# 挂起标记一消失，早就过了 300s 的旧动作立刻被判成"它可能卡住了"，
# 还把耗时写成一个夸大的数字。用户越忙没点，通知越多、越像 Agent 坏了。


def test_an_expired_card_does_not_become_a_stuck_action_report() -> None:
    """到期不回填 → 那个动作作废，不翻旧账。

    这条同时钉住**顺序**：如果 ``release_gate`` 排在 ``tick`` 后面，
    假警报这一轮就已经进 jobs 了，作废再来是马后炮。
    """
    at = _now() - 700
    host = _Host([_action_step(at, session="s1", action_id="tu_1")])
    host._approval = _FakeApproval([_gate_stopped("s1")])             # noqa: SLF001
    host.poll()
    assert host.pushed == [], "到期把旧动作翻成「未回包」了"


def test_an_allowed_gate_releases_nothing() -> None:
    """工具真的跑了 → 它的结果在路上，不许作废。

    判据就是 ``tool_did_not_run``，和 ``_record_gate_stopped`` 同一条：
    允许之后工具跑了，它的失败是**真实**失败。
    """
    host = _Host()
    host._approval = _FakeApproval([_gate_allowed("s1")])             # noqa: SLF001
    assert host._released_gate_sessions(set()) == set()               # noqa: SLF001


def test_a_session_still_holding_another_card_is_not_released() -> None:
    """它还挂着另一张卡 → 那个动作确实正当被扣着，等那张了结再释放。

    提前作废会把第二张卡放行的工具结果也一起作废掉。
    """
    host = _Host()
    host._approval = _FakeApproval([_gate_stopped("s1"), _pending("s1")])  # noqa: SLF001
    still = host._approval_blocked_sessions()                         # noqa: SLF001
    assert still == {(AGENT, "s1")}
    assert host._released_gate_sessions(still) == set()               # noqa: SLF001


def test_the_same_card_is_only_released_once() -> None:
    """幂等靠 request_id：snapshot 会留着已了结记录一小时。

    不记账的话同一会话每分钟又被"释放"一次，每次都白扫一遍
    open_actions——功能上没错，但日志会吵，而且那是没设计的重复。
    """
    host = _Host()
    rows = [_gate_stopped("s1", request_id="req-1")]
    host._approval = _FakeApproval(rows)                              # noqa: SLF001
    assert host._released_gate_sessions(set()) == {(AGENT, "s1")}     # noqa: SLF001
    assert host._released_gate_sessions(set()) == set()               # noqa: SLF001
    assert list(host._gate_released) == ["req-1"]                     # noqa: SLF001


def test_a_row_without_a_request_id_is_not_released() -> None:
    """没有 request_id 就无法判重，宁可不释放也不每分钟释放一次。"""
    host = _Host()
    row = _gate_stopped("s1")
    del row["request_id"]
    host._approval = _FakeApproval([row])                             # noqa: SLF001
    assert host._released_gate_sessions(set()) == set()               # noqa: SLF001


def test_the_release_leaves_a_log_line() -> None:
    """"少播了一条"必须有痕迹。

    静默的抑制等于没有抑制：真机上出了一次"该报的没报"，
    排查时唯一能依靠的就是这一行。
    """
    at = _now() - 700
    host = _Host([_action_step(at, session="s1", action_id="tu_1")])
    host._approval = _FakeApproval([_gate_stopped("s1")])             # noqa: SLF001
    host.poll()
    assert any("作废" in line for line in host.logger.lines), host.logger.lines


# ── F. P1c：闸门造成的失败不点亮 🔴 ─────────────────────────────────────
#
# 2026-11-24 真机：审批 570s 到期 → tool_result is_error=true → 120 秒后
# 连发三条「出事了 / 它没自己恢复」。失败是插件的闸门造成的，
# 却被报成 Agent 故障。用户越忙没点卡，通知越多、越像 Agent 坏了。

def test_no_approval_gateway_records_nothing() -> None:
    """审批中继没起来 → 簿子空着。"没有自伤失败"正是没有审批时的正确表现。"""
    host = _Host()
    host._record_gate_stopped()                       # noqa: SLF001
    assert len(host._gated) == 0                       # noqa: SLF001


def test_a_pending_approval_records_nothing() -> None:
    """还挂着等的记录着么子——工具还没被拒，谈不上一自伤。"""
    host = _Host()
    host._approval = _FakeApproval([_pending("s1")])   # noqa: SLF001
    host._record_gate_stopped()                        # noqa: SLF001
    assert len(host._gated) == 0                       # noqa: SLF001


def test_an_allowed_approval_records_nothing() -> None:
    """放行了、工具真的跑了 → 它的失败是真实失败，不许被静音。"""
    host = _Host()
    host._approval = _FakeApproval([_gate_allowed("s1")])  # noqa: SLF001
    host._record_gate_stopped()                        # noqa: SLF001
    assert len(host._gated) == 0                       # noqa: SLF001


def test_an_expired_approval_is_recorded() -> None:
    host = _Host()
    host._approval = _FakeApproval([_gate_stopped("s1")])  # noqa: SLF001
    host._record_gate_stopped()                        # noqa: SLF001
    assert len(host._gated) == 1                       # noqa: SLF001


def test_recording_twice_keeps_one_entry() -> None:
    """轮询每 3 秒全扫一遍，不幂等的话 ``at`` 会被不断刷新、窗口永不过期。"""
    host = _Host()
    host._approval = _FakeApproval([_gate_stopped("s1")])  # noqa: SLF001
    for _ in range(5):
        host._record_gate_stopped()                    # noqa: SLF001
    assert len(host._gated) == 1                       # noqa: SLF001


def test_the_gate_caused_tool_result_never_reaches_the_chat() -> None:
    """**P1c 的核心。** 到期后那条 is_error 只闭环、不记账：
    既不进 pending_failures（没有「出事了」），也不留成孤儿 action
    （没有「有一步没有回包」）。"""
    at = _now() - 400
    host = _Host([_action_step(at, session="s1", action_id="tu_1"),
                  _failed_step(at + 1, session="s1", action_id="tu_1")])
    host._approval = _FakeApproval([_gate_stopped("s1")])  # noqa: SLF001
    host.poll()
    assert host.pushed == []
    # action 真的闭环了：留成孤儿的话 300 秒后会变成"未回包"
    assert host._tracker.state_of(AGENT, "s1").open_actions == {}  # noqa: SLF001
    for _ in range(3):
        assert host.poll() == []


def test_a_real_failure_in_the_same_session_still_speaks() -> None:
    """同会话里另一次调用失败了，不能因为"刚过期过一条审批"就一起静音。"""
    at = _now() - 400
    host = _Host([_action_step(at, session="s1", action_id="tu_1"),
                  _failed_step(at + 1, session="s1", action_id="tu_1"),
                  _failed_step(at + 2, session="s1", action_id="tu_2")])
    host._approval = _FakeApproval([_gate_stopped("s1")])  # noqa: SLF001
    host.poll()
    assert len(host.pushed) == 1
    assert "出事了" in host.pushed[0]["parts"][0]["text"]


def test_a_failure_in_another_session_still_speaks() -> None:
    at = _now() - 400
    host = _Host([_failed_step(at, session="s2", action_id="tu_9")])
    host._approval = _FakeApproval([_gate_stopped("s1")])  # noqa: SLF001
    host.poll()
    assert len(host.pushed) == 1


def test_an_allowed_tool_that_fails_is_a_real_failure() -> None:
    """反向钉死：审批放行了、工具跑了、它失败了——那必须报。"""
    at = _now() - 400
    host = _Host([_action_step(at, session="s1", action_id="tu_1"),
                  _failed_step(at + 1, session="s1", action_id="tu_1")])
    host._approval = _FakeApproval([_gate_allowed("s1")])  # noqa: SLF001
    host.poll()
    assert len(host.pushed) == 1
    assert "出事了" in host.pushed[0]["parts"][0]["text"]


def test_a_successful_tool_result_is_never_gate_caused() -> None:
    """只对 is_error 问这个问题。成功的 outcome 走 ok=True 分支，
    压根不进失败记账。"""
    host = _Host()
    assert host._is_gate_caused(                        # noqa: SLF001
        _ok_step(_now(), session="s1", action_id="tu_1")) is False


def test_the_degraded_path_is_logged_every_time() -> None:
    """拿不到 tool_use_id 时的降级必须留痕——静默的降级等于没有降级，
    真机上出了一次"该报的没报"，排查时唯一能依靠的就是这一行。"""
    at = _now() - 400
    host = _Host([_failed_step(at, session="s1", action_id="tu_1")])
    host._approval = _FakeApproval([_gate_stopped("s1", tool_use_id="")])  # noqa: SLF001
    host.poll()
    assert host.pushed == []
    assert any("降级规则静音" in line for line in host.logger.lines)


def test_the_degraded_path_only_saves_one_failure() -> None:
    """降级是"两边都没有 id"时的办法，一次审批只免一次。"""
    at = _now() - 400
    host = _Host([_failed_step(at, session="s1", action_id="tu_1"),
                  _failed_step(at + 1, session="s1", action_id="tu_2")])
    host._approval = _FakeApproval([_gate_stopped("s1", tool_use_id="")])  # noqa: SLF001
    host.poll()
    assert len(host.pushed) == 1, "降级路径把第二次失败也静音了"


# ── G. P1c(b)：同工作目录的重复通知合成一条 ─────────────────────────────

def test_three_sessions_in_one_directory_send_one_notice() -> None:
    """2026-11-24 真机：同目录三会话一字不差的三条 → 一条带数量的。"""
    at = _now() - 300
    events = [_failed_step(at, session=f"s{index}", action_id=f"tu_{index}",
                           working_directory=r"D:\proj")
              for index in range(3)]
    host = _Host(events)
    host.poll()
    assert len(host.pushed) == 1
    text = host.pushed[0]["parts"][0]["text"]
    assert "同目录另有 2 个会话" in text
    assert text.startswith(r"D:\proj")


def test_two_directories_stay_two_notices() -> None:
    """合并不是为了少说话，是为了不说重复的话。"""
    at = _now() - 300
    events = [_failed_step(at, session="s1", action_id="tu_1",
                           working_directory=r"D:\proj"),
              _failed_step(at, session="s2", action_id="tu_2",
                           working_directory=r"D:\other")]
    host = _Host(events)
    host.poll()
    assert len(host.pushed) == 2


def test_sessions_without_a_directory_are_not_merged() -> None:
    """``who`` 会退回 agent 名，而那是所有无目录会话的共同值。
    按它合并等于把互不相干的会话揉成一条。"""
    at = _now() - 300
    events = [_failed_step(at, session=f"s{index}", action_id=f"tu_{index}")
              for index in range(3)]
    host = _Host(events)
    host.poll()
    assert len(host.pushed) == 3


# ── H. P1c(b)：跨批次合并的接线 ─────────────────────────────────────────
#
# §G 证明"同一轮里合得好"。真机验收（2026-11-24 测试 8）证明这不够：
# 6 个会话 3 秒内失败，通知时间各自 +120s，跨过 3 秒轮询相位就分成 3 条。
# 这里证明合并器真的接在了 _poll_once 上、而且跨轮询仍然合。

class _FakeClock:
    """假时钟，替换 ``plugin_module.time``。

    ``offer`` 的 ``now`` 是判断的唯一依据，不可注入就等于没法测"窗口过了"。
    除了 ``time()`` 还要给 ``monotonic()``——``_poll_source`` 的限频用它。
    """

    def __init__(self, start: float) -> None:
        self.now = start
        self._mono = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self._mono += seconds


def _wired(events, *, window: float) -> tuple[_Host, _FakeClock]:
    """一个跑真 ``_poll_once`` 的宿主 + 假时钟。"""
    host = _Host(events, progress={"notice_merge_seconds": window})
    clock = _FakeClock(_now())
    host._merger = NoticeMerger(window=window)
    host._merge_window = window
    return host, clock


def test_two_sessions_failing_in_different_polls_send_one_notice(monkeypatch) -> None:
    """**真机测试 8 的直接复现。** 两个会话的失败落在不同轮询批次
    （间隔 3 秒 = 轮询周期），也必须合成一条。"""
    at = _now() - 300
    events = [_failed_step(at, session="s1", action_id="tu_1",
                           working_directory=r"D:\proj")]
    host, clock = _wired(events, window=20.0)
    monkeypatch.setattr(plugin_module, "time", clock)

    host._poll_once(host.config())                    # 第一轮：s1 失败
    assert host.pushed == []                          # 压着，窗口没到
    clock.advance(3.0)                                # 下一轮轮询

    # 第二个会话此时才失败（源吐新事件）
    host._transcript_adapter = _StubSource(
        [_failed_step(at, session="s2", action_id="tu_2",
                      working_directory=r"D:\proj")])
    host._poll_once(host.config())
    assert host.pushed == []                          # 还在同一个窗口里，继续压

    clock.advance(17.0)                               # 窗口到期
    host._poll_once(host.config())
    assert len(host.pushed) == 1, "跨批次没有合并——这正是真机测试 8 的根因"
    text = host.pushed[0]["parts"][0]["text"]
    assert text.startswith(r"D:\proj")
    assert "（同目录另有 1 个会话）" in text


def test_the_merger_survives_across_polls(monkeypatch) -> None:
    """合并器不能每轮重建——重建了就退回"只合同一轮"，也就是真机测试 8
    不过的那个状态。这是接线最容易被"优化"坏的一处：把
    ``NoticeMerger()`` 挪进 ``_poll_once`` 里看着更干净，功能却没了，
    而且没有任何测试会因此红（除了这一条）。"""
    host, clock = _wired([], window=20.0)
    monkeypatch.setattr(plugin_module, "time", clock)
    before = host._merger                                 # noqa: SLF001
    host._poll_once(host.config())
    clock.advance(3.0)
    host._poll_once(host.config())
    assert host._merger is before                         # noqa: SLF001


def test_the_window_comes_from_the_config(monkeypatch) -> None:
    """notice_merge_seconds 真的被读、真的能改。"""
    host = _Host()
    host._sync_merge_window({"notice_merge_seconds": 45})
    assert host._merge_window == 45                    # noqa: SLF001
    assert any("跨批合并窗口就绪" in line for line in host.logger.lines)
    # 坏值退回默认，不让观测循环崩
    host._sync_merge_window({"notice_merge_seconds": "半小时"})
    assert host._merge_window == NOTICE_MERGE_SECONDS   # noqa: SLF001


def test_the_merge_window_is_logged_separately() -> None:
    """**刻意不并进「进度状态机阈值就绪」那一行**：那一行是真机验收的
    主通过信号，任务包要求它一字不差。"""
    host = _Host()
    host._sync_merge_window({"notice_merge_seconds": 30})
    signal_lines = [line for line in host.logger.lines if "进度状态机阈值就绪" in line]
    assert signal_lines == []                          # 没被污染
    assert any("窗口=30s" in line for line in host.logger.lines)


def test_closing_the_window_releases_what_is_held(monkeypatch) -> None:
    """用户把窗口调成 0，压在手里的通知要立刻发出去——
    不然改个配置反而让几条通知永远消失。"""
    at = _now() - 300
    host, clock = _wired([_failed_step(at, session="s1", action_id="tu_1",
                                       working_directory=r"D:\proj")],
                         window=20.0)
    monkeypatch.setattr(plugin_module, "time", clock)
    host._poll_once(host.config())
    assert host.pushed == []

    host._sync_merge_window({"notice_merge_seconds": 0})
    host._poll_once(host.config())
    assert len(host.pushed) == 1
