"""版本控制目录：档位说不准，但毁掉的是不可恢复的状态。

## 为什么有这一批

2026-11-27 实测发现一个**方向和其他所有修复相反**的洞。这个项目到 0.6.20
为止的十几轮全在压误报——把猫弄得安静了。安静是有代价的：

    rm -rf .git      → 档位 normal / 严重度 warn / 类别 recoverable
    rm -rf ../.git   → 档位 extreme / 严重度 critical / 类别 irreversible

同一个动作，两种待遇。而**更常见的那种（在当前目录删）恰好是哪边都拦不住的**：

1. ``recoverable`` 类别过得了 L2 闸门的第一道墙——``auto_decide_max_severity``
   的上限就允许设到 ``warn``，设到那儿 ``rm -rf .git`` 会被**自动放行**：
   没有卡片，Claude 直接删掉提交历史与全部未提交工作。
2. ``warn`` 严重度默认可送审——B11 的模型一句"常规"就能把通知也压掉。

B11 给模型的授权是「识别常规」，**不是「重新定危险」**。所以修法是只顶
动作类别 + 不许送审，**不动档位、不动严重度**：``rm -rf .git`` 是两难的
（刚 clone 完删它是常规，有三天未提交工作删它是灾难，规则层分不出），
拿【严重】去描述一件分不出轻重的事，正是 B8 禁止的那件事。

## 这批测试盯的四件事

1. 判据本身：按**路径分量**判，不按整串。``.git`` / ``./.git`` / ``../.git``
   / ``foo/.git`` / ``.git/objects`` 都算；``.gitignore`` / ``.github`` /
   ``git`` / ``git-backup`` 都不算。
2. 闸门真的拦住了，**在阈值上限也拦**。
3. 播报不再可送审。
4. **没有顺手扩大打击面**：``node_modules`` / ``.venv`` / ``build`` 照旧
   该放行放行、该送审送审。精度原则双向有效——为了堵一个洞把一堆常规
   操作全拦下，用户下一次就把插件关了。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.approval import (
    ACTION_CLASS_IRREVERSIBLE,
    DEFAULT_ACTION_CLASS,
    _classify,
    auto_decide_gate,
)
from plugin.plugins.agent_deskmate.commands import (
    destroys_irreversible_state,
    severity_for_tier,
)
from plugin.plugins.agent_deskmate import _reviewable_broadcast
from plugin.plugins.agent_deskmate.rules import evaluate_delete_segments
from types import SimpleNamespace

WD = r"D:\workspace\proj"

# 阈值上限。``AUTO_DECIDE_MAX_SEVERITY_CEILING`` 就是 "warn"——
# 用户被允许把阈值设到这儿，所以"拦不住"在最坏配置下必须仍然拦得住。
CEILING = "warn"

# 两侧授权都签了字、模型也判了低风险允许——**能放行的条件全给足**，
# 这样测出来的"拦"才是类别墙拦的，不是别的条件凑不齐。
FULL_GO = dict(
    ai_judgement=True,
    has_key=True,
    auto_decide="low_risk_only",
    max_severity=CEILING,
    authorized_scopes=("ai_judgement", "auto_decide"),
    readonly=True,
)


def _hit(command: str, severity: str = "warn"):
    hits = evaluate_delete_segments(
        command, agent="a", session="s",
        source=SimpleNamespace(requester="", requester_evidence="", executor="",
                               working_directory=WD, session="s", tool="Bash"))
    assert hits, f"{command!r} 没命中删除规则，用例写错了"
    return max(hits, key=lambda h: {"critical": 3, "warn": 2,
                                    "notice": 1}.get(h.severity, 0))


# ── 1. 判据本身 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf .git",
    "rm -rf .git/",
    "rm -rf ./.git",
    "rm -rf ../.git",
    "rm -rf ../../.git",
    "rm -rf foo/.git",
    "rm -rf .git/objects",          # 删对象库一样毁掉整个仓库
    "rm -rf .git .gitignore",       # 混在别的目标里也要认出来
    'rm -rf "my repo/.git"',
    "rm -rf $HOME/proj/.git",
    "rm -rf .hg",
    "rm -rf .svn",
])
def test_version_control_directories_count(command: str) -> None:
    assert destroys_irreversible_state(command) is True


@pytest.mark.parametrize("command", [
    # 名字挨得近但**不是**版本控制目录的
    "rm -rf .gitignore",
    "rm -rf .github",
    "rm -rf .gitmodules",
    "rm -rf git",
    "rm -rf ./git",
    "rm -rf a.git",
    "rm -rf git-backup",
    "rm -rf .git-old",
    # 常规删除，一条都不许被这套判据捎上
    "rm -rf node_modules",
    "rm -rf .venv",
    "rm -rf build",
    "rm -rf dist",
    "rm -rf target",
    # 根本不是删除
    "git status",
    "echo .git",
    "git add .gitignore",
])
def test_lookalikes_do_not_count(command: str) -> None:
    assert destroys_irreversible_state(command) is False


def test_the_match_is_per_path_component_not_substring() -> None:
    """分量匹配，不是子串——``.gitignore`` 里有 ``.git`` 但不是它。

    ``_RECOVERABLE_MARKERS`` 的注释写死了子串匹配会把
    ``D:\\workspace\\templates\\`` 一起误判；同一个错误换到安全方向上更贵。
    """
    assert destroys_irreversible_state("rm -rf .gitignore") is False
    assert destroys_irreversible_state("rm -rf .github") is False
    # 而真的是一个分量的，藏多深都认
    assert destroys_irreversible_state("rm -rf a/b/c/.git/d/e") is True


# ── 2. 闸门：在阈值上限也拦得住 ────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf .git",
    "rm -rf .git/",
    "rm -rf ./.git",
    "rm -rf foo/.git",
])
def test_the_gate_refuses_git_deletion_even_at_the_ceiling(command: str) -> None:
    """能放行的条件全给足，仍然拦——而且原因写明是类别墙。

    2026-11-27 之前这里**是放行的**：``.git`` 在工作区内 → normal 档 →
    recoverable 类别，第一道墙形同不存在。
    """
    _, _, action_class, _ = _classify(command, WD)
    assert action_class == ACTION_CLASS_IRREVERSIBLE

    allowed, why = auto_decide_gate(severity="warn",
                                    action_class=action_class, **FULL_GO)
    assert allowed is False
    assert "irreversible" in why, why


def test_the_gate_still_refuses_when_the_threshold_is_lowered() -> None:
    """阈值降到 notice（默认值）也拦——两道墙，塌了一道还有一道。"""
    _, _, action_class, _ = _classify("rm -rf .git", WD)
    allowed, why = auto_decide_gate(severity="warn",
                                    action_class=action_class,
                                    **{**FULL_GO, "max_severity": "notice"})
    assert allowed is False


# ── 3. 播报：不再可送审 ────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf .git",
    "rm -rf .git/",
    "rm -rf ./.git",
    "rm -rf foo/.git",
    "rm -rf .git/objects",
])
def test_git_deletion_is_never_sent_for_review(command: str) -> None:
    """不许送 B11 审查——模型的授权是"识别常规"，不是"重新定危险"。"""
    assert _reviewable_broadcast(_hit(command)) is False


def test_the_severity_is_deliberately_left_alone() -> None:
    """严重度**刻意不升**：两难的事不拿【严重】冒充（B8）。

    这条钉的是"只关掉没人看就过去的那两条路，不动档位不动严重度"——
    否则下一个人会顺手把 tier 也改了，而改 tier 会连带改掉审批卡片的
    呈现与 B3 排序。
    """
    _, _, _, tier = _classify("rm -rf .git", WD)
    assert severity_for_tier(tier) == "warn", (
        "严重度被改了——见 commands.destroys_irreversible_state 的说明："
        "这件事两难，不该顶成 critical")


# ── 4. 没有顺手扩大打击面 ──────────────────────────────────────────────

@pytest.mark.parametrize("command,expected_class", [
    ("rm -rf node_modules", DEFAULT_ACTION_CLASS),
    ("rm -rf .venv", DEFAULT_ACTION_CLASS),
    ("rm -rf build", DEFAULT_ACTION_CLASS),
])
def test_routine_deletions_are_untouched(command: str,
                                         expected_class: str) -> None:
    """精度原则双向有效：堵一个洞不能把常规操作全拦下。"""
    _, _, action_class, _ = _classify(command, WD)
    assert action_class == expected_class


def test_recoverable_deletions_still_reach_the_gate() -> None:
    """``node_modules`` 照旧该放行放行——这套判据不碰它。"""
    _, _, action_class, _ = _classify("rm -rf node_modules", WD)
    allowed, _ = auto_decide_gate(severity="notice",
                                  action_class=action_class, **FULL_GO)
    assert allowed is True


def test_recoverable_deletions_are_still_reviewable() -> None:
    """同理，播报侧也不该被这套判据挡住。"""
    assert _reviewable_broadcast(_hit("rm -rf node_modules")) is True


# ── 5. 真灾难一格没让 ──────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf /",
    "rm -rf D:/",
    "rm -rf C:/Windows/System32",
    "rm -rf *",
    "rm -rf ../*",
])
def test_real_disasters_are_untouched(command: str) -> None:
    """这套判据是补漏，不是改判——真灾难的待遇一个字不许变。"""
    _, _, action_class, tier = _classify(command, WD)
    assert action_class == ACTION_CLASS_IRREVERSIBLE
    assert tier == "extreme"
    assert _reviewable_broadcast(_hit(command)) is False
