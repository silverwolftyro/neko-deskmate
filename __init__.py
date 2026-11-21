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
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from plugin.sdk.plugin import (
    NekoPluginBase,
    Ok,
    lifecycle,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
)

from .adapters import ClaudeCodeAdapter, GatewayAdapter
from .adapters.claude_code import (
    iter_transcript_files,
    read_new_transcript_events,
)
from .adapters.gateway import current_watermark, read_new_gateway_events
from .broadcast import Broadcaster, render_text
from .events import UnifiedEvent, attribute
from .rules import (
    Coalescer,
    RuleHit,
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


class StatusReply(BaseModel):
    """status 入口的输出契约。返回模型实例，不要返回 dict。"""

    status: str = Field(description="running / stopped")
    plugin_id: str = Field(description="插件 id，恒为 agent_deskmate")
    source: str = Field(description="播报来源标签，恒为 agent_deskmate")
    stage: str = Field(description="当前所处的 M0-lite 阶段")
    uptime_sec: float | None = Field(default=None, description="运行秒数；未启动为 null")
    adapters: dict[str, str] = Field(default_factory=dict, description="各观测适配器的就绪状态")


class DumpEventsReply(BaseModel):
    """dump_events 入口的输出契约。"""

    count: int = Field(description="实际返回的事件条数")
    events: list[dict] = Field(description="统一事件列表，最新在最后")
    sources: dict[str, str] = Field(default_factory=dict, description="各数据源的读取情况")


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

    if attributed.event == "tool_use" and attributed.command:
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
        await self._log_first_run_hint()
        return Ok({
            "status": "running",
            "plugin_id": self.plugin_id,
            "source": SOURCE,
            "stage": STAGE,
        })

    async def _log_first_run_hint(self) -> None:
        """首启引导：没声明 mine roots 就在日志里提示一次。

        **只打日志，不播报**——这是配置建议，不是观测结果，按红线 1
        不能走 push_message 打扰用户。也不猜归因：提示只告诉用户
        "想显示『你自己开的』需要声明什么"，不替他声明。
        """
        try:
            config = await self._plugin_config()
        except Exception as exc:  # noqa: BLE001  引导失败绝不能拖垮 startup
            self.logger.warning("首启引导跳过（读配置失败：{}）", type(exc).__name__)
            return
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
        self._started_at = None
        return Ok({"status": "shutdown"})

    @lifecycle(id="config_change")
    def on_config_change(self, **_):
        """配置改了就让下一轮重新读路径与合并窗口，不等重启。"""
        self._watch_paths = None
        self._transcript_adapter = None
        self._gateway_adapter = None
        self._broadcaster = None
        self.logger.info("配置已变更，观测适配器将在下一轮重建")
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
