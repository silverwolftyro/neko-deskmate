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
from dataclasses import replace
from typing import Any, Callable

from .events import (
    ATTRIBUTION_KNOWN,
    EVIDENCE_SIDECHAIN,
    EVIDENCE_USER_DECLARED,
    REQUESTER_USER,
)
from .progress import (
    LIGHT_ERROR,
    LIGHT_IDLE,
    LIGHT_RUNNING,
    LIGHT_WAITING,
    Role,
)
from .rules import (
    RULE_DELEGATION,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_NOTICE,
    SEVERITY_WARN,
    RuleHit,
)
from .tracker import Notice

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


# ── 推送的唯一出口（红线 1） ─────────────────────────────────────────────────
# 规则层（Broadcaster）与状态机层（broadcast_notice）都走 ``push_text``。
# **刻意只有一处**：红线 1 的四条铁律——显式 source、ai_behavior="blind"、
# 永不 await push_message、submitted=False 是正常分支——写成两份就一定会走岔，
# 而走岔那天没有任何测试会红，只会是真机上一条用角色口气说出来的假警报。

def push_text(push: Callable[..., dict], text: str, *, source: str, priority: int,
              logger: Any, tag: str = "", log_commands: bool = False,
              command_length: int | None = None) -> str:
    """把一段文本推去聊天窗。返回 ``sent`` / 拒绝原因，**永不抛异常**。

    ``tag`` 只进日志（``播报未被接受（backpressure）：[error] …``），不进正文。
    """
    try:
        result = push(
            source=source,
            visibility=["chat"],
            ai_behavior="blind",
            parts=[{"type": "text", "text": text}],
            priority=priority,
        )
    except Exception as exc:  # noqa: BLE001  播报失败绝不能炸掉观测循环
        logger.warning("播报抛出异常（{}）：{}", type(exc).__name__,
                       for_log(text, log_commands=log_commands,
                               command_length=command_length))
        return "exception"

    logged = for_log(text, log_commands=log_commands, command_length=command_length)
    if not isinstance(result, dict):
        logger.warning("播报返回了非预期类型 {}：{}", type(result).__name__, logged)
        return "unexpected"

    if result.get("submitted") is True:
        return "sent"

    reason = str(result.get("reason") or "unknown")
    # payload_too_large 是永久性问题，值得 warning；其余是瞬时拥塞，info 就够
    level = logger.warning if reason not in _PUSH_REJECTION_INFO else logger.info
    level("播报未被接受（{}）：[{}] {}", reason, tag, logged)
    return reason


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
        command_length = getattr(hit, "command_length", None)
        outcome = push_text(
            self._push, text,
            source=self._source,
            priority=priority_for(hit.severity),
            logger=self._logger,
            tag=hit.severity,
            log_commands=self._log_commands,
            command_length=command_length,
        )
        if outcome == "sent":
            self.sent += 1
            # 聊天窗里给人看的是原文；落盘的一律过 for_log（脱敏 + 可选截断）
            self._logger.info("已播报 [{}] {}（累计 {} 条）", hit.severity,
                              for_log(text, log_commands=self._log_commands,
                                      command_length=command_length), self.sent)
        else:
            self.rejected += 1
        return outcome


# ── 状态机播报（C 批 §三） ─────────────────────────────────────────────────

# 灯 → 宿主 priority。**放在这里而不是 tracker.py**：priority 是宿主的刻度，
# 而 tracker 是纯逻辑层，它连 SDK 都不导入。把宿主刻度塞进纯逻辑层，
# 下次换宿主就要连状态机一起改——那正是 C 批 §一 把契约层独立出来的理由。
NOTICE_PRIORITY = {
    LIGHT_ERROR: 7,
    LIGHT_WAITING: 6,
    LIGHT_RUNNING: 4,
    LIGHT_IDLE: 3,
}


def _light_key(light: object) -> str:
    """灯的字符串形式。

    ``Light`` 是 ``str`` 混血 Enum，``str(Light.ERROR)`` 在不同 Python 版本
    可能给出 ``"error"`` 也可能给出 ``"Light.ERROR"``——``__str__`` 的 MRO
    在 ``str`` 与 ``Enum`` 之间争位，3.11 前后行为不一致。所以**不赌
    ``str()``**，直接取 ``.value``；不是 Enum 时退回 ``str()``。

    这个坑在本项目已经付过一次学费：``notify_enabled`` 曾用 ``is`` 比较
    ``str`` 混血 Enum，配置传来的裸字符串于是永远匹配不上。
    """
    value = getattr(light, "value", None)
    return value if isinstance(value, str) else str(light)


# ── 同工作目录合并（P1c(b)）───────────────────────────────────────────────
#
# 2026-11-24 真机：同一目录下三个会话同时出事，连发三条
# 「X 出事了 / Bash 执行失败，它没自己恢复」——**文案一字不差**。
# 三条相同的通知不是三倍的信息，是三分之一的信息：用户读到第 2 条就开始无视，
# 第 3 条一定无视，然后连第 1 条也一起无视。
#
# ⚠️ 与 B12 P0-2 删掉的东西**不是一回事**，不要因为怕回到老路就不做：
# 当年删的是「有 N 条待判断」那种会 0→1→0 抖动的抽象计数；
# 这里是一次性真实批量（n 个会话确实同时因同一原因失败），
# 合并符合「精度 > 召回」。
MERGE_SUFFIX = "（同目录另有 {count} 个会话）"

# 跨批次合并的窗口（秒）。**取值依据写在这里，不许拍脑袋**：
# 三类 OUTCOME 通知本来就已经晚于设计——「出事了」≥ failure_confirm_seconds(120s)、
# 「未回包」≥ unclosed_action_seconds(300s)、「兜圈子」虽即时但只在同一类别
# 连续失败 3 次后触发。所以再压 20s 的相对代价是 7%~17%，
# 换来的是"同目录多个会话同时失败只响一次"。
# 填 0 = 不延迟（通知当轮就发），但**同一轮内的合并仍然生效**——
# 那是 0.5.0 真机验收确认过的行为（同一个 3 秒轮询批次里落地的通知
# 本来就是同时的，合掉它们没有代价）。
NOTICE_MERGE_SECONDS = 20.0


def merge_key(notice: Notice) -> tuple[str, str] | None:
    """这条通知该进哪个合并桶。**None = 不参与合并。**

    两个条件，少一个都不行：
      * **只合 OUTCOME**（出事了 / 没有回包 / 在兜圈子）——四角色里唯一会在
        多会话间一字不差重复的一类。WAITING 指名的是"哪个会话在等你"，
        合起来就答不了这个问题了；
      * **只合真有工作目录的**。``Notice.who`` 在拿不到目录时会退回 agent 名，
        而 ``claude-code`` 是**所有**无目录会话的共同退回值——按它合并等于把
        一堆互不相干的会话揉成一条，那是用漏报换简洁，而且「同目录另有 N 个
        会话」在没有目录时是一句假话。所以判据是 ``notice.workspace``，
        不是 ``notice.who``。
    """
    if notice.role != Role.OUTCOME or not notice.workspace:
        return None
    return (notice.workspace, notice.kind)


def coalesce_same_workspace(notices: list[Notice]) -> list[Notice]:
    """同一工作目录、同一工具类别的通知合成一条，其余折进第二行的尾巴。

    合并规则只有这一份真相（``merge_key`` + ``_merge_detail``），
    ``NoticeMerger`` 复用它，只额外加"时间"这一个维度。

    返回新列表，不改入参（tracker 那边每轮都会重建 notices，但函数签名
    不该暗示"我会动你的数据"）。
    """
    order: list[Notice] = []
    # 桶 → 它在 ``order`` 里的位置。**必须记位置而不是只记 Notice**：
    # 早先的写法把合并结果塞回 dict、却让 ``kept`` 留着原对象，
    # 于是"合并"静默地什么都没做——通知照旧三条，一条不少。
    index: dict[tuple[str, str], int] = {}
    for notice in notices:
        bucket = merge_key(notice)
        if bucket is None:
            order.append(notice)
            continue
        position = index.get(bucket)
        if position is None:
            index[bucket] = len(order)
            order.append(notice)
            continue
        # 同目录同类别：把这条折进第一条的尾巴。用 dataclasses.replace
        # 而不是改字段——Notice 是 frozen 的，而且"改调用方的对象"
        # 正是这类合并最容易出的 bug。
        first = order[position]
        order[position] = replace(first, detail=_merge_detail(first, notice))
    return order


def _merge_detail(first: Notice, extra: Notice) -> str:
    """把 ``extra`` 折进 ``first`` 的第二行。

    从 ``first.detail`` 里已有的尾巴续号，而不是另起一个计数器——
    三个会话时第一条先变成「…（同目录另有 1 个会话）」，再来第三条要变成
    「…（同目录另有 2 个会话）」，不是「…（同目录另有 1 个会话）（同目录另有 1 个会话）」。
    """
    count = _merged_count(first.detail) + 1
    detail = _MERGE_TAIL.sub("", first.detail).rstrip()
    return f"{detail}{MERGE_SUFFIX.format(count=count)}"


def _merged_count(detail: str) -> int:
    """第二行尾巴上已经合并了几条。没有尾巴就是 0。"""
    match = _MERGE_TAIL.search(detail)
    return int(match.group("count")) if match else 0


_MERGE_TAIL = re.compile(r"（同目录另有 (?P<count>\d+) 个会话）\s*$")


class NoticeMerger:
    """同目录通知的**跨批次**合并器。

    第一版只有 ``coalesce_same_workspace``，它在 ``_poll_once`` 里对**本轮**
    notices 合并。真机验收（2026-11-24 测试 8）证明这不够：6 个会话 3 秒内
    全部失败，而「出事了」的播出时间是各自首次失败 +120s，只要跨过 3 秒轮询
    相位就落进不同批次 → 屏幕收到 **3 条**而不是 1 条（7 条失败按 4/2/1 分组）。

    **为什么必须延迟，而不能"把后来那条折进先前那条"**：已经发出去的聊天
    消息没有编辑入口——``push_message`` 不返回可改的消息 id。所以计数在物理上
    无处可去，只能等窗口关闭一起发。真机验收（2026-11-24）在"延迟带数量"和
    "立即不带数量"之间选了前者，代价是最晚一条晚一个窗口。

    **实现上刻意复用 ``coalesce_same_workspace``**：合并规则只有一份真相，
    这个类只负责"时间"这一个维度。规则写在两处的后果，就是下次改文案时
    只改到一处——而另一处静默地留着旧文案。
    """

    def __init__(self, window: float = NOTICE_MERGE_SECONDS) -> None:
        self._window = max(0.0, float(window))
        # 桶 → (首次收入时刻, 该桶累计的通知)。存"首次收入时刻"而不是到期时刻，
        # 这样 set_window 改窗口时能重算所有到期时间——否则用户把窗口调成 0，
        # 也救不回已经压在手里的那几条（它们会按旧窗口再等一轮）。
        self._groups: dict[tuple[str, str], tuple[float, list[Notice]]] = {}

    def __len__(self) -> int:
        return sum(len(group) for _, group in self._groups.values())

    def set_window(self, window: float) -> None:
        """改窗口。**已经压在手里的条目按新窗口重算到期时间**——
        调成 0 应该立刻恢复"当轮就发"，而不是让那几条再等一个旧窗口。

        注意 0 **不等于"不合并"**：同一轮批次内的合并照旧（见
        ``NOTICE_MERGE_SECONDS`` 的注释）。要的是"不延迟"，不是"不合"。
        """
        self._window = max(0.0, float(window))

    def offer(self, notices: list[Notice], now: float) -> list[Notice]:
        """收入一批通知，返回**此刻该发出去**的那些。

        **先把过期组放出去，再收新通知**。顺序反了会出这种事：轮询卡了
        一会（某个观测源抛异常被兜住、宿主忙），一个早就过了窗口的组还压在
        手里，新来的通知挤进它 → 那条新通知被旧窗口拖着一起晚发。
        生产里轮询是 3 秒，最多晚 3 秒；但这个顺序不该靠"轮询够勤"来保证。

        不参与合并的（``merge_key`` 为 None）当轮直接返回，不进桶——
        「在等你」指名的是哪个会话，压它一个窗口没有任何好处。
        """
        ready = self._release(now)
        for notice in notices:
            bucket = merge_key(notice)
            if bucket is None:
                ready.append(notice)
                continue
            held = self._groups.get(bucket)
            if held is None:
                self._groups[bucket] = (now, [notice])
            else:
                held[1].append(notice)
        # 再放一次：刚收入的也可能立刻就到期（窗口 = 0 时全部如此）。
        ready.extend(self._release(now))
        return ready

    def _release(self, now: float) -> list[Notice]:
        """把到期组合成一条发出去。按首次收入时间排序，先攒的先发。"""
        ready: list[Notice] = []
        for bucket in sorted(self._groups, key=lambda b: self._groups[b][0]):
            first_at, group = self._groups[bucket]
            if first_at + self._window > now:
                continue
            ready.extend(coalesce_same_workspace(group))
            del self._groups[bucket]
        return ready

    def flush(self, now: float) -> list[Notice]:
        """不等窗口了，全部发出。**停机/重载时调**——把用户的通知吞在手里
        比晚发严重得多（这个项目的一条硬规矩：一条都不许丢）。"""
        ready: list[Notice] = []
        for bucket in sorted(self._groups, key=lambda b: self._groups[b][0]):
            ready.extend(coalesce_same_workspace(self._groups[bucket][1]))
        self._groups = {}
        return ready


def broadcast_notice(push: Callable[..., dict], notice: Notice, *, source: str,
                     logger: Any) -> str:
    """播一条状态机 Notice。返回 ``sent`` / 拒绝原因，**永不抛异常**。

    **刻意不与 Broadcaster 合体**：规则层播 RuleHit（要脱敏、要合并、要按
    严重度定优先级），状态机播 Notice（tracker 已经按 fingerprint 去过重、
    已经定好两行、优先级按灯定）。硬塞进一个类，两边都会改不动。

    ``log_commands=True`` 不是"这是命令"，而是"这条要整条落盘"：通知不是命令，
    走默认的 40 字截断会得到 ``命令 47 字：…`` 这种前言不搭后语的日志，
    而 Notice 全文也就两行、每行有 120 字上限。脱敏仍然照跑。

    **正文不脱敏**（既定策略：聊天窗给人看原文，for_log 管落盘那份）。
    但 ``Notice.detail`` 将来会装 ``WAITING`` 的自由文本理由——等
    ``PendingApproval → WAITING`` 桥接上之前，**必须先在桥接处过 redact()**，
    否则 agent 的等待理由能把任何东西带出来。见任务书待办。
    """
    text = "\n".join(notice.lines())
    key = _light_key(notice.light)
    return push_text(
        push, text,
        source=source,
        priority=NOTICE_PRIORITY.get(key, 5),
        logger=logger,
        tag=key,
        log_commands=True,
    )
