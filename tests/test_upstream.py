"""上游进程链归因（规格 Q1 / §11）。

**为什么需要它**：卡片原来只写「claude-code 请求执行」，而真实链路常常是
DSH 的 agent → 起 pwsh → 起 ``claude -p`` → 它调 Bash。用户看到卡片时
问不出"这是我自己开的吗"——那个发起 claude 的程序在卡片上不存在。

**三条硬规则**（规格 §11「只展示，不参与任何决策」）：

1. **爬不到就说不知道**：空链、字段缺失、形状不对一律「上游未知」，
   绝不拿当前进程名凑一个。猜错归因比没有归因危险得多。
2. **友好名不许误报**：``desk`` 不能命中 ``Desktop``。误报会让人以为
   某个程序发起了这次请求，而它没有。
3. **归因不进门控**：这条链上一个字节都不参与放行判断。测试盯死
   ``auto_decide_gate`` 根本不收上游信息。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from plugin.plugins.agent_deskmate import hook_relay
from plugin.plugins.agent_deskmate.approval import (
    UPSTREAM_FIELD,
    UPSTREAM_MAX,
    UPSTREAM_UNKNOWN,
    build_card_html,
    build_request,
    describe_upstream,
    explain_request,
)

DSH_CHAIN = [
    {"name": "pwsh.exe",
     "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\pwsh.exe"},
    {"name": "node.exe",
     "path": r"C:\Users\alpha\AppData\Local\Programs\DeepSeek Harness\node.exe"},
]
# B5 之后纯 shell 层在展示时被滤掉，所以这条链显示出来只剩 node 那个节点。
DSH_RENDERED = "node.exe（DeepSeek Harness）"


def test_the_two_sides_agree_on_the_field_name() -> None:
    """中继写的键与插件读的键必须同名。

    两边各写一份字符串是因为中继不能 import 插件包（hook 触发时 sys.path
    里没有它）。改名只改一边的后果是"爬到了却不显示"，无声无息。
    """
    assert hook_relay.UPSTREAM_FIELD == UPSTREAM_FIELD  # noqa: SLF001
    assert UPSTREAM_FIELD.startswith("deskmate_"), (
        "不带前缀的话，Claude Code 将来加个同名字段就会被我们当成进程链")


def test_the_wire_format_agrees_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """中继发出去的字节 → 插件读出来的对象，链必须原样到达。

    这是两边唯一的接触点：序列化在这里，清洗在那边。只测一边正常
    说明不了对接没问题（第一版的键名就差点只在一边改）。
    """
    monkeypatch.setattr(hook_relay, "_upstream", lambda: DSH_CHAIN)
    on_the_wire = hook_relay._with_upstream(_payload())  # noqa: SLF001

    request = build_request(json.loads(on_the_wire.decode("utf-8")))

    assert request.upstream == DSH_CHAIN
    assert describe_upstream(request.upstream) == DSH_RENDERED


# ── 展示：链 → 一行人话 ───────────────────────────────────────────────────

def test_chain_renders_with_arrows_and_friendly_names() -> None:
    """规格 Q1 的原话形状，B5 起纯 shell 层被滤掉。

    原来这里期望 ``pwsh.exe ← node.exe（DeepSeek Harness）``。
    真机反馈：用户要找的是 DeepSeek Harness，中间那层 pwsh 只是
    "命令怎么被递到的"，不是"谁发起的"——所以展示时滤掉。
    """
    assert describe_upstream(DSH_CHAIN) == DSH_RENDERED


def test_unknown_when_there_is_no_chain() -> None:
    """爬不到 → 「上游未知」。一条都不许编。"""
    for empty in ([], (), None, "nonsense", 42, {}, [None, 3, "x"]):
        assert describe_upstream(empty) == UPSTREAM_UNKNOWN, empty


def test_entries_without_a_name_are_dropped() -> None:
    """没有 exe 名的条目等于没有信息，丢掉而不是显示一个空白。"""
    assert describe_upstream([{"name": "", "path": "C:/x"},
                              {"path": "C:/y"}, "str",
                              {"name": "node.exe", "path": "C:/node.exe"}]
                             ) == "node.exe"


def test_bad_entries_do_not_push_good_ones_out() -> None:
    """上限是按**可用条目**算的，不是按收到的条数。

    先截断再过滤的话，三条坏条目就能把后面一条好条目挤出窗外，
    结果是"明明知道却显示上游未知"。
    """
    assert describe_upstream([{"name": ""}, {"name": ""}, {"name": ""},
                              {"name": "node.exe", "path": "C:/n"}]) == "node.exe"


def test_chain_is_capped() -> None:
    """最多三条。再往上是终端/资源管理器，对"是我开的吗"没有增量。"""
    long = [{"name": f"p{i}.exe", "path": f"C:/p{i}.exe"} for i in range(9)]
    assert describe_upstream(long).count("←") == UPSTREAM_MAX - 1


def test_friendly_name_does_not_match_a_desktop_path() -> None:
    """``desk`` 不许命中 ``Desktop``——那是误报，比没有友好名更糟。"""
    assert describe_upstream(
        [{"name": "foo.exe", "path": r"C:\Users\alpha\Desktop\foo.exe"}]
    ) == "foo.exe"


@pytest.mark.parametrize("path,friendly", [
    (r"C:\Program Files\DeepSeek\app.exe", "DeepSeek Harness"),
    (r"D:\tools\dsh\dsh.exe", "DeepSeek Harness"),
    (r"D:\tools\mcode\mcode.exe", "mcode"),
    (r"C:\Program Files\nodejs\node.exe", ""),
    (r"C:\Windows\System32\cmd.exe", ""),
])
def test_friendly_names_come_from_the_executable_path(path: str,
                                                      friendly: str) -> None:
    """友好名只从**可执行路径**里认，认不出就显示 exe 名本身。

    这一点要在测试里说清楚：如果真实机器上那个发起者是系统自带的
    ``node.exe``，我们只能显示 ``node.exe``——我们有的证据只有路径，
    不猜（见 report 里的说明）。
    """
    rendered = describe_upstream([{"name": "app.exe", "path": path}])
    assert rendered == ("app.exe" if not friendly
                        else f"app.exe（{friendly}）")


# ── 请求：upstream 字段的清洗 ─────────────────────────────────────────────

def test_request_picks_up_the_upstream_field() -> None:
    request = build_request({"tool_name": "Bash", "tool_input": {"command": "ls"},
                             UPSTREAM_FIELD: DSH_CHAIN})
    assert request.upstream == DSH_CHAIN


def test_request_survives_a_missing_upstream_field() -> None:
    """直接打 /hook/permission 的请求没有这个字段 → 空链 → 上游未知。"""
    request = build_request({"tool_name": "Bash"})
    assert request.upstream == []
    assert describe_upstream(request.upstream) == UPSTREAM_UNKNOWN


@pytest.mark.parametrize("raw", ["nonsense", 42, None, {"a": 1},
                                 [1, 2, "x"], [{"name": None}, {"name": 7}]])
def test_request_drops_a_malformed_upstream(raw: Any) -> None:
    """中继是独立脚本，它读到的内容不能当可信——形状不对就当没有。"""
    assert build_request({UPSTREAM_FIELD: raw}).upstream == []


def test_request_caps_and_clips_the_upstream() -> None:
    raw = ([{"name": f"p{i}.exe", "path": "C:/" + "x" * 400} for i in range(9)]
           + [{"name": "extra.exe", "path": "C:/e"}])
    cleaned = build_request({UPSTREAM_FIELD: raw}).upstream
    assert len(cleaned) == UPSTREAM_MAX
    assert all(len(entry["path"]) <= 260 for entry in cleaned)


# ── 卡片：上游是其中一行小字 ──────────────────────────────────────────────

def test_the_main_card_no_longer_carries_the_upstream_line() -> None:
    """B7 第 3 条：上游移出主卡，进「这是什么」。

    原来它压在卡片最下面，把动作行挤得看不见。用户要找的是"它要干什么"，
    不是"这条进程链"——后者点「这是什么」照样看得到。
    """
    html = build_card_html(subject="claude-code", tool_summary="Bash: ls",
                           session_id="800257f3", working_directory=None,
                           upstream=DSH_CHAIN)
    assert "上游" not in html
    assert DSH_RENDERED not in html
    # 但「这是什么」里有
    explanation = explain_request(
        tool_name="Bash", tool_summary="Bash: ls", working_directory=None,
        rule="", severity="notice", upstream=DSH_CHAIN)
    assert f"上游：{DSH_RENDERED}" in explanation["upstream"]


def test_card_escapes_an_upstream_supplied_path() -> None:
    """进程路径来自外部，拼进 HTML 前必须转义。"""
    html = build_card_html(subject="c", tool_summary="Bash: ls", session_id="s",
                           working_directory=None,
                           upstream=[{"name": "<script>x</script>",
                                      "path": "C:/x"}])
    assert "<script>" not in html


# ── 中继：把链塞进请求体 ─────────────────────────────────────────────────

def _payload(**extra: Any) -> bytes:
    body: dict[str, Any] = {"tool_name": "Bash", "session_id": "s1"}
    body.update(extra)
    return json.dumps(body).encode("utf-8")


def test_relay_adds_the_upstream_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook_relay, "_upstream", lambda: DSH_CHAIN)
    sent = json.loads(hook_relay._with_upstream(_payload()))  # noqa: SLF001
    assert sent[UPSTREAM_FIELD] == DSH_CHAIN
    # 原字段一个都不少
    assert sent["tool_name"] == "Bash" and sent["session_id"] == "s1"


def test_relay_leaves_the_body_alone_without_a_chain(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """爬不到就原样转发——不塞空字段，不改一个字节。"""
    monkeypatch.setattr(hook_relay, "_upstream", lambda: [])
    body = _payload()
    assert hook_relay._with_upstream(body) == body  # noqa: SLF001


@pytest.mark.parametrize("body", [b"", b"not json", b"[1,2]", b'"str"',
                                  b"null"])
def test_relay_survives_a_body_it_cannot_parse(body: bytes,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """为了一个展示字段把整个审批请求搞坏，是本末倒置。"""
    monkeypatch.setattr(hook_relay, "_upstream", lambda: DSH_CHAIN)
    assert hook_relay._with_upstream(body) == body  # noqa: SLF001


# ── §11：归因只展示，不参与任何决策 ──────────────────────────────────────

def test_attribution_never_reaches_the_gate() -> None:
    """门控的签名里根本没有上游这一项——想掺和进来都插不进去。"""
    import inspect

    from plugin.plugins.agent_deskmate.approval import auto_decide_gate

    params = set(inspect.signature(auto_decide_gate).parameters)
    assert not {"upstream", "chain", "parent"} & params, (
        f"门控收了归因信息：{sorted(params)}")


def test_the_real_walk_returns_a_list() -> None:
    """真机上跑一次进程树：返回值必须是 list，且每项有 name/path。

    不断言具体内容——测试机上的进程链每次都不一样。断言的是**形状**：
    这条链最终要进 JSON，形状错了比空链更麻烦。
    """
    chain = hook_relay._upstream()  # noqa: SLF001
    assert isinstance(chain, list)
    for entry in chain:
        assert set(entry) == {"name", "path"}
        assert isinstance(entry["name"], str)
        assert isinstance(entry["path"], str)


def test_the_walk_reports_the_real_parent_first() -> None:
    """第一条必须是我们真正的父进程，不能把自己算进去。

    第一版犯过这个错：拿 ``table[pid]`` 里存的**自己**的名字当父进程名，
    结果报出"python.exe 的父亲是 python.exe"。所以这里拿进程表对一遍。
    """
    import os

    table = hook_relay._process_table()  # noqa: SLF001
    parent_pid = table.get(os.getpid(), (0, ""))[0]
    if not parent_pid:
        pytest.skip("这台机器上拿不到父进程")
    expected = table.get(parent_pid, ("", ""))[1]
    assert hook_relay._upstream()[0]["name"] == expected  # noqa: SLF001


def test_relay_script_is_the_one_the_host_runs() -> None:
    """这条链的逻辑必须活在**被 hook 指向的那份文件**里。

    2026-11-22 真机事故：宿主清空了安装副本，hook 指向一个死链接。
    所以 relay 的功能只能靠源码这一份，装配时它会被复制到数据目录。
    """
    assert Path(hook_relay.__file__).name == "hook_relay.py"
