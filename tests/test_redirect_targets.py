"""重定向的目标不是命令的参数（2026-11-26 第六条误报，实测最严重的一条）。

## 这条 bug 的形状

``_SEPARATORS`` 把 ``<>`` 当分隔符，于是重定向**操作符自己消失了**，
而它指向的文件名留在 token 流里，被 ``delete_targets`` 当成删除目标：

    rm -f x > /dev/null        目标 = ('x', '/dev/null')   → 【严重】
    rm -f x 2>/dev/null        目标 = ('x', '2', '/dev/null') → 【严重】
    rm -rf build > /dev/null   目标 = ('build', '/dev/null')  → 【严重】

``/dev/null`` 是绝对路径且在工作区外 → extreme → critical。
而 ``> /dev/null`` 在 shell 里极其常见，所以这条不是边角，
是**最大的一块剩余误报**（实测本机 6 次）。

## 修法

在 ``_segments`` 里把「操作符 + 文件名」一起剥掉。放这一处是因为它是
``simple_commands`` 与 ``delete_targets`` 的共同上游——放任一侧都会
让另一侧漂移，而那个项目已经为"两套判据"付过好几次代价了。

## 铁律没放

``rm -rf *`` / ``rm -rf $HOME`` / ``rm -rf ../..`` 照旧 critical。
剥掉的是**重定向目标**，不是命令参数——这两个集合不许混。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.plugins.agent_deskmate.commands import (
    TIER_EXTREME,
    TIER_NORMAL,
    TIER_RECOVERABLE,
    delete_targets,
    severity_for_tier,
    tier_for_command,
)
from plugin.plugins.agent_deskmate.rules import evaluate_delete, match_delete

WD = r"D:\workspace\proj"


def _source() -> SimpleNamespace:
    return SimpleNamespace(requester="", requester_evidence="", executor="",
                           working_directory=WD, session="s", tool="Bash")


def _tier(command: str) -> str:
    return tier_for_command(command, WD)


def _severity(command: str) -> str:
    hit = evaluate_delete(command, source=_source())
    assert hit is not None, f"{command!r} 没命中删除规则，用例写错了"
    return hit.severity


# ── 重定向目标不许进删除目标集合 ────────────────────────────────────────


@pytest.mark.parametrize("command,targets", [
    ("rm -f x > /dev/null", ("x",)),
    ("rm -f x >> /dev/null", ("x",)),
    ("rm -f x 2>/dev/null", ("x",)),
    ("rm -f x 2>> /dev/null", ("x",)),
    ("rm -f x 2>&1", ("x",)),
    ("rm -f x >&2", ("x",)),
    ("rm -rf build > /dev/null", ("build",)),
    ("rm -f x >> out.txt", ("x",)),
    ("rm -f x > \"out file.txt\"", ("x",)),
    ("ls -la && rm -f x > /dev/null", ("x",)),
])
def test_redirect_targets_are_not_delete_targets(command: str,
                                                 targets: tuple[str, ...]) -> None:
    """**这是这次改动的核心**：重定向指向的文件不是命令的参数。"""
    assert delete_targets(command) == targets


@pytest.mark.parametrize("command,expected", [
    ("rm -f x > /dev/null", TIER_NORMAL),
    ("rm -rf build > /dev/null", TIER_RECOVERABLE),
    ("rm -f x 2>/dev/null", TIER_NORMAL),
    ("rm -rf src > /dev/null", TIER_NORMAL),
])
def test_a_redirect_no_longer_escalates_the_tier(command: str,
                                                 expected: str) -> None:
    """剥掉重定向之后，档位由**真正的目标**决定。"""
    assert _tier(command) == expected


def test_the_dev_null_case_end_to_end() -> None:
    """端到端复现：``rm -f x > /dev/null`` 从【严重】变成【警告】。"""
    assert _severity("rm -f x > /dev/null") == "warn"
    assert _severity("rm -rf build > /dev/null") == "notice"


# ── 铁律：剥掉的是重定向，不是命令参数 ──────────────────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf *",
    "rm -rf $HOME",
    "rm -rf ../..",
    "rm -rf ../../..",
    "rm -rf ./* > /dev/null",     # 带重定向的裸通配，照样极端
    "rm -rf ../* > /dev/null",
])
def test_real_danger_survives_the_strip(command: str) -> None:
    """真危险的目标一个不许因为这次改动漏掉。

    后两条特意带 ``> /dev/null``：剥重定向不许顺手把真目标也剥了。
    （用 ``./*`` 而不是 ``./build/*``——后者按 B2 就是 recoverable，
    ``build`` 把作用域圈住了，那是对的，不是漏洞。）
    """
    assert _tier(command) == TIER_EXTREME
    assert _severity(command) == "critical"


@pytest.mark.parametrize("command", [
    "rm -rf $DIR > /dev/null",
    "rm -rf `cat list` > /dev/null",
])
def test_hidden_targets_still_hide_through_a_redirect(command: str) -> None:
    """变量/命令替换藏住的目标，加了重定向照样藏得住。"""
    from plugin.plugins.agent_deskmate.commands import target_unparseable

    assert target_unparseable(command) is True
    assert _tier(command) == TIER_EXTREME


# ── 重定向不得破坏别的判定 ──────────────────────────────────────────────


def test_a_bare_redirect_still_leaves_the_command_intact() -> None:
    """剥重定向不许把命令主体也剥掉——否则 ``ls > /dev/null`` 会变成空段。"""
    from plugin.plugins.agent_deskmate.commands import simple_commands

    assert simple_commands("ls -la > /dev/null") == [["ls", "-la"]]
    assert simple_commands("git status > /dev/null") == [["git", "status"]]


def test_echoing_a_redirect_is_not_a_delete() -> None:
    """``echo "x > /dev/null"`` 一个文件都不删，不许误报。"""
    assert match_delete('echo "x > /dev/null"') is None


def test_heredoc_and_redirect_together() -> None:
    """两种剥离叠在一起也要对：heredoc 正文 + 重定向目标都不算参数。"""
    command = ("cd proj && python - <<'PYEOF'\n"
               "cases = ['rm -f /tmp/a', 'rm -rf ~']\n"
               "PYEOF\n"
               "rm -f out.log > /dev/null")
    assert delete_targets(command) == ("out.log",)
    assert _tier(command) == TIER_NORMAL


def test_severity_matches_the_tier() -> None:
    """档位与严重度不许各说各话（播报层与闸门层共用的那条不变式）。"""
    for command in ("rm -f x > /dev/null", "rm -rf build > /dev/null",
                    "rm -rf ~ > /dev/null"):
        assert severity_for_tier(_tier(command)) == _severity(command)
