"""B1：命令归一化——判定不许对**命令写法**敏感。

2026-11-22 真机记录到的现象：同一个删除动作，verdict 随前缀与尾巴一起变。
任务书里记的是「加 `VAR=` 前缀判 notice」，实测复现出来的根因不是那个前缀，
而是**帮助豁免只看段首**：

    rm -rf ~              → critical
    rm -rf ~ --help       → notice     ← 真删除被赦免
    sudo rm -rf ~ --help  → critical   ← 加个 sudo 反而不赦免

同一个动作、两种结论，差在一个前缀。所以 B1 修的不是"堵住某个前缀"，
而是让"命令主体是谁"这个词有稳定含义：先归一化，再判。

本文件全部钉在这条上：**写法可以变，verdict 不能变**。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.approval import assess_risk, assess_tier
from plugin.plugins.agent_deskmate.commands import (
    TIER_SCOPE_UNKNOWN,
    command_body_unknown,
    delete_targets,
    simple_commands,
)
from plugin.plugins.agent_deskmate.rules import match_delete

WD = r"D:\workspace\proj"

# 任务书点名的五种写法。全部是同一个动作：删掉 `x` / `~`。
PREFIX_WRITINGS = [
    "rm -rf ~",
    "VAR=x rm -rf ~",
    'VAR="a b" rm -rf ~',
    "env VAR=x rm -rf ~",
    "sudo rm -rf ~",
    "sudo -u root rm -rf ~",
    "command rm -rf ~",
    "nohup rm -rf ~",
    "time rm -rf ~",
    "exec rm -rf ~",
    "(rm -rf ~)",
    "{ rm -rf ~; }",
    "! rm -rf ~",
    "xargs rm -rf ~",
    'bash -c "rm -rf ~"',
    "/bin/rm -rf ~",
    r"C:\Windows\System32\rm.exe -rf ~",
]


@pytest.mark.parametrize("command", PREFIX_WRITINGS)
def test_every_writing_of_the_same_delete_is_still_a_delete(command: str) -> None:
    """任务书 B1 测试第一条：这五种（以及更多）写法都必须判成删除类。"""
    assert match_delete(command) == "rm"


@pytest.mark.parametrize("command", PREFIX_WRITINGS)
def test_writing_does_not_change_the_verdict(command: str) -> None:
    """同一动作的不同写法，闸门结论必须一致。

    这是 B1 真正的验收点：不是"某个前缀能绕过"被堵住，
    而是**整组写法给出同一个档位**。加一条新前缀进来，这组断言自动覆盖它。
    """
    assert assess_tier("Bash", {"command": command}, WD) == "extreme"


def test_the_help_bypass_is_closed() -> None:
    """`--help` 不再是赦免令——这是本次真机问题的真正根因。

    老实现：段首是删除命令 + 后面任意位置有 `--help` → 整段赦免。
    于是 `rm -rf ~ --help`（真删除）判 notice，而 `sudo rm -rf ~ --help`
    因为 `sudo` 占了段首反而不赦免。两种 verdict，差一个前缀。
    """
    assert match_delete("rm -rf ~ --help") == "rm"
    assert match_delete("rm -rf ~ -h") == "rm"
    assert match_delete("rm -rf ~ /?") == "rm"
    assert match_delete("sudo rm -rf ~ --help") == "rm"
    assert assess_tier("Bash", {"command": "rm -rf ~ --help"}, WD) == "extreme"


def test_pure_help_requests_are_still_exempt() -> None:
    """纯问帮助仍然赦免——看帮助不等于真删文件。"""
    assert match_delete("rm --help") is None
    assert match_delete("del --help") is None
    assert match_delete("rmdir --help") is None
    assert match_delete("rm -h") is None


def test_help_flag_after_a_target_does_not_exempt() -> None:
    """有目标就不是问帮助，哪怕帮助开关在最后。"""
    assert match_delete("del x.txt --help") == "del"
    assert match_delete("rm -rf build --help") == "rm"


def test_strings_containing_a_delete_token_are_not_deletes() -> None:
    """任务书 B1 回归项：字符串内不执行。"""
    assert match_delete('echo "rm -rf x"') is None
    assert assess_risk("Bash", {"command": 'echo "rm -rf x"'}, WD)[0] == "notice"


def test_a_delete_inside_a_git_commit_message_still_matches_on_purpose() -> None:
    """`git commit -m "rm -rf x"` 仍判删除——**这是刻意的**。

    2026-11-21 定下的取舍：误报在 summary 里一眼可辨（用户看得到那是 commit），
    漏判却是隐形的。所以 `git` 刻意**不在**"不执行参数"名单里——
    收了它，`git rm --cached x` 这种真删除就会被漏掉。
    真要消掉这条误报，得等 B3 的拆段 + 引号感知，不是 B1 的事。
    """
    assert match_delete('git commit -m "rm -rf everything"') == "rm"
    assert match_delete("git rm --cached secret.txt") == "rm"


def test_real_deletes_in_unusual_positions_still_match() -> None:
    """漏判是隐形的，所以兜底匹配保留：命令主体不认识时照旧命中。"""
    assert match_delete("git rm --cached secret.txt") == "rm"
    assert match_delete("xargs -I{} rm -rf {}") == "rm"


def test_normalisation_never_touches_the_displayed_command() -> None:
    """任务书 B1 第 2 条：归一化只用于判定，原文仍给卡片（A1 不变）。"""
    command = 'VAR="a b" sudo rm -rf D:/Other/X.TXT'
    # 判定侧：剥干净了（赋值、包装词都没了，命令主体是 rm）。
    # 注意这里是小写——判定用归一化文本，大小写不影响它。
    assert simple_commands(command) == [["rm", "-rf", "d:/other/x.txt"]]
    # 展示侧：目标路径保留**原文大小写**，一个字符都没动。
    # 两条路各管一头：判定可以随便归一化，用户屏幕上必须是他刚敲的那个路径。
    assert delete_targets(command) == ("D:/Other/X.TXT",)
    assert assess_risk("Bash", {"command": command}, WD)[0] == "critical"


def test_normalisation_is_pure() -> None:
    """纯函数：同入参同出参，且绝不依赖调用顺序（没有隐藏状态）。"""
    command = 'VAR=x sudo rm -rf ~'
    assert simple_commands(command) == [["rm", "-rf", "~"]]
    assert simple_commands(command) == simple_commands(command)
    assert delete_targets(command) == ("~",)
    # 再来一遍还是一样的——归一化不记账、不计数
    assert simple_commands(command) == [["rm", "-rf", "~"]]


@pytest.mark.parametrize("command,expected", [
    ("VAR=x", True),           # 整条只有赋值，没有命令主体
    ("", False),               # 空串不是"看不懂"，是"没有"
    ("   ", False),
    ("echo hi", False),        # 认得出主体
    ("rm -rf x", False),
    ("VAR=x && ", True),       # 第二段剥完是空的
])
def test_command_body_unknown_is_the_failsafe_signal(command: str, expected: bool) -> None:
    assert command_body_unknown(command) is expected


def test_unreadable_commands_never_fall_below_warn() -> None:
    """任务书 B1 第 3 条：归一化后仍无法确定主体 → 绝不低于 notice。

    B2 进一步要求至少 warn：看不懂的命令按最坏的猜，比按最好的猜安全。
    """
    severity, rule, action_class = assess_risk("Bash", {"command": "VAR=x"}, WD)
    assert severity == "warn"
    assert rule == "unclear"
    assert action_class == "recoverable"


def test_no_working_directory_is_not_the_same_as_worst_case() -> None:
    """B8 改了这里的老口径。

    老实现：拿不到 cwd → 按最坏的猜 → critical。
    实测后果：转录里每条缺 cwd 的删除都顶【严重】，而审批卡片
    （hook payload 带 cwd）对同一条命令说 notice——**同一动作两个结论**。
    现在落「范围未知」档：severity 仍是 warn（不低于 notice、仍要人点），
    但不再冒充一个我们并不掌握的事实（"它在工作区外"）。
    """
    assert assess_risk("Bash", {"command": "rm -rf x"}, None)[0] == "warn"
    assert assess_tier("Bash", {"command": "rm -rf x"}, None) == TIER_SCOPE_UNKNOWN
    # 与工作目录无关的判据不受影响：家目录/系统目录照样极高危
    assert assess_risk("Bash", {"command": "rm -rf ~"}, None)[0] == "critical"
    # 认得出的只读命令也不受影响
    assert assess_risk("Bash", {"command": "ls -la"}, None)[0] == "notice"
    # 目标解析失败（另一回事）仍然极高危
    assert assess_risk("Bash", {"command": "rm -rf $(cat p)"}, None)[0] == "critical"


def test_non_bash_tools_are_untouched() -> None:
    assert assess_risk("Read", {"file_path": "x"}, WD) == ("notice", "", "recoverable")
    assert assess_risk("Bash", None, WD) == ("notice", "", "recoverable")
    assert assess_risk("Bash", {}, WD) == ("notice", "", "recoverable")
