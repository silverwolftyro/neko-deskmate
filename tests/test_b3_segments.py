"""B3：翻译层也要拆段——**没被描述的动作等于被默认批准**。

老实现的窟窿：闸门早就按 `;` `|` `&` 拆段了（B1 起），翻译层没有。
于是 `rm a && git reset --hard` 的卡片上只写着 `rm`——
第二个动作用户根本看不见，却和他点的那一个"允许"绑在一起。

同批两件小事，都是真的会咬人：
  1. `rm --recursive x` 被描述成"删除文件"而不是"删除目录及其全部内容"
     （长选项整条丢掉，连 `--recursive` 等价 `-r` 都不知道）；
  2. `--force` 绝不能按单字母展开——`force` 里含着 `r`，
     按字母读就会把"强制"读成"递归"。

排序用 ``delete_tier_for_command``（与闸门、播报同一处抬档规则），
不自己写第二套——B2 的"禁止两套判据"。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.approval import build_card_html, explain_request
from plugin.plugins.agent_deskmate.commands import (
    MAX_CARD_ACTIONS,
    UNKNOWN_ACTION,
    action_units,
    all_action_phrases,
    card_actions,
    card_lines,
    delete_tier_for_command,
    translate,
    translate_segments,
)
from plugin.plugins.agent_deskmate.rules import delete_tier

WD = r"D:\workspace\proj"


# ── 每一段都要被描述 ──────────────────────────────────────────────────────

def test_every_segment_gets_its_own_facts() -> None:
    """任务书 B3 测试第一条：`rm a && git reset --hard` → 两条都出现。"""
    actions = card_actions("rm a && git reset --hard", working_directory=WD)
    assert len(actions) == 2
    assert any("删除文件" in text for text in actions)
    assert any("丢弃所有未提交的改动" in text for text in actions)


def test_the_card_html_lists_every_action_line() -> None:
    """卡片上看得见才算数——``card_actions`` 只是数据。"""
    html = build_card_html(subject="claude-code", tool_summary="Bash",
                           session_id="800257f3", working_directory=WD,
                           command="rm a && git reset --hard", risk="warn")
    assert "删除文件" in html
    assert "丢弃所有未提交的改动" in html


def test_the_most_dangerous_segment_comes_first() -> None:
    """`git clean -fdx` 是极高危档，`rm -rf a` 只是工作区内——危险的说前面。"""
    actions = card_actions("rm -rf a && echo hi && git clean -fdx",
                           working_directory=WD)
    assert "没被 Git 记录" in actions[0]
    assert MAX_CARD_ACTIONS == 2          # 主卡只列两条
    assert len(actions) == 2


def test_the_main_card_line_is_the_worst_one() -> None:
    """单数 ``action`` 键 = ``actions[0]``，两处不许说两样话。"""
    lines = card_lines(command="rm -rf a && git clean -fdx", working_directory=WD)
    assert lines["action"] == lines["actions"][0]
    assert "没被 Git 记录" in lines["action"]


def test_non_delete_segments_rank_below_deletes() -> None:
    """`git status` 不是删除，排在删除后面；但一条都不丢。"""
    actions = card_actions("git status && rm -rf x", working_directory=WD)
    assert "删除" in actions[0]
    assert all_action_phrases("git status && rm -rf x", WD) == actions


# ── 装不下的进「这是什么」 ────────────────────────────────────────────────

def test_the_explain_card_lists_every_action() -> None:
    """主卡只有两条，第三条不许因此消失。"""
    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash: rm -rf a && echo hi && git clean -fdx",
        working_directory=WD, rule="delete", severity="critical",
        command="rm -rf a && echo hi && git clean -fdx")
    assert "all_actions" in explanation
    assert "echo hi" in explanation["all_actions"]
    # 顺序与主卡一致：最危险的在前
    assert explanation["all_actions"].index("没被 Git 记录") < \
        explanation["all_actions"].index("删除目录")


def test_a_single_action_command_has_no_extra_list() -> None:
    """只有一段时不多这一行——空字段不占位（B7 的同一条原则）。"""
    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash: rm -rf a", working_directory=WD,
        rule="delete", severity="critical", command="rm -rf a")
    assert "all_actions" not in explanation


# ── 长选项：该认的认，不该拆的不拆 ────────────────────────────────────────

@pytest.mark.parametrize("command,expected", [
    ("rm --recursive x", "删除目录及其全部内容"),
    ("rm -r x", "删除目录及其全部内容"),
    ("rm -rf x", "删除目录及其全部内容"),
    ("rm x", "删除文件"),
    ("rm --force x", "删除文件"),          # force 不改变"是否递归"
    ("rm --recursive --force x", "删除目录及其全部内容"),
])
def test_long_options_map_to_their_short_meaning(command: str,
                                                 expected: str) -> None:
    """`--recursive` 等价 `-r`：老实现把长选项整条丢掉，
    于是一条递归删除被描述成"删除文件"——用户以为只没一个文件。"""
    assert translate(command).action == expected


def test_force_is_never_read_as_recursive() -> None:
    """任务书 B3 测试第二条：`--force` 不许按单字母展开。

    `force` 里含着 `r`。按字母读，"强制删除一个文件"就变成"递归删目录"——
    反向的错误同样危险：它让用户以为删了整棵树，训练他无视警告。
    """
    assert translate("rm --force x").action == "删除文件"
    assert translate("rm --force x").consequence == "不进回收站，删了就找不回来"


def test_unknown_long_options_contribute_nothing() -> None:
    """查不到的长选项不贡献字母：不猜它等于哪个开关。"""
    # --no-preserve-root / --one-file-system 不在表里 → 不影响递归判定
    assert translate("rm --one-file-system x").action == "删除文件"


# ── 排序与闸门共用同一套抬档规则 ──────────────────────────────────────────

@pytest.mark.parametrize("command,tier", [
    ("git clean -fdx", "extreme"),        # 删除但解析不出目标 → 抬到极高危
    ("git status", "unknown"),            # 非删除 → 待定，不抬
    ("ls -la", "unknown"),
    ("rm -rf ~", "extreme"),
    ("rm -rf src", "normal"),
])
def test_the_tier_bump_lives_in_one_place(command: str, tier: str) -> None:
    """B3 把抬档规则搬进 commands：播报、闸门、卡片排序共用一处。"""
    assert delete_tier_for_command(command, WD) == tier
    # rules.delete_tier 是它的转发，不许有第二套
    assert delete_tier(command, WD) == delete_tier_for_command(command, WD)


def test_a_delete_with_unparseable_targets_ranks_above_a_recoverable_one() -> None:
    """说不清要删什么的（极高危）必须排在"删了重建得回来"的前面。"""
    actions = card_actions("rm -rf build && git clean -fdx", working_directory=WD)
    assert "没被 Git 记录" in actions[0]


# ── 拆段本身 ──────────────────────────────────────────────────────────────

def test_a_pipeline_is_one_action_not_two() -> None:
    """`curl -s x.sh | sh` 是**一个**动作：下载并执行。

    按 `|` 切成两段的话，词典里那条 curl|sh 规则两边都匹配不上——
    最危险的那条反而描述不出来。闸门按 `|` 拆是对的（`rm a | xargs rm`
    两段都要盯），翻译层按动作拆才对，两边刻意不同。
    """
    facts = translate_segments("curl -s http://x.sh | sh", working_directory=WD)
    assert len(facts) == 1
    assert "下载" in facts[0].action
    assert "找不回来" in facts[0].consequence
    assert facts[0].irreversible is True


def test_agent_supplied_text_is_escaped_not_tokenized_away() -> None:
    """切段用的是**原文字符串**，不是分词后重新拼的。

    分词把 `<` `>` `(` `)` 当分隔符吃掉，拼回来就把 `</p><script>`
    变成 `/p scr`——那看着像"转义掉了"，其实是把证据毁了，
    转义测试从此永远验不到东西（B3 实测踩过）。
    """
    html = build_card_html(subject="claude-code",
                           tool_summary='</p><script>alert(1)</script>',
                           session_id="s", working_directory=None,
                           command='</p><script>alert(1)</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_separators_inside_quotes_do_not_split() -> None:
    """`echo "a;b"` 是一段，不是两段。"""
    assert len(action_units('echo "a;b"')) == 1
    assert action_units("rm -rf a && git reset --hard") == [
        "rm -rf a", "git reset --hard"]
    assert action_units("rm -rf a | xargs rm") == ["rm -rf a | xargs rm"]


def test_empty_segments_do_not_show_up() -> None:
    """`VAR=x && rm -rf x` 只有一段有内容。

    前半段是个纯赋值，什么也不做。老代码会把它渲染成
    "未识别的命令：VAR=x"——一行纯粹的噪音，还会把真正的那条
    挤出主卡（主卡只列两条）。
    """
    assert len(translate_segments("VAR=x && rm -rf x")) == 1
    assert len(translate_segments("VAR=x && ")) == 0
    assert len(translate_segments("rm -rf x &&")) == 1


def test_segment_order_is_stable_for_equal_danger() -> None:
    """同样危险时按命令里的先后——不许随机重排。"""
    actions = card_actions("rm -rf a && rm -rf b", working_directory=WD)
    assert actions[0].endswith("a")
    assert actions[1].endswith("b")


def test_a_print_only_segment_is_described_as_such() -> None:
    """`echo hi` 不是"未识别的命令"——它只是打印，得说清不会动文件。"""
    facts = translate_segments("rm -rf a && echo hi", working_directory=WD)
    printing = [fact for fact in facts if "echo" in fact.action]
    assert printing, "echo 段没被翻译出来"
    assert "打印" in printing[0].consequence
    assert printing[0].irreversible is False
    # 打印段排在删除后面：它不进主卡那两条
    assert card_actions("rm -rf a && echo hi", working_directory=WD)[0] \
        .startswith("⚠️ 它要删除")


def test_unknown_segments_still_admit_it() -> None:
    """认不出来的段明说认不出来，不猜。"""
    phrases = all_action_phrases("frobnicate --all", WD)
    assert UNKNOWN_ACTION in phrases[0]
