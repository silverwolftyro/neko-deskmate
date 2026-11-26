"""rules.py L0 规则层单测（M0-lite-3a：删除类 + 失败类）。

样本来源：2026-11-21 deepseek-worker 产出的测试样本表 + 我对其设计的修正。
重点在「不误报」——误报会让用户把插件静音，比漏判更伤产品。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.events import (
    ATTRIBUTION_KNOWN,
    ATTRIBUTION_UNKNOWN,
    EVENT_ERROR,
    EVIDENCE_MCP_DELEGATION,
    EVIDENCE_SIDECHAIN,
    EVIDENCE_USER_DECLARED,
    UnifiedEvent,
    attribute,
)
from plugin.plugins.agent_deskmate.rules import (
    RULE_DELEGATION,
    RULE_DELETE,
    RULE_FAILURE,
    SEVERITY_CRITICAL,
    SEVERITY_NOTICE,
    SEVERITY_WARN,
    TRANSIENT_STATUS,
    Coalescer,
    RuleHit,
    _attribution_of,
    evaluate_delegation,
    evaluate_delete,
    evaluate_delete_segments,
    evaluate_failure,
    evaluate_transcript_error,
    match_delete,
    merge_multi_delete,
    severity_for_status,
)

# ── 删除类：命中 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("command,expected", [
    # 任务书点名的六个模式，各至少一条
    ("rm -rf /tmp/build", "rm"),
    ("rm -fv *.log", "rm"),
    ("rmdir /s /q C:\\old", "rmdir"),
    ("del C:\\temp\\a.txt", "del"),
    ("Remove-Item -Recurse -Force C:\\data", "remove-item"),
    ("REMOVE-ITEM -Recurse -Force .\\node_modules", "remove-item"),
    ("git clean -fdx", "git clean"),
    ("git clean -fd", "git clean"),
    ("DROP TABLE users;", "drop table"),
    ("Drop Table orders", "drop table"),
    # 链式与管道
    ("ls && rm -rf x", "rm"),
    ("cd a; rm b", "rm"),
    ("ls | tee log && del x.txt", "del"),
    # 路径形式（第一版按「命令位置」会漏掉这几条）
    ("/bin/rm -rf /var", "rm"),
    ("C:\\Windows\\System32\\del file.txt", "del"),
    # 包装器前缀（第一版同样会漏）
    ("sudo rm -rf /tmp/x", "rm"),
    ("xargs rm -f", "rm"),
    ('bash -c "rm -rf x"', "rm"),
    # 嵌入在更长命令里
    ("cd /app && npm run build && rm -rf dist && echo done", "rm"),
    # 已知的「过度匹配」：docker rm 删的是容器不是文件，但确实是删除类操作，
    # 按整 token 规则会命中。刻意保留——宁可多报一个看得懂的，也不错过一个。
    ("docker rm -f container", "rm"),
    ("git rm --cached secret.txt", "rm"),
])
def test_delete_matches(command: str, expected: str) -> None:
    assert match_delete(command) == expected


# ── 删除类：不误报 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "ls -la",
    "git status",
    "npm run clean",                      # clean 单独出现，且前面不是 git
    "git status --porcelain | grep clean",  # git 与 clean 不相邻
    "alarm=1; echo $alarm",               # 含 rm 字母但不是 token
    "rmlint --gui",                       # 名字以 rm 开头但不是 rm
    "delicate=true",                      # 含 del 字母但不是 token
    "remove-itemized-list.txt",           # 含 remove-item 字母但不是 token
    "",
    "   ",
    None,
])
def test_delete_does_not_match(command: str | None) -> None:
    assert match_delete(command) is None


def test_delete_help_flag_is_exempt() -> None:
    """看帮助不等于删文件。"""
    assert match_delete("del --help") is None
    assert match_delete("rm -h") is None
    # 但帮助豁免只在危险 token 是第一个 token 时生效，不能顺带赦免真正的删除
    assert match_delete("rm -rf x && del --help") == "rm"


def test_evaluate_delete_returns_hit() -> None:
    hit = evaluate_delete("rm -rf build/", agent="claude-code", session="s1", ts="2026-11-21T00:00:00+00:00")
    assert hit is not None
    assert hit.rule == RULE_DELETE
    # B2 之后严重度按**目标档位**定：`build/` 是可重建的构建产物目录，
    # 落"可恢复"档 → notice。不再是"出现删除 token 就一律 critical"。
    assert hit.severity == SEVERITY_NOTICE
    assert hit.matched == "rm"
    # 合并键里带命令指纹：同一条命令重复出现才合并，不同命令必须各自播
    assert hit.coalesce_key.startswith("delete|s1|claude-code|rm|")
    assert len(hit.coalesce_key.rsplit("|", 1)[-1]) == 12
    assert "build/" in hit.summary


def test_evaluate_delete_severity_follows_the_target_tier() -> None:
    """同一个 `rm`，目标不同严重度不同（B2 的核心变化）。"""
    def severity_of(command: str, cwd: str | None = r"D:\workspace\proj") -> str:
        source = _source(command=command, working_directory=cwd)
        hit = evaluate_delete(command, agent="a", session="s", source=source)
        assert hit is not None
        return hit.severity

    assert severity_of("rm -rf ~") == SEVERITY_CRITICAL                 # 家目录
    assert severity_of("rm -rf /etc") == SEVERITY_CRITICAL              # 系统目录
    assert severity_of("rm -rf D:/other/x.txt") == SEVERITY_CRITICAL    # 工作区外
    assert severity_of("rm -rf D:/workspace/proj/src") == SEVERITY_WARN  # 工作区内
    assert severity_of("rm -rf node_modules") == SEVERITY_NOTICE        # 可重建
    assert severity_of("rm -rf build") == SEVERITY_NOTICE
    # 拿不到工作目录 → **范围未知**档（B8），不是按最坏的猜。
    # 老实现这里返回 critical，实测后果是同一条命令两个结论：
    # 审批卡片（hook 带 cwd）说 notice，播报说 critical。
    assert severity_of("rm -rf x", None) == SEVERITY_WARN


def test_evaluate_delete_none_on_safe() -> None:
    assert evaluate_delete("ls -la") is None


# ── 失败类：严重度阶梯 ────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [
    (200, None),
    (204, None),
    (301, None),
    (400, SEVERITY_WARN),
    (401, SEVERITY_WARN),
    (403, SEVERITY_WARN),
    (404, SEVERITY_WARN),
    (429, SEVERITY_NOTICE),   # 限流会自己重试，不该按 critical 打扰人
    # 2026-11-26 第五条误报：502/503/504 与 429 同类，全是瞬态。
    # 实测连报 4 条【严重】都是网关侧 502，而它说的是"上游临时接不上"，
    # 不是老文档写的"全线不可用"。
    (502, SEVERITY_NOTICE),
    (503, SEVERITY_NOTICE),
    (504, SEVERITY_NOTICE),
    # **非瞬态的 5xx 照旧 critical**：这条是这次改动的安全边界。
    # 500 是服务端真的出了错，501 是端点不支持，都不是"稍后再来"。
    (500, SEVERITY_CRITICAL),
    (501, SEVERITY_CRITICAL),
    (505, SEVERITY_CRITICAL),
    (None, SEVERITY_WARN),    # 无状态码但已知失败
    ("abc", SEVERITY_WARN),   # 坏值不值得比真 5xx 更严重
])
def test_severity_for_status(status: object, expected: str | None) -> None:
    assert severity_for_status(status) == expected


def test_only_the_three_transient_codes_are_downgraded() -> None:
    """瞬态集合必须正好是这三个，一个不多一个不少。

    写宽了（把 500 也算瞬态）会把服务端真的崩了说成"稍后再来"，
    那是最危险的方向；写窄了只是多几条红字。
    """
    assert set(TRANSIENT_STATUS) == {502, 503, 504}


def test_failure_hit_for_server_error() -> None:
    """失败类的 summary / matched 形状。

    用 503 而不是 500：这条测的是**字段形状**（状态码与原文都进 summary），
    不是严重度——严重度由 ``test_severity_for_status`` 那张表管。
    2026-11-26 起 503 是 notice（瞬态），钉它会和那张表打架。
    """
    hit = evaluate_failure(status_code=503, error_message="service_unavailable: overloaded",
                           agent="claude", session="s1")
    assert hit is not None
    assert hit.rule == RULE_FAILURE
    assert hit.severity == SEVERITY_NOTICE
    assert "503" in hit.summary
    assert "overloaded" in hit.summary
    assert hit.matched == "503"


def test_a_real_server_error_is_still_critical() -> None:
    """500 不是瞬态：服务端真的出了错，照旧顶格。"""
    hit = evaluate_failure(status_code=500, error_message="internal error",
                           agent="claude", session="s1")
    assert hit is not None
    assert hit.severity == SEVERITY_CRITICAL
    assert "500" in hit.summary


def test_failure_none_on_success() -> None:
    assert evaluate_failure(status_code=200, error_message="") is None
    assert evaluate_failure(status_code=200, error_message="无所谓") is None


def test_failure_without_status_still_fails() -> None:
    """网络层错误没有状态码，但有 message，仍算失败。"""
    hit = evaluate_failure(status_code=None, error_message="connection reset by peer")
    assert hit is None or hit.severity == SEVERITY_WARN


def test_transcript_error_becomes_failure() -> None:
    event = UnifiedEvent(agent="claude-code", session="s1", event=EVENT_ERROR,
                         tool=None, summary="出错：api_error（429 rate_limited）",
                         ts="2026-11-21T00:00:00+00:00")
    hit = evaluate_transcript_error(event)
    assert hit is not None
    assert hit.rule == RULE_FAILURE
    assert hit.severity == SEVERITY_WARN
    assert hit.matched == "transcript-error"


def test_transcript_non_error_yields_nothing() -> None:
    event = UnifiedEvent(agent="claude-code", session="s1", event="tool_use",
                         tool="Bash", summary="Bash: ls", ts="2026-11-21T00:00:00+00:00")
    assert evaluate_transcript_error(event) is None


# ── 合并 ──────────────────────────────────────────────────────────────────

def test_coalescer_suppresses_within_window() -> None:
    coalescer = Coalescer(window_seconds=60.0)
    first = evaluate_delete("rm -rf a", session="s1")
    same_again = evaluate_delete("rm -rf a", session="s1")
    assert coalescer.offer(first, now=1000.0) is first
    assert coalescer.offer(same_again, now=1010.0) is None   # 同一条命令，窗口内合并
    assert coalescer.offer(same_again, now=1061.0) is same_again  # 出窗，放行


def test_distinct_deletions_are_not_swallowed() -> None:
    """同一会话里不同的删除必须各自播——按会话合并会让用户一分钟内漏掉多次删除。"""
    coalescer = Coalescer(window_seconds=60.0)
    a = evaluate_delete("rm -rf a", session="s1")
    b = evaluate_delete("rm -rf b", session="s1")
    assert coalescer.offer(a, now=1000.0) is a
    assert coalescer.offer(b, now=1001.0) is b


def test_coalescer_keys_by_session() -> None:
    """不同会话的同类事件不该互相吞掉。"""
    coalescer = Coalescer(window_seconds=60.0)
    a = evaluate_delete("rm -rf a", session="s1")
    b = evaluate_delete("rm -rf a", session="s2")
    assert coalescer.offer(a, now=1000.0) is a
    assert coalescer.offer(b, now=1001.0) is b


def test_failure_coalesces_by_session_and_status() -> None:
    """失败类按 会话+状态码 合并：网关每请求都记一行，不合并会刷屏。"""
    coalescer = Coalescer(window_seconds=60.0)
    first = evaluate_failure(status_code=503, error_message="overloaded", session="s1")
    second = evaluate_failure(status_code=503, error_message="overloaded again", session="s1")
    other_status = evaluate_failure(status_code=500, error_message="boom", session="s1")
    assert coalescer.offer(first, now=1000.0) is first
    assert coalescer.offer(second, now=1001.0) is None      # 同会话同状态，合并
    assert coalescer.offer(other_status, now=1002.0) is other_status  # 状态不同，放行


def test_evaluate_event_rules_for_error_only() -> None:
    coalescer = Coalescer(window_seconds=60.0)
    error_event = UnifiedEvent(agent="claude-code", session="s1", event=EVENT_ERROR,
                               tool=None, summary="出错：api_error", ts="t")
    ok_event = UnifiedEvent(agent="claude-code", session="s1", event="tool_use",
                            tool="Bash", summary="Bash: ls", ts="t")
    assert len(coalescer.evaluate_event(error_event, now=1000.0)) == 1
    assert coalescer.evaluate_event(ok_event, now=1001.0) == []


# ── 归因字段透传（2026-11-21 决策记录 §5） ──────────────────────────────────
# 规则层自己绝不猜发起者：requester 只能从源事件带过来，不传就是「未知」。


def _source(**overrides) -> UnifiedEvent:
    base = dict(agent="claude-code", session="800257f3-aaaa", event="tool_use",
                tool="Bash", command="rm -rf build/", ts="2026-11-21T00:00:00+00:00",
                executor="claude-code", working_directory=r"D:\workspace\N.E.K.O")
    base.update(overrides)
    return UnifiedEvent(**base)


def test_rule_hit_defaults_to_unknown_attribution() -> None:
    """不传 source（测试里直调纯函数）→ 一切按未知，规则层不自己填。"""
    hit = evaluate_delete("rm -rf build/", agent="claude-code", session="s1")
    assert hit.requester is None
    assert hit.attribution == ATTRIBUTION_UNKNOWN
    assert hit.chain_id == ""
    assert hit.delegated_to is None


def test_delete_carries_attribution_from_source() -> None:
    source = attribute(_source(), mine_roots=[r"D:\workspace"])
    hit = evaluate_delete("rm -rf build/", agent=source.agent, session=source.session,
                          ts=source.ts, source=source)
    assert hit.requester == "user"
    assert hit.requester_evidence == EVIDENCE_USER_DECLARED
    assert hit.attribution == ATTRIBUTION_KNOWN
    assert hit.executor == "claude-code"
    assert hit.working_directory == r"D:\workspace\N.E.K.O"
    assert hit.chain_id == source.session      # 今天恒等于 session
    assert hit.delegated_to is None


def test_failure_carries_attribution_from_source() -> None:
    source = attribute(_source(event="error", tool=None, command=None,
                               source_type="proxy_request_logs", status_code=503),
                       mine_roots=[r"D:\workspace"])
    hit = evaluate_failure(status_code=503, error_message="overloaded",
                           agent=source.agent, session=source.session,
                           ts=source.ts, source=source)
    assert hit.attribution == ATTRIBUTION_KNOWN
    assert hit.requester_evidence == EVIDENCE_USER_DECLARED
    assert hit.chain_id == source.session


def test_transcript_error_uses_event_itself_as_source() -> None:
    source = attribute(_source(event="error", tool=None, command=None, sidechain=True))
    hit = evaluate_transcript_error(source)
    assert hit.requester_evidence == EVIDENCE_SIDECHAIN
    assert hit.attribution == ATTRIBUTION_KNOWN
    assert hit.chain_id == source.session


def test_mcp_source_sets_delegated_to() -> None:
    """下派目标来自 mcp__ 的 server 名，且不猜它要干什么。"""
    source = attribute(_source(tool="mcp__deepseek-worker__deepseek_task"))
    hit = evaluate_delete("rm -rf build/", source=source)
    assert hit.delegated_to == "deepseek-worker"
    assert hit.requester_evidence == EVIDENCE_MCP_DELEGATION
    assert hit.attribution == ATTRIBUTION_KNOWN


def test_attribution_never_enters_the_coalesce_key() -> None:
    """归因是显示层的事，不进合并键（决策记录 §3）——
    否则同一条命令换个发起者就会变成两条播报。
    """
    a = evaluate_delete("rm -rf build/", session="s1")
    b = evaluate_delete("rm -rf build/", session="s1",
                        source=attribute(_source(), mine_roots=[r"D:\workspace"]))
    assert a.coalesce_key == b.coalesce_key


def test_attribution_of_none_is_empty() -> None:
    assert _attribution_of(None) == {}


# ── 下派观测（C / 归因第 4 步） ─────────────────────────────────────────────
# 只认转录工具名的 mcp__ 前缀；不读 args、不猜动作、不判危险。


def _mcp_event(**overrides) -> UnifiedEvent:
    base = dict(agent="claude-code", session="800257f3-aaaa", event="tool_use",
                tool="mcp__deepseek-worker__deepseek_task", command=None, ts="t",
                executor="claude-code", working_directory=r"D:\workspace\N.E.K.O")
    base.update(overrides)
    return UnifiedEvent(**base)


def test_delegation_fires_on_mcp_tool() -> None:
    hit = evaluate_delegation(_mcp_event())
    assert hit is not None
    assert hit.rule == RULE_DELEGATION
    assert hit.severity == SEVERITY_NOTICE      # 可见性，不是事故
    assert hit.summary == "通过 deepseek-worker 下派了一次任务"
    assert hit.matched == "deepseek-worker"
    assert hit.delegated_to == "deepseek-worker"


def test_delegation_ignores_non_mcp_tools() -> None:
    assert evaluate_delegation(_mcp_event(tool="Bash")) is None
    assert evaluate_delegation(_mcp_event(tool=None)) is None
    assert evaluate_delegation(_mcp_event(tool="Read")) is None


def test_delegation_ignores_non_tool_use_events() -> None:
    assert evaluate_delegation(_mcp_event(event="error")) is None
    assert evaluate_delegation(_mcp_event(event="turn_end")) is None


def test_delegation_never_reads_args() -> None:
    """判据只有工具名——不读 tool_input，所以参数里藏什么都不会影响判定。"""
    hit = evaluate_delegation(_mcp_event())
    assert "task" not in hit.summary
    assert hit.matched == "deepseek-worker"


def test_delegation_coalesces_by_session_and_server() -> None:
    """同一会话反复调同一个 worker，窗口内只报一条——否则批量任务刷屏。"""
    coalescer = Coalescer(window_seconds=60.0)
    first = evaluate_delegation(_mcp_event())
    same_server = evaluate_delegation(_mcp_event(ts="t2"))
    other_server = evaluate_delegation(_mcp_event(tool="mcp__hermit__run_task"))
    other_session = evaluate_delegation(_mcp_event(session="s2"))

    assert coalescer.offer(first, now=1000.0) is first
    assert coalescer.offer(same_server, now=1001.0) is None       # 同会话同 server → 合并
    assert coalescer.offer(other_server, now=1002.0) is other_server   # 换 server → 放行
    assert coalescer.offer(other_session, now=1003.0) is other_session # 换会话 → 放行


def test_delegation_coalesce_key_is_session_scoped() -> None:
    a = evaluate_delegation(_mcp_event(session="s1"))
    b = evaluate_delegation(_mcp_event(session="s2"))
    assert a.coalesce_key != b.coalesce_key
    assert a.coalesce_key == "delegation|s1|claude-code|deepseek-worker"


def test_delegation_carries_attribution() -> None:
    """MCP 事件的证据是 mcp-delegation（不是 user-declared）——
    哪怕 cwd 落在用户声明的目录里，这条事件的归因依据也是"它下派了"。
    """
    source = attribute(_mcp_event(), mine_roots=[r"D:\workspace"])
    hit = evaluate_delegation(source)
    assert hit.attribution == ATTRIBUTION_KNOWN
    assert hit.requester == "session:800257f3-aaaa"
    assert hit.requester_evidence == EVIDENCE_MCP_DELEGATION
    assert hit.chain_id == source.session


# ── 一条命令里的多个删除：合成一条，但危险的独立 ────────────────────────
#
# 2026-11-26 用户要求按"合成一条"走，同时要求：
# 「如果这个事情真的是有危险，或者被自动审查判断为不是遵从 prompt 的情况下，
#   他应该独立报告。」
#
# 所以合成只做一半：critical 一律不合成（调用方也不在送审前合成）。


def _seg(command: str, index: int) -> RuleHit:
    hits = evaluate_delete_segments(command, agent="a", session="s")
    assert len(hits) > index, f"{command!r} 只有 {len(hits)} 段"
    return hits[index]


def test_one_command_yields_one_hit_per_delete_segment() -> None:
    """逐段判——审查才能对每段各自表态。

    老实现一条命令一条命中，模型对整批点头就把三条一起压掉，
    包括里面那个 config.yaml。
    """
    assert len(evaluate_delete_segments("rm -rf a; rm -rf b; rm -rf c",
                                        agent="a", session="s")) == 3


def test_each_segment_gets_its_own_coalesce_key() -> None:
    """合并键必须带段序号。

    共用一个键的后果：合并器把后几段当重复吃掉——那正是要修的东西。
    """
    hits = evaluate_delete_segments("rm -rf a; rm -rf b", agent="a", session="s")
    assert hits[0].coalesce_key != hits[1].coalesce_key


def test_each_segment_gets_its_own_tier() -> None:
    """分段各自定档——``a`` 在工作目录内，``~/x`` 不是。"""
    hits = evaluate_delete_segments("rm -rf a; rm -rf ~/x", agent="a",
                                    session="s")
    assert [h.severity for h in hits] == ["warn", "critical"]


def test_the_whole_command_tier_is_still_worst_across_segments() -> None:
    """整条的档位没变——各段取最坏。

    这条钉的是"没有第二套档位"：闸门与卡片看整条，播报与审查看分段，
    两条路都对，且能互相还原。
    """
    hit = evaluate_delete("rm -rf a; rm -rf ~/x", agent="a", session="s")
    assert hit is not None and hit.severity == "critical"


def test_three_routine_deletes_merge_into_one_line() -> None:
    hits = evaluate_delete_segments("rm -rf a; rm -rf b; rm -rf c",
                                    agent="a", session="s")
    merged = merge_multi_delete(hits)
    assert len(merged) == 1
    assert "删了 3 处" in merged[0].summary
    assert merged[0].severity == "warn"


def test_a_critical_segment_is_never_merged() -> None:
    """**安全边界**：红的自己播，不折进一句话里。"""
    hits = evaluate_delete_segments("rm -rf a; rm -rf ~/x", agent="a",
                                    session="s")
    merged = merge_multi_delete(hits)
    assert len(merged) == 2
    assert [h.severity for h in merged] == ["warn", "critical"]


def test_merging_takes_the_worst_severity() -> None:
    """合成后取最坏的档——不会因为另外几个是 notice 就降。"""
    hits = evaluate_delete_segments("rm -rf build; rm -rf src", agent="a",
                                    session="s")
    assert {h.severity for h in hits} == {"notice", "warn"}
    merged = merge_multi_delete(hits)
    assert len(merged) == 1
    assert merged[0].severity == "warn"


def test_different_commands_are_not_merged_together() -> None:
    """只合同一条命令内的段。两条不同的命令是两件事。"""
    hits = (evaluate_delete_segments("rm -rf a", agent="a", session="s")
            + evaluate_delete_segments("rm -rf b", agent="a", session="s"))
    assert len(merge_multi_delete(hits)) == 2


def test_a_single_delete_passes_through_unchanged() -> None:
    hits = evaluate_delete_segments("rm -rf a", agent="a", session="s")
    assert merge_multi_delete(hits) == hits


def test_merge_leaves_non_delete_hits_alone() -> None:
    """失败类命中不是删除，原样返回——别把它算进"删了几处"。"""
    failure = evaluate_failure(status_code=400, agent="a", session="s")
    assert failure is not None
    assert merge_multi_delete([failure]) == [failure]


def test_merging_an_empty_list_is_a_noop() -> None:
    assert merge_multi_delete([]) == []
