"""broadcast.py 播报层单测（M0-lite-4）。

核心是红线 1 与任务书要求：
  - source 必须显式、ai_behavior 必须 blind；
  - ``submitted=False`` 是正常分支，不抛异常；
  - 永不 await push_message（播报器是同步函数，测试里用同步桩就能证明）。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.broadcast import (
    NOTICE_MERGE_SECONDS,
    RESPOND_COOLDOWN,
    Broadcaster,
    NoticeMerger,
    broadcast_notice,
    coalesce_same_workspace,
    for_log,
    priority_for,
    read_instruction,
    redact,
    render_text,
    speak,
)
from plugin.plugins.agent_deskmate.events import UnifiedEvent, attribute
from plugin.plugins.agent_deskmate.progress import Light, Role
from plugin.plugins.agent_deskmate.rules import (
    SEVERITY_CRITICAL,
    SEVERITY_NOTICE,
    SEVERITY_WARN,
    Coalescer,
    RuleHit,
    _attribution_of,
    evaluate_delegation,
    evaluate_delete,
    evaluate_failure,
)
from plugin.plugins.agent_deskmate.tracker import Notice

SOURCE = "agent_deskmate"


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        # 必须真的做 {} 插值：只存模板的话，"日志里不准出现密钥"这类断言
        # 验的其实是模板字符串，等于没验。
        try:
            rendered = str(message).format(*args) if args else str(message)
        except (IndexError, KeyError, ValueError):
            rendered = f"{message} | args={args!r}"
        self.lines.append((level, rendered))

    def info(self, message: str, *args: object) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log("warning", message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log("error", message, *args)


class RecordingPush:
    """同步记录桩。若被测代码 await 它，这里会直接炸——正好守住"永不 await"。"""

    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"submitted": True}
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.result


def _delete_hit(command: str = "rm -rf D:/other/x.txt"):
    """删除类命中的样本。

    **刻意用工作区外的路径 + 显式给一个工作目录**：B2 之后删除按目标分档，
    `rm -rf build/` 落在"可恢复"档、播报前缀是【提示】；而 B8 之后
    "没有工作目录"落「范围未知」档、前缀是【警告】。
    下面这些用例测的是【严重】那条路本身，所以两头都得钉住：
    目标在工作区外、且我们确实知道工作目录。可恢复档另见
    ``test_recoverable_target_downgrades_the_broadcast``。
    """
    source = UnifiedEvent(agent="claude-code", session="s1", event="tool_use",
                          tool="Bash", command=command, ts="t",
                          executor="claude-code",
                          working_directory=r"D:\workspace\proj")
    return evaluate_delete(command, agent="claude-code", session="s1", ts="t",
                           source=source)


# ── 严重度 → priority ─────────────────────────────────────────────────────

def test_priority_ladder() -> None:
    assert priority_for(SEVERITY_CRITICAL) == 9
    assert priority_for(SEVERITY_WARN) == 6
    assert priority_for(SEVERITY_NOTICE) == 4
    assert priority_for("info") == 2
    assert priority_for("没见过的严重度") == 5  # 不猜，给中间值


def test_render_text_has_severity_and_agent() -> None:
    text = render_text(_delete_hit())
    assert text.startswith("【严重】")
    assert "claude-code" in text
    assert "rm -rf D:/other/x.txt" in text
    # 不用 emoji：这是状态播报不是装饰
    assert not any(ord(ch) > 0x1F000 for ch in text)


# ── 红线 1：push_message 的调用形状 ───────────────────────────────────────

def test_push_carries_explicit_source_and_blind() -> None:
    push = RecordingPush()
    Broadcaster(push, FakeLogger(), source=SOURCE).broadcast(_delete_hit())
    assert len(push.calls) == 1
    call = push.calls[0]
    assert call["source"] == SOURCE          # 必须显式，不能是默认空串
    assert call["ai_behavior"] == "blind"    # 纯播报，不让角色开口
    assert call["visibility"] == ["chat"]
    assert call["priority"] == 9             # critical
    assert call["parts"][0]["type"] == "text"
    assert "rm -rf D:/other/x.txt" in call["parts"][0]["text"]


def test_broadcaster_is_synchronous() -> None:
    """播报器是同步函数：push 被同步调用，不产生协程。"""
    import inspect

    assert not inspect.iscoroutinefunction(Broadcaster.broadcast)


# ── submitted=False 是正常分支 ────────────────────────────────────────────

@pytest.mark.parametrize("reason,expect_warning", [
    ("backpressure", False),
    ("transport_error", False),
    ("transport_unavailable", False),
    ("payload_too_large", True),
])
def test_rejection_is_a_normal_branch(reason: str, expect_warning: bool) -> None:
    """四种拒绝原因都不抛异常；只有永久性问题记 warning，瞬时问题记 info。"""
    push = RecordingPush({"ok": False, "submitted": False, "reason": reason})
    logger = FakeLogger()
    broadcaster = Broadcaster(push, logger, source=SOURCE)

    outcome = broadcaster.broadcast(_delete_hit())

    assert outcome == reason           # 原样返回原因，便于调用方判断
    assert broadcaster.rejected == 1
    assert broadcaster.sent == 0
    levels = [level for level, _ in logger.lines]
    assert ("warning" in levels) is expect_warning


def test_submitted_true_counts_as_sent() -> None:
    push = RecordingPush({"submitted": True})
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "sent"
    assert broadcaster.sent == 1
    assert broadcaster.rejected == 0


def test_push_exception_does_not_propagate() -> None:
    """播报抛异常绝不能炸掉观测循环。"""

    def exploding(**_: object) -> dict:
        raise RuntimeError("transport gone")

    broadcaster = Broadcaster(exploding, FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "exception"
    assert broadcaster.rejected == 1


def test_unexpected_result_type() -> None:
    broadcaster = Broadcaster(RecordingPush("不是dict"), FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "unexpected"


def test_missing_reason_field_is_unknown() -> None:
    push = RecordingPush({"submitted": False})
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE)
    assert broadcaster.broadcast(_delete_hit()) == "unknown"


# ── 合并 ──────────────────────────────────────────────────────────────────

def test_coalesced_hit_never_reaches_push() -> None:
    push = RecordingPush()
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE,
                              coalescer=Coalescer(window_seconds=60.0))
    # 同一条命令重复 → 第二条被合并，推不到 SDK
    assert broadcaster.broadcast(_delete_hit("rm -rf a")) == "sent"
    assert broadcaster.broadcast(_delete_hit("rm -rf a")) == "coalesced"
    assert len(push.calls) == 1
    assert broadcaster.coalesced == 1


def test_distinct_deletions_each_reach_push() -> None:
    """不同命令必须各自播——否则用户一分钟内漏掉多次删除。"""
    push = RecordingPush()
    broadcaster = Broadcaster(push, FakeLogger(), source=SOURCE,
                              coalescer=Coalescer(window_seconds=60.0))
    assert broadcaster.broadcast(_delete_hit("rm -rf a")) == "sent"
    assert broadcaster.broadcast(_delete_hit("rm -rf b")) == "sent"
    assert len(push.calls) == 2
    assert broadcaster.coalesced == 0


# ── 规则 → 播报 的接线（M0-lite-4 把 3a 的规则接到 SDK 上） ────────────────

def test_failure_severity_reaches_priority() -> None:
    """失败类的严重度必须真的走到 priority，否则用户在聊天窗里看不出严重程度。

    2026-11-26：502/503/504 降成 notice（瞬态，Agent 自己会重试），
    所以这里拆成两半——**非瞬态 5xx 照旧顶格，瞬态的不再顶格**。
    老版本这条用 503 钉 priority 9，那正是第五条误报的来源。
    """
    push = RecordingPush()
    hit = evaluate_failure(status_code=500, error_message="internal",
                           agent="claude", session="s1")
    Broadcaster(push, FakeLogger(), source=SOURCE).broadcast(hit)
    assert push.calls[0]["priority"] == 9
    assert "500" in push.calls[0]["parts"][0]["text"]

    push2 = RecordingPush()
    hit2 = evaluate_failure(status_code=503, error_message="overloaded",
                            agent="claude", session="s1")
    Broadcaster(push2, FakeLogger(), source=SOURCE).broadcast(hit2)
    assert push2.calls[0]["priority"] == 4        # notice，不再是 9
    assert "503" in push2.calls[0]["parts"][0]["text"]

    push3 = RecordingPush()
    hit3 = evaluate_failure(status_code=429, error_message="rate limited",
                            agent="claude", session="s1")
    Broadcaster(push3, FakeLogger(), source=SOURCE).broadcast(hit3)
    assert push3.calls[0]["priority"] == 4        # notice，不按 critical 打扰人
    assert push3.calls[0]["ai_behavior"] == "blind"


# ── 归因文案：四种模版（2026-11-21 决策记录 §1/§2） ─────────────────────────
# 核心原则：主语位只放「可证的请求源」；执行者降级到括号里；拿不准就写未知。
# 用户会把主语读成「发起者」——主语位放执行者，就等于把锅扣给手。
# 真实事故：DSH spawn 的 claude -p 删了文件，播报说「claude-code 删除」，
# 用户第一反应是"我什么时候让 Claude 删东西了？"

CWD = r"C:\Users\someone\AppData\Local\Temp"


def _hit(**overrides):
    base = dict(rule="delete", severity=SEVERITY_CRITICAL,
                summary="删除文件：rm -f deskmate-probe.docx",
                matched="rm", agent="claude-code", session="800257f3-aaaa-bbbb",
                ts="t", executor="claude-code", working_directory=CWD)
    base.update(overrides)
    return RuleHit(**base)


def _source(**overrides) -> UnifiedEvent:
    base = dict(agent="claude-code", session="800257f3-aaaa-bbbb", event="tool_use",
                tool="Bash", command="rm -rf ~", ts="t", executor="claude-code",
                working_directory=CWD)
    base.update(overrides)
    return UnifiedEvent(**base)


# 1) 单 Agent：用户声明过这个目录 → 主语是"你自己开的"

def test_template_single_agent_user_declared() -> None:
    source = attribute(_source(), mine_roots=[CWD])
    hit = evaluate_delete("rm -rf ~/deskmate-probe.docx", agent=source.agent,
                          session=source.session, ts=source.ts, source=source)
    text = render_text(hit)
    assert text.startswith("【严重】你自己开的 claude-code 删除文件：")
    assert "rm -rf ~/deskmate-probe.docx" in text
    # 定位三件套：会话短 id + cwd，让用户 3 秒内判断"这是我开的吗"
    assert "会话 800257f3" in text
    assert CWD in text


# 2) 父子代理

def test_template_subagent() -> None:
    source = attribute(_source(sidechain=True))
    hit = evaluate_delete("rm -rf ~/deskmate-probe.docx", source=source)
    text = render_text(hit)
    assert "（经子代理）" in text
    assert text.startswith("【严重】claude-code（经子代理）删除文件：")
    # 主语是父会话，执行者降级到括号里
    assert "你自己开的" not in text


# 3) 贵脑 + 廉价手

def test_template_delegated_to_worker() -> None:
    source = attribute(_source(tool="mcp__deepseek-worker__deepseek_task", command=None))
    hit = RuleHit(rule="delete", severity=SEVERITY_CRITICAL,
                  summary="删除文件：rm -f x", matched="rm",
                  agent="claude-code", session=source.session, ts="t",
                  **_attribution_of(source))
    text = render_text(hit)
    # 主语是大脑（用户对话的对象），worker 降级成"手"
    assert "让 deepseek-worker 执行" in text
    assert text.startswith("【严重】claude-code 让 deepseek-worker 执行删除文件：")
    # 绝不把 worker 说成主语——那正是用户那句"我让 DeepSeek 执行任务了？"
    assert not text.startswith("【严重】deepseek-worker")


# 4) 未知发起者（critical 的默认）

def test_template_unknown_requester_is_the_critical_default() -> None:
    hit = _hit()   # 不传任何归因 → unknown
    assert hit.attribution == "unknown"
    text = render_text(hit)
    assert "发起者未知" in text
    # 陈述句 + 线索，不是免责声明腔
    assert "无法确定" not in text and "请自行核实" not in text
    # 第二行给出执行者 + 会话 + cwd，用户自己就能补上最后一跳
    assert text.splitlines()[1] == f"由 claude-code 会话 800257f3 执行 @ {CWD}"


def test_unknown_template_matches_the_real_incident() -> None:
    """锁住真实事故的期望输出：DSH spawn 的 claude -p 在 %TEMP% 删文件。

    这条播报必须让用户 3 秒内明白"我没直接让 Claude 干这件事"，
    同时给足线索让他自己想起来"哦对，是我让 DSH 清理临时文件"。
    """
    hit = _hit(summary="删除文件：rm -f deskmate-probe.docx")
    text = render_text(hit)
    assert text == (
        "【严重】删除文件：rm -f deskmate-probe.docx，发起者未知\n"
        f"由 claude-code 会话 800257f3 执行 @ {CWD}"
    )


# ── 按后果分级 ──────────────────────────────────────────────────────────────

def test_notice_severity_may_name_the_visible_agent() -> None:
    """notice/warn 可以直接写可见的 agent——没人会为一条 429 质疑授权。"""
    hit = _hit(rule="failure", severity=SEVERITY_NOTICE,
               summary="请求失败（状态 429）：rate limited", matched="429")
    text = render_text(hit)
    assert "发起者未知" not in text
    assert text.startswith("【提示】claude-code 请求失败（状态 429）")


def test_warn_severity_may_name_the_visible_agent() -> None:
    hit = _hit(rule="failure", severity=SEVERITY_WARN,
               summary="转录报错：api_error", matched="transcript-error")
    assert "发起者未知" not in render_text(hit)


def test_critical_never_guesses_even_for_failures() -> None:
    """非瞬态 5xx 是 critical：同样不把主语交给推测。

    这条测的是"critical 不猜归因"，与具体哪个状态码无关，所以 hit 是手搭的。
    2026-11-26 起 502/503/504 不再产生 critical（瞬态，见
    ``severity_for_status``），这里换成 500——它才是"服务端真的出了错"。
    """
    hit = _hit(rule="failure", severity=SEVERITY_CRITICAL,
               summary="请求失败（状态 500）：internal", matched="500")
    assert "发起者未知" in render_text(hit)


def test_known_attribution_escapes_the_hedge_at_any_severity() -> None:
    """有证据就不加"未知"——分级只管"没证据时要不要坦白"，不管有证据的。"""
    source = attribute(_source(), mine_roots=[CWD])
    hit = evaluate_delete("rm -rf ~", source=source)
    assert hit.severity == SEVERITY_CRITICAL
    assert "发起者未知" not in render_text(hit)


# ── 边界 ────────────────────────────────────────────────────────────────────

def test_no_locator_when_session_and_cwd_missing() -> None:
    hit = _hit(session="", working_directory=None)
    text = render_text(hit)
    assert "（）" not in text
    assert "会话" not in text


def test_hedged_line_handles_missing_session() -> None:
    hit = _hit(session="", working_directory=None)
    text = render_text(hit)
    assert text.splitlines()[1] == "由 claude-code 执行"


def test_executor_falls_back_to_agent() -> None:
    """executor 空串时用 agent——不留"未知执行者"这种噪音。"""
    hit = _hit(executor="")
    assert "claude-code" in render_text(hit)
    assert "未知执行者" not in render_text(hit)


def test_summary_without_colon_still_renders() -> None:
    hit = _hit(summary="删除文件")
    assert render_text(hit).startswith("【严重】")


def test_severity_and_attribution_stay_orthogonal() -> None:
    """严重度衡量动作危险度，归因衡量谁负责——两者不许互相编码。

    把"未知"降成 notice 会让用户忽略真正危险的删除；升成 critical 会让 429 也变严重。
    """
    unknown = _hit()
    known = evaluate_delete("rm -rf ~", session="s",
                            source=attribute(_source(session="s"), mine_roots=[CWD]))
    assert unknown.severity == known.severity == SEVERITY_CRITICAL
    assert priority_for(unknown.severity) == priority_for(known.severity)


# ── 日志脱敏（A） ───────────────────────────────────────────────────────────
# 聊天窗给人看原文，落盘日志必须先打码：AGENTS.md 要求不得记录会话文本/密钥。
# 这里不删日志（排查要靠它确认播了什么），只脱敏。



@pytest.mark.parametrize("command,secret", [
    ("mysql -u root -pSuperSecret123", "SuperSecret123"),
    ("curl -p hunter2 https://x", "hunter2"),
    ("deploy --token=ghp_ABCDEFGHIJKLMNOP", "ghp_ABCDEFGHIJKLMNOP"),
    ("deploy --api-key sk-live-999888777", "sk-live-999888777"),
    ("export password=MyPassw0rd", "MyPassw0rd"),
    ("export api_key: abc123def456", "abc123def456"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ("neko --secret topsecret", "topsecret"),
    # ── A 批 A4：带下划线前缀的环境变量风格 ──
    # 第一版的正则用 ``\b`` 开头，而 ``\b`` 在下划线之前**不成立**，
    # 于是下面三条一个都不匹配——它们恰是真机上最常见的写法。
    ("export GITHUB_TOKEN=ghp_realtokenvalue123456", "ghp_realtokenvalue123456"),
    ("OPENAI_API_KEY=sk-abcdef123456 ls", "sk-abcdef123456"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY"),
    ("DB_PASSWORD: hunter2", "hunter2"),
    ("my_credential=x", "x"),
    # ── 已知令牌形状 ──
    ("git push https://gho_16C7e42F292c6912E7710c838347Ae178B4a@github.com/x",
     "gho_16C7e42F292c6912E7710c838347Ae178B4a"),
    ("git push https://github_pat_11ABCDEFG0abcdefghijklm@github.com/x",
     "github_pat_11ABCDEFG0abcdefghijklm"),
    ("aws --key AKIAIOSFODNN7EXAMPLE --region us-east-1", "AKIAIOSFODNN7EXAMPLE"),
    ("slack notify --token xoxb-1234567890-abcdefghijkl", "xoxb-1234567890-abcdefghijkl"),
])
def test_redact_removes_secret_values(command: str, secret: str) -> None:
    assert secret not in redact(command)


def test_redact_masks_a_pem_private_key_block_whole() -> None:
    """私钥块要**整体**打码，不能逐行各打一遍。

    逐行打会留下 ``***\\n***\\n***`` 这种还能看出"这里有东西"的残骸，
    而且行与行之间的结构信息（这是 RSA 还是 EC）照样漏出去。
    """
    block = ("-----BEGIN RSA PRIVATE KEY-----\n"
             "MIIEowIBAAKCAQEA\n"
             "aVerySecretLookingLine\n"
             "-----END RSA PRIVATE KEY-----")
    text = redact(f"ssh-add <(cat <<'EOF'\n{block}\nEOF\n)")
    assert "MIIEowIBAAKCAQEA" not in text
    assert "aVerySecretLookingLine" not in text
    assert "PRIVATE KEY-----\nMIIEow" not in text


def test_redact_masks_a_long_undelimited_token() -> None:
    """40 字以上、无分隔、**形状像令牌**（同时含字母与数字）的一整块打掉。

    B6 收窄了判据。老规则只要"40 字以上的 [A-Za-z0-9_-]"，于是纯字母的长串
    （不带引号的 ``git commit -m``、长标识符）也被打成马赛克——
    日志反而看不出当时跑了什么。所以现在要求同时含字母和数字。
    """
    token = "a1b2c3d4e5" * 5                      # base64 形状：字母 + 数字
    assert len(token) >= 40
    assert token not in redact(f"curl -H 'X-Api: {token}' https://x")
    # 纯十六进制长串（git sha / md5）也打
    sha = "d41d8cd98f00b204e9800998ecf8427e" * 2
    assert sha not in redact(f"git checkout {sha}")


def test_redact_leaves_pure_letter_runs_alone() -> None:
    """B6 收窄的另一面：纯字母长串不是密钥形状，不打。

    那条最常见的真实场景是 `git commit -m` 里一句不带引号的长说明——
    把它打成 *** 之后，日志里"当时提交了什么"就再也看不出来了。
    """
    words = "a" * 48
    assert redact(f"git commit -m {words}") == f"git commit -m {words}"
    slug = "someverylongidentifierwithoutanydigitsinit"
    assert slug in redact(f"npm run {slug}")


def test_redact_leaves_long_urls_and_paths_alone() -> None:
    """长 URL / 长路径**不打**——那是日志里最需要的排查信息。

    这条与上一条是同一枚硬币的两面：通用的"长串就打"规则会把它们一起
    打飞，而一个被打成马赛克的日志等于没有日志（那也是一种静默失败）。
    所以通用规则刻意不含 ``/``、``\\``、``:``。
    """
    url = "https://example.com/api/v1/users/1234567890/profile?expand=all&fields=a,b,c"
    path = r"D:\workspace\N.E.K.O\neko-src\some\deeply\nested\folder\name\file.txt"
    assert redact(f"curl {url}") == f"curl {url}"
    assert redact(f"rm -rf {path}") == f"rm -rf {path}"


def test_redact_keeps_the_variable_name() -> None:
    """打掉值，留下名字——"哪个变量带着密钥"正是排障要用的信息。"""
    text = redact("GITHUB_TOKEN=ghp_abc123")
    assert text == "GITHUB_TOKEN=***"


def test_redact_keeps_the_rest_readable() -> None:
    """脱敏不能把整条命令打成马赛克——否则日志对排查没用。"""
    text = redact("rm -rf build/ && echo ok")
    assert text == "rm -rf build/ && echo ok"


def test_redact_is_idempotent() -> None:
    once = redact("mysql -pSecret123 -u root")
    assert redact(once) == once


def test_for_log_default_keeps_redacted_full_text() -> None:
    text = for_log("mysql -pSecret123 -u root --database app", log_commands=True)
    assert "Secret123" not in text
    assert "--database app" in text      # 脱敏后仍保留可排查信息


def test_for_log_disabled_keeps_only_length_and_head() -> None:
    long_command = "rm -rf " + "x" * 200 + " -pSecret123"
    text = for_log(long_command, log_commands=False)
    assert "Secret123" not in text
    assert f"命令 {len(long_command)} 字" in text
    assert "x" * 200 not in text          # 超过 40 字的原文不落盘


def test_log_commands_defaults_to_off() -> None:
    """**默认不记原文**（A 批 A4 第 3 条）。

    原来默认是 true：命令原文是用户这一侧最敏感的东西之一，
    而"默认就往磁盘写"是一个需要用户主动去改才安全的默认。
    新装的插件必须一上来就是关的。
    """
    assert "命令 13 字" in for_log("rm -rf build/")
    # 前 40 字照样会露出来（那是设计：要能排查），但**整条不落盘**
    long_command = "rm -rf " + "x" * 200
    assert "命令" in for_log(long_command)
    assert "x" * 200 not in for_log(long_command)


def test_broadcaster_defaults_to_not_logging_commands() -> None:
    """构造 Broadcaster 时不给 log_commands，也必须是关的。

    两处默认值（for_log 与 Broadcaster）说的是同一件事，写歪一个
    就会出现"以为关了、其实在记"——所以两个都钉住。
    """
    logger = FakeLogger()
    long_command = "rm -rf " + "y" * 200
    hit = _hit(summary=f"删除文件：{long_command}")
    Broadcaster(RecordingPush(), logger, source=SOURCE).broadcast(hit)
    joined = " ".join(message for _, message in logger.lines)
    # 前 40 字仍可能含一段短命令（那是设计：要能排查），
    # 但**整条原文不许落盘**——这里用一条 200 字以上的命令来判。
    assert "y" * 200 not in joined
    assert "命令 2" in joined


def test_for_log_disabled_still_redacts_the_head() -> None:
    """关掉 log_commands 也要脱敏——前 40 字照样可能含密钥。"""
    text = for_log("curl -H 'Authorization: Bearer sk-supersecretvalue' https://x",
                   log_commands=False)
    assert "sk-supersecretvalue" not in text


def test_broadcast_log_is_redacted() -> None:
    """端到端：含 sk- 的命令，日志里不得出现原文。"""
    logger = FakeLogger()
    push = RecordingPush()
    hit = _hit(summary="删除文件：curl -H 'Authorization: Bearer sk-live-999888777' https://x")
    Broadcaster(push, logger, source=SOURCE, log_commands=True).broadcast(hit)

    assert push.calls[0]["parts"][0]["text"].count("sk-live-999888777") == 1  # 聊天窗保留原文
    joined = " ".join(message for _, message in logger.lines)
    assert "sk-live-999888777" not in joined
    assert "***" in joined


def test_broadcast_log_honours_log_commands_false() -> None:
    logger = FakeLogger()
    hit = _hit(summary="删除文件：" + "rm -rf " + "y" * 200)
    Broadcaster(RecordingPush(), logger, source=SOURCE,
                log_commands=False).broadcast(hit)
    joined = " ".join(message for _, message in logger.lines)
    assert "y" * 200 not in joined
    assert "命令 " in joined


# ── 下派观测的文案（C / 归因第 4 步） ───────────────────────────────────────



def _delegation_hit(**overrides):
    source = UnifiedEvent(
        agent="claude-code", session="800257f3-aaaa-bbbb", event="tool_use",
        tool="mcp__deepseek-worker__deepseek_task", command=None, ts="t",
        executor="claude-code", working_directory=CWD,
    )
    source = attribute(source, mine_roots=[CWD])
    return evaluate_delegation(source)


def test_delegation_broadcast_text() -> None:
    """notice 级，主语写大脑，worker 降级成"手"——不出现"DeepSeek 请求"这种主语。

    主语就是可见的 claude-code：MCP 事件的归因证据是 mcp-delegation，
    即使用户声明过 cwd 所属目录，这条的判据也是"它下派了"，不是"你开的"。
    """
    text = render_text(_delegation_hit())
    assert text == (
        "【提示】claude-code 通过 deepseek-worker 下派了一次任务"
        f"（会话 800257f3 @ {CWD}）"
    )


def test_delegation_subject_is_not_doubled() -> None:
    """正文已说明"通过 X 下派"，主语不能再写"让 X 执行"，否则一句话说两遍。"""
    text = render_text(_delegation_hit())
    assert "让 deepseek-worker 执行" not in text
    assert text.count("deepseek-worker") == 1


def test_delegation_unknown_requester_still_names_the_brain() -> None:
    """notice 级按后果分级可以直接写可见 agent，不套"发起者未知"。"""
    source = UnifiedEvent(
        agent="claude-code", session="800257f3-aaaa-bbbb", event="tool_use",
        tool="mcp__hermit__run_task", command=None, ts="t",
        executor="claude-code", working_directory=r"C:\Temp",
    )
    hit = evaluate_delegation(attribute(source))
    text = render_text(hit)
    assert "发起者未知" not in text
    assert text.startswith("【提示】claude-code 通过 hermit 下派了一次任务")


def test_delegation_severity_is_notice_not_critical() -> None:
    assert priority_for(_delegation_hit().severity) == 4


def test_describe_actor_is_reusable_for_cards() -> None:
    """M0-lite-5 的审批卡片要用同一套主语语法，不能另写一套。"""
    from plugin.plugins.agent_deskmate.broadcast import describe_actor

    assert describe_actor(executor="claude-code") == "claude-code"
    assert describe_actor(executor="claude-code", delegated_to="deepseek-worker") == \
        "claude-code 让 deepseek-worker 执行"
    assert describe_actor(executor="claude-code", requester_evidence="sidechain") == \
        "claude-code（经子代理）"
    assert describe_actor(executor="claude-code", requester="user",
                          requester_evidence="user-declared") == "你自己开的 claude-code"
    # 下派观测例外：正文已说明下派，主语不重复
    assert describe_actor(executor="claude-code", delegated_to="deepseek-worker",
                          rule="delegation") == "claude-code"


# ── 同工作目录合并（P1c(b)）───────────────────────────────────────────────
#
# 2026-11-24 真机：同一目录下三个会话同时出事，连发三条一字不差的
# 「X 出事了 / Bash 执行失败，它没自己恢复」。三条相同的通知不是三倍
# 的信息，是三分之一的信息——用户读到第 2 条就开始无视，第 3 条一定无视，
# 然后连第 1 条也一起无视。

def _notice(*, session: str = "s1", kind: str = "Bash",
            workspace: str | None = r"D:\proj",
            role=Role.OUTCOME, detail: str = "Bash 执行失败，它没自己恢复"
            ) -> Notice:
    return Notice(role=role, light=Light.ERROR, agent="claude-code",
                  session=session, who=workspace or "claude-code",
                  headline=f"{workspace or 'claude-code'} 出事了",
                  detail=detail, at=1_700_000_000.0, fingerprint=f"fp|{session}",
                  kind=kind, workspace=workspace)


def test_three_sessions_in_one_directory_become_one_notice() -> None:
    notices = coalesce_same_workspace([_notice(session=f"s{index}")
                                       for index in range(3)])
    assert len(notices) == 1
    assert notices[0].detail == "Bash 执行失败，它没自己恢复（同目录另有 2 个会话）"


def test_two_sessions_merge_with_the_right_count() -> None:
    notices = coalesce_same_workspace([_notice(session="s1"),
                                       _notice(session="s2")])
    assert len(notices) == 1
    assert notices[0].detail.endswith("（同目录另有 1 个会话）")


def test_a_different_tool_kind_is_not_merged() -> None:
    """同目录里 Bash 挂了、WebFetch 也挂了——那是两件事，合成一条就丢了
    "两种东西同时坏"这个信息。"""
    notices = coalesce_same_workspace([_notice(session="s1", kind="Bash"),
                                       _notice(session="s2", kind="WebFetch")])
    assert len(notices) == 2


def test_a_different_directory_is_not_merged() -> None:
    notices = coalesce_same_workspace([
        _notice(session="s1", workspace=r"D:\proj"),
        _notice(session="s2", workspace=r"D:\other"),
    ])
    assert len(notices) == 2


def test_sessions_without_a_directory_are_not_merged() -> None:
    """**这条是精度的心脏。** ``who`` 拿不到目录时会退回 agent 名，而
    ``claude-code`` 是所有无目录会话的共同退回值。按它合并等于把一堆
    互不相干的会话揉成一条，而且「同目录另有 N 个会话」在没目录时是句假话。
    """
    notices = coalesce_same_workspace([
        _notice(session="s1", workspace=None),
        _notice(session="s2", workspace=None),
        _notice(session="s3", workspace=None),
    ])
    assert len(notices) == 3


def test_waiting_notices_are_not_merged() -> None:
    """WAITING 指名的是"哪个会话在等你"，合起来就答不了这个问题了。"""
    notices = coalesce_same_workspace([
        _notice(session="s1", role=Role.WAITING, detail="等「允许」"),
        _notice(session="s2", role=Role.WAITING, detail="等「允许」"),
    ])
    assert len(notices) == 2


def test_the_input_list_is_not_mutated() -> None:
    """tracker 每轮都重建 notices，但"我会动你的数据"不该是函数签名
    暗示出来的事——Notice 还是 frozen 的。"""
    original = [_notice(session="s1"), _notice(session="s2")]
    before = [n.detail for n in original]
    coalesce_same_workspace(original)
    assert [n.detail for n in original] == before


def test_a_single_notice_is_returned_untouched() -> None:
    only = _notice(session="s1")
    assert coalesce_same_workspace([only]) == [only]


# ── NoticeMerger：跨批次合并（P1c(b)，真机验收 2026-11-24 测试 8 的根因）──
#
# 第一版只合同一轮。真机实测：6 个会话 3 秒内全部失败，而出事了的播出时间是
# 各自首次失败 +120s，跨过 3 秒轮询相位就落进不同批次 → 屏幕收到 3 条。
# 这里钉住"跨批次"这一个维度。

class _Clock:
    """假时钟。``offer`` 的 now 是判断的唯一依据，不可注入就等于没法测窗口。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_notices_in_the_same_batch_still_merge() -> None:
    """同一轮里的行为不能因为加了时间维度就退化。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    ready = merger.offer([_notice(session="s1"), _notice(session="s2")], clock.now)
    assert ready == []                       # 窗口没到，压着
    clock.advance(20.0)
    assert len(merger.offer([], clock.now)) == 1


def test_notices_in_different_batches_merge_too() -> None:
    """**这一条是测试 8 的根因。** 两个会话的失败落在不同轮询批次，
    间隔只有 3 秒（轮询周期），也必须合成一条。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    assert merger.offer([_notice(session="s1")], clock.now) == []
    clock.advance(3.0)                        # 下一轮轮询
    assert merger.offer([_notice(session="s2")], clock.now) == []
    clock.advance(17.0)                       # 窗口到期
    ready = merger.offer([], clock.now)
    assert len(ready) == 1
    assert ready[0].detail.endswith("（同目录另有 1 个会话）")


def test_the_count_accumulates_across_batches() -> None:
    """跨批次也要续号，不是每个批次各起一个计数器。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1")], clock.now)
    for step in range(2, 5):
        clock.advance(3.0)
        merger.offer([_notice(session=f"s{step}")], clock.now)
    clock.advance(20.0)
    ready = merger.offer([], clock.now)
    assert len(ready) == 1
    assert ready[0].detail.endswith("（同目录另有 3 个会话）")


def test_the_window_is_measured_from_the_first_notice() -> None:
    """窗口从**首次收入**算，不是每条各算一次——否则第 5 条会把到期时间
    无限往后推，这批通知永远发不出去。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1")], clock.now)
    for _ in range(10):
        clock.advance(3.0)
        merger.offer([_notice(session="sx")], clock.now)
        assert merger.offer([], clock.now) == []      # 一直压着
    clock.advance(20.0)
    assert len(merger.offer([], clock.now)) == 1


def test_a_notice_outside_the_window_starts_a_new_group() -> None:
    """超过窗口的第二次失败是**新的一批**，该单独播一条——
    合成一条等于把两次独立事故说成一次。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1")], clock.now)
    clock.advance(21.0)
    assert len(merger.offer([_notice(session="s2")], clock.now)) == 1   # 第一批到期
    clock.advance(20.0)
    assert len(merger.offer([], clock.now)) == 1                        # 第二批到期


def test_a_zero_window_means_no_delay_but_same_batch_merging_survives() -> None:
    """``notice_merge_seconds=0`` = 不延迟，**不是不合并**。

    同一轮批次内的合并照旧——那是 0.5.0 真机验收确认过的行为（同一个 3 秒
    轮询批次里落地的通知本来就是同时的，合掉它们没有代价）。
    测试里大量用 0：要的是"当轮就看到通知"，不是"关掉合并"。
    """
    merger = NoticeMerger(window=0.0)
    clock = _Clock()
    ready = merger.offer([_notice(session="s1"), _notice(session="s2")],
                         clock.now)
    assert len(ready) == 1
    assert ready[0].detail.endswith("（同目录另有 1 个会话）")
    assert len(merger) == 0                       # 没有压在手里的


def test_setting_the_window_to_zero_releases_what_is_held() -> None:
    """调成 0 要立刻恢复"当轮就发"，而不是让压着的几条再等一个旧窗口。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1")], clock.now)
    merger.set_window(0.0)
    ready = merger.offer([], clock.now)
    assert len(ready) == 1
    assert "另有" not in ready[0].detail


def test_unmergeable_notices_are_never_held() -> None:
    """WAITING 指名的是哪个会话，压它一个窗口没有任何好处；
    没有工作目录的也当轮直接过（``who`` 会退回 agent 名）。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    ready = merger.offer([
        _notice(session="s1", role=Role.WAITING, detail="等「允许」"),
        _notice(session="s2", workspace=None),
    ], clock.now)
    assert len(ready) == 2
    assert len(merger) == 0


def test_flush_releases_everything_without_waiting() -> None:
    """停机/重载时不许把用户的通知吞在手里——这个项目的硬规矩是
    "一条都不许丢"，而这里是唯一一个会丢的地方。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1"), _notice(session="s2")], clock.now)
    assert len(merger.flush(clock.now)) == 1
    assert len(merger) == 0
    assert merger.offer([], clock.now) == []


def test_different_directories_are_never_merged() -> None:
    """合并不是为了少说话，是为了不说重复的话。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1", workspace=r"D:\proj"),
                  _notice(session="s2", workspace=r"D:\other")], clock.now)
    clock.advance(20.0)
    assert len(merger.offer([], clock.now)) == 2


def test_different_kinds_are_never_merged() -> None:
    """同目录里 Bash 挂了、WebFetch 也挂了——那是两件事，
    合成一条就丢了"两种东西同时坏"这个信息。"""
    merger = NoticeMerger(window=20.0)
    clock = _Clock()
    merger.offer([_notice(session="s1", kind="Bash"),
                  _notice(session="s2", kind="WebFetch")], clock.now)
    clock.advance(20.0)
    assert len(merger.offer([], clock.now)) == 2


def test_the_default_window_is_the_documented_one() -> None:
    assert NOTICE_MERGE_SECONDS == 20.0


# ── 播报方式：blind（只出文字）↔ respond（让猫娘说一说） ────────────────


class _RecordingPush:
    """记录 push_message 收到的参数。只看 kwargs，不看正文。"""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "accepted"}


def _speak_hit():
    from plugin.plugins.agent_deskmate.rules import RuleHit
    return RuleHit(rule="delete", severity="critical", summary="删除文件：x",
                   matched="rm", agent="claude-code", session="547ea690",
                   ts="2026-11-26T00:00:00", command_length=12)


def test_default_is_blind():
    """默认只出文字——这是插件一直的行为，不能因为加了开关就变。"""
    push = _RecordingPush()
    b = Broadcaster(push, FakeLogger(), source="agent_deskmate")
    assert b.ai_behavior == "blind"
    b.broadcast(_speak_hit())
    assert push.calls[0]["ai_behavior"] == "blind"


def test_speak_true_switches_to_respond():
    push = _RecordingPush()
    b = Broadcaster(push, FakeLogger(), source="agent_deskmate", speak=True)
    assert b.ai_behavior == "respond"
    b.broadcast(_speak_hit())
    assert push.calls[0]["ai_behavior"] == "respond"


def test_source_and_visibility_unchanged_by_speak():
    """红线 1 的四条不随播报方式变：source 显式、visibility=["chat"]、
    永不 await、只有这一个出口。"""
    push = _RecordingPush()
    b = Broadcaster(push, FakeLogger(), source="agent_deskmate", speak=True)
    b.broadcast(_speak_hit())
    call = push.calls[0]
    assert call["source"] == "agent_deskmate"
    assert call["visibility"] == ["chat"]
    # 原文始终上屏——语音那一路漏了，人也看得见
    assert call["parts"][0]["type"] == "text"


def test_notice_follows_the_same_switch():
    """通知类（进度/卡住/等待）跟着同一个开关。
    用户要的是"时不时说一下进行到哪一步"——那正是这一类，
    只给删除类开 speak 会得到半成品。
    """
    from plugin.plugins.agent_deskmate.broadcast import broadcast_notice
    push = _RecordingPush()
    notice = Notice(role=Role.OUTCOME, light=Light.WAITING,
                      agent="claude-code", session="547ea690",
                      who="claude-code", headline="在等你",
                      detail="等你决定", at=1_700_000_000.0,
                      fingerprint="fp|speak")
    broadcast_notice(push, notice, source="agent_deskmate",
                     logger=FakeLogger(), ai_behavior="respond")
    assert push.calls[0]["ai_behavior"] == "respond"

    push2 = _RecordingPush()
    broadcast_notice(push2, notice, source="agent_deskmate",
                     logger=FakeLogger(), ai_behavior="blind")
    assert push2.calls[0]["ai_behavior"] == "blind"


def test_push_text_default_stays_blind():
    """push_text 的默认值必须是 blind——它还有其他调用方。"""
    from plugin.plugins.agent_deskmate.broadcast import push_text
    push = _RecordingPush()
    push_text(push, "x", source="s", priority=5, logger=FakeLogger())
    assert push.calls[0]["ai_behavior"] == "blind"


# ── respond 冷却：不连发（绕宿主前端一个弄坏要重启 App 的 bug） ──────────


def test_two_responds_in_a_row_second_is_demoted():
    """宿主前端 `app-audio-playback.js:1590` 每次新语音接管都
    `incomingAudioEpoch += 1`，676 行按代际丢块。连续接管疑似把音频
    推进"开头吞字"的坏状态，恢复要重启整个 App（侦查笔记结论 3，
    机制已在代码里核实；但"连发就会坏"本身未做可控复现）。
    所以插件侧不连发：第二条自动降级成 blind，文字照旧上屏。
    """
    b = Broadcaster(RecordingPush(), FakeLogger(), source="s", speak=True)
    assert b.resolve_ai_behavior(now=1000.0) == "respond"
    assert b.resolve_ai_behavior(now=1001.0) == "blind"
    # 过了冷却窗才恢复
    assert b.resolve_ai_behavior(now=1000.0 + RESPOND_COOLDOWN) == "respond"


def test_cooldown_does_not_leak_into_later_broadcasts():
    """降温只影响窗口内那一条，不是把自己关掉。"""
    b = Broadcaster(RecordingPush(), FakeLogger(), source="s", speak=True)
    b.resolve_ai_behavior(now=1000.0)
    assert b.resolve_ai_behavior(now=1005.0) == "blind"
    assert b.resolve_ai_behavior(now=1100.0) == "respond"
    assert b.ai_behavior == "respond"   # 配置没变，只是这一条降温


def test_blind_mode_never_touches_the_cooldown():
    b = Broadcaster(RecordingPush(), FakeLogger(), source="s", speak=False)
    for i in range(5):
        assert b.resolve_ai_behavior(now=1000.0 + i) == "blind"
    # 从来没让她说过话，所以之后一开就能说
    b.speak = True
    assert b.resolve_ai_behavior(now=1001.0) == "respond"


def test_real_broadcast_sequence_respects_cooldown():
    """端到端：连着播两条，第一条 respond、第二条 blind。"""
    push = RecordingPush()
    b = Broadcaster(push, FakeLogger(), source="s", speak=True)
    b.broadcast(_speak_hit())
    b.broadcast(_speak_hit())
    assert push.calls[0]["ai_behavior"] == "respond"
    assert push.calls[1]["ai_behavior"] == "blind"
    # 降级不改别的：source / visibility / 原文都在（红线 1 的四条不随冷却变）
    assert push.calls[1]["source"] == "s"
    assert push.calls[1]["visibility"] == ["chat"]
    assert push.calls[1]["parts"][0]["type"] == "text"


# ── speak()：让她「念」台词（手法照抄侦查笔记结论 5） ────────────────────


class _Rec:
    """记录桩。返回值形状照 RecordingPush——push_text 认 submitted=True。"""

    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return {"submitted": True}


def test_read_instruction_has_all_three_elements():
    """prompt 三要素缺一不可——每一条都对应一次真实跑偏。"""
    got = read_instruction("已自动放行，风险很低。")
    assert "念" in got                      # 动词是念，不是播报/通知
    assert "不要改写" in got                # 禁令：防纯改写+追问
    assert "舞台提示" in got                # 禁令：防「本喵念得标准吧？」
    assert "『" in got and "』" in got       # 引号隔离：分清台词与指示


def test_read_instruction_puts_lead_inside_the_quotes():
    """语气词垫在台词**里面**——被吞的才落在垫子上而不是内容上。"""
    got = read_instruction("abc", lead="嗯，")
    assert "『嗯，abc』" in got


def test_read_instruction_lead_can_be_disabled():
    assert read_instruction("abc", lead="") == (
        "请把下面这句原样念出来，不要改写、不要加任何说明或舞台提示：『abc』")


def test_speak_sends_two_pushes():
    push = _Rec()
    spoken, precise = speak(push, "已自动放行。", source="s",
                            logger=FakeLogger())
    assert len(push.calls) == 2
    # ① 念：空 visibility + respond + 指示包裹
    assert push.calls[0]["visibility"] == []
    assert push.calls[0]["ai_behavior"] == "respond"
    assert "念出来" in push.calls[0]["parts"][0]["text"]
    # ② 留底：chat + blind + 精确原文（不包引号、不加语气词）
    assert push.calls[1]["visibility"] == ["chat"]
    assert push.calls[1]["ai_behavior"] == "blind"
    assert push.calls[1]["parts"][0]["text"] == "已自动放行。"
    assert (spoken, precise) == ("sent", "sent")


def test_speak_backstop_is_byte_exact():
    """用户眼睛看的那条必须一字不差——垫的语气词和引号都不许进去。"""
    push = _Rec()
    speak(push, "它要执行：rm -rf build/", source="s", logger=FakeLogger())
    assert push.calls[1]["parts"][0]["text"] == "它要执行：rm -rf build/"


def test_speak_blind_mode_only_sends_backstop():
    """没配 key 时优雅降级：红线 3 是零 key 只跑 L0。"""
    push = _Rec()
    spoken, precise = speak(push, "x", source="s", logger=FakeLogger(),
                            ai_behavior="blind")
    assert len(push.calls) == 1
    assert push.calls[0]["ai_behavior"] == "blind"
    assert spoken == "skipped"
    assert precise == "sent"


def test_speak_source_is_explicit():
    push = _Rec()
    speak(push, "x", source="agent_deskmate", logger=FakeLogger())
    for call in push.calls:
        assert call["source"] == "agent_deskmate"


def test_speak_never_raises_on_push_failure():
    def boom(**kw):
        raise RuntimeError("宿主不在")

    spoken, precise = speak(boom, "x", source="s", logger=FakeLogger())
    assert spoken == "exception"
    assert precise == "exception"


def test_speak_logs_what_it_said_on_success():
    """念成功了**必须留一行日志**。

    0.6.15 漏的就是这条：``push_text`` 只在失败时记日志，而
    ``broadcast_notice`` 早在 0.6.14 就补了成功那一行——新增的
    ``speak()`` 这条路没跟上。后果 2026-11-27 真机实测：Yui 确实开口
    说了「这一轮跑完了」，插件日志里一个字都没有，
    「语音线到底生没生效」只能靠用户截图确认。
    用户原话：「这个暂时没办法做确认」。
    """
    log = FakeLogger()
    speak(_Rec(), "claude-code 这一轮跑完了，共 2 步，主要是 Bash。",
          source="s", logger=log, tag="boundary",
          keep="第 1 轮，共 2 步 · 会话 xxxxxxxx")

    said = [line for _, line in log.lines if line.startswith("已念")]
    assert len(said) == 1, log.lines
    assert "claude-code 这一轮跑完了" in said[0]
    assert "boundary" in said[0]
    assert "留底" in said[0]


def test_speak_logs_the_no_key_case_too():
    """没念**也要记**，且和"没走到这条路"区分得开。

    否则「无 key 所以没出声」与「压根没执行」在日志里长得一模一样，
    排查时只能靠猜——而这两件事的修法完全相反。
    """
    log = FakeLogger()
    speak(_Rec(), "x", source="s", logger=log, tag="boundary",
          ai_behavior="blind")

    lines = [line for _, line in log.lines if line.startswith("未念")]
    assert len(lines) == 1, log.lines
    assert "只出文字" in lines[0]
    # 不能同时记成"已念"——那会让人以为她开口了
    assert not [line for _, line in log.lines if line.startswith("已念")]


def test_speak_does_not_log_success_when_the_push_failed():
    """推失败了就**不记"已念"**——那是一次撒谎。

    失败本身由 ``push_text`` 记（它失败时记），这里只保证成功那条不虚发。
    """
    def boom(**kw):
        raise RuntimeError("宿主不在")

    log = FakeLogger()
    speak(boom, "x", source="s", logger=log, tag="boundary")
    assert not [line for _, line in log.lines if line.startswith("已念")]


def test_force_skips_the_speak_switch_but_not_the_cooldown():
    """``force=True`` 只绕过 speak 开关，**不绕过冷却**。

    2026-11-26：「这一轮跑完了」要语音提醒，而 ``speak`` 是"让不让猫娘接话"
    的总开关、默认关。拿它卡一个明确要的功能，结果是功能装了却不响。
    但冷却仍然生效——宿主前端连发 respond 会坏（见上面两条测试）。
    """
    off = Broadcaster(RecordingPush(), FakeLogger(), source="s", speak=False)
    assert off.resolve_ai_behavior(now=1000.0) == "blind"
    assert off.resolve_ai_behavior(now=1000.0, force=True) == "respond"
    # 冷却照旧：紧接着第二条 force 也降级
    assert off.resolve_ai_behavior(now=1001.0, force=True) == "blind"
    assert off.resolve_ai_behavior(now=1000.0 + RESPOND_COOLDOWN,
                                   force=True) == "respond"


def test_force_does_not_disturb_the_plain_path():
    """force 只影响被 force 的那条，不把普通播报也跟着改成 respond。"""
    b = Broadcaster(RecordingPush(), FakeLogger(), source="s", speak=False)
    b.resolve_ai_behavior(now=1000.0, force=True)
    assert b.resolve_ai_behavior(now=1100.0) == "blind"


def test_a_successful_notice_leaves_a_log_line():
    """**通知层也要可观测**——此前只有失败才打日志。

    2026-11-26 真机：用户说跑完提醒没响，而我 grep 遍日志只能得出
    "没有记录"，分不清是没播、还是播了没痕迹。规则层的
    ``Broadcaster.broadcast`` 一向有成功日志，通知层没有——
    两层可观测性不一样，而排查的人只有日志可看。

    这一行同时带 ``ai_behavior``：``respond`` 才是"她开口了"，
    ``blind`` 只上文字。没有这一项就分不清"说了"和"写了"。
    """
    from plugin.plugins.agent_deskmate.progress import Role
    from plugin.plugins.agent_deskmate.tracker import Notice

    logger = FakeLogger()
    notice = Notice(role=Role.BOUNDARY, light=Light.IDLE, agent="a",
                    session="s", who="claude-code",
                    headline="claude-code 这一轮跑完了",
                    detail="第 1 轮，共 3 步（Bash 2 次）",
                    at=1000.0, fingerprint="f")
    assert broadcast_notice(RecordingPush(), notice, source="s",
                            logger=logger, ai_behavior="respond") == "sent"
    lines = [text for _level, text in logger.lines if "已播通知" in text]
    assert len(lines) == 1
    assert "respond" in lines[0]
    assert "这一轮跑完了" in lines[0]
    assert "Bash 2 次" in lines[0]


def test_a_blind_notice_says_so_in_the_log():
    """没 key 时降级成 blind，日志要能看出来——否则会以为她说话了。"""
    from plugin.plugins.agent_deskmate.progress import Role
    from plugin.plugins.agent_deskmate.tracker import Notice

    logger = FakeLogger()
    notice = Notice(role=Role.BOUNDARY, light=Light.IDLE, agent="a",
                    session="s", who="claude-code",
                    headline="claude-code 这一轮跑完了",
                    detail="第 1 轮，共 3 步", at=1000.0, fingerprint="f")
    broadcast_notice(RecordingPush(), notice, source="s", logger=logger,
                     ai_behavior="blind")
    assert any("blind" in text
               for _level, text in logger.lines if "已播通知" in text)
