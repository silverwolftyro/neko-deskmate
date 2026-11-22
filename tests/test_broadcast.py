"""broadcast.py 播报层单测（M0-lite-4）。

核心是红线 1 与任务书要求：
  - source 必须显式、ai_behavior 必须 blind；
  - ``submitted=False`` 是正常分支，不抛异常；
  - 永不 await push_message（播报器是同步函数，测试里用同步桩就能证明）。
"""

from __future__ import annotations

import pytest

from plugin.plugins.agent_deskmate.broadcast import (
    Broadcaster,
    for_log,
    priority_for,
    redact,
    render_text,
)
from plugin.plugins.agent_deskmate.events import UnifiedEvent, attribute
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
    """5xx 必须用 critical 的 priority，否则用户在聊天窗里看不出严重程度。"""
    push = RecordingPush()
    hit = evaluate_failure(status_code=503, error_message="overloaded",
                           agent="claude", session="s1")
    Broadcaster(push, FakeLogger(), source=SOURCE).broadcast(hit)
    assert push.calls[0]["priority"] == 9
    assert "503" in push.calls[0]["parts"][0]["text"]

    push2 = RecordingPush()
    hit2 = evaluate_failure(status_code=429, error_message="rate limited",
                            agent="claude", session="s1")
    Broadcaster(push2, FakeLogger(), source=SOURCE).broadcast(hit2)
    assert push2.calls[0]["priority"] == 4        # notice，不按 critical 打扰人
    assert push2.calls[0]["ai_behavior"] == "blind"


# ── 归因文案：四种模版（2026-11-21 决策记录 §1/§2） ─────────────────────────
# 核心原则：主语位只放「可证的请求源」；执行者降级到括号里；拿不准就写未知。
# 用户会把主语读成「发起者」——主语位放执行者，就等于把锅扣给手。
# 真实事故：DSH spawn 的 claude -p 删了文件，播报说「claude-code 删除」，
# 用户第一反应是"我什么时候让 Claude 删东西了？"

CWD = r"C:\Users\alpha\AppData\Local\Temp"


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
    """5xx 是 critical：同样不把主语交给推测。"""
    hit = _hit(rule="failure", severity=SEVERITY_CRITICAL,
               summary="请求失败（状态 503）：overloaded", matched="503")
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
