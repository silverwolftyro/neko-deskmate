"""发现层单测（2026-11-25）。

全部用 ``tmp_path`` 造家目录，**绝不碰真 ``~``**——
这个模块的输入是家目录，拿真家目录测会让结果随机器变化，
测试就失去了"同样输入同样输出"这个性质。

重点盯三件事：
1. **五态各命中一次**，尤其是 ``conditional`` 与 ``blind`` 的区分
   （一个知道该读什么但读不到，一个压根不知道读什么）。
2. **观测深度必须把 ``observable`` 再切一刀**——Claude 有转录能出审批卡，
   Codex 只有网关请求元数据，两者不能共用同一个"正在观测"徽标。
3. **登记表自身的不变式**——重复 id / 空标记 / ``wired`` 却没登记数据路径，
   这三样错了面板会显示不存在的行为，而代码看起来完全正常。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate.adapters.discovery import (
    ABSENT,
    BLIND,
    CONDITIONAL,
    FEASIBLE,
    OBSERVABLE,
    AgentProbe,
    _Agent,
    classify,
    discover_agents,
    summarize,
)


def _mk(path: Path, *parts: str) -> None:
    """造一个文件（连父目录一起）。"""
    target = path.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")


# ── 五态 ─────────────────────────────────────────────────────────────────


def test_absent_when_no_marker_exists(tmp_path: Path) -> None:
    probe = classify(tmp_path, _Agent(id="x", label="X", markers=(".x",)))
    assert probe.state == ABSENT
    assert probe.home is None
    assert not probe.reported


def test_observable_when_wired_and_data_present(tmp_path: Path) -> None:
    _mk(tmp_path, ".claude/projects/keep")
    probe = classify(tmp_path, _Agent(
        id="claude", label="Claude Code", markers=(".claude",),
        data_markers=(".claude/projects",), wired=True))
    assert probe.state == OBSERVABLE
    assert probe.reported
    assert probe.data == tmp_path / ".claude/projects"


def test_conditional_when_wired_but_data_missing(tmp_path: Path) -> None:
    """codex 的实况：家目录在、适配器有，但没走网关就什么都没有。"""
    _mk(tmp_path, ".codex/keep")
    probe = classify(tmp_path, _Agent(
        id="codex", label="Codex", markers=(".codex",),
        data_markers=(".cc-switch/cc-switch.db",), wired=True))
    assert probe.state == CONDITIONAL
    assert probe.reported
    # conditional 也要给出家目录——用户需要知道"它在这，只是没接上"。
    assert probe.home == tmp_path / ".codex"
    assert probe.data is None


def test_feasible_when_data_present_but_adapter_unwritten(tmp_path: Path) -> None:
    """mcode 的实况：数据verified在，适配器没写。"""
    _mk(tmp_path, ".minimax/v2/sqlite/runtime-state.sqlite")
    probe = classify(tmp_path, _Agent(
        id="mcode", label="mcode", markers=(".minimax",),
        data_markers=(".minimax/v2/sqlite/runtime-state.sqlite",),
        wired=False))
    assert probe.state == FEASIBLE
    assert probe.data is not None


def test_blind_when_home_present_and_no_known_source(tmp_path: Path) -> None:
    _mk(tmp_path, ".dsh/keep")
    probe = classify(tmp_path, _Agent(
        id="dsh", label="DeepSeek Harness", markers=(".dsh",), wired=False))
    assert probe.state == BLIND
    assert probe.reported
    assert probe.home is not None
    assert probe.data is None


def test_blind_differs_from_conditional(tmp_path: Path) -> None:
    """这两个状态在面板上是两行不同的字，不许塌成一个。"""
    _mk(tmp_path, ".dsh/keep")
    _mk(tmp_path, ".codex/keep")
    blind = classify(tmp_path, _Agent(
        id="dsh", label="dsh", markers=(".dsh",), wired=False))
    conditional = classify(tmp_path, _Agent(
        id="codex", label="Codex", markers=(".codex",),
        data_markers=(".cc-switch/cc-switch.db",), wired=True))
    assert blind.state != conditional.state
    assert {blind.state, conditional.state} == {BLIND, CONDITIONAL}


# ── 观测深度：observable 内部还要分一刀 ──────────────────────────────────


def test_observable_with_full_depth_badges_full(tmp_path: Path) -> None:
    _mk(tmp_path, ".claude/projects/keep")
    probe = classify(tmp_path, _Agent(
        id="claude", label="Claude Code", markers=(".claude",),
        data_markers=(".claude/projects",), wired=True, depth="full"))
    assert probe.state == OBSERVABLE
    assert probe.badge == "full"


def test_metadata_only_agent_does_not_share_the_full_badge(tmp_path: Path) -> None:
    """**本轮真机截图逼出来的那条**：Codex 与 Claude 都标"正在观测"，
    但前者只有网关请求元数据——没有转录、没有思考、出不了审批卡。
    徽标必须分开，否则就是让面板宣称得比实现给的多。
    """
    _mk(tmp_path, ".codex/keep")
    _mk(tmp_path, ".cc-switch/cc-switch.db")
    probe = classify(tmp_path, _Agent(
        id="codex", label="Codex", markers=(".codex",),
        data_markers=(".cc-switch/cc-switch.db",), wired=True,
        depth="metadata"))
    assert probe.state == OBSERVABLE
    assert probe.badge == "metadata"
    assert probe.badge != "full"


def test_depth_dropped_when_not_observable(tmp_path: Path) -> None:
    """feasible 是"还没接"，谈不到看得多深——塞个 full 进去就是谎报。"""
    _mk(tmp_path, ".minimax/v2/sqlite/runtime-state.sqlite")
    probe = classify(tmp_path, _Agent(
        id="mcode", label="mcode", markers=(".minimax",),
        data_markers=(".minimax/v2/sqlite/runtime-state.sqlite",),
        wired=False, depth="full"))
    assert probe.state == FEASIBLE
    assert probe.depth == ""
    assert probe.badge == FEASIBLE


def test_observable_without_declared_depth_falls_back_to_state(tmp_path: Path) -> None:
    """登记表漏写 depth 时徽标退回 state，不崩也不显示空白。"""
    _mk(tmp_path, ".claude/projects/keep")
    probe = classify(tmp_path, _Agent(
        id="claude", label="Claude Code", markers=(".claude",),
        data_markers=(".claude/projects",), wired=True))
    assert probe.badge == OBSERVABLE


def test_as_dict_exposes_badge_and_depth() -> None:
    row = AgentProbe(id="x", label="X", state=OBSERVABLE,
                     depth="metadata").as_dict()
    assert row["badge"] == "metadata"
    assert row["depth"] == "metadata"
    assert row["reported"] is True


def test_as_dict_absent_is_not_reported() -> None:
    row = AgentProbe(id="x", label="X", state=ABSENT).as_dict()
    assert row["reported"] is False
    assert row["badge"] == ABSENT


def test_registry_observable_entries_declare_depth() -> None:
    """observable 却不写 depth = 徽标分不开深浅，这条不变式就是为那个截图的。"""
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    for agent in _AGENTS:
        if agent.wired:
            assert agent.depth, f"{agent.id} 标了 wired 但没声明观测深度"


# ── 标记优先级与异常 ─────────────────────────────────────────────────────


def test_first_marker_wins(tmp_path: Path) -> None:
    """两个标记都在时取靠前的那个——顺序就是优先级。"""
    _mk(tmp_path, ".minimax/keep")
    _mk(tmp_path, ".mcode/keep")
    probe = classify(tmp_path, _Agent(
        id="mcode", label="mcode", markers=(".minimax", ".mcode")))
    assert probe.home == tmp_path / ".minimax"


def test_later_marker_used_when_first_absent(tmp_path: Path) -> None:
    _mk(tmp_path, ".mcode/keep")
    probe = classify(tmp_path, _Agent(
        id="mcode", label="mcode", markers=(".minimax", ".mcode")))
    assert probe.home == tmp_path / ".mcode"


def test_nested_marker_resolves(tmp_path: Path) -> None:
    _mk(tmp_path, ".config/opencode/keep")
    probe = classify(tmp_path, _Agent(
        id="opencode", label="OpenCode",
        markers=(".config/opencode", ".opencode")))
    assert probe.state == BLIND
    assert probe.home == tmp_path / ".config/opencode"


def test_unreadable_marker_does_not_crash(tmp_path: Path) -> None:
    """家目录不可达时跳过该候选，不当成崩溃，也不谎报 absent。"""
    agent = _Agent(id="x", label="X", markers=(".x", ".y"))
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:  # noqa: ANN001
        if self.name == ".x":
            raise OSError("拒绝访问")
        return real_exists(self)

    _mk(tmp_path, ".y/keep")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(Path, "exists", fake_exists)
    try:
        probe = classify(tmp_path, agent)
    finally:
        monkey.undo()
    assert probe.home == tmp_path / ".y"


# ── discover_agents / summarize ──────────────────────────────────────────


def test_discover_uses_given_home_not_real_home(tmp_path: Path) -> None:
    """默认参数是 ``~``，但测试必须能注入——否则结果随机器变。"""
    _mk(tmp_path, ".claude/projects/keep")
    probes = discover_agents(tmp_path)
    by_id = {p.id: p for p in probes}
    assert by_id["claude"].state == OBSERVABLE
    # 其余条目在这个空家目录下必须全是 absent，不能漏出真家目录的东西。
    for probe in probes:
        if probe.id != "claude":
            assert probe.state == ABSENT, probe.id


def test_discover_returns_absent_too(tmp_path: Path) -> None:
    """absent 也返回，由调用方决定要不要显示。"""
    probes = discover_agents(tmp_path)
    assert probes
    assert all(p.state == ABSENT for p in probes)


def test_summarize_counts_only_reported(tmp_path: Path) -> None:
    _mk(tmp_path, ".claude/projects/keep")
    _mk(tmp_path, ".dsh/keep")
    _mk(tmp_path, ".minimax/v2/sqlite/runtime-state.sqlite")

    # 不给网关索引：dsh 是 conditional（知道该读什么，但没证据说读得到）
    counts = summarize(discover_agents(tmp_path))
    assert counts[OBSERVABLE] == 1
    assert counts[CONDITIONAL] == 1
    assert counts[FEASIBLE] == 1

    # 给了索引且里面有 dsh：dsh 升成 observable，conditional 归零
    counts = summarize(discover_agents(tmp_path, gateway={"dsh": 7221}))
    assert counts[OBSERVABLE] == 2
    assert counts[CONDITIONAL] == 0
    assert counts[FEASIBLE] == 1
    # absent 不进汇总——它是"本机没装"，不是一种要用户处理的状态。
    assert ABSENT not in counts


def test_summarize_ignores_unknown_state() -> None:
    counts = summarize([AgentProbe(id="x", label="X", state="不存在的东西")])
    assert set(counts.values()) == {0}


# ── 登记表自身的不变式 ───────────────────────────────────────────────────


def test_registry_ids_unique() -> None:
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    ids = [a.id for a in _AGENTS]
    assert len(ids) == len(set(ids)), "重复 id 会让面板出现两行同一个 Agent"


def test_registry_markers_do_not_collide() -> None:
    """两个 Agent 不能认同一个家目录，否则状态会互相污染。"""
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    seen: dict[str, str] = {}
    for agent in _AGENTS:
        for marker in agent.markers:
            assert marker not in seen, (
                f"{marker} 同时被 {seen[marker]} 和 {agent.id} 认领")
            seen[marker] = agent.id


def test_registry_wired_entries_declare_a_source() -> None:
    """``wired=True`` 却不知道该读什么 = 面板会显示"正在观测"一个空源。"""
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    for agent in _AGENTS:
        if agent.wired:
            assert agent.data_markers or agent.gateway_app_type, (
                f"{agent.id} 标了 wired 但既没登记本地路径也没登记 app_type")


def test_registry_has_no_duplicate_gateway_app_type() -> None:
    """两个 Agent 认同一个 ``app_type``，网关会话就会列到两个人名下。"""
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    seen: dict[str, str] = {}
    for agent in _AGENTS:
        if not agent.gateway_app_type:
            continue
        assert agent.gateway_app_type not in seen, (
            f"app_type {agent.gateway_app_type} 同时被 "
            f"{seen[agent.gateway_app_type]} 和 {agent.id} 认领")
        seen[agent.gateway_app_type] = agent.id


def test_registry_dsh_is_not_blind() -> None:
    """**2026-11-25 真机更正的回归测试**。

    早先把 dsh 登记成 blind，理由是"暂无已知的本地可读数据源"。
    实测网关里有 7,221 条 dsh 请求——而网关适配器本来就不按 agent 过滤，
    所以插件一直看得见它，只是发现层不知道。
    dsh 是问卷里被提名最多的 Agent，这一条错不得。
    """
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    by_id = {a.id: a for a in _AGENTS}
    assert by_id["dsh"].gateway_app_type == "dsh"
    assert by_id["dsh"].wired is True


# ── 网关侧判定：状态必须由网关内容决定，不是 db 文件在不在 ────────────────


def test_gateway_agent_observable_when_app_type_present(tmp_path: Path) -> None:
    _mk(tmp_path, ".dsh/keep")
    probe = classify(tmp_path, _Agent(
        id="dsh", label="DeepSeek Harness", markers=(".dsh",),
        gateway_app_type="dsh", wired=True, depth="metadata"),
        gateway={"dsh": 7221})
    assert probe.state == OBSERVABLE
    assert probe.badge == "metadata"


def test_gateway_agent_conditional_when_app_type_absent(tmp_path: Path) -> None:
    """网关在、但这个 Agent 从来没走过它 → conditional，不是 observable。"""
    _mk(tmp_path, ".dsh/keep")
    probe = classify(tmp_path, _Agent(
        id="dsh", label="DeepSeek Harness", markers=(".dsh",),
        gateway_app_type="dsh", wired=True, depth="metadata"),
        gateway={"mcode": 37816, "claude": 13268})
    assert probe.state == CONDITIONAL


def test_gateway_index_not_supplied_does_not_guess(tmp_path: Path) -> None:
    """没给网关索引时不猜看得见——降级到 conditional，不冒充 observable。"""
    _mk(tmp_path, ".dsh/keep")
    probe = classify(tmp_path, _Agent(
        id="dsh", label="DeepSeek Harness", markers=(".dsh",),
        gateway_app_type="dsh", wired=True, depth="metadata"))
    assert probe.state == CONDITIONAL


def test_local_path_wins_over_gateway(tmp_path: Path) -> None:
    """两者都有时走本地：本地库有思考和工具调用，强一个量级。"""
    _mk(tmp_path, ".minimax/v2/sqlite/runtime-state.sqlite")
    probe = classify(tmp_path, _Agent(
        id="mcode", label="mcode", markers=(".minimax",),
        data_markers=(".minimax/v2/sqlite/runtime-state.sqlite",),
        gateway_app_type="mcode", wired=False),
        gateway={"mcode": 37816})
    assert probe.state == FEASIBLE
    assert probe.data is not None


def test_as_dict_exposes_gateway_app_type() -> None:
    row = AgentProbe(id="dsh", label="dsh", state=OBSERVABLE,
                     depth="metadata",
                     gateway_app_type="dsh").as_dict()
    assert row["gateway_app_type"] == "dsh"


def test_registry_entries_have_labels_and_detail() -> None:
    from plugin.plugins.agent_deskmate.adapters.discovery import _AGENTS
    for agent in _AGENTS:
        assert agent.label, agent.id
        assert agent.markers, agent.id
        assert agent.detail, f"{agent.id} 没有给用户的解释"


def test_real_home_discovery_does_not_raise() -> None:
    """真家目录跑一遍不许炸。不断言具体值——那随机器变。"""
    probes = discover_agents()
    assert isinstance(probes, list)
    for probe in probes:
        assert probe.state in {ABSENT, OBSERVABLE, CONDITIONAL, FEASIBLE, BLIND}


def test_expanduser_is_used_for_default_home() -> None:
    """确认默认家目录走 expanduser，不是硬编码 ``C:\\Users\\...``。"""
    import inspect

    from plugin.plugins.agent_deskmate.adapters import discovery
    source = inspect.getsource(discovery.discover_agents)
    assert "expanduser" in source
    assert "C:\\" not in source
