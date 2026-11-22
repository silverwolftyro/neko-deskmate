"""B7：按信息价值裁剪卡片——没用的就不显示。

真机反馈的原话：主卡上常年挂着几行**没有判定价值**的内容，
「这些东西对 UI 的侵占过于厉害，把这些可有可无的去掉，才能凸显主体」。

原则两条：**空字段不占位**（说不清就不显示，不许写「未知」「无」）、
**无价值不显示**（「无法判断，需要你决定」只是承认"我不知道"，
却占着最显眼的位置——沉默比说"我不知道"更干净）。

预算：**动作（1–2 行）+ 至多两行辅助 + 三个按钮**，超出一律进「这是什么」。
辅助行按信息价值排：结论 > 「你看全了吗」> 范围 > 后果。
范围排在后果前面，是因为删除类的后果基本是同一句（"删了找不回来"），
而范围（"这个文件在它的工作目录之外"）才是这条请求独有的判据。
"""

from __future__ import annotations

import re

import pytest

from plugin.plugins.agent_deskmate.approval import (
    ADVICE_STATE_READY,
    MAX_CARD_AUX_LINES,
    build_card_html,
    explain_request,
)
from plugin.plugins.agent_deskmate.commands import (
    CONCLUSION_ALLOW,
    CONCLUSION_DENY,
    CONCLUSION_UNSURE,
    consequence_is_specific,
    scope_is_specific,
)

WD = r"D:\workspace\proj"
_TAG_RE = re.compile(r"<[^>]+>")


def _visible(html: str) -> str:
    return _TAG_RE.sub("", html)


def _text_lines(html: str) -> list[str]:
    """渲染后的**可见文本行**（去掉标签，去掉空行）。"""
    return [line for line in (text.strip() for text in _visible(html).splitlines())
            if line]


def _card(**overrides) -> str:
    base = dict(subject="claude-code", tool_summary="Bash: rm -f x.txt",
                session_id="800257f3", working_directory=WD,
                command="rm -f D:/other/x.txt")
    base.update(overrides)
    return build_card_html(**base)


# ── 1. 结论行 ────────────────────────────────────────────────────────────

def test_no_advice_means_no_conclusion_line_at_all() -> None:
    """任务书 B7 测试第一条：无模型建议 → 没有结论行、没有建议块。"""
    for state in ("disabled", "no_key", "unauthorized", "fallback"):
        html = _card(advice_state=state, conclusion=CONCLUSION_ALLOW)
        assert 'class="verdict"' not in html, state
        assert CONCLUSION_ALLOW not in _visible(html), state


def test_the_unsure_conclusion_is_silent_not_printed() -> None:
    """「无法判断，需要你决定」整行不显示。"""
    html = _card(advice_state=ADVICE_STATE_READY, conclusion=CONCLUSION_UNSURE)
    assert CONCLUSION_UNSURE not in _visible(html)
    # 沉默不等于没有卡片：动作行和按钮照旧
    assert "⚠️ 它要删除文件" in html
    assert 'data-neko-action="deskmate_approve"' in html


@pytest.mark.parametrize("conclusion", [CONCLUSION_ALLOW, CONCLUSION_DENY])
def test_a_definite_conclusion_is_bold_and_on_top(conclusion: str) -> None:
    """任务书 B7 测试第四条：有确定结论 → 结论行在最上方、加粗。"""
    html = _card(advice_state=ADVICE_STATE_READY, conclusion=conclusion)
    assert f'<p class="verdict"><strong>{conclusion}</strong></p>' in html
    assert html.index(f"<strong>{conclusion}</strong>") < html.index("⚠️ 它要")


# ── 2. 后果 / 范围：空字段不占位 ─────────────────────────────────────────

def test_an_unknown_command_shows_neither_consequence_nor_scope() -> None:
    """任务书 B7 测试第三条：目标解析失败 → 两行都不显示，也不显示"未知"。"""
    html = _card(command="npm run build")
    assert "未识别的命令" in html          # 动作行照旧——那是主体
    assert "后果：" not in html
    assert "范围：" not in html
    assert "看不出" not in html
    assert "未知" not in html


def test_a_non_delete_command_shows_neither() -> None:
    """任务书 B7 测试第二条：非删除类请求（如 git status）不显示后果/范围。"""
    html = _card(command="git status")
    assert "后果：" not in html
    assert "范围：" not in html
    # 但动作行仍然在——它说的是"未识别"，不是空白
    assert "⚠️" in html


def test_a_delete_still_shows_its_scope() -> None:
    """删除类的范围是**这条请求独有**的判据，必须显示。"""
    html = _card(command="rm -rf D:/other/x.txt")
    assert "范围：这个文件在它的工作目录之外" in html


def test_the_specificity_predicates() -> None:
    assert scope_is_specific("这个文件在它的工作目录之外") is True
    assert scope_is_specific("这是系统目录，改坏了系统起不来") is True
    assert scope_is_specific("看不出它会影响哪些文件") is False
    assert scope_is_specific("影响范围不止一个文件") is False
    assert scope_is_specific("") is False
    assert consequence_is_specific("不进回收站，删了就找不回来") is True
    assert consequence_is_specific("看不出这一步最坏会怎样") is False
    assert consequence_is_specific("") is False


# ── 3. 执行 / 上游 移出主卡 ─────────────────────────────────────────────

def test_the_main_card_has_no_actor_or_upstream_lines() -> None:
    html = _card(upstream=[{"name": "node.exe",
                            "path": r"C:\Program Files\DeepSeek Harness\node.exe"}])
    assert "执行：" not in html
    assert "上游" not in html
    assert "DeepSeek Harness" not in html
    assert "会话" not in html


def test_they_land_in_the_explain_card_instead() -> None:
    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash: rm -f x.txt", working_directory=WD,
        rule="delete", severity="critical", session_id="800257f3",
        subject="claude-code",
        upstream=[{"name": "node.exe",
                   "path": r"C:\Program Files\DeepSeek Harness\node.exe"}])
    assert explanation["actor"] == "执行：claude-code 会话 800257f3"
    assert "DeepSeek Harness" in explanation["upstream"]


# ── 4. 动作行始终显示 ───────────────────────────────────────────────────

def test_the_action_line_always_survives() -> None:
    """动作行是主体，任何裁剪都不许把它裁掉。"""
    for command in ("rm -rf ~", "git status", "npm run build",
                    "rm a && git reset --hard", ""):
        html = _card(command=command)
        assert "act" in html, command
        assert 'data-neko-action="deskmate_approve"' in html, command


# ── 5. 行数上限 ─────────────────────────────────────────────────────────

def test_the_card_stays_within_the_line_budget() -> None:
    """任务书 B7 测试第五条：动作 + 至多两行辅助 + 三个按钮。

    按**渲染后的可见文本行**计：标题、结论（有的话）、动作、辅助、三个按钮。
    """
    cases = [
        dict(command="rm -rf ~"),                                   # 删除 + 结论 + 范围
        dict(command="rm -rf ~", advice_state=ADVICE_STATE_READY,
             conclusion=CONCLUSION_DENY),
        dict(command="rm a && git reset --hard"),                    # 两条动作
        dict(command="git status"),                                 # 什么都说不清
        dict(command="npm run build"),
    ]
    for case in cases:
        html = _card(**case)
        lines = _text_lines(html)
        actions = html.count('class="act"')
        assert actions <= 2, (case, actions)
        assert len(lines) <= 1 + actions + MAX_CARD_AUX_LINES + 3, (
            case, lines)
        assert 'class="verdict"' in html or actions >= 1


def test_a_compound_command_gets_two_action_lines_and_one_auxiliary() -> None:
    """两条动作 + 结论 = 预算用满；后果让位（范围优先，它是独有判据）。"""
    html = _card(command="rm -rf a && git reset --hard",
                 advice_state=ADVICE_STATE_READY, conclusion=CONCLUSION_ALLOW)
    assert html.count('class="act"') == 2
    assert 'class="verdict"' in html
    assert "后果：" not in html
    assert "范围：" in html          # 范围优先于后果，它是这条请求独有的
    # 主卡只列两条；第三条没有丢——在「这是什么」里
    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash", working_directory=WD,
        rule="delete", severity="warn",
        command="rm -rf a && git reset --hard && echo hi")
    assert "all_actions" in explanation
    assert "echo hi" in explanation["all_actions"]


def test_the_over_limit_note_counts_as_an_auxiliary_line() -> None:
    """「命令共 N 字，只显示前 80 字」是安全件，必须占得到一个位置。"""
    long_command = "rm -rf " + " ".join(f"dir{i}" for i in range(60))
    html = _card(command=long_command)
    assert "命令共" in html
    assert "只显示前 80 字" in html


def test_an_over_limit_command_shows_no_conclusion() -> None:
    """超长命令不许出现"建议允许"（A 批 A1 第 5 条）。

    **渲染边界自己守**，不只靠调用方：``build_card_html`` 是纯函数，
    别的调用方漏了这道检查，一个没有依据的"建议允许"照样会印到卡上。
    """
    long_command = "rm -rf " + " ".join(f"dir{i}" for i in range(60))
    html = _card(command=long_command, advice_state=ADVICE_STATE_READY,
                 conclusion=CONCLUSION_ALLOW)
    assert 'class="verdict"' not in html
    assert "建议允许" not in _visible(html)
    assert "命令共" in html          # 但"你没看全"那句在
