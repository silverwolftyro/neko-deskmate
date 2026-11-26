"""B5：上游归因爬深 + 字数口径统一。

两件真机实测的事：

1. **上游断在 shell 上**：卡片写 ``bash.exe ← bash.exe ← claude.exe``，
   看不到 DeepSeek Harness。原因是只爬 3 层，而真实链路常常是
   DSH → pwsh → node → claude -p → 又一层 shell → python(中继)。
   修：爬 5 层，**展示时**滤掉纯 shell 层（爬照爬，滤在展示）——
   不爬过去就找不到更上面那个有意义的名字。

2. **同一条命令两个字数**：播报落盘写「命令 120 字」，
   卡片上写「命令共 268 字」。因为播报数的是**渲染后的一行**
   （含【严重】前缀与定位尾巴，命令本身还被截到 80 字），
   卡片数的是完整命令。用户是拿这个数字判断"我有没有看全"的——
   两个数字等于没有数字。修：唯一来源 ``commands.command_length``。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate import hook_relay
from plugin.plugins.agent_deskmate.approval import (
    UPSTREAM_MAX,
    UPSTREAM_SHELLS,
    UPSTREAM_UNKNOWN,
    _is_shell_exe,
    build_card_html,
    describe_upstream,
)
from plugin.plugins.agent_deskmate.broadcast import for_log
from plugin.plugins.agent_deskmate.commands import (
    command_display_note,
    command_length,
)
from plugin.plugins.agent_deskmate.rules import evaluate_delete

LONG = "rm -rf " + " ".join(f"dir{i}" for i in range(60))


def _hit(command: str, cwd: str = r"D:\workspace\proj"):
    from plugin.plugins.agent_deskmate.events import UnifiedEvent
    source = UnifiedEvent(agent="claude-code", session="s1", event="tool_use",
                          tool="Bash", command=command, ts="t",
                          executor="claude-code", working_directory=cwd)
    return evaluate_delete(command, agent="claude-code", session="s1", ts="t",
                           source=source)


# ── 爬深：5 层 ───────────────────────────────────────────────────────────

def test_the_relay_crawls_five_layers() -> None:
    """任务书 B5：只爬 3 层会断在 ``bash.exe ← bash.exe ← claude.exe``。"""
    assert hook_relay.UPSTREAM_DEPTH == 5


def test_a_five_layer_chain_shows_the_harness_node() -> None:
    """任务书 B5 测试第一条：五层链，shell 层被跳过，最终显示 Harness。

    真机那条链的形状：DSH → pwsh → node → claude -p → 又一层 shell → 中继。
    老实全部显示的话是 5 个名字，用户要找的那个被埋在中间。
    """
    chain = [
        {"name": "bash.exe", "path": r"C:\Program Files\Git\bin\bash.exe"},
        {"name": "claude.exe", "path": r"C:\Users\someone\.local\bin\claude.exe"},
        {"name": "node.exe",
         "path": r"C:\Users\someone\AppData\Local\Programs\DeepSeek Harness\node.exe"},
        {"name": "pwsh.exe", "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\pwsh.exe"},
        {"name": "WindowsTerminal.exe", "path": r"C:\Windows\System32\WindowsTerminal.exe"},
    ]
    rendered = describe_upstream(chain)
    assert "DeepSeek Harness" in rendered
    # 纯 shell 层一个都不出现
    assert "bash.exe" not in rendered
    assert "pwsh.exe" not in rendered
    # 有意义的节点都在
    assert "claude.exe" in rendered
    assert "node.exe" in rendered
    assert "WindowsTerminal.exe" in rendered


def test_shell_layers_do_not_consume_the_display_budget() -> None:
    """先滤后截，不是先截后滤。

    反过来做的话，三条 shell 就能把真正的发起者挤出窗外——
    结果是"明明知道却显示上游未知"。
    """
    chain = ([{"name": "bash.exe", "path": "C:/git/bash.exe"}] * (UPSTREAM_MAX + 2)
             + [{"name": "node.exe",
                 "path": r"C:\Program Files\DeepSeek Harness\node.exe"}])
    rendered = describe_upstream(chain)
    assert rendered == "node.exe（DeepSeek Harness）"


@pytest.mark.parametrize("name", sorted(UPSTREAM_SHELLS))
def test_every_shell_in_the_list_is_recognised(name: str) -> None:
    assert _is_shell_exe(name) is True
    assert _is_shell_exe(f"{name}.exe") is True
    assert _is_shell_exe(name.upper()) is True


@pytest.mark.parametrize("name", ["node.exe", "claude.exe", "python.exe",
                                  "deskmate.exe", "explorer.exe"])
def test_non_shells_are_not_skipped(name: str) -> None:
    assert _is_shell_exe(name) is False


def test_an_all_shell_chain_says_unknown() -> None:
    """整条链都是 shell → 上游未知。不拿终端名凑一个发起者。"""
    assert describe_upstream([
        {"name": "bash.exe", "path": "C:/git/bash.exe"},
        {"name": "cmd.exe", "path": "C:/Windows/cmd.exe"},
    ]) == UPSTREAM_UNKNOWN


def test_the_card_line_reflects_the_filtering() -> None:
    """B7 之后主卡不再有上游那一行；过滤逻辑由解释卡那边验。"""
    html = build_card_html(
        subject="claude-code", tool_summary="Bash: ls", session_id="s",
        working_directory=None,
        upstream=[{"name": "bash.exe", "path": "C:/git/bash.exe"},
                  {"name": "node.exe",
                   "path": r"C:\Program Files\DeepSeek Harness\node.exe"}])
    assert "上游" not in html
    assert "bash.exe" not in html


# ── 友好名：整词匹配 + 长词优先 ──────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    (r"C:\Program Files\DeepSeek\app.exe", "app.exe（DeepSeek Harness）"),
    (r"D:\tools\dsh\dsh.exe", "dsh.exe（DeepSeek Harness）"),
    (r"D:\tools\mcode\mcode.exe", "mcode.exe（mcode）"),
    (r"C:\Program Files\nodejs\node.exe", "node.exe"),
    # 纯 shell 层整条被滤掉 → 上游未知（不是显示一个 cmd.exe）
    (r"C:\Windows\System32\cmd.exe", UPSTREAM_UNKNOWN),
    # 目录名误判：词是 desk 的**一部分**，不算命中
    (r"C:\Users\someone\Desktop\foo.exe", "foo.exe"),
    (r"C:\app\nodesk\foo.exe", "foo.exe"),
    (r"C:\app\mydsh\foo.exe", "foo.exe"),
    # 下划线 / 点 / 连字符都算分隔：三种写法的答案必须一致
    (r"D:\dsh_tools\x.exe", "x.exe（DeepSeek Harness）"),
    (r"D:\dsh-tools\x.exe", "x.exe（DeepSeek Harness）"),
    (r"D:\dsh.tools\x.exe", "x.exe（DeepSeek Harness）"),
])
def test_friendly_names_match_whole_words(path: str, expected: str) -> None:
    assert describe_upstream([{"name": path.rsplit("\\", 1)[-1],
                              "path": path}]) == expected


def test_the_longest_needle_wins() -> None:
    """短词恰好是长词的开头时，长词先匹配。

    今天四个词互不为子串，所以这条现在是空转；但它是给将来加词上的保险——
    加一个 ``dshx`` 而忘了排序，``dsh`` 就会抢走本该归它的条目。
    """
    from plugin.plugins.agent_deskmate.approval import _FRIENDLY_BY_LENGTH
    needles = [needle for needle, _ in _FRIENDLY_BY_LENGTH]
    assert needles == sorted(needles, key=len, reverse=True)


# ── 字数：唯一来源 ───────────────────────────────────────────────────────

def test_command_length_is_one_function() -> None:
    """同一个命令，三处问出来是同一个数。"""
    command = "rm -rf " + " ".join(f"dir{i}" for i in range(60))
    hit = _hit(command)
    note = command_display_note(command)
    assert hit.command_length == command_length(command)
    # 卡片提示里的那个数与 RuleHit 上带的是同一个
    assert f"命令共 {hit.command_length} 字" in note


def test_the_broadcast_log_uses_the_full_command_length() -> None:
    """任务书 B5 测试第二条：播报与卡片字数一致。

    老实现播报数的是**渲染行**的长度（含【严重】前缀、定位尾巴，
    命令还被截到 80 字），所以长命令会小一大截。
    """
    command = "rm -rf " + " ".join(f"dir{i}" for i in range(60))
    hit = _hit(command)
    logged = for_log("【严重】Claude Code 删除文件：rm -rf dir0 …（会话 s1 @ D:\\w）",
                     command_length=hit.command_length)
    assert f"命令 {hit.command_length} 字" in logged
    # 与卡片上那句是同一个数
    assert f"命令共 {hit.command_length} 字" in command_display_note(command)


def test_without_a_length_it_falls_back_to_the_rendered_line() -> None:
    """没带 command_length 时退回老行为——不猜，也不崩。"""
    assert "命令 13 字" in for_log("rm -rf build/")


def test_whitespace_does_not_inflate_the_count() -> None:
    """连续空格、换行、制表符都不算字数——否则同一个命令三个数。"""
    assert command_length("rm   -rf\n\tx") == command_length("rm -rf x")
    assert command_length("rm -rf x") == 8          # 空格算，它是命令的一部分
    assert command_length("") == 0
    assert command_length(None) == 0


def test_a_short_command_reports_no_note() -> None:
    assert command_display_note("rm -rf build/") == ""
    assert _hit("rm -rf build/").command_length == 13
