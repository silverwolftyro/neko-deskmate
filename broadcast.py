"""L0 播报：把规则命中推进 N.E.K.O 聊天窗口（红线 1）。

铁律（AGENTS.md + 任务书）：
  - ``source`` 必须显式传，且等于 plugin_id；
  - 纯播报必须 ``ai_behavior="blind"``——默认的 ``"respond"`` 会让角色开口，
    那是把插件的观测结果变成角色发言，属于骗人；
  - **永不 await push_message**：它入队后就会抛 TypeError，重试等于发两条。
    ``PushMessageResult`` 只是「本地已收」的回执，不代表用户看到、模型生成或播放；
  - ``submitted=False`` 是**正常分支**：记日志、返回原因，不抛异常。

四种拒绝原因都是可恢复的，处置不同：
  ``payload_too_large``  永久，得把内容改小（记 warning）
  其余三种               瞬时，下一轮自然恢复（记 info，避免刷日志）
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .events import (
    ATTRIBUTION_KNOWN,
    EVIDENCE_SIDECHAIN,
    EVIDENCE_USER_DECLARED,
    REQUESTER_USER,
)
from .rules import (
    RULE_DELEGATION,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_NOTICE,
    SEVERITY_WARN,
    RuleHit,
)

# 严重度 → N.E.K.O priority（0-10，越大越重要）
SEVERITY_PRIORITY = {
    SEVERITY_CRITICAL: 9,
    SEVERITY_WARN: 6,
    SEVERITY_NOTICE: 4,
    SEVERITY_INFO: 2,
}

# 人眼前缀。刻意不用 emoji：这是状态播报，不是装饰。
SEVERITY_LABEL = {
    SEVERITY_CRITICAL: "【严重】",
    SEVERITY_WARN: "【警告】",
    SEVERITY_NOTICE: "【提示】",
    SEVERITY_INFO: "【信息】",
}

_PUSH_REJECTION_INFO = frozenset({"backpressure", "transport_error", "transport_unavailable"})

# ── 日志脱敏 ────────────────────────────────────────────────────────────────
# 播报正文要给人看，不能打码；但**日志会落盘**，命令里可能带密钥。
# AGENTS.md 要求不得记录会话文本/密钥——这里用"写日志前打码"落地，
# 而不是把日志删掉（排查时还要靠它确认到底播了什么）。
#
# 覆盖：环境变量与配置项（任意前缀 + KEY/TOKEN/SECRET/PASSWORD）、
# 已知令牌形状（GitHub / AWS / Slack / PEM 私钥）、40 字以上的无分隔长串。
# 宁可比必要多打一点——漏一个密钥比多几个 *** 严重得多。
#
# 第一版的 ``\b(password|api_key|token)\s*[=:]\s*\S+`` 有个静默漏洞：
# ``\b`` 在下划线之前**不成立**，于是 ``GITHUB_TOKEN=ghp_…``、
# ``OPENAI_API_KEY=…``、``AWS_SECRET_ACCESS_KEY=…`` 一个都不匹配——
# 而它们恰是真机上最常见的写法。所以改成"任意标识符 + 关键字 + 任意标识符"。

def _REDACTED_ASSIGNMENT(match: "re.Match[str]") -> str:
    """把 ``NAME=value`` 换成 ``NAME=***``：**保留名字，只打掉值**。

    写成函数而不是替换串，是因为要留下等号左边那段。名字不是秘密，
    而"哪个变量带着密钥"正是排障时最想要的那一条信息。
    """
    text = match.group(0)
    head, separator, _ = (text.partition("=") if "=" in text
                          else text.partition(":"))
    return f"{head}{separator}***"


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM 私钥块必须排在最前：它跨多行，单行规则会把每一行各打一遍，
    # 留下还能看出结构的残骸。整体换成一个标记。
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
                r"-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
     "[私钥块已打码]"),
    # Bearer 令牌必须排在 KEY=value 之前：否则 `Authorization: Bearer eyJ…`
    # 会先被后者匹配成 `Authorization=***`，把真正的令牌原样留下。
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer ***"),
    # Authorization 头整体打码。带 Bearer 的交给上一条处理（它已经吃掉令牌），
    # 这里用负向断言避开，否则会留下一个多余的 ***。
    (re.compile(r"(?i)\bauthorization\s*[=:]\s*(?!bearer\b)\S+"), "Authorization=***"),
    # -p<密码> / -p <密码>（curl、mysql 等的惯用写法）
    (re.compile(r"(?i)((?:^|\s)-p\s*)(\S+)"), r"\1***"),
    # --token=xxx / --api-key xxx / --password=xxx
    (re.compile(r"(?i)(--(?:token|api-key|apikey|password|passwd|secret)[= ]+)(\S+)"), r"\1***"),
    # 任意前缀的 KEY / TOKEN / SECRET / PASSWORD 赋值：
    #   GITHUB_TOKEN=…、OPENAI_API_KEY: …、my_secret=…、DB_PASSWORD=…
    # 两侧的 ``[A-Za-z0-9_]*`` 都可以为空，所以裸写 token=… 也照样命中。
    (re.compile(r"(?i)[A-Za-z0-9_]*(?:key|token|secret|password|passwd|credential)"
                r"[A-Za-z0-9_]*\s*[=:]\s*\S+"),
     _REDACTED_ASSIGNMENT),
    # 已知令牌形状。按前缀认，不用通用规则——通用规则要么漏要么误伤。
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "gh***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "xox***"),
    # 纯十六进制长串（git sha / md5 / 证书指纹）。32 起：短的不可能是密钥，
    # 而 32 是 md5 的长度。**必须含数字**——纯字母的 32 串（比如 60 个 a）
    # 不是令牌形状，把它打掉只会让日志看不出当时跑了什么。
    (re.compile(r"(?<![0-9a-fA-F])"
                r"(?=[0-9a-fA-F]{32,})(?=[0-9a-fA-F]*[0-9])"
                r"[0-9a-fA-F]{32,}(?![0-9a-fA-F])"), "hex***"),
    # 40 字以上、无空白、**且同时含字母与数字**的一整块。这是"形状像令牌"的兜底：
    # 认不出品种的长串宁可打掉。**刻意不含 ``/``、``\``、``:``**——否则长 URL
    # 和长路径会被整段打飞，日志就没法看了（那也是一种静默失败）。
    # **B6 收窄**：老规则只要"40 字以上的 [A-Za-z0-9_-]"，于是长
    # ``git commit -m``（不带引号的那半句）和长的单字标识符也被打成马赛克，
    # 日志反而看不出当时跑了什么。加上"必须同时含字母和数字"之后，
    # 纯字母的长串（说明文字、长文件名）不再被打，base64/hex 形状的照样打。
    (re.compile(r"(?<![A-Za-z0-9_\-])"
                r"(?=[A-Za-z0-9_\-]{40,})"        # 够长
                r"(?=[A-Za-z0-9_\-]*[0-9])"       # 含数字
                r"(?=[A-Za-z0-9_\-]*[A-Za-z])"    # 含字母
                r"[A-Za-z0-9_\-]{40,}"), "***"),
    # OpenAI/DeepSeek 风格的 key（比 40 字规则更早的形状，保留原替换文案）
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
)

# log_commands=false 时保留的原文长度
LOG_COMMAND_HEAD_CHARS = 40


def redact(text: str) -> str:
    """把文本里的密钥形状打成 `***`。纯函数，便于单测。

    **审计写盘、模型 prompt、日志，三条出口都要过它**——漏掉哪一条，
    另外两条做得再好也白搭。聊天窗里给人看的是原文（那是用户自己的屏
    幕，他要看全文才能判断），打码只针对"会离开本机或落盘"的那几份。
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def for_log(text: str, *, log_commands: bool = False,
            command_length: int | None = None) -> str:
    """写进日志前的最后一道处理。

    总是先脱敏（关掉 log_commands 也要脱敏——前 40 字照样可能含密钥）；
    ``log_commands=false``（**默认**）时进一步只留「长度 + 前 40 字」。

    默认值从这里改成 false：命令原文是用户这一侧最敏感的东西之一，
    而"默认就往磁盘写原文"是一个需要用户主动去改才安全的默认。

    ``command_length`` 是**完整命令**的字数（``commands.command_length`` 算的），
    由调用方从 RuleHit 上带下来。不传就退回数渲染行本身的长度——
    那个数含【严重】前缀与定位尾巴，而且命令早被截断过，
    跟卡片上"命令共 N 字"不是同一个数（B5 前的真机 bug：120 vs 268）。
    """
    safe = redact(text)
    if log_commands:
        return safe
    total = command_length if command_length is not None else len(text)
    return f"命令 {total} 字：{safe[:LOG_COMMAND_HEAD_CHARS]}"

# 会话短 id 的长度。全 UUID 有 36 字符，在一行播报里纯属噪音。
SESSION_SHORT_LEN = 8

UNKNOWN_EXECUTOR = "未知执行者"


def priority_for(severity: str) -> int:
    return SEVERITY_PRIORITY.get(severity, 5)


# ── 归因文案（2026-11-21 决策记录 §1/§2） ──────────────────────────────────
# 核心原则：主语位只放「可证的请求源」；执行者降级到括号里；拿不准就写未知。
# 用户会把播报的主语读成「发起者」，主语位一旦放执行者，就等于把锅扣给手——
# 真实事故：DSH spawn 的 claude -p 删了文件，播报说「claude-code 删除」，
# 用户第一反应是"我什么时候让 Claude 删东西了？"


def _split_summary(summary: str) -> tuple[str, str]:
    """把 ``删除文件：rm -rf x`` 拆成 ``("删除文件", "rm -rf x")``。

    规则层的 summary 一律是「短语：细节」形状，这里只切第一个冒号。
    没有冒号时整句当短语、细节为空——不猜。
    """
    phrase, separator, detail = summary.partition("：")
    if not separator:
        return summary.strip(), ""
    return phrase.strip(), detail.strip()


def _executor_of(hit: RuleHit) -> str:
    return hit.executor or hit.agent or UNKNOWN_EXECUTOR


def _short_session(session: str) -> str:
    return session[:SESSION_SHORT_LEN] if session else ""


def _session_of(hit: RuleHit) -> str:
    """显示用会话 id。优先 ``session``，缺失时退回 ``chain_id``。

    两者今天恒等（``chain_id`` 就是从源事件的 session 带过来的），但调用方
    可能只传了其中一个——退回一下，免得定位三件套因为一个漏传的 kwargs
    就少一块，而那正是用户 3 秒内自证的依据。
    """
    return hit.session or hit.chain_id


def describe_actor(*, executor: str, delegated_to: str | None = None,
                   requester: str | None = None, requester_evidence: str = "",
                   rule: str = "") -> str:
    """主语位放谁。四种情况，判据全部可证（决策记录 §2）。

    单独抽成函数而不是埋在 ``_subject`` 里：审批卡片（M0-lite-5）要用同一套
    语法，否则用户会在播报里看到"你的 Claude 让 worker 执行"、
    在卡片里又看到"DeepSeek 请求删除"，两套说法自相矛盾。

    ``rule`` 用来排除"下派观测"：那条规则的正文已经说明"通过 X 下派了一次任务"，
    主语再写"让 X 执行"就重复了。
    """
    # 贵脑 + 廉价手：本会话是大脑，它把活下派给了外部 worker。
    # 主语是大脑（用户对话的对象），worker 降级成"手"。
    if rule != RULE_DELEGATION and delegated_to:
        return f"{executor} 让 {delegated_to} 执行"

    # 父子代理：动手的是子代理，责任仍在父会话。
    if requester_evidence == EVIDENCE_SIDECHAIN:
        return f"{executor}（经子代理）"

    # 用户自己声明过这个目录里的会话是他开的——这是用户给的事实，不是猜测。
    if requester == REQUESTER_USER and requester_evidence == EVIDENCE_USER_DECLARED:
        return f"你自己开的 {executor}"

    return executor


def _subject(hit: RuleHit) -> str:
    return describe_actor(
        executor=_executor_of(hit),
        delegated_to=hit.delegated_to,
        requester=hit.requester,
        requester_evidence=hit.requester_evidence,
        rule=hit.rule,
    )


def _locator(hit: RuleHit) -> str:
    """定位三件套：会话短 id + 工作目录。让用户 3 秒内自己判断"这是我开的吗"。

    只有 cwd 时不要写成 `（@ C:\…）`——那个 @ 是给"会话 + 目录"配对用的，
    单独挂着很怪。缺什么就不写什么，不留空壳。
    """
    short = _short_session(_session_of(hit))
    cwd = hit.working_directory
    if short and cwd:
        return f"（会话 {short} @ {cwd}）"
    if short:
        return f"（会话 {short}）"
    if cwd:
        return f"（{cwd}）"
    return ""


# 主语以拉丁字符结尾时才补空格（"claude-code 删除文件"）；
# 以中文结尾时不补（"（经子代理）删除文件"、"…执行 删除文件"会多一个空）。
_NEEDS_SPACE = re.compile(r"[0-9A-Za-z_-]$")


def _glue(subject: str, phrase: str) -> str:
    return f"{subject} {phrase}" if _NEEDS_SPACE.search(subject) else f"{subject}{phrase}"


def _hedged(hit: RuleHit) -> bool:
    """要不要走「发起者未知」模版。

    按后果分级（决策记录 §2）：主语的可信度要与动作后果成正比。
    critical 不把主语交给推测；notice/warn 可以直接写可见的 agent——
    没人会为一条 429 限流播报质疑"是我让它请求的吗"。
    """
    return hit.severity == SEVERITY_CRITICAL and hit.attribution != ATTRIBUTION_KNOWN


def render_text(hit: RuleHit) -> str:
    """一行（严重时两行）给人看的文本。

    四种模版，见 ``_subject`` / ``_hedged``：
      1. 单 Agent        【严重】Claude Code 删除文件：rm -f …（会话 800257f3 @ D:\\…）
      2. 父子代理        【严重】Claude Code（经子代理）删除文件：…
      3. 贵脑+廉价手     【严重】Claude Code 让 deepseek-worker 执行删除：…
      4. 未知发起者      【严重】删除文件，发起者未知：rm -f …
                         由 Claude Code 会话 800257f3 执行 @ C:\\…\\Temp

    第 4 种是 critical 的默认。刻意**不写免责声明腔**（"无法确定发起者，请自行核实"）——
    那是把举证责任推给用户；写成陈述句加线索，用户自己就能在 3 秒内补上最后一跳。
    """
    label = SEVERITY_LABEL.get(hit.severity, SEVERITY_LABEL[SEVERITY_INFO])
    phrase, detail = _split_summary(hit.summary)
    body = f"{phrase}：{detail}" if detail else phrase

    if _hedged(hit):
        line = f"{label}{body}，发起者未知"
        trailer = f"由 {_executor_of(hit)}"
        short = _short_session(_session_of(hit))
        if short:
            trailer += f" 会话 {short}"
        trailer += " 执行"
        if hit.working_directory:
            trailer += f" @ {hit.working_directory}"
        return f"{line}\n{trailer}"

    return f"{label}{_glue(_subject(hit), body)}{_locator(hit)}"


class Broadcaster:
    """把 RuleHit 播出去。``push`` 由插件注入，便于单测时换成记录桩。"""

    def __init__(
        self,
        push: Callable[..., dict],
        logger: Any,
        *,
        source: str,
        coalescer: Any = None,
        log_commands: bool = False,
    ) -> None:
        self._push = push
        self._logger = logger
        self._source = source
        self._coalescer = coalescer
        self._log_commands = log_commands
        self.sent = 0
        self.rejected = 0
        self.coalesced = 0

    def broadcast(self, hit: RuleHit) -> str:
        """播一条。返回 ``sent`` / ``coalesced`` / 拒绝原因，永不抛异常。"""
        if self._coalescer is not None:
            kept = self._coalescer.offer(hit)
            if kept is None:
                self.coalesced += 1
                return "coalesced"
            hit = kept

        text = render_text(hit)
        try:
            result = self._push(
                source=self._source,
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": text}],
                priority=priority_for(hit.severity),
            )
        except Exception as exc:  # noqa: BLE001  播报失败绝不能炸掉观测循环
            self.rejected += 1
            self._logger.warning("播报抛出异常（{}）：{}", type(exc).__name__,
                                 for_log(text, log_commands=self._log_commands,
                                         command_length=getattr(hit, "command_length",
                                                               None)))
            return "exception"

        return self._handle_result(result, hit, text)

    def _handle_result(self, result: object, hit: RuleHit, text: str) -> str:
        # 聊天窗里给人看的是原文；落盘的一律过 for_log（脱敏 + 可选截断）
        logged = for_log(text, log_commands=self._log_commands,
                         command_length=getattr(hit, "command_length", None))
        if not isinstance(result, dict):
            self.rejected += 1
            self._logger.warning("播报返回了非预期类型 {}：{}", type(result).__name__, logged)
            return "unexpected"

        if result.get("submitted") is True:
            self.sent += 1
            self._logger.info("已播报 [{}] {}（累计 {} 条）", hit.severity, logged, self.sent)
            return "sent"

        reason = str(result.get("reason") or "unknown")
        self.rejected += 1
        # payload_too_large 是永久性问题，值得 warning；其余是瞬时拥塞，info 就够
        level = self._logger.warning if reason not in _PUSH_REJECTION_INFO else self._logger.info
        level("播报未被接受（{}）：[{}] {}", reason, hit.severity, logged)
        return reason
