"""B8：播报与审批的档位必须**同源**。

真机实测（2026-11-22）：同一条命令 ``rm -rf ./_b2nm/node_modules``
（cwd = 项目目录），

    审批卡片（hook payload 带 cwd）  → notice
    播报（读转录，该行 has_cwd=False）→ critical【严重】

根因不是判定函数不同，是**喂给它的工作目录不同**：转录记录的顶层 ``cwd``
只出现在约一半的行里（实测 4612/9008），拿不到就按最坏的猜。

后果比"数字难看"严重：两条路径对同一件事给两个结论，用户只能不信其中一边。
而他选择不信的那一边，往往是他觉得吵的那一边——**闸门就是这么被关掉的**。

本文件钉三件事：
  1. 会话级 cwd 缓存（首个带 cwd 的记录沿用给后续缺 cwd 的）；
  2. 真沿用不到 → 「范围未知」档 + 文案明说，**不冒充【严重】**；
  3. 两条路径对同一 (命令, 工作目录) 必须给出同一个档位与同一个严重度。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate.adapters.claude_code import (
    ClaudeCodeAdapter,
    CwdCache,
)
from plugin.plugins.agent_deskmate.approval import (
    ACTION_CLASS_RECOVERABLE,
    AUTO_LOW_RISK_ONLY,
    assess_risk,
    assess_tier,
    auto_decide_gate,
)
from plugin.plugins.agent_deskmate.commands import (
    TIER_EXTREME,
    TIER_NORMAL,
    TIER_RECOVERABLE,
    TIER_SCOPE_UNKNOWN,
    TIER_UNKNOWN,
    severity_for_tier,
)
from plugin.plugins.agent_deskmate.events import UnifiedEvent, parse_transcript_line
from plugin.plugins.agent_deskmate.rules import (
    delete_tier,
    evaluate_delete,
    l2_readonly_allowed,
)

WD = r"D:\workspace\proj"
OTHER = r"D:\other"


# ── 造记录 ────────────────────────────────────────────────────────────────

def _tool_record(command: str, *, cwd: str | None, session: str = "sess-1") -> dict:
    """一条真实的 assistant tool_use 转录记录。``cwd=None`` 时不写这个键。"""
    record: dict = {
        "type": "assistant",
        "sessionId": session,
        "isSidechain": False,
        "timestamp": "2026-11-22T06:00:00.000Z",
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                 "input": {"command": command}},
            ],
        },
    }
    if cwd is not None:
        record["cwd"] = cwd
    return record


def _event(command: str, *, cwd: str | None, session: str = "sess-1") -> UnifiedEvent:
    events = parse_transcript_line(json.dumps(_tool_record(command, cwd=cwd,
                                                          session=session),
                                              ensure_ascii=False))
    assert len(events) == 1
    return events[0]


def _broadcast_severity(command: str, *, cwd: str | None) -> str | None:
    """播报侧：转录事件 → 删除规则 → 严重度。

    返回 None 表示播报侧**不出规则命中**（不是删除类、词典也不认的
    不可恢复动作）。那种命令两侧本就无可比性，见
    ``test_the_discard_rule_is_not_broadcast``。
    """
    hit = evaluate_delete(command, source=_event(command, cwd=cwd))
    return None if hit is None else hit.severity


# ── 根因：缺 cwd 不再一律 critical ────────────────────────────────────────

def test_the_real_machine_case_no_longer_contradicts_itself() -> None:
    """任务书记的真机那一条：``rm -rf ./_b2nm/node_modules``。

    **先说诚实的部分**：这一条在当前代码上复现不出矛盾——目标名里带
    ``node_modules``，两侧都判可恢复档、都给 notice。任务书里的
    "播报 critical / 审批 notice" 应该是 B2 上线前的旧版本留下的
    （那时播报对任何删除一律 critical）。

    但**根因是真的**，换个目标就能看见：同一个 ``rm -rf x``，
    老代码在缺 cwd 时判 critical，而审批卡片（hook 带 cwd）判 notice。
    下面这条钉的就是修好之后的状态。
    """
    command = "rm -rf x"
    # 审批侧（hook 带 cwd）：工作区内 → warn
    assert assess_risk("Bash", {"command": command}, WD) == ("warn", "delete",
                                                            "recoverable")
    # 播报侧（转录缺 cwd）：范围未知 → warn，不是 critical
    assert _broadcast_severity(command, cwd=None) == "warn"
    # 任务书原例：两侧都是 notice，自洽
    assert _broadcast_severity("rm -rf ./_b2nm/node_modules", cwd=None) == "notice"
    assert assess_risk("Bash", {"command": "rm -rf ./_b2nm/node_modules"},
                       WD)[0] == "notice"


def test_scope_unknown_is_its_own_tier_not_extreme() -> None:
    """「范围未知」与「解析不出目标」是两件事，不许混成一档。

    后者是真不知道要删什么（按最坏的猜 → 极高危）；前者是知道删什么、
    只是说不清位置（→ warn + 文案明说）。混成一档就等于把 B8 又改回去了。
    """
    assert assess_tier("Bash", {"command": "rm -rf x"}, None) == TIER_SCOPE_UNKNOWN
    assert assess_tier("Bash", {"command": "rm -rf $(cat p)"}, None) == TIER_EXTREME
    assert severity_for_tier(TIER_SCOPE_UNKNOWN) == "warn"
    assert severity_for_tier(TIER_EXTREME) == "critical"


def test_scope_unknown_still_needs_a_human() -> None:
    """范围未知 ≠ 低风险：severity 是 warn，仍高于默认阈值 notice，
    所以默认配置下它不会被 L2 自动放行。"""
    assert assess_tier("Bash", {"command": "rm -rf x"}, None) == TIER_SCOPE_UNKNOWN
    assert severity_for_tier(TIER_SCOPE_UNKNOWN) != "notice"


def test_scope_unknown_broadcast_says_so_in_words() -> None:
    """任务书 B8 第 2 条：文案明说"无法确定在不在工作区外"。

    不说的后果：用户看到【警告】却不知道为什么警告，只能当成又一次狼来了。
    """
    hit = evaluate_delete("rm -rf x", source=_event("rm -rf x", cwd=None))
    assert hit is not None
    assert "说不清在不在工作区外" in hit.summary
    assert hit.severity == "warn"
    # 有 cwd 时不加这句——信息够了，不加噪音
    known = evaluate_delete("rm -rf x", source=_event("rm -rf x", cwd=WD))
    assert known is not None
    assert "说不清在不在工作区外" not in known.summary


# ── 会话级 cwd 缓存 ───────────────────────────────────────────────────────

def test_a_record_without_cwd_inherits_the_sessions_cwd() -> None:
    """任务书 B8 测试第二条：同一会话第二条记录缺 cwd → 沿用第一条。"""
    first = _event("rm -rf x", cwd=WD)
    second = _event("rm -rf y", cwd=None)
    filled = CwdCache().fill([first, second])
    assert filled[0].working_directory == WD
    assert filled[1].working_directory == WD
    # 沿到之后就能正常分档了：不再是"范围未知"
    assert delete_tier("rm -rf y", filled[1].working_directory) == TIER_NORMAL


def test_the_cache_does_not_lend_across_sessions() -> None:
    """会话之间没有理由共享目录。A 会话的 cwd 不许替 B 会话说话。"""
    events = [_event("rm -rf x", cwd=WD, session="a"),
              _event("rm -rf y", cwd=None, session="b")]
    filled = CwdCache().fill(events)
    assert filled[0].working_directory == WD
    assert filled[1].working_directory is None


def test_a_none_cwd_does_not_erase_a_known_one() -> None:
    """缓存只被**有值**更新。None 覆盖已知值会让分档随机抖动。"""
    cache = CwdCache()
    cache.fill([_event("rm -rf x", cwd=WD)])
    later = cache.fill([_event("rm -rf y", cwd=None)])
    assert later[0].working_directory == WD


def test_records_without_a_session_are_left_alone() -> None:
    """没有 session 就没有"同一会话"可言，不猜。"""
    cache = CwdCache()
    cache.fill([_event("rm -rf x", cwd=WD)])
    orphan = _event("rm -rf y", cwd=None, session="")
    assert cache.fill([orphan])[0].working_directory is None


def test_a_session_that_never_reports_cwd_stays_scope_unknown() -> None:
    """缓存是线索不是事实：整条会话都没出现过 cwd 时，宁可说"范围未知"，
    也不拿别的会话/别的时间点的目录来填。"""
    filled = CwdCache().fill([_event("rm -rf x", cwd=None)])
    assert filled[0].working_directory is None
    assert delete_tier("rm -rf x", None) == TIER_SCOPE_UNKNOWN


def test_fill_does_not_mutate_the_original_events() -> None:
    """原事件不改：播报与诊断可能各自持有一份，原地改会互相看不见。"""
    original = _event("rm -rf x", cwd=None)
    CwdCache().fill([_event("rm -rf y", cwd=WD), original])
    assert original.working_directory is None


def test_the_latest_cwd_wins_within_a_session() -> None:
    """会话中途换过目录：后来的记录带上新 cwd，缓存跟着更新。"""
    cache = CwdCache()
    cache.fill([_event("rm -rf x", cwd=WD)])
    filled = cache.fill([_event("rm -rf y", cwd=OTHER),
                         _event("rm -rf z", cwd=None)])
    assert filled[1].working_directory == OTHER


# ── 适配器接线 ───────────────────────────────────────────────────────────

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records)
                    + "\n", encoding="utf-8")


def test_the_adapter_fills_cwd_before_returning(tmp_path: Path) -> None:
    """补 cwd 必须发生在**返回之前**：让每个调用方都自己记得补一遍，
    就等于谁都不会补。

    流程按真实情况摆：首轮只见水位（不解析历史），所以把
    「带 cwd 的记录 + 缺 cwd 的记录」一起追加进去再读——第二条要拿到
    第一条的目录。
    """
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [_tool_record("ls -la", cwd=WD)])
    adapter = ClaudeCodeAdapter(tmp_path)
    assert adapter.poll() == []            # 首轮只记水位
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_tool_record("echo hi", cwd=WD),
                                ensure_ascii=False) + "\n")
        handle.write(json.dumps(_tool_record("rm -rf x", cwd=None),
                                ensure_ascii=False) + "\n")
    events = adapter.poll()
    assert len(events) == 2
    assert events[0].working_directory == WD
    assert events[1].working_directory == WD


def test_the_adapter_cache_survives_across_polls(tmp_path: Path) -> None:
    """跨轮次沿用：缓存是适配器的状态，不是一次性的。"""
    target = tmp_path / "a.jsonl"
    _write_jsonl(target, [_tool_record("ls -la", cwd=WD)])
    adapter = ClaudeCodeAdapter(tmp_path)
    adapter.poll()
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_tool_record("echo hi", cwd=WD),
                                ensure_ascii=False) + "\n")
        handle.write(json.dumps(_tool_record("rm -rf x", cwd=None),
                                ensure_ascii=False) + "\n")
    first = adapter.poll()
    assert first[1].working_directory == WD
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_tool_record("rm -rf y", cwd=None),
                                ensure_ascii=False) + "\n")
    second = adapter.poll()
    assert len(second) == 1
    assert second[0].working_directory == WD


# ── 两条路径对拍 ─────────────────────────────────────────────────────────

COMMANDS = [
    "rm -rf node_modules",
    "rm -rf src",
    "rm -rf D:/workspace/proj/src",
    "rm -rf D:/other/x.txt",
    "rm -rf ~",
    "rm -rf /etc",
    "rm -rf *",
    "rm -rf $(cat pathlist)",
    "git clean -fdx",
    "git reset --hard",
    "git status",
    "ls -la",
]


COMMANDS = [
    "rm -rf node_modules",
    "rm -rf src",
    "rm -rf D:/workspace/proj/src",
    "rm -rf D:/other/x.txt",
    "rm -rf ~",
    "rm -rf /etc",
    "rm -rf *",
    "rm -rf $(cat pathlist)",
    "git clean -fdx",
]
# 播报侧**不出规则命中**的命令：不是删除类，词典认得的不可恢复动作
# （discard）目前只进闸门、不进播报。档位照比对，严重度无可比性。
OTHER_COMMANDS = ["git reset --hard", "git status", "ls -la"]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("cwd", [WD, OTHER, None])
def test_both_paths_give_the_same_tier(command: str, cwd: str | None) -> None:
    """任务书 B8 第 3 条：同一命令、同一工作目录 → 同一个档位。

    两侧走的是**同一个函数**（``commands.tier_for_command``），
    这条测试锁的是"没人给某一侧塞一份私有判据"。
    """
    assert (assess_tier("Bash", {"command": command}, cwd)
            == delete_tier(command, cwd)), command


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("cwd", [WD, OTHER, None])
def test_both_paths_give_the_same_severity(command: str, cwd: str | None) -> None:
    """档位相同 → 严重度必须相同。这是用户直接看到的那一层。"""
    severity, _, _ = assess_risk("Bash", {"command": command}, cwd)
    broadcast = _broadcast_severity(command, cwd=cwd)
    assert broadcast is not None, command     # 删除类两侧都该有命中
    assert broadcast == severity, command


@pytest.mark.parametrize("command", OTHER_COMMANDS)
@pytest.mark.parametrize("cwd", [WD, OTHER, None])
def test_the_discard_rule_is_not_broadcast(command: str, cwd: str | None) -> None:
    """**B8 顺带发现的一处不同源，本批不修，先钉住现状。**

    ``git reset --hard``：审批侧判 warn（词典认得它不可恢复），
    播报侧**根本不播**——``match_delete`` 不认它，
    而播报层只挂删除/失败/下派三条规则，没有 discard 这条。

    这不是 B8 的根因（与 cwd 无关），但是同一类病：
    "审批说危险、播报不说话"。修它要给播报层加 discard 规则，
    属于新功能不是修 bug，所以留给单独一项，不混进 B8 的提交里。
    """
    assert _broadcast_severity(command, cwd=cwd) is None
    assert assess_tier("Bash", {"command": command}, cwd) == TIER_UNKNOWN


def test_tier_and_severity_never_disagree() -> None:
    """档位是唯一的，严重度由它推导——出现"档位可恢复但 critical"就是两套判据。"""
    for command in (*COMMANDS, *OTHER_COMMANDS):
        for cwd in (WD, OTHER, None):
            tier = assess_tier("Bash", {"command": command}, cwd)
            severity, _, _ = assess_risk("Bash", {"command": command}, cwd)
            if tier in (TIER_RECOVERABLE, TIER_NORMAL, TIER_EXTREME,
                        TIER_SCOPE_UNKNOWN):
                assert severity == severity_for_tier(tier), (command, cwd, tier)
            else:
                # 非删除类的档位是个占位（unknown），severity 由 discard 规则
                # 或默认值决定，与占位档位无关。
                assert tier == TIER_UNKNOWN, (command, cwd)


def test_scope_unknown_never_reaches_the_low_risk_bucket() -> None:
    """范围未知不许因为"档位不高"就被当成低风险放行。

    它 severity 是 warn（高于默认阈值 notice），而且删除命令永远进不了
    L2 只读白名单——两道墙都在，这条把两道墙一起钉住。
    """
    eligible, why = auto_decide_gate(
        severity="warn", ai_judgement=True, has_key=True,
        auto_decide=AUTO_LOW_RISK_ONLY, max_severity="warn",
        authorized_scopes=("ai_judgement", "auto_decide"),
        action_class=ACTION_CLASS_RECOVERABLE,
        readonly=l2_readonly_allowed("rm -rf x"))
    assert eligible is False
    assert "只读白名单" in why
