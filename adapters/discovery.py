"""本机 Agent 发现层（2026-11-25）。

回答一个问题：**这台机器上装了哪些 Agent，插件能观测其中哪些。**

设计抄哨兵（`radar.py` 的 ``_PATH_MARKERS``），但把它的"路径标记表"
和插件的"适配器是否已写"合成一张表，于是同一个枚举能同时回答
「在不在」和「看不看得见」——这两个问题在面板上必须一起出现，
只报一个就是在瞒。

## 三条硬规则

1. **只判存在，不读内容。** 这里全部是 ``Path.exists()`` / ``is_dir()``。
   绝不打开任何文件——发现层不需要知道 Agent 里面有什么，
   而"顺手读一下"正是隐私事故的标准入口。
2. **状态从事实推出来，不写死。** 同一个 Agent 在"没装 cc-switch 的机器"上
   和"装了"的机器上状态不同，这张表不替它提前决定。
3. **``blind`` 是一等答案。** 「本机有、插件看不见」必须原样报出来。
    silently 跳过会让面板显示「一切正常」，而用户明明开着 6 个 Agent。

## 状态机（五态，全部由事实推导）

| 状态 | 条件 | 面板该说什么 |
|---|---|---|
| ``absent`` | 家目录不存在 | 什么都不显示（本机没这个 Agent） |
| ``observable`` | 家目录在 + 适配器已写 + 观测数据也在 | 正在观测 |
| ``conditional`` | 家目录在 + 适配器已写 + **观测数据不在** | 有前提条件没满足（如没走网关） |
| ``feasible`` | 家目录在 + 适配器没写 + **但数据在** | 数据在本机，适配中 |
| ``blind`` | 家目录在 + 适配器没写 + 也不知道数据在哪 | 暂无本地数据源 |

``feasible`` 与 ``blind`` 的区分是这篇模块存在的主要理由：
它让「已验证可读但还没接」（mcode）和「压根不知道读什么」（dsh）
在面板上是两行不同的字，而不是一起归成"不支持"。

## 表的增长方式

加一个 Agent = 加一条 ``_AGENTS`` 记录，别的不动。
哨兵对 14 个外部 Agent CLI 只给 6 个写了深度解析、其余全走文本兜底，
代价写在他们自己的文档里——所以这里的默认预期就是
**大部分条目停在 ``blind``，这是正常的，不是欠账**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

# 五态。字符串常量而不是 Enum：要直接进 i18n key 和面板徽标，
# 多一层抽象只会让查表多跳一步。
ABSENT = "absent"
OBSERVABLE = "observable"
CONDITIONAL = "conditional"
FEASIBLE = "feasible"
BLIND = "blind"

#: 需要向用户解释的状态（``absent`` 不需要——本机没装，说出来只是噪音）。
REPORTED_STATES = (OBSERVABLE, CONDITIONAL, FEASIBLE, BLIND)


@dataclass(frozen=True)
class AgentProbe:
    """一个 Agent 的发现结果。

    ``home`` / ``data`` 是**实际找到的路径**，不是登记的候选——
    面板要能告诉用户"你的数据在 ``C:\\Users\\x\\.minimax``"，
    而不只是"检测到了"。
    """

    id: str
    label: str
    state: str
    home: Path | None = None
    data: Path | None = None
    detail: str = ""
    #: 观测深度，见 ``_Agent.depth``。
    depth: str = ""
    #: 这个 Agent 在网关里的 ``app_type``。空 = 不走网关。
    gateway_app_type: str = ""

    @property
    def reported(self) -> bool:
        """该不该出现在面板上。"""
        return self.state in REPORTED_STATES

    @property
    def badge(self) -> str:
        """面板徽标用的 key（``panel.agents.badge.<key>``）。

        **observable 要按观测深度再分一刀**：Claude Code 有转录、能出审批卡，
        Codex 只在网关里看得到一条请求元数据——两者都标"正在观测"，
        就是让徽标宣称得比实现给的多。这类落差用户已经揪过一次
        （"为什么对外说多 Agent、面板只讲 Claude"），不在这里再犯。
        """
        if self.state == OBSERVABLE and self.depth:
            return self.depth
        return self.state

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict（路径一律转字符串）。

        **不用 ``dataclasses.asdict``**：那个会把 ``Path`` 原样留着，
        而 ``reported`` / ``badge`` 是 property 不是字段，压根不会出现——
        调用方于是拿不到"该不该显示"，只能默认显示，``absent`` 就漏到面板上了。
        """
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "depth": self.depth,
            "badge": self.badge,
            "detail": self.detail,
            "reported": self.reported,
            # 面板据此去网关盘这个 Agent 自己的会话——网关是共表，
            # 不过滤就会把 Claude 的会话列到 Codex 名下。
            "gateway_app_type": self.gateway_app_type,
            "home": str(self.home) if self.home is not None else "",
            "data": str(self.data) if self.data is not None else "",
        }


@dataclass(frozen=True)
class _Agent:
    """登记表的一条。``markers`` 相对家目录，可以嵌套（``.config/opencode``）。"""

    id: str
    label: str
    #: 家目录标记。任一存在即认为"本机装过这个 Agent"。
    markers: tuple[str, ...]
    #: 本地观测数据所在（相对家目录）。空元组 = 不知道读什么。
    data_markers: tuple[str, ...] = ()
    #: **若观测入口是 cc-switch 网关，这里写它的 ``app_type``。**
    #: 网关是所有 agent 共写一张表的，所以一个 Agent 能不能被网关看见，
    #: 取决于网关里有没有它的 ``app_type``——不是取决于 db 文件在不在。
    #: 实测本机（2026-11-25）：mcode 37816 / claude 13268 / **dsh 7221** /
    #: claude-desktop 2325 / **codex 160** / **copilot 67** / **opencode 34**。
    #: 早先只给 codex 登记了这一项，把 dsh 和 opencode 误判成 blind——
    #: 而 dsh 恰是问卷里被提名最多的那个。
    gateway_app_type: str = ""
    #: 插件今天是否已经有适配器在读它。
    wired: bool = False
    #: **观测深度**，只在 ``observable`` 有意义。
    #: ``full`` = 有结构化事件流（转录 / 本地库），能出审批卡；
    #: ``metadata`` = 只有网关那种请求级元数据，看不到内容、出不了卡。
    depth: str = ""
    #: 给用户的一句话。写"为什么是这个状态"，不写"这个状态是什么"。
    detail: str = ""


# --------------------------------------------------------------------------
# 登记表 —— 每一条都经过本机实测或源码确认，没有猜的路径
# --------------------------------------------------------------------------
#
# 来源分三种，写清楚是为了将来好核对：
#   ① 哨兵 radar.py 的 _PATH_MARKERS（读码得到）
#   ② 本机 ~ 目录实测存在性（2026-11-25）
#   ③ 插件自己的 adapters/（代码事实）
_AGENTS: tuple[_Agent, ...] = (
    _Agent(
        id="claude",
        label="Claude Code",
        markers=(".claude",),
        data_markers=(".claude/projects",),
        wired=True,
        depth="full",
        detail="转录目录可直接增量读，是当前唯一有审批卡的 Agent。",
    ),
    _Agent(
        id="codex",
        label="Codex",
        markers=(".codex",),
        # cc-switch 不在 codex 家目录下，但它是 codex 唯一的观测入口。
        gateway_app_type="codex",
        wired=True,
        # 只有请求级元数据：没有转录、没有思考、出不了审批卡。
        # 所以徽标不能跟 Claude 一样写"正在观测"。
        depth="metadata",
        detail="只经 cc-switch 网关可见；没把 Agent 挂到网关时观测不到。",
    ),
    _Agent(
        id="mcode",
        label="mcode",
        # 哨兵的表里 .mcode 与 .minimax 都指向 mcode；本机只有 .minimax。
        markers=(".minimax", ".mcode"),
        # 本地库优先：它有思考和工具调用，比网关的请求元数据强一个量级。
        # 网关那一层留作 ``feasible`` 期间的可见性说明，不作为主路径。
        data_markers=(".minimax/v2/sqlite/runtime-state.sqlite",),
        gateway_app_type="mcode",
        wired=False,
        detail="本地库已验证可增量读（含思考与工具调用），适配器未写。",
    ),
    _Agent(
        id="dsh",
        label="DeepSeek Harness",
        markers=(".dsh",),
        # 2026-11-25 更正：**不再是 blind**。网关里有 7,221 条 dsh 请求
        # （最近 24h 还有 112 条），而网关适配器本来就不按 agent 过滤——
        # 也就是说插件其实一直看得见它，只是发现层不知道。
        gateway_app_type="dsh",
        wired=True,
        depth="metadata",
        detail="问卷里被提名最多的 Agent；经 cc-switch 网关可见（请求级元数据）。",
    ),
    _Agent(
        id="opencode",
        label="OpenCode",
        markers=(".config/opencode", ".opencode"),
        # 同为 2026-11-25 更正：网关里有 opencode 的请求。
        gateway_app_type="opencode",
        wired=True,
        depth="metadata",
        detail="经 cc-switch 网关可见（请求级元数据）；本地会话数据落点未勘查。",
    ),
    _Agent(
        id="workbuddy",
        label="WorkBuddy",
        markers=(".workbuddy",),
        wired=False,
        detail="从未出现在 cc-switch 网关里，本地数据源未勘查。",
    ),
    _Agent(
        id="codebuddy",
        label="CodeBuddy",
        markers=(".codebuddy",),
        wired=False,
        detail="本地数据源未勘查。",
    ),
    _Agent(
        id="copilot",
        label="GitHub Copilot",
        markers=(".copilot",),
        # 网关里 67 条。家目录 ``.copilot`` 在本机存在，但那是 Copilot CLI
        # 的配置目录，未必与网关里那 67 条同源——所以 detail 说清楚。
        gateway_app_type="copilot",
        wired=True,
        depth="metadata",
        detail="经 cc-switch 网关可见（请求级元数据）。",
    ),
)


def _first_existing(home: Path, markers: Iterable[str]) -> Path | None:
    """返回第一个存在的标记路径。找不到返回 ``None``。

    顺序即优先级：``markers`` 里靠前的是更可能的正主。
    """
    for marker in markers:
        candidate = home / marker
        try:
            if candidate.exists():
                return candidate
        except OSError:
            # 家目录不可达（权限/路径过长）时不当成"不存在"——
            # 但也给不出更多信息，只能跳过这个候选。
            continue
    return None


#: 「数据在网关里」的记号。**不是真路径**——``AgentProbe.data`` 对它永远是
#: ``None``，面板要表达"在网关里"靠的是 ``gateway_app_type``，不靠这个。
#: 单独一个哨兵对象而不是 ``True`` 或字符串：``_first_existing`` 的返回类型是
#: ``Path | None``，混进别的类型迟早让人在下面踩空。
_GATEWAY = Path("<gateway>")


def classify(home: Path, agent: _Agent,
             gateway: Mapping[str, int] | None = None) -> AgentProbe:
    """给一个 Agent 定性。纯函数：同样的输入永远得到同样的状态。

    分开成一个独立函数而不是塞进 ``discover`` 的循环里，是为了能
    拿构造的 ``_Agent`` 直接单测五种状态，不必真去建目录。

    ``gateway`` 是 ``app_type → 请求数`` 的映射（``sessions.gateway_app_types``
    的结果）。**给了它，网关侧 Agent 的状态才是事实**；不给就退回
    "db 文件在不在"这个粗判——所以调用方（面板）应该给。
    """
    home_path = _first_existing(home, agent.markers)
    if home_path is None:
        return AgentProbe(id=agent.id, label=agent.label, state=ABSENT,
                         gateway_app_type=agent.gateway_app_type)

    # ── 数据在不在 ────────────────────────────────────────────────────────
    # 本地路径优先，网关兜底。两者都有的话（mcode）走本地那条，
    # 因为本地库有思考和工具调用，比请求元数据强一个量级。
    data_path = _first_existing(home, agent.data_markers)
    if data_path is None and agent.gateway_app_type:
        if gateway and gateway.get(agent.gateway_app_type, 0) > 0:
            data_path = _GATEWAY
        # gateway 是 None（调用方没给）时不猜：降到 conditional，
        # 让面板说"有条件"而不是假装看得见。

    if data_path is None:
        if agent.data_markers or agent.gateway_app_type:
            # 知道该读什么，只是现在读不到。
            # 与 blind 的区别就在这里：blind 是"不知道读什么"。
            return AgentProbe(
                id=agent.id, label=agent.label, state=CONDITIONAL,
                home=home_path, detail=agent.detail,
                gateway_app_type=agent.gateway_app_type)
        # 家目录在，但登记表不知道读什么 → blind。
        # 这是"承认不知道"，不是"以后也不知道"。
        return AgentProbe(
            id=agent.id, label=agent.label, state=BLIND,
            home=home_path, detail=agent.detail,
            gateway_app_type=agent.gateway_app_type)

    state = OBSERVABLE if agent.wired else FEASIBLE
    return AgentProbe(
        id=agent.id, label=agent.label, state=state,
        home=home_path,
        gateway_app_type=agent.gateway_app_type,
        # ``_GATEWAY`` 只是个"数据在网关里"的记号，不是真路径。
        data=None if data_path is _GATEWAY else data_path,
        detail=agent.detail,
        # depth 只在 observable 时带上：feasible 是"还没接"，
        # 谈不到看得多深，塞个 full 进去就是谎报。
        depth=agent.depth if state == OBSERVABLE else "")


def discover_agents(home: Any = None,
                    agents: Iterable[_Agent] | None = None,
                    *,
                    gateway: Mapping[str, int] | None = None,
                    ) -> list[AgentProbe]:
    """扫一遍本机，返回**所有** Agent 的状态（含 ``absent``）。

    ``absent`` 也返回而不是过滤掉：调用方可能想统计"本机一共几个"，
    而"装配 hook 只对 Claude 有用"这种判断需要知道别人不在。
    面板自己按 ``AgentProbe.reported`` 过滤。

    ``home`` 默认取 ``~``。显式传入是为了测试——测试绝不碰真家目录。
    ``gateway`` 是 ``app_type → 请求数``；给了它，网关侧 Agent 的状态
    才由事实决定，调用方（面板）应该给。见 ``classify``。
    """
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    table = tuple(agents) if agents is not None else _AGENTS
    return [classify(base, agent, gateway) for agent in table]


def summarize(probes: Iterable[AgentProbe]) -> dict[str, int]:
    """按状态计数。给面板顶部的「本机 8 个 Agent，观测中 1 个」用。"""
    counts: dict[str, int] = {state: 0 for state in REPORTED_STATES}
    for probe in probes:
        if probe.state in counts:
            counts[probe.state] += 1
    return counts
