"""命令位判据：**只有 shell 真的会执行的 token 才算删除动作**。

2026-11-26 真机实测抓到的一类误报，根子是"这段里有删除动作吗"的答案
写成了"任一个 token 的 basename 等于删除词"：

.. code-block::

    print('allow 里带 rm 的:')            # 一句只读代码
      → 判成删除段
      → delete_targets 把 perm.get('allow',[]) 里的 [] 也当目标
      → _target_hides_scope 说"作用域被 glob 藏住了"
      → target_tier(unparseable=True) 直接给 EXTREME
      → 一条【严重】播报，而 critical 是"强制常亮、永不递减"的那一档

所以这次改的是**判据本身**，不是加豁免名单。本文件大半测试在钉两件事：

1. 引号里 / 非段首整词引号里的删除词，**不算**命令；
2. 上面那些收窄，**一条危险命令都不许漏**——第 2 条比第 1 条重要。

方向说明：本次只做"少误报"，不做"多匹配"。已知的改动前就有的误报
（``git commit -m "rm the file"``、``git clean --help``）**刻意留着**，
它们各自另有原因，混在一起改会分不清是谁起的作用。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.plugins.agent_deskmate.commands import (
    _CODE_STRING_OWNERS,
    _command_position_tokens,
    _is_quoted_word,
    delete_command_bodies,
    is_delete_command,
    target_unparseable,
)
from plugin.plugins.agent_deskmate.rules import evaluate_delete

WD = r"D:\workspace\proj"
SRC = SimpleNamespace(requester="", requester_evidence="", executor="",
                      working_directory=WD, session="s", tool="Bash")


def _hit(command: str):
    return evaluate_delete(command, agent="a", session="s", source=SRC)


# ── 第 2 条（更重要）：收窄之后危险命令一条都不许漏 ────────────────────


@pytest.mark.parametrize("command", [
    "rm -rf src",
    "rm src",
    "rmdir build",
    "del /f a.txt",
    "Remove-Item -Recurse x",
    # 包装词 / 赋值 / 分组：剥噪音之后命令词落到 0 号位
    "sudo rm -rf x",
    "env FOO=1 rm -rf x",
    'VAR="a b" rm -rf x',
    "xargs rm -rf",
    "(rm -rf x)",
    "! rm -rf x",
    "/bin/rm -rf x",
    r"C:\Windows\System32\rm.exe -rf x",
    # 代码串主人：引号串里跑的是 shell
    'bash -c "sudo rm -rf /"',
    "sh -c 'rm -rf /'",
    'bash -c "rm -rf /"',
    'sudo bash -c "sudo rm -rf /"',
    'ssh host "sudo rm -rf /data"',
    'ssh host "rm -rf /data"',
    "docker exec c sh -c 'rm -rf /'",
    'powershell -Command "Remove-Item x"',
    # 引号包住整个命令名
    '"rm" -rf x',
    "'rm' -rf x",
    'sudo "rm" -rf x',
    'xargs "rm" -rf',
    # 管道 / 多段 / find -exec
    "cd D:/w && rm -rf x",
    "find . -name x -exec rm {} \\;",
    "echo hi | xargs rm",
    "rm -rf a; rm -rf b",
    # 引号里的路径
    'rm -rf "my rm dir"',
    'rm -rf "$HOME"',
    # 成对的危险动作
    "git clean -fdx",
    "git clean -xdf",
    'mysql -e "drop table users"',
])
def test_dangerous_commands_still_match(command: str) -> None:
    """**这次改动唯一重要的验收**：少误报不能靠漏危险命令换来。"""
    assert is_delete_command(command) is True, command
    assert _hit(command) is not None, command


def test_the_real_false_positive_is_gone() -> None:
    """真机抓到的那条命令：一句只读 python 不再是删除。

    改动前它是 ``critical``——而 critical 是"强制常亮、永不递减"的那档。
    """
    command = (
        'python -c "\n'
        "import json,io\n"
        "p=r'C:\\Users\\alpha\\.claude\\settings.json'\n"
        "perm=d.get('permissions',{})\n"
        "print('allow 里带 rm 的:')\n"
        'for r in perm.get(\'allow\',[]):\n'
        "    if 'rm' in r.lower(): print('  ',r)\n"
        '"\n'
    )
    assert _hit(command) is None
    # 档位那一侧也跟着说不清：没有目标，也不是"藏住了作用域"
    assert target_unparseable(command) is False


# ── 第 1 条：引号里的删除词不是命令 ──────────────────────────────────


@pytest.mark.parametrize("command", [
    "print('allow 里带 rm 的:')",
    "echo \"please rm this\"",
    "echo \"rm -rf 是危险操作\"",
    "grep -r \"rm \" .",
    "grep 'rm' file",
    "python -c \"print('x rm y')\"",
    "if 'rm' in r.lower(): print('',r)",
    "python -c \"import json; perm=d.get('permissions',{})\"",
    "awk '{print $1}' file",
    "sed -i 's/rm/x/g' file",
])
def test_delete_words_inside_quotes_are_data(command: str) -> None:
    """引号内部往后都是数据——要打印/匹配的字符不是要跑的命令。"""
    assert _hit(command) is None, command


def test_help_exemption_is_untouched() -> None:
    assert _hit("rm --help") is None
    assert _hit("sudo rm --help") is None


# ── 判据本身 ──────────────────────────────────────────────────────────


def test_quoted_interior_tokens_are_dropped() -> None:
    """引号内部的 token 不进命令位；剥噪音后的主体仍然保留它们。

    两边都要有：``match_delete`` 的段首/帮助/打印类豁免看的是**主体**，
    兜底扫描看的是**命令位**。合成一份两个都要给。
    """
    body, position = delete_command_bodies("echo \"please rm this\"")[0]
    assert body == ["echo", "please", "rm", "this"]
    assert position == ["echo", "please"]


def test_a_non_command_position_quoted_word_is_a_value() -> None:
    """``grep 'rm' file`` 的 rm 是被比较的字符串，不是命令名。"""
    position = _command_position_tokens(
        [("grep", "grep"), ("'rm'", "rm"), ("file", "file")])
    assert position == ["grep", "file"]


@pytest.mark.parametrize("command", [
    '"rm" -rf x',
    "'rm' -rf x",
    'sudo "rm" -rf x',
    'xargs "rm" -rf',
    'VAR="a b" "rm" -rf x',
])
def test_a_quoted_word_at_the_command_position_is_the_command(
        command: str) -> None:
    """命令位例外：整词被引号包住，shell 照样执行。

    判据是"它是不是剥掉包装词之后的段首"，**不是"它在第几位"**——
    ``xargs "rm" -rf`` 的 ``"rm"`` 在第二位，可 xargs 是包装词，
    剥掉之后它就是段首。按位数判会漏掉它（实测踩过）。
    """
    assert _is_quoted_word('"rm"') is True
    assert _hit(command) is not None, command


def test_an_unclosed_quote_is_not_a_quoted_word() -> None:
    """``'rm`` 是一长串引号内容的开头，本身不是值。

    这条是 ``sh -c 'rm -rf /'`` 能继续命中的原因：剥掉 ``sh -c`` 之后
    段首就是 ``'rm``，它必须算命令位。
    """
    assert _is_quoted_word("'rm") is False
    assert _is_quoted_word('"sudo') is False
    # 空串也是"整词引号"——它是值，不是命令名
    assert _is_quoted_word("''") is True


@pytest.mark.parametrize("owner", sorted(_CODE_STRING_OWNERS))
def test_code_string_owners_keep_every_token_in_command_position(
        owner: str) -> None:
    """代码串主人不豁免：``bash -c "sudo rm -rf /"`` 的 rm 是真删除。

    逐个参数化是为了让"往这个集合里加词"这件事有测试可见——
    加错一个词的后果是那一类命令整体漏判。
    """
    position = _command_position_tokens(
        [(owner, owner), ("-c", "-c"), ('"sudo', "sudo"), ("rm", "rm"),
         ('-rf"', "-rf")])
    assert "rm" in position, owner


def test_python_is_not_a_code_string_owner() -> None:
    """**python 故意不在 ``_CODE_STRING_OWNERS`` 里。**

    它在 ``_EXEC_WRAPPERS`` 里（``python -c "import shutil; …"`` 确实会
    执行代码），但 Python 的字符串是数据：``python -c "print('… rm …')"``
    的 rm 只是要打印的字符。bash 跑的是 shell、python 跑的是 Python，
    只有前者的字符串里每个词都是命令。
    """
    assert "python" not in _CODE_STRING_OWNERS
    assert "python3" not in _CODE_STRING_OWNERS
    assert "node" not in _CODE_STRING_OWNERS


def test_the_owner_must_appear_before_the_quote_opens() -> None:
    """代码串主人要在引号**之前**出现，否则就是普通的数据命令。

    ``echo "run bash -c 'rm -rf /'"`` 的 bash 在被打印的字符串里，
    那时引号已经开着——不能因为它就把整段当代码。
    """
    position = _command_position_tokens(
        [("echo", "echo"), ('"run', "run"), ("bash", "bash"),
         ("-c", "-c"), ("'rm", "rm"), ("-rf", "-rf"), ("/'", "/")])
    assert "rm" not in position


def test_a_code_string_owner_may_come_after_a_wrapper() -> None:
    """``sudo bash -c "…"``：包装词在前，主人在后，照样算代码串。"""
    position = _command_position_tokens(
        [("sudo", "sudo"), ("bash", "bash"), ("-c", "-c"),
         ('"sudo', "sudo"), ("rm", "rm"), ('-rf"', "-rf")])
    assert "rm" in position


# ── 一处判据 ──────────────────────────────────────────────────────────


def test_is_delete_command_and_segment_judgment_are_the_same_function() -> None:
    """``is_delete_command`` 就是 ``_segment_has_delete`` 逐段 any。

    2026-11-26 之前一个按 ``simple_commands``（已剥噪音）看、一个按
    ``_segments``（未剥）看，同一句"什么叫删除"开了两个入口——
    正是项目"禁止两套判据"要消灭的东西。
    """
    from plugin.plugins.agent_deskmate.commands import (
        _segment_has_delete,
        _segments,
    )
    for command in ("rm -rf x", "print('rm')", 'bash -c "sudo rm -rf /"',
                    "echo hi", "git clean -fdx"):
        assert is_delete_command(command) is any(
            _segment_has_delete(segment) for segment in _segments(command))


def test_delete_targets_only_come_from_delete_segments() -> None:
    """``perm.get('allow',[])`` 里的 ``[]`` 不再被当成删除目标。

    它不再是删除段之后，这个 ``[]`` 根本进不了目标集合——
    于是也不会被 ``_target_hides_scope`` 读成"作用域被 glob 藏住了"。
    """
    command = "python -c \"print('allow 里带 rm 的:')\""
    assert delete_targets_of(command) == ()


def delete_targets_of(command: str) -> tuple[str, ...]:
    from plugin.plugins.agent_deskmate.commands import delete_targets
    return delete_targets(command)


# ── 诚实的边界：改动前就有的误报，本次刻意不碰 ────────────────────────


@pytest.mark.parametrize("command", [
    'mysql -e "drop table users"',
    'psql -c "drop table users"',
])
def test_a_paired_action_split_across_a_quote_still_matches(
        command: str) -> None:
    """成对动作跨引号也要认：``drop table`` 的 table 在引号里。

    这条钉的是**两处判据不许分叉**：``match_delete`` 按"剥噪音后相邻"
    认得出它；``_segment_has_delete`` 若改成按命令位 token 判"都在"，
    ``table`` 在引号内、命令位没有它，于是播报说删、档位说不删。
    真机后果是档位从 critical 掉到 warn（实测踩过）。
    """
    assert is_delete_command(command) is True, command
    assert _hit(command) is not None, command


@pytest.mark.parametrize("command", [
    # 引号串**以**删除词开头：开头那个 token 在命令位，照旧算删除。
    # 方向是"宁可多报"——漏判是隐形的，误报至少能在 summary 里看见。
    'git commit -m "rm the file"',
    # 帮助豁免只看 body[1:]，而 git clean 的段首是 clean 不是 --help。
    # 与本次改动无关（改动前后一致）。
    "git clean --help",
])
def test_pre_existing_false_positives_are_left_alone(command: str) -> None:
    """**这是登记，不是验收**：钉住它们现在长什么样。

    留着是为了下次改动能看出"这两条变了没有"——混在一起改，
    就分不清是谁起的作用。要修得单独开任务。
    """
    assert _hit(command) is not None, command
