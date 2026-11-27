"""tracker.py 三态灯状态机单测。

**这批测试的重点是"它不开口"**，不是"它能开口"。
任务书 §六第 4 条：全新实例 + 一串健康事件 → push 次数 = 0。
精度 > 召回的全部压力都落在这里：误报一次，用户就开始无视这个插件。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.progress import (
    Role,
    Signal,
    failure_is_loud,
)
from plugin.plugins.agent_deskmate.tracker import (
    ProgressTracker,
    TrackerConfig,
    _elapsed,
    notices_to_lines,
)

T0 = 1_700_000_000.0
WD = "D:\\proj"


def _sig(role: Role, *, at: float = T0, agent: str = "some-agent",
         session: str = "s1", workspace: str | None = WD, **kwargs) -> Signal:
    return Signal(role=role, agent=agent, session=session, workspace=workspace,
                  at=at, **kwargs)


def _action(at: float = T0, *, action_id: str = "tu_1", kind: str = "Bash",
            **kw) -> Signal:
    return _sig(Role.ACTION, at=at, action_id=action_id, kind=kind, **kw)


def _fail(at: float = T0, *, kind: str = "Bash", action_id: str | None = "tu_1",
          **kw) -> Signal:
    return _sig(Role.OUTCOME, at=at, action_id=action_id, ok=False, kind=kind,
                **kw)


def _ok(at: float = T0, *, action_id: str | None = "tu_1", kind: str = "Bash",
        **kw) -> Signal:
    return _sig(Role.OUTCOME, at=at, action_id=action_id, ok=True, kind=kind,
                **kw)


def _wait(at: float = T0, **kw) -> Signal:
    return _sig(Role.WAITING, at=at, reason="等「允许」", **kw)


def _boundary(at: float = T0, **kw) -> Signal:
    return _sig(Role.BOUNDARY, at=at, **kw)


def _confirm(tracker: ProgressTracker, at: float = T0) -> list:
    """把时钟推过失败确认窗口，取这一轮该播的失败通知。"""
    return tracker.tick(at + 121)


# ── 1. 默认静默 ──────────────────────────────────────────────────────────

def test_a_fresh_tracker_stays_silent_on_healthy_events() -> None:
    """任务书 §6.4：全新实例 + 一串健康事件 → 零条通知。"""
    tracker = ProgressTracker()
    for step in range(50):
        at = T0 + step
        assert tracker.feed(_action(at=at)) == []
        assert tracker.feed(_ok(at=at)) == []
    assert tracker.tick(T0 + 50) == []
    counts = tracker.counts()
    assert counts["running"] == 1
    assert counts["waiting"] == 0
    assert counts["error"] == 0


def test_ticking_a_hundred_times_on_a_healthy_session_pushes_nothing() -> None:
    """任务书 §2.3 的测试要求：tick 跑 100 次、状态健康 → push 数 = 0。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_ok(at=T0))
    for step in range(100):
        assert tracker.tick(T0 + step) == []


def test_only_the_actionable_roles_can_ever_speak() -> None:
    # action / boundary 的处理器存在，但开关关着——留分支是为了
    # 开关表改的时候有地方可改，也为了让这条断言能钉住"它真的没播"。
    tracker = ProgressTracker()
    assert tracker.feed(_action(at=T0)) == []
    assert tracker.feed(_boundary(at=T0 + 3600)) == []


# ── 2. 🔴 失败 ───────────────────────────────────────────────────────────

def test_a_loud_failure_speaks_once_it_is_confirmed() -> None:
    """失败不立刻播——先看 Agent 能不能自己恢复。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    assert tracker.feed(_fail(at=T0 + 1)) == []      # 先不开口
    notices = _confirm(tracker, T0 + 1)
    assert len(notices) == 1
    notice = notices[0]
    assert notice.role is Role.OUTCOME
    assert notice.light.value == "error"
    assert notice.headline == f"{WD} 出事了"
    assert "Bash" in notice.detail
    assert tracker.counts()["error"] == 1


def test_a_failure_the_agent_recovers_from_is_never_announced() -> None:
    """本批最重要的一条。实测：一个 80 小时会话里 93 次响亮失败，
    **85 次在 120 秒内自己恢复了**（中位数约 15 秒）——Agent 改个参数
    重试、换个写法重试，都是常态。那些用户根本不需要知道，报了就是狼来了。
    通知量因此从 93 降到 8。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_1"))
    tracker.feed(_fail(at=T0 + 1, action_id="tu_1"))
    # Agent 自己恢复：换个写法重试，成功了
    tracker.feed(_action(at=T0 + 20, action_id="tu_2"))
    tracker.feed(_ok(at=T0 + 21, action_id="tu_2"))
    assert _confirm(tracker, T0 + 21) == []
    # 之后再也不该提这件事
    assert tracker.tick(T0 + 10000) == []


def test_the_light_still_flashes_on_a_recovered_failure() -> None:
    """恢复了的失败不播报，但灯要真红过——否则"它出错了吗"事后查不到。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_1"))
    tracker.feed(_fail(at=T0 + 1, action_id="tu_1"))
    state = tracker.state_of("some-agent", "s1")
    assert state is not None
    assert state.light.value == "error"
    tracker.feed(_ok(at=T0 + 20, action_id="tu_2", kind="Edit"))
    assert state.light.value == "running"


def test_a_failure_is_not_announced_before_the_window_closes() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_fail(at=T0 + 1))
    # 从失败那一刻算起，差一秒都不行
    assert tracker.tick(T0 + 120) == []
    assert len(tracker.tick(T0 + 121)) == 1


def test_repeated_failures_do_not_postpone_the_first_confirmation() -> None:
    """每 100 秒失败一次，不许把首次确认无限期推后。"""
    tracker = ProgressTracker()
    for step in range(5):
        at = T0 + step * 100
        tracker.feed(_action(at=at, action_id=f"tu_{step}"))
        tracker.feed(_fail(at=at, action_id=f"tu_{step}"))
    notices = tracker.tick(T0 + 500)
    assert any(n.fingerprint.startswith("fail|") for n in notices)


def test_parallel_actions_each_pair_with_their_own_outcome() -> None:
    """真机 dry run 抓到的设计级 bug，这条是它的回归测试。

    Claude Code 会并行发多个工具调用。`open_actions` 曾经是**单槽**，
    后一个 action 覆盖前一个，前一个的 outcome 永远配不上——
    实测一个会话里 105 条"失败"全是这么来的假货，
    占该会话通知数的 55%。单槽改 dict 才对。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_a", kind="Bash"))
    tracker.feed(_action(at=T0 + 1, action_id="tu_b", kind="Edit"))
    # tu_a 失败：待确认，且带的是 tu_a 自己的工具名（不是被 tu_b 覆盖）
    tracker.feed(_fail(at=T0 + 2, action_id="tu_a", kind="Bash"))
    state = tracker.state_of("some-agent", "s1")
    assert state is not None
    assert state.pending_failures == {"Bash": [T0 + 2, 1]}
    # tu_b 成功闭环，且**它自己那个槽被正确清掉**
    tracker.feed(_ok(at=T0 + 3, action_id="tu_b", kind="Edit"))
    assert state.open_actions == {}
    # 注意：tu_b 的成功同时把 tu_a 的待确认失败取消了——它自己恢复了，
    # 那不是用户的事。这条语义由 test_a_failure_the_agent_recovers_from_
    # is_never_announced 单独钉，这里只验配对。
    assert state.pending_failures == {}


def test_an_unrecovered_parallel_failure_carries_its_own_tool_name() -> None:
    """配对正确的直接证据：通知里的工具名来自配上的那个 action。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_a", kind="Bash"))
    tracker.feed(_action(at=T0 + 1, action_id="tu_b", kind="Edit"))
    tracker.feed(_fail(at=T0 + 2, action_id="tu_a", kind="Bash"))
    notices = _confirm(tracker, T0 + 2)
    assert [n.kind for n in notices] == ["Bash"]
    assert "Bash" in notices[0].detail


def test_a_parallel_action_left_open_is_reported_individually() -> None:
    """并行里只闭环了一个，另一个该单独报未回包，不是被吞掉。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_a", kind="Bash"))
    tracker.feed(_action(at=T0 + 1, action_id="tu_b", kind="Edit"))
    tracker.feed(_ok(at=T0 + 2, action_id="tu_a", kind="Bash"))
    notices = tracker.tick(T0 + 400)
    assert len(notices) == 1
    assert "tu_b" in notices[0].fingerprint
    assert "Edit" in notices[0].detail


def test_an_orphan_action_does_not_become_a_fake_failure() -> None:
    """闭环不了的动作不许伪装成"失败"——那正是单槽时代的 105 条假货。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_a", kind="Bash"))
    tracker.feed(_action(at=T0 + 1, action_id="tu_b", kind="Bash"))
    # tu_a 的 outcome 迟到了，但 tu_b 还开着——不许把 tu_b 判成失败
    tracker.feed(_fail(at=T0 + 2, action_id="tu_a", kind="Bash"))
    notices = _confirm(tracker, T0 + 2)
    assert len(notices) == 1
    assert notices[0].kind == "Bash"
    state = tracker.state_of("some-agent", "s1")
    assert state is not None
    assert set(state.open_actions) == {"tu_b"}


def test_a_boundary_clears_every_orphan_not_just_one() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_a"))
    tracker.feed(_action(at=T0 + 1, action_id="tu_b"))
    tracker.feed(_action(at=T0 + 2, action_id="tu_c"))
    tracker.feed(_boundary(at=T0 + 3))
    assert tracker.tick(T0 + 400) == []


def test_the_same_kind_failing_ten_times_pushes_one_primary() -> None:
    """任务书 §6.5：同一 kind 连错 10 次 → **主通知** 只播 1 条（指纹合并）。

    注意这里数的是主通知。10 次全没恢复时会响 3 条：1 条「出事了」
    + 2 条「在兜圈子」（第 3、5 次升级）——后者的条数由 §6.6 单独钉。
    """
    tracker = ProgressTracker()
    for step in range(10):
        at = T0 + step          # 每步 1 秒，远在 120 s 合并窗口内
        tracker.feed(_action(at=at, action_id=f"tu_{step}"))
        tracker.feed(_fail(at=at, action_id=f"tu_{step}"))
    notices = _confirm(tracker, T0 + 9)
    primaries = [n for n in notices if n.fingerprint.startswith("fail|")]
    assert len(primaries) == 1


def test_the_same_failure_after_the_window_speaks_again() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_fail(at=T0 + 1))
    # 窗口内第二次：同一个待确认条目，不新增
    tracker.feed(_fail(at=T0 + 60))
    # 第三次失败：重试链当场升级（连续失败本身就是模式，不必等确认）
    escalated = tracker.feed(_fail(at=T0 + 200))
    assert any(n.fingerprint.startswith("retry|") for n in escalated)
    # 主通知则要等确认窗口过去
    confirmed = tracker.tick(T0 + 201)
    assert [n.fingerprint for n in confirmed] == ["fail|some-agent|s1|Bash"]


def test_a_read_only_failure_stays_silent() -> None:
    """grep 没匹配到退出码 1、文件不存在——那是搜索的正常结果。"""
    tracker = ProgressTracker()
    for kind in ("Grep", "Read", "Glob", "LS", "find"):
        tracker.feed(_action(at=T0, action_id="tu_1", kind=kind))
        assert tracker.feed(_fail(at=T0 + 1, kind=kind)) == []
    assert tracker.counts()["error"] == 0


def test_a_write_or_execute_failure_is_loud() -> None:
    for kind in ("Bash", "Edit", "Write", "WebFetch"):
        tracker = ProgressTracker()
        tracker.feed(_action(at=T0, action_id="tu_1", kind=kind))
        tracker.feed(_fail(at=T0 + 1, kind=kind))
        notices = _confirm(tracker, T0 + 1)
        assert len(notices) == 1, f"{kind} 失败应该开口"


def test_an_unpaired_failure_is_loud() -> None:
    # 认不出是哪个工具的失败（会话级错误，如 api_error）当然该响。
    assert failure_is_loud("") is True
    tracker = ProgressTracker()
    tracker.feed(_fail(at=T0, action_id=None, kind=""))
    assert len(_confirm(tracker, T0)) == 1


# ── 3. 重试链 ────────────────────────────────────────────────────────────

def test_the_retry_chain_escalates_exactly_twice() -> None:
    """任务书 §6.6：同 kind 连错 10 次 → retry 相关 push = 2（第 3、5 次）。"""
    tracker = ProgressTracker()
    retry_pushes = 0
    for step in range(10):
        at = T0 + step * 200        # 跨过 120 s 合并窗口，让每次都算一次失败
        tracker.feed(_action(at=at, action_id=f"tu_{step}", kind="Edit"))
        notices = tracker.feed(_fail(at=at, action_id=f"tu_{step}", kind="Edit"))
        retry_pushes += sum(1 for n in notices if n.fingerprint.startswith("retry"))
    assert retry_pushes == 2


def test_a_success_resets_the_failure_streak() -> None:
    tracker = ProgressTracker()
    for step in range(2):
        at = T0 + step * 200
        tracker.feed(_action(at=at, action_id=f"tu_{step}"))
        tracker.feed(_fail(at=at, action_id=f"tu_{step}"))
    tracker.feed(_action(at=T0 + 1000, action_id="tu_ok"))
    tracker.feed(_ok(at=T0 + 1001, action_id="tu_ok"))
    # 之前失败 2 次，现在清零；再失败一次不该触发第 3 档升级
    tracker.feed(_action(at=T0 + 2000, action_id="tu_x"))
    notices = tracker.feed(_fail(at=T0 + 2001, action_id="tu_x"))
    assert not any(n.fingerprint.startswith("retry") for n in notices)


# ── 4. 🟡 在等你 ─────────────────────────────────────────────────────────

def test_waiting_speaks_immediately_and_names_the_workspace() -> None:
    tracker = ProgressTracker()
    notices = tracker.feed(_wait(at=T0))
    assert len(notices) == 1
    notice = notices[0]
    assert notice.light.value == "waiting"
    assert notice.headline == f"{WD} 在等你"
    assert "允许" in notice.detail
    assert tracker.counts()["waiting"] == 1


def test_each_waiting_episode_reports_once() -> None:
    tracker = ProgressTracker()
    assert len(tracker.feed(_wait(at=T0))) == 1
    assert tracker.feed(_wait(at=T0 + 5)) == []
    assert tracker.feed(_wait(at=T0 + 100)) == []


def test_waiting_escalates_once_after_the_threshold() -> None:
    tracker = ProgressTracker()
    tracker.feed(_wait(at=T0))
    escalated = tracker.tick(T0 + 180)
    assert len(escalated) == 1
    assert "还在等你" in escalated[0].headline
    assert "3 分" in escalated[0].detail
    # 再 tick 一百次也不重复
    for step in range(100):
        assert tracker.tick(T0 + 181 + step) == []


def test_waiting_does_not_escalate_before_the_threshold() -> None:
    tracker = ProgressTracker()
    tracker.feed(_wait(at=T0))
    assert tracker.tick(T0 + 179) == []


def test_handling_it_ends_the_episode() -> None:
    """用户处理完了（出现新 action）→ 不再是"在等你"，也不会再升级。"""
    tracker = ProgressTracker()
    tracker.feed(_wait(at=T0))
    tracker.feed(_action(at=T0 + 10))
    tracker.feed(_ok(at=T0 + 11))
    assert tracker.counts()["waiting"] == 0
    assert tracker.tick(T0 + 400) == []


# ── 5. 未回包超时（唯一必须靠 tick 的跃迁）──────────────────────────────

def test_an_unclosed_action_reports_exactly_once() -> None:
    """任务书 §6.8：action 后永不配 outcome → 过阈值恰好播一条；
    然后 tick 再跑 100 次 → 不再播。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    assert tracker.tick(T0 + 299) == []
    notices = tracker.tick(T0 + 300)
    assert len(notices) == 1
    assert notices[0].light.value == "error"
    assert "没有回包" in notices[0].headline
    for step in range(100):
        assert tracker.tick(T0 + 301 + step) == []


def test_an_outcome_arriving_late_cancels_the_unclosed_alarm() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    # 迟到的成功结果在超时之前到达
    tracker.feed(_ok(at=T0 + 250, action_id="tu_1"))
    assert tracker.tick(T0 + 400) == []
    assert tracker.counts()["error"] == 0


def test_a_boundary_clears_the_orphan_action() -> None:
    """一轮结束了还有动作没闭环——别让它在 300 秒后变假警报。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_boundary(at=T0 + 10))
    assert tracker.tick(T0 + 400) == []


def test_no_new_events_is_not_the_same_as_stuck() -> None:
    """实测有无新记录的空档达 12 h / 36 h，那些是会话根本没动。
    未回包的判据必须是"有 action 无配对 outcome"，不是"多久没新事件"。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_ok(at=T0 + 1))
    assert tracker.tick(T0 + 36 * 3600) == []


# ── 5b. 审批挂起的会话：跳过评估，不是过滤通知 ────────────────────────────
#
# 2026-11-24 真机：三张审批卡挂着等人，300 秒后连发三条
# 「有一步没有回包 / 它可能卡住了」。真实原因是**权限请求在等审批**
# （hook 阻塞），Agent 根本没卡——三条全是假警报。
#
# 修法的关键在"跳过评估"而不是"过滤通知"：评估一旦跑过，action_id 就进
# ``unclosed_reported``，**从此永远不再报**。等人那几分钟恰好被这一口吞掉，
# 等用户点完卡、Agent 真的卡死了，该报的那一次已经不报了。
# 精度变成了漏报，而漏报正是这个插件存在的理由。

def _blocked_key(tracker: ProgressTracker, agent: str = "some-agent",
                 session: str = "s1") -> tuple[str, str]:
    return (agent, session)


def test_a_session_waiting_on_an_approval_is_not_evaluated() -> None:
    """审批挂着 → 那一轮不评估未回包。假警报当场消失。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    assert tracker.tick(T0 + 300, [_blocked_key(tracker)]) == []


def test_the_block_does_not_burn_the_action_permanently() -> None:
    """**这一条是 P1b 的核心。** 跳过评估 ≠ 把通知吞掉。

    审批挂着的那几分钟里 ``unclosed_reported`` 不许被写进去；否则用户点完卡、
    Agent 真的卡死了，该报的那一次已经不报了——精度变成了漏报。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    blocked = [_blocked_key(tracker)]

    # 审批挂着，连转 100 圈：一次都不该响
    for step in range(100):
        assert tracker.tick(T0 + 300 + step * 3, blocked) == []

    # 用户点完卡、审批了结 → 阻塞解除，Agent 却真的再也没回来
    notices = tracker.tick(T0 + 300 + 100 * 3)
    assert len(notices) == 1, "阻塞解除后该报的那一次被提前吞掉了"
    assert "没有回包" in notices[0].headline


def test_a_blocked_session_still_gets_its_other_transitions() -> None:
    """边界要写明：阻塞只免掉「未回包」一种评估。

    失败确认、重试链、在等你 照旧——那些不是 hook 阻塞造成的，
    拿审批当理由把它们一起静音才是漏报。（审批过期造成的自伤连锁
    由 tool_use_id 关联在桥上层处理，不在这里加宽。）
    """
    tracker = ProgressTracker()
    tracker.feed(_fail(at=T0))                       # 一次响亮失败
    blocked = [_blocked_key(tracker)]
    assert tracker.tick(T0 + 119, blocked) == []      # 确认窗口内，与阻塞无关
    notices = tracker.tick(T0 + 120, blocked)
    assert len(notices) == 1
    assert "出事了" in notices[0].headline


def test_blocking_one_session_does_not_silence_another() -> None:
    """三个会话同时挂着卡，只静音挂着的那几个。

    同目录多会话是真机常态；按 session 精确屏蔽，不能按 workspace 一刀切。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, session="s1"))
    tracker.feed(_action(at=T0, session="s2"))
    blocked = [_blocked_key(tracker, session="s1")]
    notices = tracker.tick(T0 + 300, blocked)
    assert len(notices) == 1
    assert notices[0].session == "s2"


def test_the_block_key_matches_the_tracker_bucket_key() -> None:
    """桶键是 ``(agent, session)`` 二元组，不是裸 session id。

    两个 Agent 撞同一个 session id 是实测存在的（`test_two_agents_sharing_a_
    session_id_do_not_collide`）。只按 session 屏蔽会连带静音另一个 Agent
    的真卡死——所以这一条把形状钉死：塞裸字符串进来必须匹配不上任何桶。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    notices = tracker.tick(T0 + 300, ["s1"])           # 裸字符串，形状不对
    assert len(notices) == 1, "裸 session id 把另一个 Agent 的桶也屏蔽了"
    assert tracker.tick(T0 + 301, [("some-agent", "s1")]) == []


# ── 5d. 闸门解除：作废被扣住的动作，别把旧账翻出来 ────────────────────────
#
# 2026-11-24 真机（0.6.0 验收 §3）：卡片 12:33 弹出、12:42:53 到期，
# 12:44:09 宿主张口就是一条「有一步没有回包 / Bash 执行了 10 分 46 秒」。
# 那个动作 12:33 就发出去了，没有结果是因为**插件扣着它等审批**。
# 而到期按设计不回填，所以那个 outcome 永远不会来——
# 每一张没人点的卡，到期后都必然产出这么一条假警报。


def test_a_released_gate_does_not_report_the_action_it_held() -> None:
    """挂起解除的那一刻，被扣住的动作不许变成「有一步没有回包」。

    ``tick(blocked=...)`` 只挡得住挂着的期间：挂起标记一消失，
    早就过了 300s 的旧动作立刻被判成"它可能卡住了"，还把耗时写成
    一个夸大的数字。所以解除时要作废它。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    key = _blocked_key(tracker)
    assert tracker.tick(T0 + 300, [key]) == []          # 挂着，静音
    tracker.release_gate([key])
    assert tracker.tick(T0 + 700, [key]) == [], "旧账被翻出来了"


def test_releasing_before_ticking_is_what_makes_it_work() -> None:
    """顺序钉死：先作废再评估。

    反了的话这一轮 tick 先把假警报连同一个夸大的耗时发出去，
    作废再来已经是马后炮——调用方看一眼这条就该知道怎么排。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    # 故意不调 release_gate——"忘了先释放"就是这个结果。
    notices = tracker.tick(T0 + 700, [])
    assert len(notices) == 1 and "没有回包" in notices[0].headline


def test_releasing_one_session_leaves_the_others_alone() -> None:
    """同目录多会话同时挂着卡是常态，只作废解除的那几个。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, session="s1"))
    tracker.feed(_action(at=T0, session="s2"))
    tracker.release_gate([_blocked_key(tracker, session="s1")])
    notices = tracker.tick(T0 + 700, [])
    assert [n.session for n in notices] == ["s2"]


def test_releasing_keeps_the_session_usable_for_what_comes_next() -> None:
    """作废不是把会话扔掉：之后新来的动作照旧计时、照旧能报。

    否则"审批跑过一次的会话从此不再报未回包"是用漏报换精度。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    key = _blocked_key(tracker)
    tracker.release_gate([key])
    tracker.feed(_action(at=T0 + 800, action_id="tu_2"))
    notices = tracker.tick(T0 + 1100, [])
    assert len(notices) == 1
    assert notices[0].session == "s1"


def test_an_outcome_arriving_after_the_release_is_still_processed() -> None:
    """释放之后结果才落盘（回填慢了一拍）——不许当成没发生过。

    这是"整会话作废"唯一的风险点，所以单独钉一条：配不上 action 时
    ``_on_outcome`` 走另一条分支，成功照样清零、失败照样进确认窗口。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    key = _blocked_key(tracker)
    tracker.release_gate([key])
    tracker.feed(_fail(at=T0 + 400))
    notices = tracker.tick(T0 + 520, [])
    assert len(notices) == 1 and "出事了" in notices[0].headline


def test_releasing_a_session_with_nothing_open_is_a_no_op() -> None:
    """没动作可作废就安静返回，别为这件事发声或报错。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_ok(at=T0 + 10))
    key = _blocked_key(tracker)
    assert tracker.release_gate([key]) == {}
    assert tracker.tick(T0 + 700, []) == []


def test_release_reports_what_it_dropped() -> None:
    """返回值是各会话作废数——桥要靠它记日志。

    静默的抑制等于没有抑制：真机上出了一次"该报的没报"，
    排查时唯一能依靠的就是那一行。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, action_id="tu_1"))
    tracker.feed(_action(at=T0 + 1, action_id="tu_2"))
    key = _blocked_key(tracker)
    assert tracker.release_gate([key]) == {key: 2}


# ── 5c. 自伤失败：闸门造成的失败不记到 Agent 头上 ────────────────────────
#
# 2026-11-24 真机：审批 570s 到期 → Claude 收到 tool_result is_error=true
# → 120 秒后连发三条「出事了 / 它没自己恢复」（三个会话，文案一字不差）。
# 失败是插件的闸门造成的，却被报成 Agent 故障。

def test_a_gate_caused_failure_says_nothing_and_closes_the_action() -> None:
    """只闭环、不记账。action 必须被清掉——否则它 300 秒后变成
    「有一步没有回包」，用一个假警报换掉另一个假警报。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_sig(Role.OUTCOME, at=T0 + 1, action_id="tu_1", ok=False,
                      self_inflicted=True))
    assert tracker.state_of("some-agent", "s1").open_actions == {}
    for step in range(200):
        assert tracker.tick(T0 + 600 + step * 3) == []


def test_a_gate_caused_failure_is_not_confirmed_later() -> None:
    """不进 pending_failures——120 秒后就没有「出事了」可播。"""
    tracker = ProgressTracker()
    tracker.feed(_sig(Role.OUTCOME, at=T0, action_id="tu_1", ok=False,
                      self_inflicted=True))
    assert tracker.tick(T0 + 600) == []


def test_a_gate_caused_failure_does_not_feed_the_retry_chain() -> None:
    """不动 fail_streak——否则连挂三次审批就是「在兜圈子」。"""
    tracker = ProgressTracker()
    for step in range(4):
        tracker.feed(_sig(Role.OUTCOME, at=T0 + step, action_id=f"tu_{step}",
                          ok=False, self_inflicted=True))
    assert tracker.tick(T0 + 600) == []


def test_a_gate_caused_failure_does_not_erase_a_real_pending_failure() -> None:
    """**不能用漏报换漏报。** 同会话里一次真实的未恢复失败不能被
    自伤失败一起抹掉——那只是把假警报换成了漏报。"""
    tracker = ProgressTracker()
    tracker.feed(_fail(at=T0, action_id="tu_1"))              # 真实失败
    tracker.feed(_sig(Role.OUTCOME, at=T0 + 1, action_id="tu_2",
                      ok=False, self_inflicted=True))        # 闸门造成的
    notices = tracker.tick(T0 + 121)
    assert [n.headline for n in notices] == [f"{WD} 出事了"]


def test_a_gate_caused_failure_leaves_the_light_alone() -> None:
    """灯也不该被它拉回 🟢——上一次真实的失败还红着。"""
    tracker = ProgressTracker()
    tracker.feed(_fail(at=T0, action_id="tu_1"))
    assert tracker.state_of("some-agent", "s1").light.value == "error"
    tracker.feed(_sig(Role.OUTCOME, at=T0 + 1, action_id="tu_2",
                      ok=False, self_inflicted=True))
    assert tracker.state_of("some-agent", "s1").light.value == "error"


def test_a_gate_caused_flag_on_a_non_outcome_is_rejected() -> None:
    """同 ok：形状错了当场炸，不要留成一个解释不了的幽灵状态。"""
    with pytest.raises(ValueError, match="self_inflicted"):
        _sig(Role.ACTION, at=T0, action_id="tu_1", self_inflicted=True)


def test_a_real_failure_is_not_silenced_by_the_flag() -> None:
    """反方向也要钉：不带这个字段的失败照旧播。"""
    tracker = ProgressTracker()
    tracker.feed(_fail(at=T0))
    notices = tracker.tick(T0 + 121)
    assert len(notices) == 1
    assert "出事了" in notices[0].headline


# ── 6. 指名与身份 ────────────────────────────────────────────────────────

def test_notices_name_the_workspace_not_the_session_id() -> None:
    """任务书 §6.9：🟡 通知文本含 workspace、不含 session id。"""
    tracker = ProgressTracker()
    notice = tracker.feed(_wait(at=T0, session="abc123-def456"))[0]
    assert WD in notice.headline
    assert "abc123" not in notice.headline
    assert "abc123" not in notice.detail


def test_two_agents_sharing_a_session_id_do_not_collide() -> None:
    tracker = ProgressTracker()
    tracker.feed(_wait(at=T0, agent="cc", session="same"))
    tracker.feed(_wait(at=T0, agent="codex", session="same"))
    # 两条都是各自会话的第一条"等你"，该各报一条
    assert len(tracker.feed(_wait(at=T0, agent="cc", session="same"))) == 0
    notices = tracker.sessions()
    waiting = [s for s in notices if s.light.value == "waiting"]
    assert len(waiting) == 2


def test_a_failure_in_one_agent_does_not_silence_another() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, agent="cc", session="s"))
    tracker.feed(_action(at=T0, agent="codex", session="s"))
    tracker.feed(_fail(at=T0 + 1, agent="cc", session="s"))
    tracker.feed(_fail(at=T0 + 1, agent="codex", session="s"))
    confirmed = _confirm(tracker, T0 + 1)
    assert len(confirmed) == 2
    assert {n.agent for n in confirmed} == {"cc", "codex"}


def test_sessions_are_sorted_by_what_they_owe_you() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, agent="a", session="1"))
    tracker.feed(_fail(at=T0 + 1, agent="a", session="1"))
    tracker.feed(_wait(at=T0 + 2, agent="b", session="2"))
    order = [s.light.value for s in tracker.sessions()]
    assert order[0] == "waiting"      # 🟡 顶最上


# ── 7. 状态机不认 Agent ──────────────────────────────────────────────────

def test_the_state_machine_names_no_specific_agent() -> None:
    """任务书 §6.12 的状态机部分：用不含任何 Agent 字样的合成 Signal 驱动。"""
    tracker = ProgressTracker()
    names = ("alpha", "beta", "gamma", "delta")
    for name in names:
        assert tracker.feed(_action(at=T0, agent=name, session="s")) == []
        tracker.feed(_fail(at=T0 + 1, agent=name, session="s"))
        confirmed = _confirm(tracker, T0 + 1)
        assert len(confirmed) == 1
        assert confirmed[0].light.value == "error"
        assert tracker.feed(_wait(at=T0 + 2, agent=name, session="s")) != []


# ── 8. 灯的变化 ──────────────────────────────────────────────────────────

def test_a_success_after_a_failure_moves_the_light_back() -> None:
    """一次失败之后灯必须能变回 🟢，否则它就再也不变了。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_fail(at=T0 + 1))
    assert tracker.counts()["error"] == 1
    tracker.feed(_action(at=T0 + 100, action_id="tu_2"))
    tracker.feed(_ok(at=T0 + 101, action_id="tu_2"))
    assert tracker.counts()["error"] == 0
    assert tracker.counts()["running"] == 1


def test_boundary_returns_to_idle() -> None:
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_boundary(at=T0 + 10))
    assert tracker.counts()["idle"] == 1


def test_the_light_stays_error_until_something_actually_succeeds() -> None:
    """失败之后灯不许被一个新动作擦掉——用户还没看到那条错误呢。

    只有一次**成功的 outcome** 能把 🔴 拉回 🟢。这条是刻意的：
    否则 Agent 失败后立刻换一招，灯就绿了，用户正好在那时候扫一眼。
    """
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0))
    tracker.feed(_fail(at=T0 + 1))
    tracker.feed(_action(at=T0 + 2, action_id="tu_2"))
    state = tracker.state_of("some-agent", "s1")
    assert state is not None
    assert state.light.value == "error"
    tracker.feed(_ok(at=T0 + 3, action_id="tu_2"))
    assert state.light.value == "running"


# ── 9. 阈值可调 ──────────────────────────────────────────────────────────

def test_thresholds_are_injectable() -> None:
    fast = TrackerConfig(unclosed_action_seconds=10.0,
                         wait_escalate_seconds=10.0,
                         fail_coalesce_seconds=5.0)
    tracker = ProgressTracker(fast)
    tracker.feed(_action(at=T0))
    assert tracker.tick(T0 + 9) == []
    assert len(tracker.tick(T0 + 10)) == 1


def test_the_default_thresholds_are_the_documented_ones() -> None:
    cfg = TrackerConfig()
    assert cfg.wait_escalate_seconds == 180.0
    assert cfg.unclosed_action_seconds == 300.0
    assert cfg.boundary_round_seconds == 600.0
    assert cfg.fail_coalesce_seconds == 120.0
    assert cfg.retry_escalate_at == (3, 5)


def test_the_confirm_window_itself_is_the_loudness_knob() -> None:
    """把确认窗口开到极大 = 什么失败都不播（精度 > 召回的极端档）。

    这是比"关掉某个角色"更细的旋钮：错误照样记进灯和审计，
    只是不开口。开窗到 0 则退化成"失败立刻播"。
    """
    never = ProgressTracker(TrackerConfig(failure_confirm_seconds=1e12))
    never.feed(_action(at=T0))
    never.feed(_fail(at=T0 + 1))
    assert never.tick(T0 + 10 ** 9) == []
    # 但灯确实红过——不是没检测到
    state = never.state_of("some-agent", "s1")
    assert state is not None
    assert state.light.value == "error"

    instant = ProgressTracker(TrackerConfig(failure_confirm_seconds=0.0))
    instant.feed(_action(at=T0))
    instant.feed(_fail(at=T0 + 1))
    assert len(instant.tick(T0 + 1)) == 1


# ── 10. 通知格式 ─────────────────────────────────────────────────────────

def test_a_notice_is_exactly_two_lines() -> None:
    """B12 P0-2：固定三行，不加第四行。"""
    tracker = ProgressTracker()
    notice = tracker.feed(_wait(at=T0))[0]
    assert notice.lines() == (notice.headline, notice.detail)


def test_notices_to_lines_flattens_in_order() -> None:
    tracker = ProgressTracker()
    tracker.feed(_wait(at=T0))
    lines = notices_to_lines(tracker.feed(_wait(at=T0 + 1, agent="b",
                                                session="s2")))
    assert len(lines) == 2
    assert all(isinstance(line, str) for line in lines)


@pytest.mark.parametrize("seconds,expected", [
    (0, "不到 1 分钟"),
    (59, "不到 1 分钟"),
    (60, "1 分 0 秒"),
    (252, "4 分 12 秒"),
    (3600, "1 小时 0 分"),
    (3720, "1 小时 2 分"),
])
def test_elapsed_reads_like_a_human(seconds: float, expected: str) -> None:
    assert _elapsed(seconds) == expected


def test_state_of_returns_none_for_an_unknown_session() -> None:
    tracker = ProgressTracker()
    assert tracker.state_of("nope", "nope") is None


def test_a_workspace_seen_later_fills_in_the_name() -> None:
    """第一条信号可能没有 workspace，后面的补上——指名不能永远缺一角。"""
    tracker = ProgressTracker()
    tracker.feed(_action(at=T0, workspace=None))
    tracker.feed(_wait(at=T0 + 1, workspace=WD))
    state = tracker.state_of("some-agent", "s1")
    assert state is not None
    assert state.workspace == WD
    notice = tracker.feed(_wait(at=T0 + 2, agent="c", session="s9",
                                workspace=None))
    # 新会话没有 workspace，只能退回 agent 名
    assert notice[0].who == "c"


# ── 「这一轮跑完了」：2026-11-26 起产品默认开口 ─────────────────────────
#
# 契约默认仍是关（progress.NOTIFY_DEFAULTS + test_progress.py 钉着不动），
# 产品默认由 __init__ 从 boundary_notify 传 extra_notify_roles 进来。
# 数据类本身保持中立——"契约默认是什么"和"这个插件默认给什么"是两件事。


def _tracker(**kw):
    cfg = TrackerConfig(**kw)
    return ProgressTracker(config=cfg)


def test_boundary_stays_quiet_under_the_bare_contract() -> None:
    """不传 extra_notify_roles 时，契约默认（关）说了算。

    这条钉的是"数据类不替产品做决定"——否则契约测试会被绕过。
    """
    tracker = _tracker()
    assert tracker.feed(_boundary(at=T0 + 3600)) == []


def test_boundary_speaks_when_the_product_opts_in() -> None:
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    out = tracker.feed(_boundary(at=T0 + 3600))
    assert [n.role for n in out] == ["boundary"]
    assert "这一轮跑完了" in out[0].headline


def test_the_round_tally_lands_in_the_detail() -> None:
    """detail 要说出这一轮用了什么——只报类别，不报内容。"""
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    tracker.feed(_action(kind="Bash"))
    tracker.feed(_action(kind="Bash"))
    tracker.feed(_action(kind="Edit"))
    out = tracker.feed(_boundary(at=T0 + 3600))
    assert "Bash 2 次" in out[0].detail
    assert "Edit 1 次" in out[0].detail
    assert "共 3 步" in out[0].detail


def test_the_tally_is_ordered_by_count_not_by_name() -> None:
    """多的在前——用户先看到它主要在干什么。"""
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    for _ in range(3):
        tracker.feed(_action(kind="Edit"))
    tracker.feed(_action(kind="Bash"))
    out = tracker.feed(_boundary(at=T0 + 3600))
    assert out[0].detail.index("Edit") < out[0].detail.index("Bash")


def test_the_tally_resets_each_round() -> None:
    """不清零的话，第二轮会把第一轮的账一起算上。"""
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    tracker.feed(_action(kind="Bash"))
    first = tracker.feed(_boundary(at=T0 + 3600))
    assert "Bash 1 次" in first[0].detail
    second = tracker.feed(_boundary(at=T0 + 7200))
    assert "Bash" not in second[0].detail


def test_an_empty_kind_is_not_tallied() -> None:
    """空 kind 表示"说不清是什么工具"，记了只会多一个没有名字的项。"""
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    tracker.feed(_action(kind=""))
    out = tracker.feed(_boundary(at=T0 + 3600))
    assert "次" not in out[0].detail
    assert "共 1 步" in out[0].detail


def test_the_tally_never_carries_command_text() -> None:
    """**隐私红线的结构性保证**：detail 里只有类别名，没有任何路径/命令。

    ``Signal`` 没有内容字段（test_progress.py 钉着），所以这里断言的是
    "我们没把类别以外的任何东西拼进给人看的句子"。
    """
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    tracker.feed(_action(kind="Bash"))
    out = tracker.feed(_boundary(at=T0 + 3600))
    text = out[0].detail
    for banned in ("rm", "-rf", "/", "\\", ".."):
        assert banned not in text, banned


# ── 念的那一句：短、能念、没有路径 ──────────────────────────────────────
#
# 2026-11-26 真机：Yui 把整条通知原文念了出来——
# 「claude-code · D:\workspace\…\agent_deskmate 这一轮跑完了
#  第 1 轮，共 2 步（Bash 2 次） · 会话 xxxxxxxx」。
# 用户原话：「他把这句话读出来了，他真就读出来了」
# 「特别是直接读路径，那个更不应该」。
#
# 路径与会话 id 是给人复制粘贴的，不是给人听的。


def test_the_spoken_line_has_no_path_and_no_uuid() -> None:
    """**这次的真 bug**：念的那句不许带路径，也不许带会话 id。"""
    from plugin.plugins.agent_deskmate.tracker import _spoken_line

    line = _spoken_line("claude-code", 12, [("Bash", 7), ("Edit", 4)])
    assert "\\" not in line and "/" not in line
    assert ":" not in line                     # 盘符 / UUID 分隔符都带冒号
    assert "workspace" not in line.lower()
    assert line == "claude-code 这一轮跑完了，共 12 步，主要是 Bash。"


def test_the_spoken_line_stays_short_without_a_tally() -> None:
    """一个类别都没记到时也要能念——不能变成一句空话。"""
    from plugin.plugins.agent_deskmate.tracker import _spoken_line

    assert _spoken_line("claude-code", 3, []) == "claude-code 这一轮跑完了，共 3 步。"


def test_the_boundary_notice_carries_a_spoken_line() -> None:
    """notice 上要带口语句，否则调用方只能退回 headline（那带路径）。"""
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    tracker.feed(_action(kind="Bash"))
    out = tracker.feed(_boundary(at=T0 + 3600))
    assert out[0].spoken
    assert "\\" not in out[0].spoken
    assert "Bash" in out[0].spoken


def test_the_detail_still_has_everything_the_voice_left_out() -> None:
    """语音省掉的，留底一条不少——路径、会话 id、分类计数都在 detail 里。"""
    tracker = _tracker(extra_notify_roles=frozenset({"boundary"}))
    tracker.feed(_action(kind="Bash"))
    out = tracker.feed(_boundary(at=T0 + 3600))
    assert "Bash 1 次" in out[0].detail
    assert "会话 " in out[0].detail
    assert WD in out[0].headline                 # 目录仍在 headline（给人看）
    assert WD not in out[0].spoken               # 但不在语音里
