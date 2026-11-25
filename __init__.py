"""同桌猫（agent_deskmate）—— N.E.K.O 被动监听插件。

基本版（M0-lite）路线：骨架能加载 → Claude Code 转录 tail + 网关元数据观测 →
L0 规则层 → SDK 播报 → 审批中继 → 零 key 行为。
当前进度：**M0-lite-4 播报**（观测轮询跑在 3 秒 timer 上，scan_now 是只读诊断入口）。

三条红线（详见 AGENTS.md）：
  1. 播报只能走 push_message(source=..., visibility=[...], ai_behavior=...)，
     source 必须显式，纯播报必须 ai_behavior="blind"（默认 respond 会让角色开口）；
  2. 审批交互只能走 create_card + data-neko-action；
  3. 零 key 只跑 L0 规则层，解释/建议类必须提示"需配 key"。

宿主约束（AGENTS.md，违反会返工）：
  - 只 import plugin.sdk.plugin，不碰 utils.* / plugin.core / plugin.sdk.shared；
  - 不用 print，日志走 self.logger（{} 占位符风格）；
  - 不硬编码路径：被观测目录/数据库来自配置，代码只做 expanduser；
  - startup 快进快出（默认 10s 预算，返回时它创建的任务会被取消）；
  - 永不 await push_message——它入队后就会抛 TypeError，重试等于发两条。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from plugin.sdk.plugin import (
    CardSubmissionError,
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
    ui,
)

from . import hookhealth
from .adapters import ClaudeCodeAdapter, GatewayAdapter
from .adapters.claude_code import (
    CwdCache,
    iter_transcript_files,
    read_new_transcript_events,
)
from .adapters.discovery import discover_agents
from .adapters.gateway import GatewayReadError, current_watermark, read_new_gateway_events
from .adapters.sessions import (
    gateway_app_types,
    glance_gateway_sessions,
    glance_transcript_sessions,
)
from .approval import (
    ADVICE_STATE_FALLBACK,
    ADVICE_STATE_READY,
    APPROVE_ALLOW,
    AUTH_SCOPES,
    AUTHORIZATION_HEADLINE,
    AUTO_LOW_RISK_ONLY,
    AUTO_OFF,
    CARD_CSS,
    DECIDED_BY_AI,
    DECIDED_BY_USER,
    DEFAULT_WARNING_FIRST_N,
    VERBOSITIES,
    VERBOSITY_STANDARD,
    ApprovalGateway,
    WarningPolicy,
    advice_text,
    auto_allow_notice,
    auto_allow_setting_line,
    build_authorize_actions,
    build_authorize_html,
    build_card_actions,
    build_card_html,
    build_explain_html,
    build_result_html,
    explain_request,
    is_known_advice_state,
    safe_conclusion,
    warning_key,
    warning_mode_for_tier,
)
from .auto_review import AutoReviewer
from .broadcast import (
    NOTICE_MERGE_SECONDS,
    Broadcaster,
    NoticeMerger,
    broadcast_notice,
    describe_actor,
    render_text,
)
from .commands import (
    COMMAND_DISPLAY_MAX,
    CONCLUSION_UNSURE,
    command_length,
)
from .events import (
    AGENT_CLAUDE_CODE,
    EVENT_TOOL_RESULT,
    UnifiedEvent,
    attribute,
    delegated_server,
)
from .gated import (
    GATED_DEGRADED_WINDOW_SECONDS,
    MODE_DEGRADED,
    GatedFailureLog,
)
from .hookhealth import classify_hook, refine_with_relay
from .hooksetup import (
    install_hook,
    installed_hook,
    settings_path,
    source_relay_path,
    sync_relay,
    uninstall_hook,
)
from .model_advice import (
    STATE_AUTH_FAILED,
    STATE_BAD_RESPONSE,
    STATE_DISABLED,
    STATE_ERROR,
    STATE_INVALID,
    STATE_NO_KEY,
    STATE_OK,
    STATE_TIMEOUT,
    STATE_UNAUTHORIZED,
    ModelAdvisor,
)
from .panel_state import build_panel_state
from .progress import signal_from_event
from .rules import (
    Coalescer,
    RuleHit,
    evaluate_delegation,
    evaluate_delete,
    evaluate_failure,
    evaluate_transcript_error,
)
from .tracker import DEFAULT_CONFIG, Notice, ProgressTracker, TrackerConfig
from .warncount import COUNTS_FILENAME

# 聊天里显示的来源标签。红线 1：source 必须显式传，不能用默认空串。
SOURCE = "agent_deskmate"

# 当前阶段标记，原样出现在 status 入口的 ``stage`` 字段里。
# **阶梯是规划期的产物，别把它当成"功能清单"读**——阶梯之外还做了
# A 批（审批可信度）与 B 批（闸门与观测），见 README 的状态表。
# 改这个值之前先看 README：两边对不上，用户会以为插件降级了。
STAGE = "M0-lite-6"

# dump_events 读转录时往回扫的字节数。转录单文件可到数 MB，全量倒进来没意义。
TRANSCRIPT_TAIL_BYTES = 65536

# 单轮最多播几条，防止适配器积压后一次刷屏。
# 超出的部分进 ``_pending_pushes`` 队列，**下一轮接着发，不是丢掉**。
MAX_BROADCAST_PER_TICK = 5

# 积压队列的硬顶。第一道防线是 Coalescer（把 100 条相似删除合成一两条），
# 这个队列只是兜持续洪水的。超顶时丢**最旧**的——正在发生的动作比
# 30 秒前发生过的更值得被看见，而两边都还在审计里可查。
MAX_PENDING_PUSHES = 50

# 单个观测源连续失败时，日志最短间隔多少秒（B4）。
# 网关库锁是**持续状态**：不限制的话每 3 秒一条 warning，
# 日志会比播报还吵，真正要看的删除记录被冲掉。
SOURCE_ERROR_LOG_EVERY = 60.0

# scan_now 每个数据源最多回看多少条事件。只读诊断，够看出规则对不对就行。
SCAN_EVENT_COUNT = 50

# scan_now 回显几条"将要播报"的原文，超过就截断。
SCAN_SAMPLE_LIMIT = 3

# 「审批挂起已解除」的记账保留多久。用来给同一张卡只释放一次——
# approval.snapshot() 按 retention（默认一小时）留着已了结记录，
# 不记账的话同一会话每分钟又被"释放"一次。取两小时是留足余量，
# 超时的条目对释放早就没有意义了（动作要么已闭环、要么已作废）。
GATE_RELEASE_MEMORY_SECONDS = 7200.0

# 审批中继默认端口。装进 Claude Code settings 的那条 command hook 用它，
# 改它之后必须重跑 hook_install，否则中继还在打旧端口（启动时会核对并报警）。
HOOKS_DEFAULT_PORT = 48921
# Claude Code 的 hook 超时（官方默认 600s）。我们的到期时间必须明显小于它，
# 否则我们还没到期宿主先断开，pending 永远等不到标记。
HOOK_TIMEOUT_SECONDS = 600
# 审批默认到期：hook 600s 前 30 秒。只标记 expired，不发任何决定。
DEFAULT_TIMEOUT_SECONDS = 570.0
DEFAULT_RETENTION_SECONDS = 3600.0


def code_fingerprint(plugin_dir: Any) -> str:
    """当前加载的代码指纹。规格 §17.6：用户在插件管理器里看不出跑的是哪一版。

    两条路，**都不假设 git 存在**（打包后没有 .git，git 也可能不在 PATH）：

    1. 有 .git 且 git 可用 → ``git rev-parse --short HEAD``
    2. 否则 → 插件自己的 .py 文件内容哈希（排序后拼起来算，保证稳定）

    刻意不 import hashlib/subprocess 到 async 函数里：本函数是同步的，
    在 startup 里被调用。subprocess 放在同步函数里也顺带避开 ASYNC 规则。
    """
    root = Path(plugin_dir)
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    digest = hashlib.sha256()
    try:
        for path in sorted(root.glob("*.py")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    except OSError:
        return "unknown"
    return "sha256:" + digest.hexdigest()[:12]


def _escape(text: str) -> str:
    """通用 HTML 转义。工具参数是 Agent 产的，不能当可信 HTML 直接拼。"""
    return _html.escape(str(text), quote=True)


class StatusReply(BaseModel):
    """status 入口的输出契约。返回模型实例，不要返回 dict。"""

    status: str = Field(description="running / stopped")
    plugin_id: str = Field(description="插件 id，恒为 agent_deskmate")
    source: str = Field(description="播报来源标签，恒为 agent_deskmate")
    stage: str = Field(description="当前所处的 M0-lite 阶段")
    uptime_sec: float | None = Field(default=None, description="运行秒数；未启动为 null")
    adapters: dict[str, str] = Field(default_factory=dict, description="各观测适配器的就绪状态")
    approval: dict[str, int] = Field(
        default_factory=dict,
        description="审批中继状态：running / pending / allow / deny / ai / expired",
    )
    authorization: dict | None = Field(
        default=None,
        description="授权状态：granted / scopes / version / confirmed_time。"
                    "审批服务未运行时为 null",
    )
    build: str = Field(default="", description="代码指纹：git short sha 或内容哈希")
    hook: dict = Field(
        default_factory=dict,
        description="审批 hook 装配状态：installed / settings_file / port",
    )
    model: dict = Field(
        default_factory=dict,
        description="模型建议配置状态：enabled / configured / model / protocol。"
                    "**不含 api_key**",
    )


class DumpEventsReply(BaseModel):
    """dump_events 入口的输出契约。"""

    count: int = Field(description="实际返回的事件条数")
    events: list[dict] = Field(description="统一事件列表，最新在最后")
    sources: dict[str, str] = Field(default_factory=dict, description="各数据源的读取情况")


class PendingListReply(BaseModel):
    """pending_list 入口的输出契约：未决 + 已了结未删的。"""

    count: int = Field(description="返回的条数")
    pending: int = Field(description="其中还没决定的条数")
    items: list[dict] = Field(description="最新在前")


class AuditListReply(BaseModel):
    """audit_list 入口的输出契约。"""

    count: int = Field(description="返回的条数")
    items: list[dict] = Field(description="最新在前")
    text: str = Field(description="同一份审计的纯文本，便于直接复制导出")


class ScanNowReply(BaseModel):
    """scan_now 入口的输出契约。只读诊断：一次也不发送播报。"""

    events_read: int = Field(description="本轮实际检查的统一事件条数")
    hits: int = Field(description="规则命中条数（未去重）")
    broadcasts: int = Field(description="去重后预计会播报的条数")
    top_rules: list[dict] = Field(description="命中规则分布，按条数降序，如 [{'rule': 'delete', 'count': 3}]")
    samples: list[str] = Field(description="最多 3 条将要播报的原文，用于肉眼确认")
    sources: dict[str, str] = Field(default_factory=dict, description="各数据源的读取情况")


def _expand(path_value: object) -> Path | None:
    """展开配置里的 ~ 和环境变量；**没配置就返回 None**，不返回 Path()。

    不能返回 ``Path()``：它等于 ``.``，而 ``Path(".").is_dir()`` 是真的。
    于是缺配置会静默变成"观测当前工作目录"——插件进程的 cwd 是宿主的目录，
    那会扫出一堆无关 jsonl，还会打出"适配器就绪"的假日志。
    （这个坑是 test_scan 的缺配置用例当场抓到的。）
    """
    text = str(path_value or "").strip()
    if not text:
        return None
    return Path(os.path.expanduser(os.path.expandvars(text)))


def _to_number(value: object, default: float) -> float:
    """配置里的数值容错转换；坏值退回默认，不让观测循环崩。"""
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _to_bool(value: object, default: bool) -> bool:
    """配置里的布尔容错转换。只认显式的 true/false，其余（含 None）退回默认。"""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return default


def _watch_section(config: dict[str, Any]) -> dict[str, Any]:
    """从 ``[agent_deskmate]`` 整节里取 ``watch`` 子表；没有就是空 dict。"""
    section = config.get("watch")
    return section if isinstance(section, dict) else {}


def _approval_section(config: dict[str, Any]) -> dict[str, Any]:
    """从 ``[agent_deskmate]`` 整节里取 ``approval`` 子表（规格 §5）。"""
    section = config.get("approval")
    return section if isinstance(section, dict) else {}


def _model_section(config: dict[str, Any]) -> dict[str, Any]:
    """从 ``[agent_deskmate]`` 整节里取 ``model`` 子表（5b：模型建议）。

    刻意不写死供应商：``protocol`` 只有 openai / anthropic 两种形状，
    ``base_url`` 指向哪里是用户的事。默认走 DeepSeek 官方只是因为
    本机现成有 key，不是偏好。
    """
    section = config.get("model")
    return section if isinstance(section, dict) else {}


def _tracker_section(config: dict[str, Any]) -> dict[str, Any]:
    """从 ``[agent_deskmate]`` 整节里取 ``progress`` 子表（C 批 §三）。

    键名叫 ``progress`` 而不叫 ``tracker``：对用户说的是"进度播报"，
    ``tracker`` 是实现里的类名。配置键不该泄露实现细节——今天叫 tracker，
    明天换掉实现，用户的配置文件不该跟着一起失效。
    """
    section = config.get("progress")
    return section if isinstance(section, dict) else {}


def _tracker_config(section: dict[str, Any]) -> TrackerConfig:
    """配置子表 → TrackerConfig。缺键取默认，坏值退回默认（不让观测循环崩）。

    刻意**不做 clamp**：这些阈值是"多久没动静算异常"，用户填 0 或填 10^9
    只影响他自己看到的播报密度，没有安全后果。真正决定"会不会误报"的是
    真机 dry run 的实测（136 → 21 条），不是配置校验。

    ``retry_escalate_at`` 是元组、**刻意不进配置**：它是"连续失败几次算兜圈子"
    的阶梯，要从 TOML 数组来就得写数组解析 + 去重 + 排序 + 防空，
    而这些校验做一半比不做更危险（写 ``[5, 3]`` 到底算几次？）。
    真机数据（93 次响亮失败里绝大多数一次就过）不支持用户去调它。
    """
    return TrackerConfig(
        wait_escalate_seconds=_to_number(
            section.get("wait_escalate_seconds"), DEFAULT_CONFIG.wait_escalate_seconds),
        unclosed_action_seconds=_to_number(
            section.get("unclosed_action_seconds"), DEFAULT_CONFIG.unclosed_action_seconds),
        boundary_round_seconds=_to_number(
            section.get("boundary_round_seconds"), DEFAULT_CONFIG.boundary_round_seconds),
        fail_coalesce_seconds=_to_number(
            section.get("fail_coalesce_seconds"), DEFAULT_CONFIG.fail_coalesce_seconds),
        failure_confirm_seconds=_to_number(
            section.get("failure_confirm_seconds"), DEFAULT_CONFIG.failure_confirm_seconds),
    )


# 「调用结果码 → 卡片展示状态」的显式映射。
# 只有这里做这件事（见 _advice_for_card 的文档）。
# 刻意**穷举** model_advice 的每个 STATE_*：漏一个就意味着那个失败档位
# 会落到兜底态——功能上安全（卡片照样出），但用户看不到具体原因。
# 所以漏映射时 _degraded_advice 会记 warning，测试也会盯住这张表覆盖全。
_DISPLAY_STATE_FOR_OUTCOME = {
    # 成功：卡片渲染「建议 + 理由 + 依据」三行。内容由 _advice_for_card 填，
    # 状态名走这张表——保证"成功对应哪个展示态"也只有一处定义。
    STATE_OK: ADVICE_STATE_READY,
    STATE_NO_KEY: "no_key",
    STATE_AUTH_FAILED: ADVICE_STATE_FALLBACK,
    STATE_TIMEOUT: ADVICE_STATE_FALLBACK,
    STATE_INVALID: ADVICE_STATE_FALLBACK,
    STATE_BAD_RESPONSE: ADVICE_STATE_FALLBACK,
    STATE_DISABLED: ADVICE_STATE_FALLBACK,
    STATE_UNAUTHORIZED: "unauthorized",
    STATE_ERROR: ADVICE_STATE_FALLBACK,
}


def _command_of(request: Any) -> str:
    """从审批请求里取**完整**命令原文。

    2026-11-22 A 批之前这里是从 ``tool_summary`` 反推的：摘要被截到
    120 字，于是模型与卡片都只看得到命令的开头，而风险判定读的是全文——
    一条 150 字的命令在末尾挂 ``&& rm -rf ~``，判定看得见、人和模型都
    看不见。现在 ``ApprovalRequest`` 上直接带一份 ``full_command``，
    这里优先用它，不再有人从截断文本里反推。

    回退分支留给不带的调用方（老测试、别的构造路径）：宁可少一截，
    不要切错——所以仍按"工具名:"前缀剥一次。
    """
    full = str(getattr(request, "full_command", "") or "")
    if full.strip():
        return full
    summary = str(getattr(request, "tool_summary", "") or "")
    name = str(getattr(request, "tool_name", "") or "")
    if name and summary.startswith(f"{name}:"):
        return summary[len(name) + 1:].strip()
    return summary


def _verbosity_of(config: dict[str, Any]) -> str:
    """「这是什么」讲多细。认不出的值按 standard——不猜用户想要多细。"""
    raw = str(_approval_section(config).get("explain_verbosity")
              or VERBOSITY_STANDARD).strip().lower()
    return raw if raw in VERBOSITIES else VERBOSITY_STANDARD


def _advisor_from_config(config: dict[str, Any]) -> ModelAdvisor:
    """按配置造一个模型建议客户端。

    ``api_key`` 的读取顺序：``[agent_deskmate.model] api_key`` 优先，
    回退到旧的 ``[agent_deskmate.approval] api_key``（5a 时的位置）。
    不兼容会让老配置静默失效——那种"配了却不生效"最难查。
    """
    section = _model_section(config)
    approval = _approval_section(config)
    api_key = str(section.get("api_key") or approval.get("api_key") or "").strip()
    return ModelAdvisor(
        protocol=str(section.get("protocol") or "openai").strip().lower(),
        base_url=str(section.get("base_url") or "").strip(),
        api_key=api_key,
        model=str(section.get("model") or "").strip(),
        # 开关仍在 approval 节：ai_judgement 是"要不要建议"，
        # model 节只描述"连哪个模型"。拆开是为了不让一处配置管两件事。
        enabled=_to_bool(approval.get("ai_judgement"), False),
    )


def _reviewer_from_config(config: dict[str, Any]) -> AutoReviewer | None:
    """按配置造 B9 的自动审查客户端。**没配就返回 None**。

    返回 None 而不是"配一个 enabled=False 的"：那会让 ``_maybe_auto_allow``
    每次都要多判一次开关，而且状态行说不清"没接"和"关了"的区别。
    没接审查器时，B9 这条路整个不存在。

    复用 ``[agent_deskmate.model]`` 的连接信息（同一个模型端点），
    但**开关独立**：``[agent_deskmate.auto_review] enabled``。
    模型建议开着不代表要开自动审查——前者只说话，后者真的会放行。
    """
    section = _model_section(config)
    review = config.get("auto_review")
    review = review if isinstance(review, dict) else {}
    if not _to_bool(review.get("enabled"), False):
        return None
    api_key = str(review.get("api_key") or section.get("api_key") or "").strip()
    reviewer = AutoReviewer(
        protocol=str(review.get("protocol") or section.get("protocol")
                     or "openai").strip().lower(),
        base_url=str(review.get("base_url") or section.get("base_url") or "").strip(),
        api_key=api_key,
        model=str(review.get("model") or section.get("model") or "").strip(),
        enabled=True,
    )
    gap = reviewer.missing()
    if gap:
        # 配了开关却缺东西：不当"没配"，要说明缺什么。
        # 静默忽略的后果是用户以为审查在跑，其实一次都没发生过。
        _REVIEWER_GAP[0] = gap
        return None
    _REVIEWER_GAP[0] = ""
    return reviewer


# 装配 B9 时缺的那一样。模块级而不是 self 上：_start_approval_service 里
# 要用它打日志，而那时 self._approval 还没建起来。
_REVIEWER_GAP: list[str] = [""]


def _mine_roots(config: dict[str, Any]) -> list[Path]:
    """读用户声明的"这些目录里的会话是我自己开的"（``[agent_deskmate.mine].roots``）。

    这是**用户给的事实**，不是插件的猜测，所以它可以当归因依据（决策记录 §2 逃生口）。
    没声明 → 空列表 → 一切按「发起者未知」处理。
    """
    mine = config.get("mine")
    raw = mine.get("roots") if isinstance(mine, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    roots: list[Path] = []
    for item in raw:
        path = _expand(item)
        if path is not None:
            roots.append(path)
    return roots


# ── 只读诊断（scan_now） ────────────────────────────────────────────────────
# 这一整块刻意与常驻轮询的状态隔离，理由见 diagnose() 的文档字符串。
# 放在模块级而不是类方法里，是为了能在没有宿主 ctx 的情况下单测。

def _scan_transcript_tail(projects_dir: Path, count: int) -> list[UnifiedEvent]:
    """只读扫一遍**最新**转录文件的尾部。

    只看最新一个文件：常驻轮询按 mtime 扫多个文件，但诊断要的是"刚才发生了什么"，
    那永远在最新的文件里。全扫多个文件既慢又没必要。
    """
    files = iter_transcript_files(projects_dir)
    if not files:
        return []
    newest = files[0]
    size = newest.stat().st_size
    offset = max(0, size - TRANSCRIPT_TAIL_BYTES)
    events, _ = read_new_transcript_events(newest, offset)
    # B8：诊断也要走同一条 cwd 线索，否则"预计播 N 条【严重】"
    # 与常驻轮询实际播的【警告】对不上——诊断自己就成了第二套判据。
    # 用一次性缓存（这一批内部沿用即可，跨批次不借）。
    return CwdCache().fill(events)[-count:]


def _scan_gateway_tail(db_path: Path, count: int) -> list[UnifiedEvent]:
    """按 rowid 回看最后 ``count`` 行（一次性，不动常驻水位）。"""
    watermark = current_watermark(db_path)
    events, _ = read_new_gateway_events(db_path, max(0, watermark - count), limit=count)
    return events[-count:]


def _hits_for_event(event: UnifiedEvent, mine_roots: list[Path] = ()) -> list[RuleHit]:
    """一个事件跑当前阶段所有规则。先归因，再判规则。

    删除类用完整命令判（summary 被截断，会漏掉长命令里的 rm）；
    失败类按真实状态码定严重度，不从 summary 文本反解析。
    ``source`` 把归因字段带进 RuleHit——不传就是「未知」，规则层绝不自己猜。
    """
    attributed = attribute(event, mine_roots=mine_roots)
    hits: list[RuleHit] = []

    if attributed.event == "tool_use":
        # MCP 下派与删除类互斥：MCP 工具没有 Bash 的 command 字段，
        # 删除规则本来就无从判起。先看下派，命中就不再判别的。
        if delegated_server(attributed.tool):
            hit = evaluate_delegation(attributed)
            if hit is not None:
                hits.append(hit)
        elif attributed.command:
            hit = evaluate_delete(
                attributed.command,
                agent=attributed.agent, session=attributed.session, ts=attributed.ts,
                source=attributed,
            )
            if hit is not None:
                hits.append(hit)

    elif attributed.event == "error":
        if attributed.source_type == "proxy_request_logs":
            hit = evaluate_failure(
                status_code=attributed.status_code,
                error_message=attributed.summary,
                agent=attributed.agent, session=attributed.session, ts=attributed.ts,
                source=attributed,
            )
        else:
            hit = evaluate_transcript_error(attributed)
        if hit is not None:
            hits.append(hit)

    return hits


def diagnose(config: dict[str, Any], *, log=None,
             per_source: int = SCAN_EVENT_COUNT) -> ScanNowReply:
    """读一遍观测源并跑规则，返回统计；**一次播报都不发送**。

    刻意不复用常驻轮询的三个状态（transcript offset、gateway rowid、合并器），
    复用会同时造成两个 bug：
      1) 把还没轮到的增量消费掉 → 3 秒后的 timer 看不到这些事件，**播报被吞**；
      2) 推进合并器的记账 → 同一条真播报反而被这次诊断**抑制**。
    所以这里自己读尾部，配一个一次性的合并器估算"会播几条"。

    因此 ``broadcasts`` 的语义是"这批事件若是新鲜的，会播几条"——
    它不知道常驻轮询已经播过什么，所以刚刚播过的同类事件这里可能仍计 1。
    这是有意的取舍：诊断宁可多说一条，也不能让宿主少播一条。
    """
    section = _watch_section(config)
    events: list[UnifiedEvent] = []
    sources: dict[str, str] = {}

    projects_dir = _expand(section.get("claude_projects_dir"))
    if projects_dir is not None and projects_dir.is_dir():
        events.extend(_scan_transcript_tail(projects_dir, per_source))
        sources["claude_code_transcript"] = str(projects_dir)
    else:
        sources["claude_code_transcript"] = "未配置或目录不存在"

    db_path = _expand(section.get("cc_switch_db"))
    if db_path is not None and db_path.is_file():
        events.extend(_scan_gateway_tail(db_path, per_source))
        sources["gateway_metadata"] = str(db_path)
    else:
        sources["gateway_metadata"] = "未配置或文件不存在"

    mine_roots = _mine_roots(config)
    coalescer = Coalescer(window_seconds=_to_number(config.get("coalesce_seconds"), 60.0))
    hits: list[RuleHit] = []
    broadcasts = 0
    samples: list[str] = []
    for event in events:
        for hit in _hits_for_event(event, mine_roots):
            hits.append(hit)
            if coalescer.offer(hit) is None:
                continue  # 一次性合并器：同键在窗口内只算一条
            broadcasts += 1
            if len(samples) < SCAN_SAMPLE_LIMIT:
                samples.append(render_text(hit))

    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.rule] = counts.get(hit.rule, 0) + 1
    top_rules = [
        {"rule": rule, "count": count}
        for rule, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    if log is not None:
        log.info("scan_now：事件 {} 条，命中 {} 条，预计播报 {} 条，来源 {}",
                 len(events), len(hits), broadcasts, sources)
    return ScanNowReply(
        events_read=len(events),
        hits=len(hits),
        broadcasts=broadcasts,
        top_rules=top_rules,
        samples=samples,
        sources=sources,
    )


@neko_plugin
class AgentDeskmatePlugin(NekoPluginBase):

    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._started_at: float | None = None
        self._watch_paths: dict[str, str] | None = None
        self._transcript_adapter: ClaudeCodeAdapter | None = None
        self._gateway_adapter: GatewayAdapter | None = None
        self._broadcaster: Broadcaster | None = None
        self._approval: ApprovalGateway | None = None
        self._hint_logged = False
        # 5b：模型建议客户端 + 跑它的线程池。advisor 为 None 表示零 key 默认态。
        self._advisor: ModelAdvisor | None = None
        self._advice_pool: ThreadPoolExecutor | None = None
        # **观测层的可变状态集中在这里创建**（B 批回归：曾把 _source_state
        # 只写在 on_config_change 里，于是每次重载后第一轮 poll 就
        # AttributeError，播报全停）。新加一个字段只许改这一个方法。
        self._reset_watch_state()

    def _reset_watch_state(self) -> None:
        """观测层的可变状态归零。``__init__`` 与配置变更都走这里。

        **必须在 ``__init__`` 里就调**：watch_tick 是 3 秒 timer，
        它不等配置变更就可能跑第一轮。少建一个字段的后果不是
        "某个功能没有"，而是那一轮整个抛异常、播报全停——
        而这种故障在日志里只是一行 AttributeError，极容易被忽略。
        """
        # 压在合并器手里的通知：重载/改配置时会被丢掉。**必须留一行**——
        # 这个项目的硬规矩是"一条都不许丢"，而这里是唯一一个会丢的地方
        # （跨批次合并的代价）。不记这一行，用户少了通知却查不到原因。
        held = getattr(self, "_merger", None)
        if held is not None and len(held):
            self.logger.warning(
                "重载时丢弃 %d 条压在合并窗口里的通知（最晚晚 %gs，"
                "跨批次合并的代价）", len(held), self._merge_window)
        self._watch_paths = None
        self._transcript_adapter = None
        self._gateway_adapter = None
        self._broadcaster = None
        # B4：各观测源上一次报错的时间，用于限频（见 ``_poll_source``）。
        self._source_error_at: dict[str, float] = {}
        # B6：各观测源的真实就绪状态（见 ``_adapter_status``）。
        self._source_state: dict[str, str] = {}
        # C 批 §三：进度/出错状态机。**建在这里而不是 _ensure_watchers**：
        # 后者开头有 `if self._broadcaster is not None: return` 的缓存早退，
        # 塞在它后半段的初始化会被缓存路径整条绕过，第一次 poll 就 AttributeError。
        self._tracker = ProgressTracker()
        self._tracker_cfg: TrackerConfig | None = None
        # 本轮没发完的推送。**这个字段修的是一个已发布 bug**：0.3.2 在
        # 条数上限处直接 `return`，而适配器的 offset 在那之前已经推进过了，
        # 于是日志写"余下留待下一轮"、实际那条通知永久消失。
        # 规则层丢的就是一条删除播报——不可接受。
        self._pending_pushes: list[Callable[[], str]] = []
        # P1c(b)：同目录通知的跨批次合并器。**建在这里**而不是 _ensure_watchers，
        # 理由同 _tracker——那条路有缓存早退，会被整条绕过。
        # 压在它手里的通知最晚晚一个窗口（默认 20s）发出；重载时会丢掉，
        # 见 _reset_watch_state 里那条 warning。
        self._merger = NoticeMerger()
        self._merge_window = NOTICE_MERGE_SECONDS
        # P1c：闸门自己造成的失败登记簿。**建在这里**而不是 _ensure_watchers，
        # 理由同 _tracker——那条路有缓存早退，会被整条绕过。
        # 审批网关起不起来都不影响它（起不来时这本簿子永远是空的，
        # 等于"没有自伤失败"，正是没有审批时的正确表现）。
        self._gated = GatedFailureLog()
        # P1b：已经"释放"过的审批 request_id → 首次见到它的时刻。
        # 同一张卡只释放一次（snapshot 会留着已了结记录一小时）。
        # 审批网关起不起来都不影响它（起不来时这本账永远是空的）。
        self._gate_released: dict[str, float] = {}

    # ── 生命周期 ───────────────────────────────────────────────────────────
    # startup 用 async def、shutdown 用 def，刻意对齐官方 memo_reminder 的写法：
    # 真机（Steam 版）日志里见过官方 app_launcher 报
    #   TypeError: object Ok can't be used in 'await' expression
    # 而它的 lifecycle 是同步 def 返回 Ok。本机源码树与 Steam 版不同版本，
    # 无法读 Steam 的 host（mypyc 编译成 .pyd），所以按最接近的样板对齐，
    # 剩下的靠真机验收确认。

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self._started_at = time.time()
        self.logger.info(
            "同桌猫启动 plugin_id={} source={} stage={} 代码={}",
            self.plugin_id, SOURCE, STAGE, code_fingerprint(self.plugin_dir),
        )
        config = await self._plugin_config()
        self._last_config = config
        self._log_first_run_hint(config)
        self._start_approval_service(config)
        # B12 P0-3：「怎么关」那一行必须等审批服务建起来之后才打——
        # 它读 _approval.reviewer_connected()，早一步调就是死代码。
        self._log_auto_allow_setting_line()
        return Ok({
            "status": "running",
            "plugin_id": self.plugin_id,
            "source": SOURCE,
            "stage": STAGE,
        })

    def _start_approval_service(self, config: dict[str, Any]) -> None:
        """起审批 HTTP 服务（M0-lite-5 / 5a 门控）。

        刻意**不用 asyncio 任务**：AGENTS.md 说 startup 返回时它创建的任务会被
        取消，而审批要活到用户点按钮那一刻。守护线程不受影响。
        端口绑不上只让审批中继不可用，观测与播报照常跑。

        ``api_key`` 只用来判断"有没有 key"，**不落日志、不进卡片、不给模型**。
        """
        # 5b：模型建议客户端。零 key（默认）时它是配好的但 enabled=False，
        # 卡片建议块走降级文案，不会有人去调模型。
        self._advisor = _advisor_from_config(config)
        if self._advice_pool is None:
            # 1 个 worker：建议是锦上添花，不该并发去打模型接口。
            # 放线程池是因为它是阻塞 IO，不能卡住审批 timer。
            self._advice_pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="deskmate-advice")
        section = _approval_section(config)
        port = int(_to_number(section.get("port"), HOOKS_DEFAULT_PORT))
        timeout = _to_number(section.get("timeout_mark_expired"), DEFAULT_TIMEOUT_SECONDS)
        retention = _to_number(section.get("retention_seconds"), DEFAULT_RETENTION_SECONDS)
        auto_decide = str(section.get("auto_decide") or AUTO_OFF).strip().lower()
        if auto_decide not in (AUTO_OFF, AUTO_LOW_RISK_ONLY):
            self.logger.warning(
                "approval.auto_decide 取值不明（{}），按 off 处理", auto_decide,
            )
            auto_decide = AUTO_OFF
        self._approval = ApprovalGateway(
            self.logger,
            port=port,
            timeout_seconds=max(1.0, timeout),
            retention_seconds=max(60.0, retention),
            ai_judgement=_to_bool(section.get("ai_judgement"), False),
            auto_decide=auto_decide,
            auto_decide_max_severity=str(
                section.get("auto_decide_max_severity") or "notice").strip().lower(),
            has_api_key=bool(self._advisor.api_key),
            audit_path=self.data_path("audit.jsonl"),
            authorization_path=self.data_path("authorization.json"),
            warning_counts_path=self.data_path(COUNTS_FILENAME),
            # **建议层的可用性单独传**（规格 Q4）：它跟 L2 的判断后端是两件事。
            # 曾经这里传的是"judge 在不在"，而 judge 恒为 None，于是模型建议
            # 明明在工作，启动行一直显示「生效=建议否」。
            advice_backend_ready=bool(self._advisor.enabled),
            # B9 自动审查档。默认 None（没配 / 没签字都不接）。
            reviewer=_reviewer_from_config(config),
        )
        if _REVIEWER_GAP[0]:
            self.logger.warning(
                "自动审查档已配置但不可用（{}），该档不生效", _REVIEWER_GAP[0],
            )
        if not self._approval.start():
            self._approval = None
            return
        self._check_installed_hook_port(port)
        if timeout >= HOOK_TIMEOUT_SECONDS:
            self.logger.error(
                "approval.timeout_mark_expired={}s 不小于 hooks.json 的 hook 超时 {}s，"
                "我们还没到期宿主就先断开了，pending 会永远等不到标记",
                timeout, HOOK_TIMEOUT_SECONDS,
            )

    def _check_installed_hook_port(self, port: int) -> None:
        """启动自检：已装进 Claude Code settings 的那条 hook 还好不好用。

        判定**不在这个方法里**——在 ``hookhealth.classify_hook`` /
        ``refine_with_relay``。这里是它的一个消费者（另一个是设置面板）。
        两处各写一遍就是两套判据，诊断自己就成了第二套判据
        （B8 同类缺陷：诊断与常驻轮询对不上）。

        本方法保留启动时才有的**副作用**：中继副本过期就自动重写。
        面板是只读消费者，不修，只报告。
        """
        target = settings_path()
        installed = installed_hook(target, self.data_path())
        health = classify_hook(installed, target=target, expected_port=port)
        health = refine_with_relay(
            health, script=installed.get("script") if installed else None,
            plugin_dir=self.plugin_dir,
        )

        # 过期：启动时自动修（这是启动自检的传统行为），修不好才报错。
        if health.state == hookhealth.OUTDATED:
            source = source_relay_path(self.plugin_dir)
            script = (installed or {}).get("script")
            ok, why = sync_relay(source, script)
            if not ok:
                self.logger.error(
                    "审批中继副本与插件里的版本不一致，且重写失败：{}。"
                    "请调一次 hook_install 入口重写这条 hook", why,
                )
                return
            self.logger.warning(
                "审批中继副本（{}）与插件里的版本不一致，已自动重写。"
                "这条 hook 指向的就是它，所以下一次权限请求起就用上新版；"
                "若重写一直不成功，请调一次 hook_install 入口", script,
            )
            return

        # 用方法名而不是数字级别：测试桩的 logger 只有 info/warning/error。
        getattr(self.logger, health.log_method)(
            health.log_message, *health.log_args)

    def _log_first_run_hint(self, config: dict[str, Any]) -> None:
        """首启引导：没声明 mine roots 就在日志里提示一次。

        **只打日志，不播报**——这是配置建议，不是观测结果，按红线 1
        不能走 push_message 打扰用户。也不猜归因：提示只告诉用户
        "想显示『你自己开的』需要声明什么"，不替他声明。

        顺带把审批语义写在这里（规格 Q2：那段话从主卡上搬走了，搬到
        「这是什么」与首启提示两处）。**到期不等于拒绝**这句话必须有个
        地方说清楚，否则用户会以为"不点就是不同意"。
        """
        if not _mine_roots(config):
            self.logger.info(
                "提示：未配置 [agent_deskmate.mine] roots，删除类播报的主语将显示为"
                "「发起者未知」。想让它显示「你自己开的 claude-code」，在配置里声明"
                "你常开会话的目录（例如 roots = [\"D:\\\\workspace\"]）。"
                "这是你自己给的事实，插件不猜；详见 config.example.toml。"
            )
        self.logger.info(
            "审批语义：允许后 Claude Code 继续这次调用；拒绝则中止这次调用"
            "（不会掐掉整个回合）。到期无人决定 = 视为未决定，由 Claude Code "
            "自己接手，插件不代你判断，也不把「一直没点」记成拒绝。"
            "卡片上点「这是什么」可以看到这段说明、会话与工作目录。"
        )
        # P2：「怎么关」**不在这里说**——搬到 _log_auto_allow_setting_line()，
        # 由 on_startup 在 _start_approval_service() 之后调。
        # 原来它就写在本方法里，而 on_startup 的顺序是"先 _log_first_run_hint、
        # 后 _start_approval_service"，于是 self._approval 恒为 None，
        # 那一行一次都没打印过（B12 P0-3）。死代码比没有代码更坏：
        # 它让人以为"怎么关"已经告诉过用户了。

    def _log_auto_allow_setting_line(self) -> None:
        """设置页常驻那一行的日志版：自动放行开着没有、怎么关（P2）。

        **必须由 on_startup 在 _start_approval_service() 之后调用**——
        它读 ``self._approval.reviewer_connected()``，而审批服务到那才建出来。
        单独拆一个方法，而不是调整那两句的相对顺序：顺序会被人无意改回去，
        方法名不会。

        **只打日志，不播报**（红线 1）：这是配置说明，不是观测结果。
        """
        if self._approval is None or not self._approval.reviewer_connected():
            return
        self.logger.info(
            "{}；要关掉，把配置里的自动审查档改回关闭即可"
            "（授权记录仍在，开关关着一样不动作）。",
            auto_allow_setting_line(enabled=True),
        )

    @lifecycle(id="shutdown")
    def on_shutdown(self, **_):
        self.logger.info("同桌猫关闭")
        if self._approval is not None:
            self._approval.stop()
            self._approval = None
        if self._advice_pool is not None:
            # 建议调用最长 15s，等它跑完再收摊——硬关会留下半截 HTTP 请求。
            self._advice_pool.shutdown(wait=True)
            self._advice_pool = None
        self._advisor = None
        self._started_at = None
        return Ok({"status": "shutdown"})

    @lifecycle(id="config_change")
    def on_config_change(self, **_):
        """配置改了就让下一轮重新读路径与合并窗口，不等重启。

        **刻意不碰审批授权**：授权是用户在卡片上签过字的，不是配置项。
        配置变更自动恢复/修改授权，就等于插件能自己给自己授权——
        规格 §2 硬规则 2 明令禁止。真要撤授权，用户改配置关掉 L1/L2 即可
        （授权记录在手，开关关着，一样不动作）。
        """
        # 观测层的可变状态全部归零，下一轮按新配置重建。
        # 审批授权**刻意不碰**：授权是用户在卡片上签过字的，不是配置项。
        # 配置变更自动恢复/修改授权，就等于插件能自己给自己授权——
        # 规格 §2 硬规则 2 明令禁止。真要撤授权，用户改配置关掉 L1/L2 即可
        # （授权记录在手，开关关着，一样不动作）。
        self._reset_watch_state()
        self.logger.info("配置已变更，观测适配器将在下一轮重建（审批授权不受影响）")
        return Ok({"status": "reloaded"})

    # ── 配置 ───────────────────────────────────────────────────────────────

    async def _plugin_config(self) -> dict[str, Any]:
        """读出 ``[agent_deskmate]`` **整节**。缺配置时返回空 dict，不猜默认值。

        刻意返回整节而不是只返回 ``watch`` 子表：``coalesce_seconds`` 在
        ``[agent_deskmate]`` 顶层（见 config.example.toml），不在 ``watch`` 里。
        旧代码只取 ``watch`` 子表后读 ``coalesce_seconds``，**永远读不到**，
        用户改这个键会被静默忽略——现在按文件里的真实层级读。
        """
        try:
            dumped = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("读取配置失败：{}", type(exc).__name__)
            return {}
        section = dumped.get("agent_deskmate")
        return section if isinstance(section, dict) else {}

    # ── 观测轮询（M0-lite-4 的常驻部分） ──────────────────────────────────

    def _ensure_watchers(self, config: dict[str, Any]) -> Broadcaster | None:
        """按当前配置建好适配器与播报器。配置没给路径就返回 None（不猜默认值）。"""
        if self._broadcaster is not None:
            return self._broadcaster

        section = _watch_section(config)
        projects_dir = _expand(section.get("claude_projects_dir"))
        db_path = _expand(section.get("cc_switch_db"))
        transcript_ok = projects_dir is not None and projects_dir.is_dir()
        gateway_ok = db_path is not None and db_path.is_file()
        if not transcript_ok and not gateway_ok:
            self.logger.warning(
                "两个观测源都不可用（transcript={} gateway={}），本轮跳过",
                projects_dir or "未配置", db_path or "未配置",
            )
            # 一个都配不了也要记状态：否则 status 永远显示"未启动"，
            # 而真实原因是没配路径（B6）。
            self._source_state["claude_code_transcript"] = (
                "path_missing" if projects_dir is not None else "unconfigured")
            self._source_state["gateway_metadata"] = (
                "path_missing" if db_path is not None else "unconfigured")
            return None

        coalesce_seconds = _to_number(config.get("coalesce_seconds"), 60.0)
        # 路径配了但不存在时同样不建适配器——建了也只会 poll 出空，还误导日志。
        # **每一源各自记一份真实状态**（B6）：老实现 status 里写死 "ready"，
        # 用户看到 ready 就以为删除播报一定在，而适配器可能从没建起来。
        # 假状态比没有状态更糟——它让排查从正确的方向开始错起。
        self._transcript_adapter = ClaudeCodeAdapter(projects_dir) if transcript_ok else None
        self._source_state["claude_code_transcript"] = (
            "ready" if transcript_ok else
            ("path_missing" if projects_dir is not None else "unconfigured"))
        # B4：网关适配器**单独兜**。它的构造就要读库（取当前 rowid 水位），
        # 库被锁 / 表被改名 / WAL 缺 -shm 都会在这里抛。
        # 老实现没有这一层：GatewayAdapter 一抛，_ensure_watchers 整轮失败，
        # 转录适配器永远建不起来——**网关坏了，删了什么都不播**。
        self._gateway_adapter = None
        self._source_state["gateway_metadata"] = (
            "ready" if gateway_ok else
            ("path_missing" if db_path is not None else "unconfigured"))
        if gateway_ok:
            try:
                self._gateway_adapter = GatewayAdapter(db_path)
            except GatewayReadError as exc:
                self._source_state["gateway_metadata"] = "init_failed"
                self.logger.warning(
                    "网关观测源建不起来（{}），本轮起只有转录生效：{}",
                    type(exc).__name__, exc)
        self._broadcaster = Broadcaster(
            self.push_message, self.logger, source=SOURCE,
            coalescer=Coalescer(window_seconds=coalesce_seconds),
            log_commands=_to_bool(config.get("log_commands"), False),
            # 「让猫娘说一说」。默认关——见 Broadcaster.__init__ 的理由。
            speak=_to_bool(_approval_section(config).get("broadcast_speak"), False),
        )
        self.logger.info(
            "观测适配器就绪 transcript={} gateway={} 合并窗口={}s 播报方式={}",
            projects_dir, db_path, coalesce_seconds,
            "猫娘复述" if self._broadcaster.speak else "只出文字",
        )
        return self._broadcaster

    def _poll_source(self, name: str, adapter: Any) -> list[UnifiedEvent]:
        """轮询一个观测源，失败只降级**这一个**源（B4）。

        老写法是两个 poll 顺序调用，网关一抛就把本轮已经读到的转录事件
        一起带走——异常冒到 watch_tick，整轮作废，删除播报首当其冲。

        限频日志：网关库锁是持续状态，不限制的话每 3 秒一条 warning，
        日志会比播报还吵，真正要看的删除记录被冲掉。
        """
        try:
            return adapter.poll()
        except GatewayReadError as exc:
            now = time.monotonic()
            last = self._source_error_at.get(name, 0.0)
            if now - last >= SOURCE_ERROR_LOG_EVERY:
                self._source_error_at[name] = now
                self.logger.warning("{}观测源本轮读取失败（{}），已跳过：{}",
                                    name, type(exc).__name__, exc)
            return []

    def _sync_tracker_config(self, config: dict[str, Any]) -> None:
        """配置变了就换一个新 tracker。

        **换而不是改**：ProgressTracker 是纯函数层，没有 setter，也不该有——
        阈值属于 TrackerConfig 这个冻结值。换实例会丢掉在途状态
        （未回包、待确认失败），那是故意的：用户刚改的阈值不该被
        上一轮的旧计时继续支配。
        """
        cfg = _tracker_config(_tracker_section(config))
        if cfg == self._tracker_cfg:
            return
        self._tracker_cfg = cfg
        self._tracker = ProgressTracker(cfg)
        self.logger.info(
            "进度状态机阈值就绪 等待升级={:g}s 未回包={:g}s 回合静默={:g}s "
            "合并={:g}s 失败确认={:g}s",
            cfg.wait_escalate_seconds, cfg.unclosed_action_seconds,
            cfg.boundary_round_seconds, cfg.fail_coalesce_seconds,
            cfg.failure_confirm_seconds)
        self._sync_merge_window(_tracker_section(config))

    def _sync_merge_window(self, section: dict[str, Any]) -> None:
        """同目录通知的跨批次合并窗口（P1c(b)）。

        **刻意不并进上面那行日志**：那一行是真机验收的"主通过信号"，
        任务包要求它一字不差。合并窗口单独一行，改配置时能看到用了哪套值。

        键和 ``fail_coalesce_seconds`` 是兄弟——一个是状态机内部的按会话去重，
        一个是播出层的按工作目录合并。放在同一节里，用户找得到。
        """
        window = _to_number(section.get("notice_merge_seconds"),
                            NOTICE_MERGE_SECONDS)
        if window == self._merge_window:
            return
        self._merge_window = window
        self._merger.set_window(window)
        if window <= 0:
            self.logger.info("同目录通知跨批合并已关闭（notice_merge_seconds=0）："
                             "通知当轮就发，不带「另有 N 个会话」")
        else:
            self.logger.info("同目录通知跨批合并窗口就绪 窗口={:g}s", window)

    def _feed_tracker(self, event: UnifiedEvent) -> list[Notice]:
        """一个事件 → 零或一条 Signal → 零或多条 Notice。

        ``signal_from_event`` 返回 None 是**正常分支**（占位消息、没有
        action_id 的 tool_use、没有判决的 error），不是错误。所以这里不记
        日志——按实测占比它才是多数，记一行就会把日志刷成噪音。

        P1c：闸门自己造成的失败照旧进 tracker（要闭环那个 action），
        但带上 ``self_inflicted``——tracker 会只闭环、不当成 Agent 的失败。
        **不在这里把事件整个丢掉**：那样 ``open_actions`` 里那个 action
        永远配不上 outcome，570 秒后变成一条「有一步没有回包」，
        用一个假警报换掉另一个假警报。
        """
        signal = signal_from_event(
            event, self_inflicted=self._is_gate_caused(event))
        if signal is None:
            return []
        return self._tracker.feed(signal)

    # ── P1c：闸门自己造成的失败 ───────────────────────────────────────────

    def _record_gate_stopped(self) -> None:
        """把「工具没跑成」的已了结审批记进自伤簿。

        数据源就是 ``approval.snapshot()``——和 ``_approval_blocked_sessions``
        同一份。刻意**不**在审批的各个了结点上打补丁：那里有五六处
        （用户点、L2 放行、到期、回填断开、关服务），漏一处的表现是
        "自伤抑制静默失效"，而这种失效在日志里什么都没有。
        轮询全扫 + ``record`` 幂等，是唯一不会漏的形状。

        代价是每 3 秒扫一遍已了结记录。记录数受 ``retention_seconds``
        限制（默认一小时），实测量级在几十条，扫一遍的成本可以忽略。
        """
        approval = self._approval
        if approval is None:
            return
        for row in approval.snapshot():
            if row.get("state") == "pending" or not row.get("tool_did_not_run"):
                continue
            self._gated.record(
                session=str(row.get("session_id") or ""),
                tool_use_id=str(row.get("tool_use_id") or ""),
                kind=str(row.get("tool_name") or ""),
            )

    def _is_gate_caused(self, event: UnifiedEvent) -> bool:
        """这个失败事件是插件自己的闸门造成的吗。

        只对 ``tool_result`` 且 ``is_error`` 问这个问题——其它事件问了
        也会被 ``Signal.__post_init__`` 拒收，不如一开始就不问。
        """
        if event.event != EVENT_TOOL_RESULT or event.is_error is not True:
            return False
        mode = self._gated.claim(
            session=event.session,
            tool_use_id=event.tool_use_id or "",
            degraded_window=GATED_DEGRADED_WINDOW_SECONDS)
        if mode == MODE_DEGRADED:
            # 降级路径每次都要留痕。静默的降级等于没有降级——真机上
            # 出了一次"该报的没报"，排查时唯一能依靠的就是这一行。
            # **在 Claude Code 上这是常态**（hook 负载不带 tool_use_id，
            # 证据链见 gated 模块 docstring），所以这一行不是异常信号，
            # 是"现在跑的是粗判"的常态标记。
            self.logger.info(
                "审批造成的失败按降级规则静音：会话 {} 拿不到 tool_use_id，"
                "只能按「会话 + {:g}s 窗口内首次」判，未做精确配对",
                event.session or "无", GATED_DEGRADED_WINDOW_SECONDS)
        return bool(mode)

    def _released_gate_sessions(
        self, still_blocked: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """本轮「审批挂起解除、且工具不会再有结果」的 (agent, session)。

        P1b 收尾。判据就是 ``tool_did_not_run``——和 ``_record_gate_stopped``
        同一条：到期没人点 / 回填被断开 / 用户点拒绝，三种情况下那个工具
        调用都没跑。而到期按设计**不回填**（``approval.DECIDED_BY_EXPIRED``：
        不拒绝、不回填、不记成"已拒绝"），所以那个 outcome 永远不会来。

        于是每张没人点的卡到期后都会留下一个永远配不上 outcome 的
        open_action，而挂起标记一解除它就早就过了 300 秒——下一轮必报一条
        「有一步没有回包」，还把耗时写成一个夸大的数字（0.6.0 真机 §3：
        12:33 的动作、12:42:53 到期、12:44:09 播报"执行了 10 分 46 秒"）。
        ``tick(blocked=...)`` 挡得住挂着的期间，挡不住解除的那一秒，
        所以要把这些会话交出去让 tracker 作废那些动作。

        **幂等靠 request_id 记账**：snapshot() 按 retention 留着已了结记录，
        不记账的话同一会话每分钟又被"释放"一次，每次都白扫一遍 open_actions。

        ``still_blocked``：当前还挂着别的审批的会话要排除——它确实还有
        一个正当被扣住的动作，等那张卡了结了再释放，别提前作废掉。
        """
        approval = self._approval
        if approval is None:
            return set()
        now = time.time()
        released: set[tuple[str, str]] = set()
        for row in approval.snapshot():
            if row.get("state") == "pending" or not row.get("tool_did_not_run"):
                continue
            request_id = str(row.get("request_id") or "")
            if not request_id or request_id in self._gate_released:
                continue
            self._gate_released[request_id] = now
            session = str(row.get("session_id") or "")
            if not session:
                # 后台/无会话场景：键匹配不上任何 tracker 桶，跳过。
                # 猜一个 session 比不跳更糟（同 _approval_blocked_sessions）。
                continue
            agent = str(row.get("agent") or "") or AGENT_CLAUDE_CODE
            released.add((agent, session))
        # 记账跟着保留期走，别无限长。
        for stale in [key for key, at in self._gate_released.items()
                      if now - at > GATE_RELEASE_MEMORY_SECONDS]:
            del self._gate_released[stale]
        return released - still_blocked

    def _approval_blocked_sessions(self) -> set[tuple[str, str]]:
        """当前有审批挂在等人点的 (agent, session)。

        给 ``ProgressTracker.tick()`` 当 ``blocked`` 用：这些会话的
        **「未回包」判定整段跳过**。

        为什么需要它（2026-11-24 真机）：审批挂着时，那条工具调用没有
        ``tool_result`` 是因为 PermissionRequest hook 在阻塞等回填，
        **不是 Agent 卡住**。而 tracker 只看得见"有 action 无配对 outcome"，
        300 秒后必然判成「它可能卡住了」。用户屏幕上同时挂着审批卡和
        "它可能卡住了"，自相矛盾。

        治本要建 ``PendingApproval → WAITING`` 桥（让播报说"它在等你审批"），
        那是单独一批；这一条是桥建好之前的正确中间态——**宁可少说一句，
        不要指错方向**。

        刻意**不**用"调大 unclosed_action_seconds"：那会让真卡住也跟着晚发现，
        直接违背"出错发现晚"这条原始痛点。
        """
        approval = self._approval
        if approval is None:
            return set()
        blocked: set[tuple[str, str]] = set()
        for row in approval.snapshot():
            if row.get("state") != "pending":
                continue
            session = str(row.get("session_id") or "")
            if not session:
                # 后台/无会话场景：键会是 ("", request_id)。没有 session 就
                # 匹配不上任何 tracker 桶，跳过——猜一个 session 比不跳更糟。
                continue
            agent = str(row.get("agent") or "") or AGENT_CLAUDE_CODE
            blocked.add((agent, session))
        return blocked

    def _poll_once(self, config: dict[str, Any]) -> list[str]:
        """扫一轮观测源 → 跑规则 → 喂状态机 → 播报。同步函数，由 timer 丢进线程执行。

        返回本轮播报结果列表，便于测试与入口观察。
        """
        broadcaster = self._ensure_watchers(config)
        if broadcaster is None:
            return []

        mine_roots = _mine_roots(config)
        events: list[UnifiedEvent] = []
        # B4：两个源各自兜住。任何一个失败只降级它自己，
        # 另一个源这一轮读到的事件照常播——网关库锁不该让删除播报消失。
        if self._transcript_adapter is not None:
            events.extend(self._poll_source("转录", self._transcript_adapter))
        if self._gateway_adapter is not None:
            events.extend(self._poll_source("网关", self._gateway_adapter))

        # C 批 §三：状态机的**状态推进先跑完**（喂事件 + tick 都是纯函数、
        # 不碰宿主），推送才和规则层合流进 jobs、共用一个条数上限。
        # 顺序反过来会出这种事：规则层撞上限提前 return，tick 一次没跑，
        # "未回包"整整晚一个回合才发现。
        self._sync_tracker_config(config)
        # P1c：先登记、后喂事件。顺序反了的话，「到期」和「tool_result 落进
        # 转录」出现在同一轮时，那条 tool_result 就来不及被认成自伤——
        # 而它 120 秒后照样播一条「出事了」。
        self._record_gate_stopped()
        notices: list[Notice] = []
        for event in events:
            notices.extend(self._feed_tracker(event))
        # P1b：审批挂起解除的那一刻，被扣住的那个 action 早就过了 300s。
        # **必须先作废、再评估**——反了的话这一轮 tick 就把「有一步没有回包」
        # 连同一个夸大的耗时发出去了，作废再来是马后炮。
        blocked = self._approval_blocked_sessions()
        dropped = self._tracker.release_gate(self._released_gate_sessions(blocked))
        for (agent, session), count in sorted(dropped.items()):
            # 这一行是"少播了一条"的唯一痕迹。静默的抑制等于没有抑制——
            # 真机上出了一次"该报的没报"，排查时唯一能依靠的就是它。
            self.logger.info(
                "审批结束后作废 %d 个未闭环动作：%s 会话 %s"
                "（工具没跑成，等不到结果，不报未回包）",
                count, agent, session or "无")
        notices.extend(self._tracker.tick(time.time(), blocked))
        # P1c(b)：同目录多会话一字不差的重复通知合成一条。
        # **跨批次**（真机验收 2026-11-24 测试 8 的根因：只合同一轮不够，
        # 6 个会话 3 秒内失败，通知时间各自 +120s，跨过轮询相位就分成 3 条）。
        # 放在这里而不是 tracker 里：tracker 的指纹去重按会话记账是对的
        # （每个会话自己的状态没被污染），要合并的是"用户看到几条"。
        # 代价是最晚一条晚一个窗口，见 broadcast.NoticeMerger 的文档。
        notices = self._merger.offer(notices, time.time())

        # 规则层排在通知层**前面**：删除/下派是安全信号，撞上条数上限时该被
        # 推迟的是进度播报，不是"它刚删了文件"。tracker 的 Notice 本就按
        # fingerprint 去过重、按窗口合并，天然稀疏，晚 3 秒无感。
        jobs: list[Callable[[], str]] = list(self._pending_pushes)
        self._pending_pushes = []
        for event in events:
            for hit in _hits_for_event(event, mine_roots):
                jobs.append(partial(broadcaster.broadcast, hit))
        for notice in notices:
            jobs.append(partial(broadcast_notice, self.push_message, notice,
                                source=SOURCE, logger=self.logger,
                                # 跟着 Broadcaster 走，不另读配置——
                                # 一处决定，避免"删除类开说、通知类不开"的半成品。
                                # 也走同一个冷却：两条播报连着来，第二条自动
                                # 降级成 blind，不连发 respond。
                                ai_behavior=broadcaster.resolve_ai_behavior()))

        outcomes: list[str] = []
        for job in jobs:
            if len(outcomes) >= MAX_BROADCAST_PER_TICK:
                break
            outcomes.append(job())

        # 超出的部分转到下一轮。**不是丢掉**——理由见 MAX_PENDING_PUSHES 注释。
        overflow = jobs[len(outcomes):]
        if overflow:
            if len(overflow) > MAX_PENDING_PUSHES:
                self.logger.warning(
                    "积压推送 {} 条超过硬顶 {} 条，丢弃最旧的 {} 条",
                    len(overflow), MAX_PENDING_PUSHES,
                    len(overflow) - MAX_PENDING_PUSHES)
                overflow = overflow[-MAX_PENDING_PUSHES:]
            self._pending_pushes = overflow
            self.logger.info("本轮推送已达上限 {} 条，余下 {} 条下一轮接着发",
                             MAX_BROADCAST_PER_TICK, len(overflow))
        return outcomes

    @timer_interval(id="watch", seconds=3)
    async def watch_tick(self, **_):
        """每 3 秒扫一次观测源。
        ``seconds`` 必须是整数字面量，不能写 ``seconds=WATCH_SECONDS``——
        ``neko-plugin check`` 对装饰器做 AST 静态分析（``_decorator_keyword_literal``），
        只认字面量，写常量名会直接报 error。所以这里写死 3，
        要改间隔就改这一行，别抽常量。

        handler 必须 async def（同步的每次 tick 都会报错，见 runtime-model.md）。
        阻塞的 I/O 丢进线程；push_message 是普通调用，任何线程都能用。
        """
        try:
            config = await self._plugin_config()
            await asyncio.to_thread(self._poll_once, config)
        except Exception as exc:  # noqa: BLE001  一轮失败不能杀死 timer
            self.logger.warning("观测轮询异常（{}）：{}", type(exc).__name__, exc)

    # ── 审批中继（M0-lite-5） ──────────────────────────────────────────────

    @timer_interval(id="approval", seconds=1)
    async def approval_tick(self, **_):
        """把新来的审批请求变成卡片、把已了结的结果写回卡片、播报自动放行。

        单独一个 1 秒 timer，不并进 3 秒的观测轮询：审批是"人在等"的交互，
        晚 3 秒才弹出卡片，体感就是插件坏了。

        建卡片必须在这里做：``create_card`` 是 async 的 SDK 调用，而 hook 的
        handler 跑在纯线程里，没有事件循环可 await。

        **不在这里推任何"状态摘要"**（B12 P0-2）：未决条数改由 ``status``
        入口按需返回。实测一次自动放行会在审查的 1–2 秒里让计数 0→1→0，
        于是先播「有 1 条待判断」、两秒后又播「没有了」——用户看到的是假警报，
        那比没提示更坏。要计数自己查，插件不推。
        """
        if self._approval is None:
            return
        try:
            for pending in self._approval.take_new():
                await self._create_approval_card(pending)
            for pending in self._approval.settle_pending_cards():
                await self._settle_approval_card(pending)
            for pending in self._approval.take_auto_allowed():
                await self._announce_auto_allow(pending)
        except Exception as exc:  # noqa: BLE001  一轮失败不能杀死 timer
            self.logger.warning("审批卡片处理异常（{}）：{}", type(exc).__name__, exc)

    async def _announce_auto_allow(self, pending: Any) -> None:
        """自动放行后发一条 blind 通知（规格 §2 硬规则 4）。

        自动放行**不弹卡片**（要人点就失去意义），但绝不能不吭声——
        看不见的放行等于后门。所以这里必须说清"放了什么、凭什么"。

        **P2 之后只发三行**（见 ``auto_allow_notice``）：放了什么 /
        它要干什么 / 凭什么。授权溯源、模型名、机制说明、关闭方法
        **一律不进通知**——它们全在审计里（``audit_list`` 看得见），
        通知里多一行就是把最该被看见的那行往下推。

        **渠道只走 ``["chat"]``**（B12 P0-1，真机实测）：``["hud"]`` 后端确认
        送到前端，但角色旁那行小字始终不显示，切页面更是彻底看不见。
        所有"要让用户看见"的文案一律 chat，不再往 hud 发任何东西。
        """
        request = pending.request
        self.logger.info(
            "自动放行通知：请求 {}（{}，{}）", request.request_id,
            pending.judge_model or "-", pending.judge_reason or "-",
        )
        # P2：固定三行——放了什么 / 它要干什么 / 凭什么。
        # 模型名、授权版本、机制说明、关闭方法**一律不进通知**，全进审计
        # （audit_list 看得见）；「怎么关」由设置页那一行常驻显示。
        text = auto_allow_notice(
            tool_name=request.tool_name or "",
            command=_command_of(request),
            reason=pending.judge_reason or "",
            rule=pending.rule or "",
        )
        try:
            self.push_message(
                source=SOURCE,
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": text}],
                priority=4,
            )
        except Exception as exc:  # noqa: BLE001  通知发不出去不影响已放行的调用
            self.logger.warning("自动放行通知发送失败（{}）：{}",
                                type(exc).__name__, exc)

    def _warning_policy(self, pending: Any) -> WarningPolicy:
        """这一次的警示该长成什么样（规格 Q3）。

        配置读不到就按出厂默认（前几次红字 / n=3）——**卡片是核心功能，
        一个配置项读不出来不该让它消失**。``seen`` 由计数文件给，
        同一目标档位第几次命中（含这次）。

        **极高危档永不递减**（B2）：连点 3 次删 `node_modules` 之后，
        第 4 次的 `rm -rf ~` 仍要整行红字。这里强制常亮，覆盖用户的递减配置——
        和 ``auto_decide_gate`` 的封顶阈值同一个理由：安全件的下限
        不该被体验旋钮调没。
        """
        section = _approval_section(getattr(self, "_last_config", None) or {})
        tier = str(getattr(pending, "risk_tier", "") or "")
        return WarningPolicy(
            mode=warning_mode_for_tier(
                tier, section.get("warning_emphasis")),
            first_n=int(_to_number(section.get("warning_first_n"),
                                   DEFAULT_WARNING_FIRST_N)),
            seen=self._approval.note_warning(warning_key(tier)),
        )

    async def _create_approval_card(self, pending: Any) -> None:
        request = pending.request
        # 主语用归因语法生成，和播报说同一套话——不能卡片一套、播报一套。
        subject = describe_actor(
            executor=request.agent or "claude-code",
            requester_evidence="", requester=None,
        )
        body = request.tool_summary or request.tool_name or "未知工具"
        advice = await self._advice_for_card(pending)
        # 整份建议留在记录上：解释卡要显示模型的理由与依据（规格 P3）。
        pending.advice = advice
        # 红字递减（规格 Q3）：先记一次命中，再按"第几次"决定警示长成
        # 红字还是徽标。**只影响外观**——门控那边一个字节都没改。
        warning = self._warning_policy(pending)
        # 卡片与模型用**同一份完整命令**（A 批 A1）：不再从截断的摘要反推。
        command = _command_of(request)
        # 2026-11-24 真机验收 §5.4：模型的结论不再进主卡，只进「这是什么」
        # （见 approval.explain_request 的 conclusion 参数）。这里仍然算 advice
        # 是为了审计与自动放行判断，卡片上不显示它。
        html = build_card_html(
            subject=subject, tool_summary=body, session_id=request.session_id,
            working_directory=request.working_directory,
            advice_state=advice["state"],
            risk=pending.severity, action_class=pending.action_class,
            command=command,
            upstream=request.upstream, warning=warning,
            logger=self.logger,
        )
        try:
            card = await self.ctx.create_card(
                html=html,
                css=CARD_CSS,
                actions=build_card_actions(request.request_id),
                summary=f"审批：{subject} 请求执行 {body}"[:80],
            )
        except CardSubmissionError as exc:
            # 卡片发不出去≠审批不存在：hook 还在等，到期后会按 expired 收尾。
            self.logger.warning("审批卡片提交失败（{}），请求 {} 将走到期标记",
                                exc, request.request_id)
            return
        self._approval.attach_card(request.request_id, card.id)
        self.logger.info("审批卡片已弹出 {}（请求 {}）", card.id, request.request_id)

    async def _advice_for_card(self, pending: Any) -> dict[str, str]:
        """给卡片取「建议 + 理由 + 依据」。**只填卡片，不参与任何回填。**

        规格 §8：模型只给建议，永不代决。所以这里的产出全部流进
        ``build_card_html`` 的建议块，没有任何一条路通向 ``hook_response``。
        L2 自动放行仍然要用户签字 + 门控全过 + 有判断后端，那条路不接这个类。

        **这里是「调用结果码 → 卡片展示状态」的唯一翻译点。**
        ``model_advice.STATE_*`` 与 ``approval.ADVICE_STATES`` 是两套词，
        曾经有人把前者直接当后者传，模型一成功就 ``ADVICE_TEXT["ok"]``
        KeyError，整张卡片崩掉（真机实测：第一次真实请求卡片根本没弹出来）。
        所以这里显式映射，并且**翻译结果必须落进 ADVICE_STATES**，
        落不进去就按未知状态兜底——宁可没有建议，不能没有卡片。

        调用放在线程池里：模型是阻塞 IO，最坏等 15s，不能卡住审批 timer
        （timer 一轮还要建别的卡、推 HUD）。
        """
        advisor = self._advisor
        if advisor is None or not advisor.enabled:
            return self._degraded_advice(self._approval.advice_state())
        # 没签字 → 不给建议。授权是 L1 的前提，不是可选项。
        if not self._approval.authorized_scopes():
            return self._degraded_advice(ADVICE_STATE_FALLBACK,
                                         reason_override="unauthorized")
        gap = advisor.missing()
        if gap:
            self.logger.info("模型建议降级：{}", gap)
            return self._degraded_advice("no_key")

        request = pending.request
        command = _command_of(request)
        # 命令超过卡片展示上限时（A 批 A1 第 5 条）：**结论强制「无法判断」**。
        # 理由：用户只看得到前 80 字，此时任何"建议允许"都是在信息不全的
        # 情况下替他省掉最不该省的那一步。模型照样调用（它的 prompt 里带头
        # 尾全文），理由仍进「这是什么」——但结论行必须是第三态。
        # 字数走 commands.command_length（B5：与播报、审计同一个数）。
        total_chars = command_length(command)
        over_limit = total_chars > COMMAND_DISPLAY_MAX
        if over_limit:
            self.logger.info(
                "请求 {} 的命令 {} 字超过展示上限 {}，建议结论按无法判断呈现",
                request.request_id, total_chars, COMMAND_DISPLAY_MAX,
            )
        loop = asyncio.get_running_loop()
        try:
            advice = await loop.run_in_executor(
                self._advice_pool, lambda: advisor.suggest(
                    tool_name=request.tool_name,
                    command=command,
                    cwd=request.working_directory or "",
                    risk=pending.severity,
                ))
        except Exception as exc:  # noqa: BLE001  建议失败不能炸掉建卡
            self.logger.warning("模型建议调用异常（{}）：{}", type(exc).__name__, exc)
            return self._degraded_advice(ADVICE_STATE_FALLBACK)

        if not advice.ok:
            self.logger.info("模型建议降级：state={} detail={}",
                             advice.state, advice.detail)
            degraded = self._degraded_advice(_DISPLAY_STATE_FOR_OUTCOME.get(
                advice.state, ADVICE_STATE_FALLBACK))
            # 没有模型结论时，结论行落最保守的第三态——那正是"该有人决定"的
            # 时刻，插件不替用户判断（规格 §8）。
            degraded["conclusion"] = CONCLUSION_UNSURE
            return degraded

        # **高风险不显示「建议允许」**（A 批 A2 第 3 条，规格 §2 硬规则 3）：
        # critical / 不可恢复的请求上，模型硬说 allow 也压成"无法判断"，
        # 并把这次冲突写进审计。卡片上只看得到结果，看不到"模型本来想放行"，
        # 而事后复盘时这两件事的差别很大。
        conclusion, conflict = safe_conclusion(
            advice.suggestion, risk=pending.severity,
            action_class=pending.action_class)
        if over_limit:
            # 看不全的时候不给"建议允许"（A 批 A1 第 5 条）。
            conclusion = CONCLUSION_UNSURE
        if conflict:
            self.logger.warning(
                "请求 {}：{}（模型说 {}，风险 {}，类别 {}）",
                request.request_id, conflict, advice.suggestion,
                pending.severity, pending.action_class,
            )

        basis = (f"工具 {request.tool_name or '未知'}；"
                 f"工作目录 {request.working_directory or '未知'}；"
                 f"本地规则风险档 {pending.severity}"
                 f"（{pending.rule or '未命中'}）；模型 {advice.model}")
        # 结论行只准三种写法（规格 P2）：建议允许 / 建议拒绝 /
        # 无法判断，需要你决定。模型的 allow / deny / unsure token 在这里
        # 翻译成人话，绝不上卡片。理由与依据不进主卡（规格 P3：折叠进
        # 「这是什么」），但它们要留在记录上，解释卡用。
        return {"state": _DISPLAY_STATE_FOR_OUTCOME[STATE_OK],
                "conclusion": conclusion,
                "reason": advice.reason, "basis": basis,
                "conflict": conflict}

    def _degraded_advice(self, state: str,
                         reason_override: str | None = None) -> dict[str, str]:
        """降级的建议块：只给一个展示状态，不给任何建议内容。

        **self-check**：state 必须落在 ``approval.ADVICE_STATES`` 里。
        不在就退回兜底态并记一条 warning——这就是那条"两套词表"的防线，
        以后谁再加第三种叫法，这里会当场抓住，而不是等真机上卡片消失。
        """
        chosen = reason_override or state
        if not is_known_advice_state(chosen):
            self.logger.warning(
                "模型建议给出了卡片不认识的展示状态 {!r}，按「无建议」渲染", chosen)
            chosen = ADVICE_STATE_FALLBACK
        return {"state": chosen, "conclusion": CONCLUSION_UNSURE,
                "reason": "", "basis": ""}

    async def _settle_approval_card(self, pending: Any) -> None:
        """用户点完（或到期）后把卡片改成结果态，并清掉按钮防重复点。"""
        request = pending.request
        if not pending.card_id:
            return
        if pending.decided_by == DECIDED_BY_USER:
            note = ("已允许，Claude Code 继续执行。" if pending.decision == APPROVE_ALLOW
                    else "已拒绝，这次调用被中止。")
        elif pending.decided_by == DECIDED_BY_AI:
            # L2 只可能自动 allow。文案必须点明"凭你的授权"，不能写成插件自己的决定。
            # **两处都不写内部词**：不写"L2"（层级名），也不用审计那一版
            # 授权溯源（``v1:ai_judgement,auto_decide by=user``）——那是给
            # 事后对账的精确写法，印在卡片上就是让用户猜配置键名。
            # P2：与通知同一套三行。授权溯源与模型名进审计，不上屏。
            note = auto_allow_notice(
                tool_name=pending.request.tool_name or "",
                command=_command_of(pending.request),
                reason=pending.judge_reason or "",
                rule=pending.rule or "",
            )
        else:
            # 到期：只陈述事实——宿主自己处理的，我们没做判断。
            # **不许出现"已自动拒绝"**，那是把宿主的决定安到插件头上。
            note = "已过期：宿主已自行处理，本插件未做判断。"
        try:
            card = self.ctx.get_card(pending.card_id)
            await card.update(html=build_result_html(note), summary=note[:60], actions={})
        except CardSubmissionError as exc:
            self.logger.warning("审批结果卡片更新失败（{}）：请求 {}",
                                exc, request.request_id)
        self._approval.mark_announced(request.request_id)

    @ui.action(id="approval_decide", label=tr("entry.approval_decide.name", default="审批决定"))
    @plugin_entry(
        id="approval_decide",
        name=tr("entry.approval_decide.name", default="审批决定"),
        description=tr(
            "entry.approval_decide.description",
            default="审批卡片的按钮落点：把用户点的「允许 / 拒绝」回填给正在等待的 "
                    "Claude Code。正常不会手动调用它。",
        ),
    )
    async def approval_decide(self, request_id: str, decision: str, _ctx: dict):
        """卡片按钮的落点。幂等：重复点击只有第一次算数。"""
        if self._approval is None:
            return Err(SdkError("审批服务未运行"))
        outcome = self._approval.resolve(request_id, decision, decided_by=DECIDED_BY_USER)
        if outcome == "unknown":
            return Err(SdkError("找不到这个审批请求（可能已过期并被清理）"))
        if outcome == "expired":
            # 过期卡片：不回填，明确告诉用户这条已经不算数了。
            return Ok({"message": "这条已过期（宿主已自行处理），不再回填"})
        if outcome == "already-resolved":
            return Ok({"message": "这个请求已经决定过了"})
        return Ok({"message": "已允许" if decision == APPROVE_ALLOW else "已拒绝"})

    @ui.action(id="approval_explain", label=tr("entry.approval_explain.name", default="这是什么"))
    @plugin_entry(
        id="approval_explain",
        name=tr("entry.approval_explain.name", default="这是什么"),
        description=tr(
            "entry.approval_explain.description",
            default="在审批卡旁边追加一张说明卡：讲清这次请求是什么、后果与影响"
                    "范围、会话与工作目录、超时后会怎样。原审批卡不会被替换，"
                    "三个按钮始终可点。不接模型时只讲规则层能讲准的事实。",
        ),
    )
    async def approval_explain(self, request_id: str, _ctx: dict):
        """「这是什么」按钮的落点：**追加**一条说明卡，原审批卡原地不动。

        2026-11-22 真机反馈（两轮）：

        * 第一轮：解释卡*替换*审批卡，按钮全被抢走，用户看完只能干等到期。
          修法是给解释卡补上按钮 + 「返回审批」。
        * 第二轮：补上去的「返回审批」在宿主侧直接报
          ``UI action 'approval_show' is not exposed by surface 'chat_card:…'``，
          用户卡在中间出不去；而且解释卡正文里还漏着 ``L0`` / ``critical`` /
          ``L2`` / ``onFailure`` 这些内部词（C5 同类缺陷）。

        所以这次不替换、不切换：**另发一条消息**。原审批卡的三个按钮
        始终可点，也就不需要「返回审批」和 ``approval_show`` 入口了——
        「切换」这个动作整个消失，比把它修靠谱。
        """
        if self._approval is None:
            return Err(SdkError("审批服务未运行"))
        pending = self._approval.find(request_id)
        if pending is None:
            return Err(SdkError("找不到这个审批请求（可能已过期并被清理）"))
        advice = getattr(pending, "advice", None) or {}
        explanation = explain_request(
            tool_name=pending.request.tool_name,
            tool_summary=pending.request.tool_summary,
            working_directory=pending.request.working_directory,
            rule=pending.rule,
            severity=pending.severity,
            command=_command_of(pending.request),
            # 完整命令原文（A 批 A1）：主卡只显示前 80 字，全文在这里。
            full_command=_command_of(pending.request),
            model_reason=advice.get("reason", ""),
            basis=advice.get("basis", ""),
            # 没接入模型/没签字时那句陈述。主卡不放它（正文四行），
            # 但用户点「这是什么」时该看到。
            model_note=(advice_text(advice.get("state", ADVICE_STATE_FALLBACK))
                        if advice.get("state") != ADVICE_STATE_READY else ""),
            # 2026-11-24 真机验收 §5.4：模型的结论从主卡撤到这里。
            # 主卡正文只留「删哪个文件 + 允许/拒绝分别会怎样」。
            # advice_state 一并传过来：结论的三道闸门（只有真拿到建议才可能是
            # "建议允许" / 命令超长时为空 / 第三态不显示）现在守在这个函数里。
            conclusion=advice.get("conclusion", ""),
            advice_state=advice.get("state", ADVICE_STATE_FALLBACK),
            session_id=pending.request.session_id,
            timeout_seconds=self._approval.timeout_seconds,
            verbosity=_verbosity_of(getattr(self, "_last_config", {}) or {}),
            # B7：执行者与上游从主卡搬进「这是什么」。
            subject=describe_actor(
                executor=pending.request.agent or "claude-code",
                requester_evidence="", requester=None,
            ),
            upstream=pending.request.upstream,
        )
        await self._show_explain_card(pending, explanation)
        return Ok(explanation)

    async def _show_explain_card(self, pending: Any,
                                 explanation: dict[str, str]) -> None:
        """把说明卡发出去。**发不出去只记日志，界面给人话，不给人啃的错误。**

        同一个请求重复点「这是什么」时更新同一张卡，而不是越点越多。
        更新失败（卡被宿主收起了）就新开一张——用户点这个按钮必须看得到东西。

        **刻意不传 ``target_lanlan``**：审批卡是网关线程建的，那边也没有
        lanlan 可传，两边都用宿主的 current_lanlan 才不会指向不同的人。
        """
        html = build_explain_html(explanation)
        card_id = getattr(pending, "explain_card_id", None)
        try:
            if card_id:
                card = self.ctx.get_card(card_id)
                await card.update(html=html, summary=explanation["title"])
            else:
                card = await self.ctx.create_card(
                    html=html, css=CARD_CSS, summary=explanation["title"],
                )
                pending.explain_card_id = card.id
            return
        except Exception as exc:  # noqa: BLE001  见 docstring：绝不外泄内部报错
            # 日志里**连异常消息一起写**：``UI action ... is not exposed``
            # 这类信息只写类型名（RuntimeError）等于没写，排查时还是要去猜。
            self.logger.warning(
                "说明卡片{}失败（{}）：{}，尝试新开一张",
                "更新" if card_id else "创建", type(exc).__name__, exc,
            )
        if not card_id:
            # 第一次就失败：没有旧卡可更新，只能告诉用户"这个按钮暂时不可用"。
            await self._note_button_unavailable()
            return
        try:
            card = await self.ctx.create_card(
                html=html, css=CARD_CSS, summary=explanation["title"],
            )
            pending.explain_card_id = card.id
        except Exception as exc:  # noqa: BLE001  宿主层面的事，我们只能记
            self.logger.warning("说明卡片创建失败（{}）：{}",
                                type(exc).__name__, exc)
            await self._note_button_unavailable()

    async def _note_button_unavailable(self) -> None:
        """按钮点不动时，界面上只出现一句人话。

        宿主/内部异常（``UI action ... is not exposed by surface ...`` 这类）
        **不许直接显示给用户**：那是给我们排查用的，甩到界面上只会让人慌，
        而且什么都做不了。所以这里另发一张只有一句话的卡，真实原因进日志。
        """
        try:
            await self.ctx.create_card(
                html=build_result_html("这个按钮暂时不可用，请稍后再试。"),
                css=CARD_CSS, summary="同桌猫",
            )
        except Exception as exc:  # noqa: BLE001  连这句话都发不出去就只能记日志
            self.logger.warning("连「按钮不可用」提示都发不出去（{}）：{}",
                                type(exc).__name__, exc)

    # ── hook 装配（5a 实测：没人装 hook，卡片永远不来） ────────────────────

    # ── 设置面板 ───────────────────────────────────────────────────────────

    @ui.context(id="deskmate", title=tr("panel.title", default="同桌猫"))
    async def panel_context(self) -> dict[str, object]:
        """设置面板要的那份状态。

        **判定不在这里**：hook 健康度走 ``hookhealth``（与启动自检同一套），
        汇总走 ``panel_state.build_panel_state``（纯函数）。这里只负责
        把插件的内部对象翻译成那两个函数的输入。

        取不到的值**不猜**：塞一个看起来像数据的占位符，比显示"未知"更坏。
        """
        config = await self._plugin_config()
        approval_cfg = (config.get("approval")
                        if isinstance(config.get("approval"), dict) else {})
        model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
        review_cfg = (config.get("auto_review")
                      if isinstance(config.get("auto_review"), dict) else {})

        roots = _mine_roots(config)
        api_key = str(model_cfg.get("api_key") or config.get("api_key") or "")
        scopes = (list(self._approval.authorized_scopes())
                  if self._approval is not None else [])

        sessions = []
        if self._tracker is not None:
            sessions = [
                {
                    "agent": s.agent,
                    "session": s.session,
                    "workspace": s.workspace,
                    "light": str(getattr(s.light, "value", s.light)),
                    "rounds": s.rounds,
                    "last_event_at": s.last_event_at,
                    # fingerprint → 上次播报时间。取不到就 0，面板显示「—」——
                    # 不拿 last_event_at（最近事件）冒充"最近播报"。
                    "last_broadcast_at": max(s.fingerprints.values(), default=0.0),
                }
                for s in self._tracker.sessions()
            ]

        approvals = []
        if self._approval is not None:
            try:
                approvals = self._approval.snapshot()
            except Exception as exc:  # 面板不该因为取不到快照就整个打不开
                self.logger.warning("面板取审批快照失败：{}", type(exc).__name__)
                approvals = []

        hook_health = {"state": "ok", "tone": "default", "title": "", "detail": "",
                       "action": None}
        if self._approval is not None:
            installed = installed_hook(settings_path(), self.data_path())
            health = classify_hook(installed, target=settings_path(),
                                   expected_port=self._approval.port)
            health = refine_with_relay(
                health,
                script=installed.get("script") if installed else None,
                plugin_dir=self.plugin_dir,
            )
            hook_health = health.as_dict()

        # 本机 Agent 发现 + 会话清单。**两者都只读元数据**：
        # discovery 只判目录存在，sessions 只 stat 文件 / 聚合查询 sqlite。
        # 所以它们不可能因为某个 Agent 的目录古怪就把面板拖垮；
        # 但保险起见仍然各自单独兜住——面板打不开比少一张卡严重得多。
        agents: list[dict[str, object]] = []
        agent_sessions: dict[str, list[dict[str, object]]] = {}
        try:
            home = Path(os.path.expanduser("~"))
            watch_cfg = _watch_section(config)
            # 网关库路径与观测循环**读同一份配置**，不另猜默认值——
            # 两边路径不一致时，面板会盘出一个观测循环根本没在读的库。
            gateway_db = _expand(watch_cfg.get("cc_switch_db"))
            projects_dir = _expand(watch_cfg.get("claude_projects_dir")) or (
                home / ".claude" / "projects")

            # 网关索引：给了它，网关侧 Agent 的状态才由事实决定
            # （dsh/opencode/copilot 都在网关里有账，不给就一律降成 conditional）。
            gateway_index: dict[str, int] = {}
            if gateway_db is not None:
                try:
                    gateway_index = gateway_app_types(gateway_db)
                except Exception as exc:
                    self.logger.warning(
                        "面板读网关 app_type 失败：{}", type(exc).__name__)
                    gateway_index = {}

            probes = discover_agents(home, gateway=gateway_index)
            agents = [probe.as_dict() for probe in probes if probe.reported]

            # 按 agent 盘会话。网关是共表，**必须按 app_type 过滤**，
            # 否则给 Codex 盘出来排第一的是 Claude Code 的会话
            # （实测本机：Claude 706 次 vs Codex 3 次）。
            for probe in probes:
                if not probe.reported:
                    continue
                try:
                    if probe.id == "claude":
                        rows = glance_transcript_sessions(
                            projects_dir, agent="claude")
                    elif probe.gateway_app_type and gateway_db is not None:
                        rows = glance_gateway_sessions(
                            gateway_db, agent=probe.id,
                            app_type=probe.gateway_app_type)
                    else:
                        rows = []
                except Exception as exc:
                    self.logger.warning(
                        "面板盘 {} 的会话失败：{}", probe.id, type(exc).__name__)
                    rows = []
                agent_sessions[probe.id] = [r.as_dict() for r in rows]
        except Exception as exc:  # 面板不该因为发现失败就整个打不开
            self.logger.warning("面板取本机 Agent 清单失败：{}", type(exc).__name__)
            agents = []
            agent_sessions = {}

        return build_panel_state(
            running=self._approval is not None and self._approval.running(),
            hook=hook_health,
            roots=roots,
            api_key_set=bool(api_key.strip()),
            auto_review_enabled=bool(review_cfg.get("enabled")),
            authorized_scopes=scopes,
            explain_verbosity=str(approval_cfg.get("explain_verbosity") or "brief"),
            warning_emphasis=str(approval_cfg.get("warning_emphasis") or "first_n"),
            warning_first_n=int(approval_cfg.get("warning_first_n") or 3),
            speak=_to_bool(approval_cfg.get("broadcast_speak"), False),
            sessions=sessions,
            approvals=approvals,
            now=time.time(),
            agents=agents,
            agent_sessions=agent_sessions,
        )

    @ui.action(id="settings_set_roots",
               label=tr("entry.settings_set_roots.name", default="保存目录声明"))
    @plugin_entry(
        id="settings_set_roots",
        name=tr("entry.settings_set_roots.name", default="保存目录声明"),
        description=tr(
            "entry.settings_set_roots.description",
            default="把「这些目录里的会话是我自己开的」写进配置。"
                    "声明后，高危通知的主语会显示「你自己开的 claude-code」，"
                    "而不是「发起者未知」。空列表等于撤销声明。",
        ),
    )
    async def settings_set_roots(self, roots: list | None = None, _ctx: dict | None = None):
        """写 ``[agent_deskmate.mine].roots``。

        为什么走入口而不是让面板直接写配置：**面板没有写配置的能力**
        （``config:write`` 在 SDK 里是个没有实现的权限，宿主侧也没有对应通道）。
        而且校验该留在 Python——项目纪律是"真正的兜底全在代码里"，
        手改配置文件的用户根本不过面板这张表单。

        顺手把 ``/`` 归一成 ``\\``：Windows 上手敲两种都认，
        但存成一种，用户回来看配置时不用猜。
        """
        raw = roots if isinstance(roots, list) else []
        cleaned: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if not text:
                continue
            path = text.replace("/", "\\").rstrip("\\") or text
            if path not in cleaned:
                cleaned.append(path)
        # 不存在的目录也收：用户可能先声明、后建目录。
        # 但**不**帮它 expanduser/expandvars——那会让配置里出现
        # 一个用户没写过的路径，回来看时不认得。
        try:
            await self.config.set("agent_deskmate.mine.roots", cleaned, timeout=5.0)
        except Exception as exc:
            self.logger.error("写 roots 失败：{}", type(exc).__name__)
            return Err(SdkError("保存失败：{}".format(type(exc).__name__)))
        self.logger.info("已声明 {} 个目录为「我自己开的会话」所在处", len(cleaned))
        return Ok({
            "roots": cleaned,
            "count": len(cleaned),
            "message": ("已保存 {} 个目录。下一次播报就用新声明；"
                        "不声明时高危通知显示「发起者未知」。".format(len(cleaned))),
        })

    @ui.action(id="settings_set_speak",
               label=tr("entry.settings_set_speak.name", default="保存播报方式"))
    @plugin_entry(
        id="settings_set_speak",
        name=tr("entry.settings_set_speak.name", default="保存播报方式"),
        description=tr(
            "entry.settings_set_speak.description",
            default="切换播报是只出文字，还是让猫娘用她自己的话复述一遍。",
        ),
    )
    async def settings_set_speak(self, speak: bool = False,
                                 _ctx: dict | None = None):
        """写 ``[agent_deskmate.approval].broadcast_speak``。

        为什么叫 ``broadcast_speak`` 而不叫 ``broadcast_voice``：
        **它不保证语音**。它只决定 ``ai_behavior`` 是 ``blind``（只出文字）
        还是 ``respond``（主模型接一轮、角色开口）。那一轮话会不会被念出来，
        取决于用户在 N.E.K.O 里有没有开角色语音——宿主侧的设置，插件管不着。
        配置名写成 voice 就是在骗人，只不过骗得深一层。

        **没配 api_key 时不许开**：``respond`` 要主模型接一轮，
        而红线 3 是零 key 只跑 L0。这时返回 Err 让面板说明原因，
        而不是假装保存成功、下次播报静默失效。
        """
        want = bool(speak)
        if want:
            config = await self._plugin_config()
            model_cfg = config.get("model") if isinstance(
                config.get("model"), dict) else {}
            key = str(model_cfg.get("api_key") or config.get("api_key") or "")
            if not key.strip():
                return Err(SdkError(
                    "没配 API key，不能让猫娘复述——那需要主模型接一轮。"
                    "先在插件管理器的「配置」页填 key，再来开这个。"))
        try:
            await self.config.set("agent_deskmate.approval.broadcast_speak",
                                  want, timeout=5.0)
        except Exception as exc:
            self.logger.error("写播报方式失败：{}", type(exc).__name__)
            return Err(SdkError("保存失败：{}".format(type(exc).__name__)))

        # **必须当场改生效的那个对象**。``broadcast_speak`` 是建 Broadcaster
        # 时读一次的，而 ``self.config.set()`` 从插件内部写配置**不会触发
        # 宿主的 config_change 生命周期**（实测 2026-11-26：日志里
        # "配置已变更" 一次都没出现）。于是配置写进去了、入口返回 Ok、
        # 面板弹"已保存"，而下一轮播报照旧用旧值——**按钮在说谎**。
        #
        # 刻意不调 ``_reset_watch_state()``：那会重建适配器，而新适配器
        # 把现有转录文件全当成"运行期新出现"从 0 开始读，等于把几个月历史
        # 倒进来播一遍（B4 的 ``_seen_at_startup`` 是每实例一份）。
        # 只翻这一个旗标：``Broadcaster.ai_behavior`` 是 property，
        # 每条播报现读，下一次就生效；通知路也现读（``getattr(broadcaster,
        # "speak")``）。不丢合并窗口里的通知，不重放历史。
        if self._broadcaster is not None:
            self._broadcaster.speak = want

        self.logger.info("播报方式已改为：{}",
                         "猫娘复述（respond）" if want else "只出文字（blind）")
        return Ok({
            "speak": want,
            "message": ("已保存。下一次播报生效；"
                        + ("猫娘会用她自己的话复述一遍，原文照旧上屏。"
                           if want else "只出文字，角色不开口。")),
        })

    # 暴露成 ui.action 是为了设置面板能一键调用（API 规定「A surface cannot
    # call arbitrary entries」）。入口点 tab 仍可用——两个装饰器叠加，
    # 不是替换。
    @ui.action(id="hook_install", label=tr("entry.hook_install.name", default="装配审批 hook"))
    @plugin_entry(
        id="hook_install",
        name=tr("entry.hook_install.name", default="装配审批 hook"),
        description=tr(
            "entry.hook_install.description",
            default="把审批中继装进 Claude Code 的 ~/.claude/settings.json。"
                    "装的是 command hook：插件没启动时中继输出空对象，"
                    "Claude Code 回退原生权限弹窗，不会卡住你。"
                    "返回里带改了哪个文件和备份在哪。",
        ),
    )
    async def hook_install(self, **_):
        """装配审批 hook。幂等：重复调用是替换，不会多出一条。

        **只动 ``~/.claude/settings.json``**，写之前先备份到
        ``<用户数据>/plugins/agent_deskmate/hook-backups/``，备份路径在返回值里。
        别人的 hook 一概不碰。

        自检不过就不写文件：装一条跑不起来的 command hook，等于让用户每次
        权限请求都看一次报错，比不装更糟。
        """
        if self._approval is None:
            return Err(SdkError("审批服务未运行，先启动插件再装 hook"))
        port = self._approval.port
        result = install_hook(
            self.plugin_dir, port,
            settings=settings_path(),
            backup_dir=self.data_path("hook-backups"),
            data_dir=self.data_path(),
        )
        if not result.get("ok"):
            self.logger.error("hook 装配失败：{}", result.get("reason"))
            return Err(SdkError(str(result.get("reason") or "装配失败")))
        self.logger.info(
            "hook 已装配到 {}（端口 {}，备份 {}）。重启 Claude Code 后生效",
            result["settings_file"], port, result.get("backup") or "无（原本没有该文件）",
        )
        return Ok({
            "installed": True,
            "settings_file": result["settings_file"],
            "backup": result.get("backup"),
            "port": port,
            "command": result["command"],
            "relay_file": result.get("relay_file"),
            "message": ("已写入。重启 Claude Code 后生效；"
                        "插件没启动时中继会输出空对象，Claude Code 回退原生权限弹窗。"),
        })

    @plugin_entry(
        id="hook_uninstall",
        name=tr("entry.hook_uninstall.name", default="卸载审批 hook"),
        description=tr(
            "entry.hook_uninstall.description",
            default="从 Claude Code 的 ~/.claude/settings.json 里摘掉审批中继。"
                    "只摘我们装的那条，别人的 hook 不动。",
        ),
    )
    async def hook_uninstall(self, **_):
        """卸载。同样只动 settings.json，先备份再写。"""
        result = uninstall_hook(
            settings=settings_path(),
            backup_dir=self.data_path("hook-backups"),
        )
        if not result.get("ok"):
            self.logger.error("hook 卸载失败：{}", result.get("reason"))
            return Err(SdkError(str(result.get("reason") or "卸载失败")))
        self.logger.info("hook 已卸载（{}，移除 {} 条）",
                         result["settings_file"], result.get("removed", 0))
        return Ok({
            "installed": False,
            "settings_file": result["settings_file"],
            "backup": result.get("backup"),
            "removed": result.get("removed", 0),
            "message": "已摘掉。Claude Code 恢复原生权限流程。",
        })

    # ── 5b：模型建议 + 测试连接 ────────────────────────────────────────────

    @plugin_entry(
        id="model_test",
        name=tr("entry.model_test.name", default="测试模型连接"),
        description=tr(
            "entry.model_test.description",
            default="向 [agent_deskmate.model] 配置的模型发一句最短的招呼，"
                    "回显模型名与耗时。不发送任何命令或上下文。",
        ),
    )
    async def model_test(self, **_):
        """「测试连接」。只验证连通性，不碰任何审批状态。

        刻意不发命令、不发上下文——连通性测试不需要那些，少发一点是一点。
        """
        advisor = self._advisor
        if advisor is None:
            advisor = _advisor_from_config(await self._plugin_config())
        result = await asyncio.get_running_loop().run_in_executor(
            self._advice_pool, advisor.ping)
        # api_key 一律不进返回值：日志、返回值、卡片都不该出现它。
        self.logger.info(
            "模型连接测试：ok={} state={} model={} 耗时={}ms",
            result.get("ok"), result.get("state"), result.get("model") or "-",
            result.get("elapsed_ms"),
        )
        if not result.get("ok"):
            # 只回原因，不把内部档位名（state）拼进去——那是调试用的，
            # 已经单独进日志。用户要看的是一句能照着做的话。
            return Err(SdkError(str(result.get("reason") or "调用失败")))
        return Ok({
            "connected": True,
            "model": result.get("model"),
            "elapsed_ms": result.get("elapsed_ms"),
            "reply": result.get("reply"),
            "message": "连通正常。模型只给建议，永不代你决定。",
        })

    @ui.action(id="approval_authorize", label=tr("entry.approval_authorize.name",
                                                 default="授权确认"))
    @plugin_entry(
        id="approval_authorize",
        name=tr("entry.approval_authorize.name", default="授权确认"),
        description=tr(
            "entry.approval_authorize.description",
            default="授权入口：弹出授权文案让用户确认（含「模型判断可能出错，"
                    "决定由你授权并承担后果」）。用户点「确认授权」后才写入授权记录并"
                    "审计。插件永不会自行调用它。",
        ),
    )
    async def approval_authorize(self, scopes: str = "", confirm: bool = False,
                                 _ctx: dict | None = None):
        """授权确认。``confirm=False`` 只弹卡片，``confirm=True`` 才签字。

        这是整个插件里**唯一**能打开 L1/L2 的路径，而它只能由用户在卡片上点
        按钮触发（``data-neko-action`` → 本入口 → ``grant_authorization``）。
        没有定时任务、hook、配置变更会走到这里——单测盯着这一点。
        """
        if self._approval is None:
            return Err(SdkError("审批服务未运行"))
        requested = [s for s in (scopes or "").replace(" ", ",").split(",") if s]
        if requested:
            wanted = tuple(s for s in requested if s in AUTH_SCOPES)
            if not wanted:
                # 传了却不合法：报错，**不许默认放成全部**——那会把没要求授权的
                # 范围一起授出去，是静默扩权。
                return Err(SdkError(
                    f"没有可授权的范围（可用：{'、'.join(AUTH_SCOPES)}）"))
        else:
            wanted = AUTH_SCOPES

        if not confirm:
            html = build_authorize_html(scopes=wanted)
            summary = f"授权确认：{'、'.join(wanted)}"
            try:
                card = await self.ctx.create_card(
                    html=html, css=CARD_CSS, actions=build_authorize_actions(wanted),
                    summary=summary[:80],
                )
                self.logger.info("授权确认卡片已弹出 {}", card.id)
            except CardSubmissionError as exc:
                # 卡片发不出去也要让用户看到文案本身——返回体里带全文。
                self.logger.warning("授权卡片提交失败（{}），仅在返回体里给出文案", exc)
            snapshot = self._approval.authorization_snapshot()
            return Ok({
                "granted": False,
                "message": ("请在卡片上点「确认授权」；未确认前，"
                            "模型建议与自动放行都不会生效。"),
                "text": snapshot["text"],
                "headline": snapshot["headline"],
                "scopes": list(wanted),
            })

        try:
            record = self._approval.grant_authorization(wanted)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        note = (f"已确认授权：{'、'.join(record.scopes)}（版本 v{record.version}）。"
                f"{AUTHORIZATION_HEADLINE}")
        if _ctx and _ctx.get("card_id"):
            try:
                card = self.ctx.get_card(_ctx["card_id"],
                                         target_lanlan=_ctx.get("lanlan_name"))
                await card.update(html=build_result_html(note), summary=note[:60],
                                  actions={})
            except (CardSubmissionError, KeyError) as exc:
                self.logger.warning("授权结果卡片更新失败（{}）", exc)
        return Ok({
            "granted": True,
            "message": note,
            "scopes": list(record.scopes),
            "version": record.version,
        })

    # ── 入口 ───────────────────────────────────────────────────────────────

    @plugin_entry(
        id="status",
        name=tr("entry.status.name", default="插件状态"),
        description=tr(
            "entry.status.description",
            default="返回同桌猫自身状态（运行时长 / 阶段 / 适配器就绪情况）。"
                    "Agent 观测在后台每 3 秒轮询中进行，用 scan_now 可立即触发一次。",
        ),
        llm_result_fields=["status", "stage"],
    )
    async def status(self, **_) -> StatusReply:
        """返回插件自身状态；不观测任何 Agent。

        注意：这个入口本身不观测——观测在 ``watch_tick`` 的 3 秒 timer 里跑。
        这句话容易读反，所以 i18n 文案里刻意写清了"观测在后台轮询中"。
        """
        uptime = round(time.time() - self._started_at, 1) if self._started_at is not None else None
        return StatusReply(
            status="running" if self._started_at is not None else "stopped",
            plugin_id=self.plugin_id,
            source=SOURCE,
            stage=STAGE,
            uptime_sec=uptime,
            adapters=self._adapter_status(),
            approval=self._approval.stats() if self._approval is not None else {"running": 0},
            authorization=(self._approval.authorization_snapshot()
                           if self._approval is not None else None),
            build=code_fingerprint(self.plugin_dir),
            hook=self._hook_status(),
            model=self._model_status(),
        )

    # 观测源的真实状态（B6）。老实现 status 里写死 "ready"，
    # 那是**假状态**：用户看到 ready 就以为删除播报一定在，
    # 而适配器可能压根没建起来（没配路径 / 路径不存在 / 库读不了）。
    # 排查时假状态比没有状态更糟——它让人从错的方向开始查。
    # 取值：not_started / unconfigured / path_missing / init_failed / ready。
    _ADAPTER_SOURCES = ("claude_code_transcript", "gateway_metadata")

    def _adapter_status(self) -> dict[str, str]:
        """各观测源的当前状态。``_ensure_watchers`` 每次重建时刷新。"""
        return {name: self._source_state.get(name, "not_started")
                for name in self._ADAPTER_SOURCES}

    def _hook_status(self) -> dict[str, Any]:
        """审批 hook 装没装、装的是哪个端口。只读，不发起任何请求。"""
        target = settings_path()
        found = installed_hook(target, self.data_path())
        script = (found or {}).get("script")
        return {
            "installed": found is not None,
            "kind": (found or {}).get("kind"),
            "settings_file": str(target),
            "port": (found or {}).get("port"),
            # 中继文件在不在也报出来：真机事故就是它被宿主清掉，
            # 而症状只是"卡片不来"，不报这个字段用户查不到这一步。
            "relay_file": script,
            "relay_present": bool(script) and Path(script).is_file(),
        }

    def _model_status(self) -> dict[str, Any]:
        """模型建议的状态。**刻意不含 api_key**——它不该出现在任何回显里。"""
        advisor = self._advisor
        if advisor is None:
            return {"enabled": False, "configured": False, "model": "",
                    "protocol": "", "missing": "未初始化"}
        return {
            "enabled": advisor.enabled,
            "configured": advisor.configured(),
            "model": advisor.model,
            "protocol": advisor.protocol,
            "base_url": advisor.base_url,
            "missing": advisor.missing(),
        }

    @plugin_entry(
        id="dump_events",
        name=tr("entry.dump_events.name", default="导出最近事件"),
        description=tr(
            "entry.dump_events.description",
            default="读一次观测源，返回最近 N 条统一事件，用于确认解析是否正确。",
        ),
        llm_result_fields=["count", "events"],
    )
    async def dump_events(self, count: int = 10, **_) -> DumpEventsReply:
        """一次性读两个观测源，返回最近 ``count`` 条统一事件。

        刻意不做增量：这是给人确认"解析对不对劲"的入口，不是常驻轮询。
        常驻轮询（M0-lite-4）用适配器的 offset / rowid 水位。
        """
        count = max(1, min(int(count), 100))
        config = await self._plugin_config()
        section = _watch_section(config)
        sources: dict[str, str] = {}
        events: list[UnifiedEvent] = []

        projects_dir = _expand(section.get("claude_projects_dir"))
        if projects_dir is not None and projects_dir.is_dir():
            events.extend(self._recent_transcript_events(projects_dir, count))
            sources["claude_code_transcript"] = str(projects_dir)
        else:
            sources["claude_code_transcript"] = "未配置或目录不存在"

        db_path = _expand(section.get("cc_switch_db"))
        if db_path is not None and db_path.is_file():
            events.extend(self._recent_gateway_events(db_path, count))
            sources["gateway_metadata"] = str(db_path)
        else:
            sources["gateway_metadata"] = "未配置或文件不存在"

        trimmed = events[-count:]
        self.logger.info(
            "dump_events：读到的源 transcript={} gateway={}，解析 {} 条，返回 {} 条",
            sources.get("claude_code_transcript", "?"),
            sources.get("gateway_metadata", "?"),
            len(events), len(trimmed),
        )
        return DumpEventsReply(
            count=len(trimmed),
            events=[e.model_dump() for e in trimmed],
            sources=sources,
        )

    @plugin_entry(
        id="scan_now",
        name=tr("entry.scan_now.name", default="立即扫描一次"),
        description=tr(
            "entry.scan_now.description",
            default="同步读一次观测源并跑规则判定，返回事件数 / 命中数 / 预计播报数与规则分布。"
                    "只读诊断，不发送任何播报。",
        ),
        llm_result_fields=["events_read", "hits", "broadcasts", "top_rules"],
    )
    async def scan_now(self, **_) -> ScanNowReply:
        """立即跑一次「观测 + 规则判定」，但**不播报**。

        常驻观测不在这里：它在 ``watch_tick`` 的 ``@timer_interval(3s)`` 里
        （入口只在被调用时跑，靠入口做常驻观测等于没人调就完全不观测）。
        这个入口是给真机调试用的——点一下就知道解析和规则对不对，
        不用等 3 秒，也不用真往聊天窗里发消息。

        读盘丢进线程：入口跑在 entry loop 里，阻塞它会拖慢同 loop 上的其它调用。
        """
        config = await self._plugin_config()
        return await asyncio.to_thread(diagnose, config, log=self.logger)

    @plugin_entry(
        id="pending_list",
        name=tr("entry.pending_list.name", default="审批待办"),
        description=tr(
            "entry.pending_list.description",
            default="返回未决与已了结（未清理）的审批：会话、命令、建立时间、状态、"
                    "decided_by。过期不删，保留一段时间供事后查。",
        ),
        llm_result_fields=["count", "pending"],
    )
    async def pending_list(self, limit: int = 50, **_) -> PendingListReply:
        """审批待办清单。过期条目也在里面——它们正是"当时到底问过什么"的证据。"""
        if self._approval is None:
            return PendingListReply(count=0, pending=0, items=[])
        items = self._approval.snapshot()[: max(1, min(int(limit), 200))]
        pending = self._approval.undecided_count()
        self.logger.info("pending_list：{} 条（其中未决 {}）", len(items), pending)
        return PendingListReply(count=len(items), pending=pending, items=items)

    @plugin_entry(
        id="audit_list",
        name=tr("entry.audit_list.name", default="审批审计"),
        description=tr(
            "entry.audit_list.description",
            default="返回每次审批决定的审计：时间、命令、决定、decided_by"
                    "（user/ai/expired）、模型、理由。附带纯文本便于导出。",
        ),
        llm_result_fields=["count"],
    )
    async def audit_list(self, limit: int = 100, **_) -> AuditListReply:
        """审批审计。每次决定都写一条；text 字段可直接复制走。"""
        if self._approval is None:
            return AuditListReply(count=0, items=[], text="# 同桌猫审批审计（共 0 条）")
        rows = self._approval.audit_rows()[: max(1, min(int(limit), 500))]
        self.logger.info("audit_list：{} 条", len(rows))
        return AuditListReply(count=len(rows), items=rows,
                              text=self._approval.audit_text())

    # ── 一次性读取（不走增量水位） ──────────────────────────────────────────

    def _recent_transcript_events(self, projects_dir: Path, count: int) -> list[UnifiedEvent]:
        """往回扫最近一个转录文件的末尾，解析出事件。"""
        files = iter_transcript_files(projects_dir)
        if not files:
            self.logger.info("转录目录下没有 jsonl：{}", projects_dir)
            return []
        newest = files[0]
        size = newest.stat().st_size
        self.logger.info("转录 tail：{} 末尾 {} 字节",
                         newest.name, min(size, TRANSCRIPT_TAIL_BYTES))
        return _scan_transcript_tail(projects_dir, count)

    def _recent_gateway_events(self, db_path: Path, count: int) -> list[UnifiedEvent]:
        """按 rowid 取最后 count 行（一次性，不动常驻水位）。"""
        events = _scan_gateway_tail(db_path, count)
        self.logger.info("网关账本：取回 {} 条", len(events))
        return events
