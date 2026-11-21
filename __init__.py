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
import html as _html
import json
import os
import time
from pathlib import Path
from typing import Any

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

from .adapters import ClaudeCodeAdapter, GatewayAdapter
from .adapters.claude_code import (
    iter_transcript_files,
    read_new_transcript_events,
)
from .adapters.gateway import current_watermark, read_new_gateway_events
from .approval import (
    APPROVE_ALLOW,
    AUTH_SCOPES,
    AUTHORIZATION_HEADLINE,
    AUTO_LOW_RISK_ONLY,
    AUTO_OFF,
    CARD_CSS,
    DECIDED_BY_AI,
    DECIDED_BY_USER,
    ApprovalGateway,
    build_authorize_actions,
    build_authorize_html,
    build_card_actions,
    build_card_html,
    build_explain_html,
    build_result_html,
    explain_request,
)
from .broadcast import Broadcaster, describe_actor, render_text
from .events import UnifiedEvent, attribute, delegated_server
from .rules import (
    Coalescer,
    RuleHit,
    evaluate_delegation,
    evaluate_delete,
    evaluate_failure,
    evaluate_transcript_error,
)

# 聊天里显示的来源标签。红线 1：source 必须显式传，不能用默认空串。
SOURCE = "agent_deskmate"

STAGE = "M0-lite-4"

# dump_events 读转录时往回扫的字节数。转录单文件可到数 MB，全量倒进来没意义。
TRANSCRIPT_TAIL_BYTES = 65536

# 单轮最多播几条，防止适配器积压后一次刷屏。
MAX_BROADCAST_PER_TICK = 5

# scan_now 每个数据源最多回看多少条事件。只读诊断，够看出规则对不对就行。
SCAN_EVENT_COUNT = 50

# scan_now 回显几条"将要播报"的原文，超过就截断。
SCAN_SAMPLE_LIMIT = 3

# hooks.json 里写死的端口。改 approval.port 必须同步改它，否则 hook 打不进来。
HOOKS_DEFAULT_PORT = 48921
# hooks.json 里写死的 hook 超时。我们的到期时间必须明显小于它。
HOOK_TIMEOUT_SECONDS = 600
# 审批默认到期：hook 600s 前 30 秒。只标记 expired，不发任何决定。
DEFAULT_TIMEOUT_SECONDS = 570.0
DEFAULT_RETENTION_SECONDS = 3600.0


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
        description="L1/L2 授权状态：granted / scopes / version / confirmed_time。"
                    "审批服务未运行时为 null",
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
    return events[-count:]


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
        self._last_hud_count = -1

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
            "同桌猫启动 plugin_id={} source={} stage={}",
            self.plugin_id, SOURCE, STAGE,
        )
        config = await self._plugin_config()
        self._last_config = config
        self._log_first_run_hint(config)
        self._start_approval_service(config)
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
            has_api_key=bool(str(section.get("api_key") or "").strip()),
            audit_path=self.data_path("audit.jsonl"),
            authorization_path=self.data_path("authorization.json"),
        )
        if not self._approval.start():
            self._approval = None
            return
        self._check_hooks_port(port)
        if timeout >= HOOK_TIMEOUT_SECONDS:
            self.logger.error(
                "approval.timeout_mark_expired={}s 不小于 hooks.json 的 hook 超时 {}s，"
                "我们还没到期宿主就先断开了，pending 会永远等不到标记",
                timeout, HOOK_TIMEOUT_SECONDS,
            )

    def _check_hooks_port(self, port: int) -> None:
        """hooks.json 里的端口和配置不一致时大声报警。

        两者对不上，Claude Code 的 PermissionRequest hook 就打不进来，
        而症状是"插件没反应"——不明确指出来能查一小时。
        """
        try:
            hooks_path = self.plugin_dir / "hooks.json"
            if not hooks_path.is_file():
                return
            declared = json.loads(hooks_path.read_text(encoding="utf-8"))
            url = declared["hooks"]["PermissionRequest"][0]["hooks"][0]["url"]
            declared_port = int(url.rsplit(":", 1)[-1].split("/", 1)[0])
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            self.logger.warning("读 hooks.json 核对端口失败（{}），跳过核对", type(exc).__name__)
            return
        if declared_port != port:
            self.logger.error(
                "端口不一致：配置 approval_port={} 但 hooks.json 里是 {}。"
                "PermissionRequest hook 会打不到插件，请把两边改成同一个值",
                port, declared_port,
            )

    def _log_first_run_hint(self, config: dict[str, Any]) -> None:
        """首启引导：没声明 mine roots 就在日志里提示一次。

        **只打日志，不播报**——这是配置建议，不是观测结果，按红线 1
        不能走 push_message 打扰用户。也不猜归因：提示只告诉用户
        "想显示『你自己开的』需要声明什么"，不替他声明。
        """
        if _mine_roots(config):
            return
        self.logger.info(
            "提示：未配置 [agent_deskmate.mine] roots，删除类播报的主语将显示为"
            "「发起者未知」。想让它显示「你自己开的 claude-code」，在配置里声明"
            "你常开会话的目录（例如 roots = [\"D:\\\\workspace\"]）。"
            "这是你自己给的事实，插件不猜；详见 config.example.toml。"
        )

    @lifecycle(id="shutdown")
    def on_shutdown(self, **_):
        self.logger.info("同桌猫关闭")
        if self._approval is not None:
            self._approval.stop()
            self._approval = None
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
        self._watch_paths = None
        self._transcript_adapter = None
        self._gateway_adapter = None
        self._broadcaster = None
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
        if not (projects_dir is not None and projects_dir.is_dir()) \
                and not (db_path is not None and db_path.is_file()):
            self.logger.warning(
                "两个观测源都不可用（transcript={} gateway={}），本轮跳过",
                projects_dir or "未配置", db_path or "未配置",
            )
            return None

        coalesce_seconds = _to_number(config.get("coalesce_seconds"), 60.0)
        # 路径配了但不存在时同样不建适配器——建了也只会 poll 出空，还误导日志。
        self._transcript_adapter = (
            ClaudeCodeAdapter(projects_dir)
            if projects_dir is not None and projects_dir.is_dir() else None
        )
        self._gateway_adapter = (
            GatewayAdapter(db_path)
            if db_path is not None and db_path.is_file() else None
        )
        self._broadcaster = Broadcaster(
            self.push_message, self.logger, source=SOURCE,
            coalescer=Coalescer(window_seconds=coalesce_seconds),
            log_commands=_to_bool(config.get("log_commands"), True),
        )
        self.logger.info(
            "观测适配器就绪 transcript={} gateway={} 合并窗口={}s",
            projects_dir, db_path, coalesce_seconds,
        )
        return self._broadcaster

    def _poll_once(self, config: dict[str, Any]) -> list[str]:
        """扫一轮观测源 → 跑规则 → 播报。同步函数，由 timer 丢进线程执行。

        返回本轮播报结果列表，便于测试与入口观察。
        """
        broadcaster = self._ensure_watchers(config)
        if broadcaster is None:
            return []

        mine_roots = _mine_roots(config)
        events: list[UnifiedEvent] = []
        if self._transcript_adapter is not None:
            events.extend(self._transcript_adapter.poll())
        if self._gateway_adapter is not None:
            events.extend(self._gateway_adapter.poll())

        outcomes: list[str] = []
        for event in events:
            for hit in _hits_for_event(event, mine_roots):
                outcomes.append(broadcaster.broadcast(hit))
                if len(outcomes) >= MAX_BROADCAST_PER_TICK:
                    self.logger.info("本轮播报已达上限 {} 条，余下留待下一轮",
                                     MAX_BROADCAST_PER_TICK)
                    return outcomes
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
        """把新来的审批请求变成卡片、把已了结的结果写回卡片、按需推 HUD。

        单独一个 1 秒 timer，不并进 3 秒的观测轮询：审批是"人在等"的交互，
        晚 3 秒才弹出卡片，体感就是插件坏了。

        建卡片必须在这里做：``create_card`` 是 async 的 SDK 调用，而 hook 的
        handler 跑在纯线程里，没有事件循环可 await。
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
            await self._push_hud_if_changed()
        except Exception as exc:  # noqa: BLE001  一轮失败不能杀死 timer
            self.logger.warning("审批卡片处理异常（{}）：{}", type(exc).__name__, exc)

    async def _announce_auto_allow(self, pending: Any) -> None:
        """L2 自动放行后发一条 blind 通知（规格 §2 硬规则 4）。

        自动放行**不弹卡片**（要人点就失去意义），但绝不能不吭声——
        看不见的放行等于后门。所以这里必须说清"放了什么、凭什么放的"：
        授权溯源、模型、理由都带上，用户事后能拿 audit_list 对账。
        """
        request = pending.request
        self.logger.info(
            "L2 自动放行通知：请求 {}（{}，{}）", request.request_id,
            pending.judge_model or "-", pending.judge_reason or "-",
        )
        text = (
            f"同桌猫：已按你的授权自动放行 {request.tool_summary or request.tool_name}"
            f"（L2 低风险，授权 {pending.authorization or '-'}，"
            f"模型 {pending.judge_model or '-'}，理由 {pending.judge_reason or '低风险'}）"
        )
        try:
            self.push_message(
                source=SOURCE,
                visibility=["hud"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": text}],
                priority=4,
            )
        except Exception as exc:  # noqa: BLE001  通知发不出去不影响已放行的调用
            self.logger.warning("L2 自动放行通知发送失败（{}）：{}", type(exc).__name__, exc)

    async def _push_hud_if_changed(self) -> None:
        """未决数量进 HUD。**只在数量变化时发一次**——每轮都发就是刷屏。

        走 push_message（红线 1）：source 显式、ai_behavior="blind"，
        只报"有 N 条待判断"这一句，不带命令原文。
        """
        count = self._approval.undecided_count()
        if count == self._last_hud_count:
            return
        self._last_hud_count = count
        if count == 0:
            # 归零也值得说一声：用户可能正等着"是不是清了"。
            text = "同桌猫：没有待判断的审批了"
        else:
            text = f"同桌猫：有 {count} 条审批待判断"
        try:
            self.push_message(
                source=SOURCE,
                visibility=["hud"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": text}],
                priority=4,
            )
        except Exception as exc:  # noqa: BLE001  HUD 推不出去不影响审批
            self.logger.warning("HUD 推送失败（{}）：{}", type(exc).__name__, exc)

    async def _create_approval_card(self, pending: Any) -> None:
        request = pending.request
        # 主语用归因语法生成，和播报说同一套话——不能卡片一套、播报一套。
        subject = describe_actor(
            executor=request.agent or "claude-code",
            requester_evidence="", requester=None,
        )
        body = request.tool_summary or request.tool_name or "未知工具"
        timeout = self._approval.timeout_seconds
        html = build_card_html(
            subject=subject, tool_summary=body, session_id=request.session_id,
            working_directory=request.working_directory, timeout_seconds=timeout,
            advice_state=self._approval.advice_state(),
            risk=pending.severity, rule=pending.rule,
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
            note = (f"已由 L2 自动放行（凭你的授权 {pending.authorization or '-'}，"
                    f"模型 {pending.judge_model or '-'}，理由 {pending.judge_reason or '低风险'}）")
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
            default="解释这条审批请求是什么、L0 规则对它有什么判断。"
                    "不接模型，只说规则层能讲准的事实。",
        ),
    )
    async def approval_explain(self, request_id: str, _ctx: dict):
        """「这是什么」按钮的落点。L0 级解释，不猜意图。"""
        if self._approval is None:
            return Err(SdkError("审批服务未运行"))
        pending = self._approval.find(request_id)
        if pending is None:
            return Err(SdkError("找不到这个审批请求（可能已过期并被清理）"))
        explanation = explain_request(
            tool_name=pending.request.tool_name,
            tool_summary=pending.request.tool_summary,
            working_directory=pending.request.working_directory,
            rule=pending.rule,
            severity=pending.severity,
        )
        try:
            card = self.ctx.get_card(_ctx["card_id"], target_lanlan=_ctx.get("lanlan_name"))
            await card.update(html=build_explain_html(explanation),
                              summary=explanation["title"], actions={})
        except (CardSubmissionError, KeyError) as exc:
            self.logger.warning("解释卡片更新失败（{}）：请求 {}", exc, request_id)
        return Ok(explanation)

    @ui.action(id="approval_authorize", label=tr("entry.approval_authorize.name",
                                                 default="授权确认"))
    @plugin_entry(
        id="approval_authorize",
        name=tr("entry.approval_authorize.name", default="授权确认"),
        description=tr(
            "entry.approval_authorize.description",
            default="L1/L2 的授权入口：弹出授权文案让用户确认（含「模型判断可能出错，"
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
                "message": "请在卡片上点「确认授权」；未确认前 L1/L2 均不生效。",
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
            adapters={
                "claude_code_transcript": "ready",
                "gateway_metadata": "ready",
            },
            approval=self._approval.stats() if self._approval is not None else {"running": 0},
            authorization=(self._approval.authorization_snapshot()
                           if self._approval is not None else None),
        )

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
