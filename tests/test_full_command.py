"""A 批 A1：完整命令贯通。

要修的是一条**看不见的缝**：风险判定读的是完整命令，而卡片与模型原来
都只有截到 120 字的 ``tool_summary``。一条 150 字的命令在末尾挂
``&& rm -rf ~``，判定看得见、人和模型都看不见——那是在请用户盲签。

这组测试盯四件事：

1. ``ApprovalRequest`` 上有一份**不截断**的 ``full_command``；
2. 卡片 / 解释卡 / 模型 prompt 都从它取，**没有人再从 summary 反推**；
3. 命令超过卡片展示上限时，卡片上必须明说「尾部未显示」，
   而「这是什么」里必须有等宽的完整原文；
4. 超过上限时结论只能是「无法判断」——看不全就不给"建议允许"。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.approval import (
    build_card_html,
    build_explain_html,
    build_request,
    explain_request,
)
from plugin.plugins.agent_deskmate.commands import (
    COMMAND_DISPLAY_MAX,
    CONCLUSION_UNSURE,
    command_display_note,
)

# 一条"看得见的部分无害、尾部致命"的命令。这正是 A1 的事故形状。
TAIL_COMMAND = (
    "npm run build -- --workspace=packages/core --workspace=packages/ui "
    "--workspace=packages/agent --sourcemap --verbose && rm -rf ~"
)


def _request(command: str, tool_name: str = "Bash") -> object:
    return build_request({
        "session_id": "sess-1234567890",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "cwd": r"D:\workspace",
    })


# ── 1. 请求上带全文 ────────────────────────────────────────────────────────

def test_request_carries_the_untruncated_command() -> None:
    request = _request(TAIL_COMMAND)
    assert request.full_command == TAIL_COMMAND
    # 摘要仍然截断——它给列表/审计用，两个字段各司其职
    assert len(request.tool_summary) < len(TAIL_COMMAND)


def test_full_command_survives_newlines() -> None:
    """多行脚本要照着原样读才判断得准，压成一行会把结构读错。"""
    command = "if [ -d build ]; then\n  rm -rf build\nfi"
    assert _request(command).full_command == command


def test_full_command_is_empty_when_there_is_nothing_to_show() -> None:
    """取不到就空串，不猜、不补。"""
    assert _request("", tool_name="Bash").full_command == ""
    assert _request("", tool_name="Read").full_command == ""


def test_full_command_covers_non_bash_tools() -> None:
    """不只 Bash：Edit/Write 的目标路径同样可能长到看不全。"""
    long_path = r"D:\workspace" + "\\x" * 200
    request = _request(long_path, tool_name="Edit")
    assert request.full_command == long_path


# ── 2. 三个消费方都用全文 ──────────────────────────────────────────────────

def test_summary_and_full_command_read_the_same_field() -> None:
    """摘要与全文必须取自同一个字段。

    两份各自维护一份顺序表的话，摘要显示 A、全文取 B，
    卡片上就会出现"看得见的不完整、完整的不对应"。
    """
    from plugin.plugins.agent_deskmate import approval

    assert approval._primary_key({"command": "ls", "path": "D:/"}) == "command"
    assert approval._primary_key({"path": "D:/x"}) == "path"
    assert approval._primary_key({"command": "   "}) is None
    assert approval._primary_key("not a dict") is None


def test_command_of_prefers_the_full_command() -> None:
    """``_command_of`` 优先用全文，不再从截断的摘要反推。"""
    from plugin.plugins.agent_deskmate import _command_of

    request = _request(TAIL_COMMAND)
    assert _command_of(request) == TAIL_COMMAND


def test_command_of_still_works_without_the_new_field() -> None:
    """老构造路径（没有 full_command）按老办法剥一次前缀。

    回退分支必须留着：宁可少一截，不要切错。
    """
    from plugin.plugins.agent_deskmate import _command_of

    class _Old:
        tool_name = "Bash"
        tool_summary = "Bash: rm -rf build/"
        full_command = ""

    assert _command_of(_Old()) == "rm -rf build/"


# ── 3. 超过上限必须明说，全文进「这是什么」 ────────────────────────────────

def test_short_command_gets_no_truncation_note() -> None:
    assert command_display_note("rm -rf build/") == ""


def test_long_command_gets_a_truncation_note() -> None:
    note = command_display_note(TAIL_COMMAND)
    assert "尾部未显示" in note
    assert f"命令共 {len(TAIL_COMMAND)} 字" in note
    assert f"只显示前 {COMMAND_DISPLAY_MAX} 字" in note
    # 必须告诉用户去哪里看全文，否则这句话是句死路
    assert "这是什么" in note


def test_card_shows_the_truncation_note() -> None:
    html = build_card_html(
        subject="claude-code", tool_summary="Bash: …",
        session_id="sess-1234567890", working_directory=r"D:\workspace",
        command=TAIL_COMMAND, risk="notice",
    )
    assert "尾部未显示" in html


def test_short_command_card_has_no_note() -> None:
    """不超长就不该多这一行——卡片是瘦的，噪音每行都要付代价。"""
    html = build_card_html(
        subject="claude-code", tool_summary="Bash: rm -rf build/",
        session_id="s", working_directory=r"D:\workspace",
        command="rm -rf build/", risk="notice",
    )
    assert "尾部未显示" not in html


def test_note_appears_before_the_buttons() -> None:
    """提示必须在按钮之前：用户点"允许"之前就该知道自己没看全。"""
    html = build_card_html(
        subject="claude-code", tool_summary="Bash: …",
        session_id="s", working_directory=None,
        command=TAIL_COMMAND, risk="notice",
    )
    assert html.index("尾部未显示") < html.index("允许</button>")


def test_explain_card_carries_the_full_command_verbatim() -> None:
    """「这是什么」里给完整等宽文本（A1 第 4 条）。

    断言用转义后的形式：``&&`` 在 HTML 里必须写成 ``&amp;&amp;``，
    而"原文进卡片"说的正是转义后的等价文本，不是把 ``&`` 原样留下。
    """
    import html

    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash: …",
        working_directory=r"D:\workspace", rule="delete", severity="critical",
        full_command=TAIL_COMMAND,
    )
    rendered = build_explain_html(explanation)
    assert html.escape(TAIL_COMMAND, quote=True) in rendered
    assert 'class="cmd"' in rendered
    # 尾部那段要真的在——只送前半段等于没送
    assert "rm -rf ~" in rendered


def test_full_command_survives_every_verbosity() -> None:
    """brief 档也不能把它裁掉——裁了，「尾部未显示」就变成了死路。"""
    for verbosity in ("brief", "standard", "detailed"):
        explanation = explain_request(
            tool_name="Bash", tool_summary="Bash: …",
            working_directory=r"D:\workspace", rule="", severity="notice",
            full_command=TAIL_COMMAND, verbosity=verbosity,
        )
        assert explanation.get("full_command") == TAIL_COMMAND, verbosity


def test_full_command_is_escaped_in_the_explain_card() -> None:
    """命令是 Agent 产的，不能当可信 HTML 直接拼。"""
    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash: …",
        working_directory=None, rule="", severity="notice",
        full_command='echo "<script>alert(1)</script>" && ' + "x" * 200,
    )
    html = build_explain_html(explanation)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── 4. 超过上限时结论只能是「无法判断」 ────────────────────────────────────

def test_display_limit_is_a_real_number() -> None:
    """上限不能是 0 或负数——那会让每条命令都算"看不全"。"""
    assert COMMAND_DISPLAY_MAX >= 20


@pytest.mark.parametrize("command,over", [
    ("rm -rf build/", False),
    ("x" * COMMAND_DISPLAY_MAX, False),
    ("x" * (COMMAND_DISPLAY_MAX + 1), True),
    (TAIL_COMMAND, True),
])
def test_over_limit_is_measured_on_the_flattened_command(
        command: str, over: bool) -> None:
    """判据用压平后的长度：卡片上显示的也是压平后的文本。"""
    from plugin.plugins.agent_deskmate import (
        COMMAND_DISPLAY_MAX as LIMIT,
    )

    assert (len(" ".join(command.split())) > LIMIT) is over


def test_conclusion_is_forced_to_unsure_when_the_command_is_too_long() -> None:
    """看不全的时候不给"建议允许"（A1 第 5 条）。

    这条与 ``safe_conclusion`` 是两道独立的阀门：那边管"高风险"，
    这边管"看不全"。两个条件各自都能把结论压到第三态。
    """
    from plugin.plugins.agent_deskmate import safe_conclusion

    # 低风险 + 模型说 allow → 本来会是"建议允许"
    conclusion, conflict = safe_conclusion("allow", risk="notice",
                                           action_class="recoverable")
    assert conclusion != CONCLUSION_UNSURE
    assert conflict == ""
    # 但卡片侧的强制覆盖与风险无关，只看长度（见 _advice_for_card）
    assert CONCLUSION_UNSURE == "无法判断，需要你决定"
