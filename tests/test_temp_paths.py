r"""临时目录降档 + `%VAR%` 逃逸封堵（2026-11-26 第七条误报）。

## 两条改动，一个共同根因

**第七条误报**：删临时文件被顶【严重】。

    【严重】删除文件：rm -f "C:/Users/alpha/AppData/Local/Temp/a.tmp"

档位判定本身没错——`AppData\Local\Temp` 确实在工作区外。但 **extreme 的
存在理由是"破坏面没有边界"**（家目录 / 盘根 / 系统目录），临时目录的破坏面
明显有边界：就是临时文件。

**为什么不能把 `"temp"` 加进 `_RECOVERABLE_MARKERS`**：那是**子串匹配**，
`D:\workspace\templates\`、`src\TempStorage\` 会一起被误判成可恢复——
而降档是这个项目明令禁止的最危险方向。所以临时目录必须单独一套精确前缀。

## 封堵的洞：`%VAR%` 不在替换检测里

加 `%TEMP%` 支持时发现 ``%TEMP%/../notes`` 归一化之后是 ``notes``，
看着像工作区内的相对路径 → 判 normal/warn。而它实际落在用户主目录。

根因：``_SUBSTITUTION_RE`` 只认 ``$`` 与反引号，不认 Windows 的 ``%NAME%``，
于是 `_is_absolute` 返回 False，调用方就按工作目录去拼它——
**可变量展开的结果跟工作目录根本没有关系**。

修法：``%NAME%``（**成对的两个 %**）进替换检测。单个 % 不加回来——
``date +%H:%M:%S`` / ``printf '%s'`` 那种是格式符，不藏路径，
2026-11-26 删掉裸 % 正是为这个。

## 铁律

``C:\Windows\Temp`` **不降档**（它在 `_SYSTEM_DIRS` 的 `c:\windows` 前缀内，
按最坏的猜是这个项目的一贯取向）；``%USERPROFILE%``、``%SOMEVAR%`` 不降档；
带 ``..`` 的临时路径一律不认（``%TEMP%/../notes``、``/tmp/../etc``）。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.commands import (
    TIER_EXTREME,
    TIER_NORMAL,
    TIER_RECOVERABLE,
    _is_temp_path,
    target_unparseable,
    tier_for_command,
)
from plugin.plugins.agent_deskmate.rules import (
    evaluate_delete,
    evaluate_delete_segments,
)

WD = r"D:\workspace\proj"
BS = chr(92)


def _tier(command: str) -> str:
    return tier_for_command(command, WD)


def _severity(command: str) -> str:
    from types import SimpleNamespace

    hit = evaluate_delete(command, source=SimpleNamespace(
        requester="", requester_evidence="", executor="",
        working_directory=WD, session="s", tool="Bash"))
    assert hit is not None, f"{command!r} 没命中删除规则，用例写错了"
    return hit.severity


# ── 该降档的：操作系统自己定义的临时目录 ────────────────────────────────


@pytest.mark.parametrize("target", [
    "%TEMP%",
    "%TEMP%" + BS + "x",
    "%TMP%",
    "C:/Users/alpha/AppData/Local/Temp/a.tmp",
    "C:" + BS + "Users" + BS + "alpha" + BS + "AppData" + BS + "Local" + BS + "Temp",
    "/tmp/x",
    "/tmp",
    "/var/tmp/y",
    "/private/tmp/z",
])
def test_os_temp_directories_are_recoverable(target: str) -> None:
    """临时目录的破坏面有边界：就是临时文件。和 ``D:/other/x`` 不是一回事。"""
    assert _is_temp_path(target) is True
    assert _tier(f"rm -rf {target}") == TIER_RECOVERABLE
    assert _severity(f"rm -rf {target}") == "notice"


def test_both_slash_styles_agree() -> None:
    """两种斜杠必须是同一个答案——同一条命令两种 verdict 是这个项目的病。"""
    forward = "C:/Users/alpha/AppData/Local/Temp/x"
    backward = forward.replace("/", BS)
    assert _tier(f"rm -rf {forward}") == _tier(f"rm -rf {backward}")
    assert _tier(f"rm -rf {forward}") == TIER_RECOVERABLE


# ── 不许跟着降的 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", [
    "D:/workspace/templates",       # 含 temp 子串，但不是临时目录
    "D:/pkg/temporary/x",
    "C:/Windows/Temp",              # 刻意留给 system 档
    "C:/Windows/Temp/x",
    "~",
    "*",
    "../../..",
])
@pytest.mark.parametrize("prefix", ["rm -rf ", "rm -f "])
def test_lookalikes_do_not_become_recoverable(target: str, prefix: str) -> None:
    """**子串匹配的危险**：`templates` / `temporary` 都不算临时目录。

    这就是为什么不能把 ``"temp"`` 塞进 `_RECOVERABLE_MARKERS`——
    那是子串匹配，一塞就把这几个一起降档了。
    """
    assert _tier(prefix + target) == TIER_EXTREME, target


def test_a_relative_lookalike_stays_normal() -> None:
    """``src/TempStorage`` 是工作区内的相对路径，本来就该 normal。

    它在这组里是反面对照：证明"名字像临时"既没让它降档，
    也没让它升档——档位由**位置**决定，不由名字决定。
    """
    assert _tier("rm -rf src/TempStorage") == TIER_NORMAL
    assert _is_temp_path("src/TempStorage") is False


def test_node_modules_is_recoverable_for_its_own_reason() -> None:
    """``node_modules`` 是 recoverable，但**不是因为临时目录**。

    两条判据不许互相冒充：它命中 `_RECOVERABLE_MARKERS`，
    和 `_is_temp_path` 没有关系。
    """
    assert _tier("rm -rf node_modules") == TIER_RECOVERABLE
    assert _is_temp_path("node_modules") is False


def test_windows_temp_is_deliberately_left_to_the_system_tier() -> None:
    """``C:\\Windows\\Temp`` 不搭这趟车。

    它确实在 `_SYSTEM_DIRS` 的 `c:\\windows` 前缀内。想清它该单独决定，
    不该借着"临时目录"的名义一起放过去。
    """
    assert _is_temp_path("C:/Windows/Temp") is False
    assert _is_temp_path("C:/Windows/Temp/x") is False


# ── 带 `..` 的临时路径一律不认 ──────────────────────────────────────────


@pytest.mark.parametrize("target", [
    "%TEMP%/../notes",
    "/tmp/../etc",
    "/var/tmp/../../etc",
])
def test_a_parent_traversal_out_of_a_temp_dir_is_not_temp(target: str) -> None:
    """``%TEMP%/../notes`` 归一化后是 ``notes``，前缀那点信息全是假的。

    认了它就是放走一个逃逸——和 0.6.4 修掉的 `../` 逃逸是同一个错误
    换了个位置。
    """
    assert _is_temp_path(target) is False
    assert _tier(f"rm -rf {target}") == TIER_EXTREME


# ── `%VAR%` 进替换检测 ─────────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf %TEMP%/../notes",
    "rm -rf %USERPROFILE%",
    "rm -rf %SOMEVAR%/x",
])
def test_percent_env_vars_hide_their_target(command: str) -> None:
    """``%NAME%`` 是 Windows 环境变量，替换值不可知。

    不认它的后果：``_is_absolute`` 返回 False，调用方按工作目录去拼它，
    而变量展开的结果跟工作目录没有关系。
    """
    assert target_unparseable(command) is True
    assert _tier(command) == TIER_EXTREME


@pytest.mark.parametrize("command", [
    "date +%H:%M:%S",
    "rm -f x && date +%H:%M:%S",
    "printf '%s' done",
    "rm -rf %TEMP%",          # 我们认识它，不需要解析
    "rm -rf %TEMP%" + BS + "x",
])
def test_a_lone_percent_is_not_a_substitution(command: str) -> None:
    """**单个 % 不许回来**：格式符不藏路径。

    2026-11-26 删掉裸 ``%`` 修的是第四条误报（``date +%H:%M:%S && rm -f x``
    爆【严重】）。这里钉住别为了 ``%TEMP%`` 把它加回来——
    要的是**成对**的 ``%NAME%``。
    """
    assert target_unparseable(command) is False


def test_the_fourth_false_positive_does_not_come_back() -> None:
    """端到端复现第四条误报的那条真机命令。"""
    command = ('cd "D:/w/agent_deskmate" && date +%H:%M:%S && echo probe > '
               '.speak-probe.tmp && rm -f .speak-probe.tmp && echo done')
    assert target_unparseable(command) is False
    assert _tier(command) == TIER_NORMAL


def test_temp_and_a_real_target_in_one_command() -> None:
    """一条命令删两个路径，取最坏的一档——临时目录不许把真目标一起降了。"""
    assert _tier("rm -rf %TEMP% D:/other/x") == TIER_EXTREME


# ── 同命令赋值：变量不再一律顶成极高危 ─────────────────────────────────
#
# 2026-11-26 真机：`MSG=/tmp/x && … && rm -f "$MSG"` 被判【严重】，
# 而这一类占 critical 播报的绝大多数——几乎全是脚本清理自己的临时文件。
# 用户原话：「其中很大部分还是误报，根本就没必要报告的事情」。
#
# 赋值就在同一条命令里写着，读它不是猜。**只读字面值**：
# 值自身要替换的（`$(mktemp)` / `$HOME`）一律不读，照旧按最坏的猜。


def _sev(command: str) -> str:
    hit = evaluate_delete(command, agent="a", session="s")
    return hit.severity if hit else "不命中"


def test_a_literal_assignment_downgrades_a_variable_target() -> None:
    """``MSG=/tmp/x && rm -f "$MSG"`` → notice，不是 critical。"""
    assert _sev('MSG=/tmp/x && git commit -F "$MSG" && rm -f "$MSG"') == "notice"


def test_braced_reference_is_expanded_too() -> None:
    assert _sev('MSG=/tmp/x && rm -rf "${MSG}"') == "notice"


def test_a_substituting_value_is_never_read() -> None:
    """值自身要替换 → 我们不知道它是什么 → 照旧最坏。

    ``MSG=$(mktemp)`` 展开成什么只有 shell 知道；猜一个值比不猜危险。
    """
    assert _sev('MSG=$(mktemp) && rm -rf "$MSG"') == "critical"
    assert _sev('X=$(rm -rf /) && rm -rf "$X"') == "critical"
    assert _sev('MSG=$HOME && rm -rf "$MSG"') == "critical"


def test_a_resolved_dangerous_target_stays_dangerous() -> None:
    """展开出来是危险的，一样顶格——读取赋值不是放水。"""
    assert _sev('MSG=/ && rm -rf "$MSG"') == "critical"
    assert _sev('MSG=/etc && rm -rf "$MSG/passwd"') == "critical"
    # 展开之后再逃逸：归一化会把它折回 /etc
    assert _sev('MSG=/tmp && rm -rf "$MSG/../../etc"') == "critical"


def test_an_unset_variable_stays_unparseable() -> None:
    """换不出来就原样留下，随后按"藏住了"判——方向是安全的。"""
    assert _sev('rm -rf "$UNSET"') == "critical"


def test_one_unresolvable_target_keeps_the_whole_command_dangerous() -> None:
    """多个目标里有一个说不清，整条就说不清（与 target_tier 取最坏同向）。"""
    assert _sev('MSG=/tmp/a && rm -rf "$MSG" "$UNSET"') == "critical"


def test_variable_names_are_case_sensitive() -> None:
    """shell 的 ``$msg`` 与 ``$MSG`` 是两个变量。宽松匹配会误放水。"""
    assert _sev('MSG=/tmp/a && rm -rf "$msg"') == "critical"


def test_the_displayed_summary_keeps_the_original_text() -> None:
    """定档用展开后的文本，**展示用原文**——用户要确认的是自己敲的那行。"""
    hit = evaluate_delete('MSG=/tmp/x && rm -f "$MSG"', agent="a", session="s")
    assert hit is not None
    assert hit.severity == "notice"
    # 目标仍是 ``"$MSG"``，没有被替换成 ``/tmp/x``
    assert hit.summary.endswith('rm -f "$MSG"')
    assert '"/tmp/x"' not in hit.summary


def test_the_assignment_may_live_in_another_segment() -> None:
    """**真机那个形状**：赋值在段 0，删除在段 N，逐段判也要能看见它。

    0.6.16 只在 ``delete_targets`` 内部展开，而 ``evaluate_delete_segments``
    是按段定档的——段 N 看不见段 0 的赋值，于是照样顶 critical。
    """
    command = ('MSG=/tmp/neko-commit-msg.txt && cat > "$MSG" <<\'EOF\'\n'
               'body\nEOF\n'
               'git commit -F "$MSG" | tail -3 && rm -f "$MSG"')
    hits = evaluate_delete_segments(command, agent="a", session="s")
    assert hits, "这条命令里有一个删除段"
    assert [h.severity for h in hits] == ["notice"]
    assert 'rm -f "$MSG"' in hits[0].summary
