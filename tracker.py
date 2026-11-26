"""C 批 §二：三态灯状态机。只认 `Signal`，不认任何一家 Agent。

**它要解决的就是问卷那句原话：「我能不能移开视线？如果需要我，我会知道吗？」**

所以它**不是仪表盘，是警报器**。设计后果全部从这一句推出来：

1. **默认全静音。** 全新实例 + 一串健康事件 → 零条通知。
   每次开口都必须是因为"这件事需要你动手"。
2. **只有跃迁触发，节点不触发。** 没有"N 分钟汇报一次"的定时器
   （问卷补充槽原话：「被 review / CI 反复唤醒烧钱」「成本掣肘」）。
3. **指纹去重。** 同一件事不播两遍。一次构建失败能刷十几条 tool_result，
   合并成一条——否则用户第 2 条就开始无视，第 10 条一定无视。
4. **指名道姓，不报数。** 用 `workspace`，不用会话 id——用户不认识
   `abc123`，认识 `D:\\proj`。多会话并行时，不带目录的通知等于没说。

**一个例外必须写明，否则实现者会漏**：**未回包超时**只能靠轮询发现——
没有新事件就没有触发源。所以它挂在 `tick()` 上（不新增定时器），
tick 只负责**评估**是否超时，不自行产生播报；发射仍是边沿触发，
且每个未闭环 action 最多发一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Container, Iterable

from .progress import (
    Light,
    Role,
    Signal,
    failure_is_loud,
    notify_enabled,
)

# ── 阈值 ────────────────────────────────────────────────────────────────
# 全部可调。默认值的依据写在注释里，不许拍脑袋。

@dataclass(frozen=True)
class TrackerConfig:
    """状态机的全部可调参数。冻结，便于测试注入。"""

    # 🟡 开始等你之后多久升级一次（「还在等你，已经 X 分钟」）。
    # 注意：**首次「在等你」立即播，不等这个阈值**——Agent 完全停摆、
    # 用户完全缺席，晚 3 分钟说没有任何好处。这个阈值只管升级那一次。
    wait_escalate_seconds: float = 180.0
    # 有 action 一直没配对 outcome 多久算「未回包」。
    # 判据**必须**是"有 action 无配对 outcome"，不能是"多久没新事件"——
    # 实测有无新记录的空档达 12 h / 36 h，那些是会话根本没动。
    unclosed_action_seconds: float = 300.0
    # 两次 boundary 间隔超过它才值得说「这一轮跑完了」（防滴答感）。
    boundary_round_seconds: float = 600.0
    # **契约默认之外，还要开口的角色**。
    #
    # ``progress.NOTIFY_DEFAULTS`` 把 BOUNDARY 定为默认关，理由原话是
    # "知会类且最吵：每一轮结束都响，正是训练用户忽略插件的那个"
    # （``test_progress.py`` 钉着这条不动）。但 2026-11-26 用户明确要求
    # 跑完要提醒、而且要 Yui 说出来——那是一个更具体的需求，覆盖那条默认。
    #
    # 默认值留空（中立）：数据类不替产品做决定，产品默认由 ``__init__``
    # 从配置传进来。这样"契约默认是什么"和"这个插件默认给什么"是两件事，
    # 前者有测试钉着，后者用户一行配置就能关掉。
    extra_notify_roles: frozenset[str] = frozenset()
    # 同一 (会话, 类别) 的失败在这个窗口内合并成一条。
    fail_coalesce_seconds: float = 120.0
    # **失败确认窗口**：一次响亮失败之后，等这么久看 Agent 能不能自己恢复。
    # 实测依据（2026-11-23，真机一个 80 小时的会话）：93 次响亮失败里
    # **85 次在 120 秒内自己恢复了，中位数约 15 秒**——Agent 改个参数重试、
    # 换个写法重试，都是常态。那些用户根本不需要知道。
    # 只有过了这个窗口还没恢复的才开口：通知量从 93 降到 8。
    # 代价是"它卡住了"这件事晚 2 分钟才知道——对"出错发现晚"来说，
    # 2 分钟是噪音；对"别拿假警报训练我无视你"来说，这是生死线。
    failure_confirm_seconds: float = 120.0
    # 同一类别连续失败到第几次时升级成「它在兜圈子」。到过就再也不升。
    retry_escalate_at: tuple[int, ...] = (3, 5)


DEFAULT_CONFIG = TrackerConfig()


# ── 通知 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Notice:
    """一条要播出去的通知。与 `Signal` 一样不含内容字段。

    `who` 指名用（workspace 优先，退回 agent 名）；
    `headline` 第一行必须是结论；`detail` 第二行说它到底在干什么——
    **类别级，不含命令行**。想带命令原文的调用方（审批中继那条路）
    自己拼，那条路有它自己的披露规则。
    """

    role: Role
    light: Light
    agent: str
    session: str
    who: str
    headline: str
    detail: str
    at: float
    fingerprint: str
    kind: str = ""
    # 指名用的工作目录原文（没有就是 None）。**``who`` 分不出这两者**：
    # 拿不到目录时 ``who`` 会退回 agent 名，而 ``claude-code`` 是**所有**
    # 无目录会话的共同退回值。P1c 的同目录合并必须只认真有目录的，
    # 否则会把一堆互不相干的会话揉成一条。
    workspace: str | None = None

    def lines(self) -> tuple[str, ...]:
        """固定三行，不加第四行（B12 P0-2 定稿）。"""
        return (self.headline, self.detail)


# ── 每会话状态 ──────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """一个 (agent, session) 的全部进度状态。可变，由 tracker 独占。"""

    agent: str
    session: str
    workspace: str | None = None
    light: Light = Light.IDLE
    last_event_at: float = 0.0
    # 未闭环的动作：action_id -> (kind, since)。**必须是 dict，不能是单槽**——
    # Claude Code 会并行发多个工具调用（实测常见），单槽会被后一个覆盖，
    # 前一个的 outcome 永远配不上。真机 dry run 第一次就抓到这条：
    # 105 条"失败"全是这么来的假货，占单个会话通知数的 55%。
    open_actions: dict[str, tuple[str, float]] = field(default_factory=dict)
    last_boundary_at: float = 0.0
    rounds: int = 0            # 单调计数，不是比例
    actions: int = 0
    # 这一轮用过的**类别**计数（``kind → 次数``），boundary 时清零。
    #
    # 为什么只记类别不记内容：``progress.Signal`` 没有任何能装内容的字段，
    # 那是隐私红线的结构性保证（``test_progress.py::test_signal_has_no_content_field``
    # 钉着）。"它干了什么"只能由类别回答——``bash 7 次、edit 4 次``
    # 是有信息量的概括，``rm -rf …`` 不是我们能碰的东西。
    round_kinds: dict[str, int] = field(default_factory=dict)
    fail_streak: dict[str, int] = field(default_factory=dict)
    ok_streak: dict[str, int] = field(default_factory=dict)
    waiting_episode: float | None = None
    waiting_escalated: bool = False
    # 待确认的失败：kind -> [首次失败时间, 连续次数]。
    # **失败不立刻播**——先看 Agent 能不能自己恢复（见 config 里的实测依据）。
    # 任何一次成功都会把整个表清空：它在往前走，就不是卡住。
    pending_failures: dict[str, list] = field(default_factory=dict)
    # 已经为"未回包"开过口的 action_id。每个未闭环 action 最多一次。
    unclosed_reported: set[str] = field(default_factory=set)
    # 指纹 → 上次播报时间。去重只此一处。
    fingerprints: dict[str, float] = field(default_factory=dict)
    # 升级过的重试链档位，到过就再也不升。
    retry_reported: set[int] = field(default_factory=set)

    def key(self) -> tuple[str, str]:
        return (self.agent, self.session)


# ── 状态机 ──────────────────────────────────────────────────────────────

class ProgressTracker:
    """把 `Signal` 流变成 `Notice` 流。**零 I/O、零 key、零模型调用。**"""

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self._sessions: dict[tuple[str, str], SessionState] = {}

    # ── 查询 ─────────────────────────────────────────────────────────────

    def sessions(self) -> list[SessionState]:
        """全部会话，按「欠你的」排序：🟡 顶最上，然后 🔴，然后 🟢。"""
        order = {Light.WAITING: 0, Light.ERROR: 1, Light.RUNNING: 2,
                 Light.IDLE: 3}
        return sorted(self._sessions.values(),
                      key=lambda s: (order.get(s.light, 9), -s.last_event_at))

    def state_of(self, agent: str, session: str) -> SessionState | None:
        return self._sessions.get((agent, session))

    def counts(self) -> dict[str, int]:
        """几个在跑 / 几个在等你 / 几个出事。`status` 入口用，永不推送。"""
        result = {"running": 0, "waiting": 0, "error": 0, "idle": 0}
        for state in self._sessions.values():
            result[state.light.value] = result.get(state.light.value, 0) + 1
        return result

    # ── 主入口 ───────────────────────────────────────────────────────────

    def feed(self, signal: Signal) -> list[Notice]:
        """吃一条信号，返回这次要播的通知（通常为空）。"""
        state = self._sessions.get(signal.key())
        if state is None:
            state = SessionState(agent=signal.agent, session=signal.session)
            self._sessions[signal.key()] = state

        if signal.workspace and not state.workspace:
            state.workspace = signal.workspace
        state.last_event_at = max(state.last_event_at, signal.at)

        handler = _HANDLERS.get(signal.role)
        if handler is None:
            return []
        return handler(self, state, signal)

    def tick(
        self,
        now: float,
        blocked: Container[tuple[str, str]] = (),
    ) -> list[Notice]:
        """轮询评估。**只评估超时，不自行产生播报**（见模块 docstring 的例外说明）。

        ``blocked``：**暂不评估「未回包」的 (agent, session) 集合**——当前有审批
        挂在等人点的会话。见 ``_tick_one`` 里那条注释，重点是"跳过评估"而不是
        "过滤通知"，两者差一个漏报。
        """
        notices: list[Notice] = []
        for state in sorted(self._sessions.values(),
                            key=lambda s: (s.agent, s.session)):
            notices.extend(self._tick_one(state, now, blocked))
        return notices

    def release_gate(self, keys: Container[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """闸门挂起解除：作废这些会话里仍未闭环的动作。返回各会话作废数。

        2026-11-24 真机（0.6.0 验收 §3）：卡片 12:33 弹出、12:42:53 到期，
        12:44:09 宿主张口就是一条「有一步没有回包 / Bash 执行了 10 分 46 秒」。
        那个动作 12:33 就发出去了，**没有结果是因为我们扣着它等审批**，
        不是 Agent 卡住。

        而到期按设计**不回填**（``approval.DECIDED_BY_EXPIRED``：不拒绝、
        不回填、不记成"已拒绝"），所以那个 outcome 永远不会来。
        也就是说：**每一张没人点的卡，到期后都必然产出这么一条假警报**，
        还把耗时写成一个夸大的数字。用户越忙没点，通知越多、越像 Agent
        坏了——正是这个插件最怕的警报疲劳。

        ``tick(blocked=...)`` 只挡得住"挂着的时候"，挡不住解除的那一秒：
        挂起标记一消失，早就过了 ``unclosed_action_seconds`` 的旧动作
        立刻被判成"它可能卡住了"。所以解除时要**作废**这些动作——
        工具没跑成就没有结果可等，继续挂在 ``open_actions`` 里只会变成
        一条指错方向的播报。

        **作废而不是把时钟推到当下**：推时钟能让这一轮不报，但 300 秒后
        照样报一条，文案依旧是"它可能卡住了"，只是晚了一点的假警报；
        而无头会话（``-p``）永远等不到原生确认框，那个 outcome 不会来。

        **为什么可以整会话作废**：Agent 被 hook 挡住时发不出新的工具调用，
        所以这一刻会话里开着的行为只有三种——被扣住的那个、同一批并行
        的兄弟、以及结果已经在路上的那个。前两种没有合法结果可等，
        第三种即便被作废，它的 outcome 到达时 ``_on_outcome`` 走
        "配不上"分支照旧处理（成功清零、失败进确认窗口），不会漏。

        **必须在 ``tick`` 之前调**：反了的话这一轮 tick 先把假警报发出去，
        作废再来已经是马后炮。
        """
        dropped: dict[tuple[str, str], int] = {}
        for state in self._sessions.values():
            if state.key() not in keys:
                continue
            count = 0
            for action_id in list(state.open_actions):
                del state.open_actions[action_id]
                # 和 _on_outcome 配对成功时保持一致：两个结构一起收，
                # 别留下一个再也配不上的孤儿记号。
                state.unclosed_reported.discard(action_id)
                count += 1
            if count:
                dropped[state.key()] = count
        return dropped

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _tick_one(self, state: SessionState, now: float,
                  blocked: Container[tuple[str, str]] = ()) -> list[Notice]:
        notices: list[Notice] = []
        cfg = self._config

        # 未回包：有 action 一直没配对 outcome。**逐个查**，不是只看一个——
        # 并行发起的多个调用各自有自己的超时。
        #
        # ⚠️ 审批挂起时整段跳过，不是把发出的通知过滤掉。区别是致命的：
        # 评估一旦跑过，action_id 就进 ``unclosed_reported``，那个 action
        # **从此永远不再报**。审批挂着的那几分钟恰好会被这一口吞掉——
        # 等用户点完卡、Agent 真的卡死了，该报的那一次已经不报了。
        # 精度变成了漏报，而漏报正是这个插件存在的理由。
        #
        # 顺带一说这不是理论问题：2026-11-24 真机，三张卡等人时 300 秒后
        # 连发三条「有一步没有回包 / 它可能卡住了」，而真实原因是
        # **权限请求在等审批**（hook 阻塞），Agent 根本没卡。
        if state.key() not in blocked:
            for action_id in sorted(state.open_actions):
                if action_id in state.unclosed_reported:
                    continue
                kind, since = state.open_actions[action_id]
                if now - since < cfg.unclosed_action_seconds:
                    continue
                state.unclosed_reported.add(action_id)
                state.light = Light.ERROR
                notice = self._emit(
                    state, now,
                    role=Role.OUTCOME, light=Light.ERROR,
                    fingerprint=f"unclosed|{state.agent}|{state.session}|{action_id}",
                    headline=f"{self._who(state)} 有一步没有回包",
                    detail=(f"{kind or '某一步'} 执行了 {_elapsed(now - since)}"
                            "，没有结果——它可能卡住了"),
                    kind=kind,
                )
                if notice is not None:
                    notices.append(notice)

        # 🟡 升级：等了太久还在等。每个 episode 只升一次。
        if (state.light is Light.WAITING
                and state.waiting_episode is not None
                and not state.waiting_escalated
                and now - state.waiting_episode >= cfg.wait_escalate_seconds):
            state.waiting_escalated = True
            notice = self._emit(
                state, now,
                role=Role.WAITING, light=Light.WAITING,
                fingerprint=(f"wait_esc|{state.agent}|{state.session}"
                             f"|{state.waiting_episode:g}"),
                headline=f"{self._who(state)} 还在等你",
                detail=f"已经 {_elapsed(now - state.waiting_episode)}，它完全停着",
            )
            if notice is not None:
                notices.append(notice)

        # 待确认的失败：过了确认窗口还没恢复的，这才开口。
        notices.extend(self._confirm_failures(state, now))
        return notices

    def _on_action(self, state: SessionState, signal: Signal) -> list[Notice]:
        state.actions += 1
        if signal.kind:
            # 只累类别。空 kind 不记——它表示"说不清是什么工具"，
            # 记一个空串键只会让汇总里出现一个没有名字的项。
            state.round_kinds[signal.kind] = state.round_kinds.get(signal.kind, 0) + 1
        if signal.action_id:
            state.open_actions[signal.action_id] = (signal.kind, signal.at)
        # 还在跑就不是"在等你"了：用户处理完了，或者它自己往下走了。
        if state.light is Light.WAITING:
            state.waiting_episode = None
            state.waiting_escalated = False
        if state.light is not Light.ERROR:
            state.light = Light.RUNNING
        # action 默认不播（见 progress.NOTIFY_DEFAULTS）。留这条分支
        # 是为了开关表改的时候有地方可改，也为了让测试能断言"它真的没播"。
        return []

    def _on_outcome(self, state: SessionState, signal: Signal) -> list[Notice]:
        action_id = signal.action_id
        # 配对成功才闭环。配不上也照样处理失败——漏一次失败比留一个
        # 幽灵 open_action 更糟（那个幽灵会在超时后变成"未回包"假警报）。
        if action_id and action_id in state.open_actions:
            kind = state.open_actions.pop(action_id)[0] or signal.kind
            state.unclosed_reported.discard(action_id)
        else:
            kind = signal.kind

        # ── 自伤失败：只闭环，不当成 Agent 的失败 ──
        # P1c。审批到期/拒绝后 Claude 收到的那条 tool_result is_error 是
        # **插件自己的闸门造成的**：工具根本没跑，Agent 没坏。
        # 2026-11-24 真机：三条「出事了 / 它没自己恢复」全是这么来的，
        # 而用户越忙没点卡，通知越多、越像 Agent 坏了。
        #
        # 三件事都必须不做：不进 pending_failures（否则 120 秒后播"出事了"）、
        # 不动 fail_streak（否则攒够 3 次播"在兜圈子"）、不染红灯。
        # **也刻意不清 pending_failures / 不改灯**——那会把同会话里一次
        # 真实的未恢复失败一起抹掉，用漏报换漏报不叫修。
        if signal.self_inflicted:
            return []

        ok = bool(signal.ok)
        streak = state.ok_streak if ok else state.fail_streak
        streak[kind] = streak.get(kind, 0) + 1
        # 成功把失败的连续记录清零：失败三次后成功一次，不算"又失败"
        if ok:
            state.fail_streak[kind] = 0
        else:
            state.ok_streak[kind] = 0

        if ok:
            # 成功的 outcome 默认不播。但它做三件重要的事：
            # 1. 把灯从 🔴 拉回 🟢——否则一次失败之后灯就再也不变了；
            # 2. 清空待确认的失败——它自己恢复了，那些不是用户的事；
            # 3. 清零该类别的失败连续记录。
            state.pending_failures.clear()
            state.fail_streak[kind] = 0
            if state.light is Light.ERROR:
                state.light = Light.RUNNING
            return []

        # ── 失败 ──
        # 灯先亮（它确实出了错），但**通知先不发**。
        # 读/找类失败（grep 无匹配、文件不存在）连灯都不该染红——
        # 那是搜索的正常结果，是狼来了。
        #
        # 重试链对任何类别都成立：同一个东西连着失败 N 次，
        # 无论它是什么工具，都已经是"它在兜圈子"而不是"搜索结果"。
        # 重复本身就是异常信号，与工具语义无关。
        notices: list[Notice] = []
        streak_n = state.fail_streak.get(kind, 0)
        loud = failure_is_loud(kind)
        if loud:
            state.light = Light.ERROR
            # 记账，等确认。已有一个待确认的就累加次数，不重置时间——
            # 否则"每 120 秒失败一次"会无限期推后首次确认。
            pending = state.pending_failures.get(kind)
            if pending is None:
                state.pending_failures[kind] = [signal.at, 1]
            else:
                pending[1] += 1

        for threshold in self._config.retry_escalate_at:
            if streak_n < threshold or threshold in state.retry_reported:
                continue
            state.retry_reported.add(threshold)
            state.light = Light.ERROR
            notice = self._emit(
                state, signal.at,
                role=Role.OUTCOME, light=Light.ERROR,
                fingerprint=f"retry|{state.agent}|{state.session}|{kind}|{threshold}",
                headline=f"{self._who(state)} 在兜圈子",
                detail=f"{kind or '同一步'} 连续失败 {streak_n} 次，"
                       "它可能在原地打转",
                kind=kind,
            )
            if notice is not None:
                notices.append(notice)
        return notices

    def _confirm_failures(self, state: SessionState,
                          now: float) -> list[Notice]:
        """确认窗口过了还没恢复的失败，这才开口。

        **这是"失败不立刻播"的另一半。** 只在 `tick()` 里跑——
        和未回包一样，它没有事件触发源（恢复与否要等下一件事发生）。
        """
        cfg = self._config
        notices: list[Notice] = []
        for kind in sorted(state.pending_failures):
            first_at, count = state.pending_failures[kind]
            if now - first_at < cfg.failure_confirm_seconds:
                continue
            # 先删再发：发不出去（被去重挡了）也不该赖在原地
            del state.pending_failures[kind]
            notice = self._emit(
                state, now,
                role=Role.OUTCOME, light=Light.ERROR,
                fingerprint=f"fail|{state.agent}|{state.session}|{kind}",
                window=cfg.fail_coalesce_seconds,
                headline=f"{self._who(state)} 出事了",
                detail=f"{kind or '有一步'} 执行失败，它没自己恢复",
                kind=kind,
            )
            if notice is not None:
                notices.append(notice)
        return notices

    def _on_boundary(self, state: SessionState, signal: Signal) -> list[Notice]:
        state.rounds += 1
        cfg = self._config
        # 间隔不够就不说。防"滴答感"：低于 boundary_round_seconds
        # 的"跑完了"是噪音，不是进度。
        quiet_enough = (state.last_boundary_at <= 0
                        or signal.at - state.last_boundary_at
                        >= cfg.boundary_round_seconds)
        state.last_boundary_at = signal.at

        # 这一轮结束了：还没闭环的动作是孤儿，别让它们在超时后
        # 变成"未回包"假警报——那一轮已经翻篇了。
        state.open_actions.clear()
        state.waiting_episode = None
        state.waiting_escalated = False

        if not quiet_enough:
            state.light = Light.IDLE
            return []

        # 这一轮用了什么。**只报类别**（见 SessionState.round_kinds 的说明）：
        # 「bash 7 次、edit 4 次」是有信息量的概括，也是 Yui 开口时
        # 唯一能凭的事实。按次数倒序，多的在前——用户先看到它主要在干什么。
        tally = ", ".join(
            f"{name} {count} 次"
            for name, count in sorted(state.round_kinds.items(),
                                      key=lambda kv: (-kv[1], kv[0]))[:4])
        detail = f"第 {state.rounds} 轮，共 {state.actions} 步"
        if tally:
            detail += f"（{tally}）"
        state.round_kinds = {}

        notice = self._emit(
            state, signal.at,
            role=Role.BOUNDARY, light=Light.IDLE,
            fingerprint=f"round|{state.agent}|{state.session}|{state.rounds}",
            headline=f"{self._who(state)} 这一轮跑完了",
            detail=detail,
        )
        state.light = Light.IDLE
        # 开不开由 extra_notify_roles 决定（2026-11-26 起产品默认开）：
        # 契约默认仍是关，见 progress.NOTIFY_DEFAULTS 与 test_progress.py。
        return [notice] if notice is not None else []

    def _on_waiting(self, state: SessionState, signal: Signal) -> list[Notice]:
        # 每个"等你" episode 只报一次。已经在等就不是新 episode。
        if state.waiting_episode is not None:
            return []
        state.waiting_episode = signal.at
        state.waiting_escalated = False
        state.light = Light.WAITING
        notice = self._emit(
            state, signal.at,
            role=Role.WAITING, light=Light.WAITING,
            fingerprint=f"wait|{state.agent}|{state.session}",
            headline=f"{self._who(state)} 在等你",
            detail=signal.reason or "它停在那儿，需要你处理才能继续",
        )
        return [notice] if notice is not None else []

    # ── 共用 ─────────────────────────────────────────────────────────────

    def _who(self, state: SessionState) -> str:
        """指名。用户认识目录，不认识 UUID。"""
        return state.workspace or state.agent

    def _emit(self, state: SessionState, at: float, *, role: Role,
              light: Light, fingerprint: str, headline: str, detail: str,
              kind: str = "", window: float | None = None) -> Notice | None:
        """构造一条通知，过三道闸：默认开关、指纹去重。

        返回 None 表示"这条不该开口"。**三道闸任何一道挡住都返回 None**，
        调用方不许自己判断——否则去重逻辑会散到各处，然后某处漏掉。
        """
        if not (notify_enabled(role) or role in self._config.extra_notify_roles):
            return None

        previous = state.fingerprints.get(fingerprint)
        if previous is not None:
            span = window if window is not None else self._config.fail_coalesce_seconds
            if at - previous < span:
                return None

        state.fingerprints[fingerprint] = at
        return Notice(
            role=role, light=light,
            agent=state.agent, session=state.session,
            who=self._who(state),
            workspace=state.workspace,
            headline=headline, detail=detail,
            at=at, fingerprint=fingerprint, kind=kind,
        )


_HANDLERS = {
    Role.ACTION: ProgressTracker._on_action,
    Role.OUTCOME: ProgressTracker._on_outcome,
    Role.BOUNDARY: ProgressTracker._on_boundary,
    Role.WAITING: ProgressTracker._on_waiting,
}


def _elapsed(seconds: float) -> str:
    """「4 分 12 秒」这种。不到一分钟就说「不到 1 分钟」。"""
    total = max(0, int(seconds))
    if total < 60:
        return "不到 1 分钟"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def notices_to_lines(notices: Iterable[Notice]) -> list[str]:
    """把通知摊成行。测试与调用方共用，免得各写一份格式。"""
    lines: list[str] = []
    for notice in notices:
        lines.extend(notice.lines())
    return lines
