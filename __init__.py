"""同桌猫（agent_deskmate）—— N.E.K.O 被动监听插件。

基本版（M0-lite）路线：骨架能加载 → Claude Code 转录 tail + 网关元数据观测 →
L0 规则层 → SDK 播报 → 审批中继 → 零 key 行为。
当前进度：**M0-lite-2 观测 + 统一事件**（M0-lite-1 骨架已通过官方四条验收）。

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
from .broadcast import Broadcaster
from .events import UnifiedEvent
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


def _expand(path_value: object) -> Path:
    """展开配置里的 ~ 和环境变量。配置给模板，代码不写死绝对路径。"""
    text = str(path_value or "").strip()
    if not text:
        return Path()
    return Path(os.path.expanduser(os.path.expandvars(text)))


def _to_number(value: object, default: float) -> float:
    """配置里的数值容错转换；坏值退回默认，不让观测循环崩。"""
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return result if result == result else default


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
        return Ok({
            "status": "running",
            "plugin_id": self.plugin_id,
            "source": SOURCE,
            "stage": STAGE,
        })

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

    async def _watch_config(self) -> dict[str, str]:
        """读出观测路径配置。缺配置时返回空 dict，由调用方判"不可用"，不猜默认路径。"""
        try:
            dumped = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("读取配置失败：{}", type(exc).__name__)
            return {}
        if not isinstance(dumped, dict):
            return {}
        watch = dumped.get("agent_deskmate")
        if not isinstance(watch, dict):
            return {}
        section = watch.get("watch", {})
        return section if isinstance(section, dict) else {}

    # ── 观测轮询（M0-lite-4 的常驻部分） ──────────────────────────────────

    def _ensure_watchers(self, section: dict[str, str]) -> Broadcaster | None:
        """按当前配置建好适配器与播报器。配置没给路径就返回 None（不猜默认值）。"""
        if self._broadcaster is not None:
            return self._broadcaster

        projects_dir = _expand(section.get("claude_projects_dir"))
        db_path = _expand(section.get("cc_switch_db"))
        if not projects_dir.is_dir() and not db_path.is_file():
            self.logger.warning(
                "两个观测源都不可用（transcript={} gateway={}），本轮跳过",
                projects_dir or "未配置", db_path or "未配置",
            )
            return None

        coalesce_seconds = _to_number(section.get("coalesce_seconds"), 60.0)
        self._transcript_adapter = ClaudeCodeAdapter(projects_dir) if projects_dir.is_dir() else None
        self._gateway_adapter = GatewayAdapter(db_path) if db_path.is_file() else None
        self._broadcaster = Broadcaster(
            self.push_message, self.logger, source=SOURCE,
            coalescer=Coalescer(window_seconds=coalesce_seconds),
        )
        self.logger.info(
            "观测适配器就绪 transcript={} gateway={} 合并窗口={}s",
            projects_dir, db_path, coalesce_seconds,
        )
        return self._broadcaster

    def _poll_once(self, section: dict[str, str]) -> list[str]:
        """扫一轮观测源 → 跑规则 → 播报。同步函数，由 timer 丢进线程执行。

        返回本轮播报结果列表，便于测试与入口观察。
        """
        broadcaster = self._ensure_watchers(section)
        if broadcaster is None:
            return []

        events: list[UnifiedEvent] = []
        if self._transcript_adapter is not None:
            events.extend(self._transcript_adapter.poll())
        if self._gateway_adapter is not None:
            events.extend(self._gateway_adapter.poll())

        outcomes: list[str] = []
        for event in events:
            for hit in self._hits_for(event):
                outcomes.append(broadcaster.broadcast(hit))
                if len(outcomes) >= MAX_BROADCAST_PER_TICK:
                    self.logger.info("本轮播报已达上限 {} 条，余下留待下一轮",
                                     MAX_BROADCAST_PER_TICK)
                    return outcomes
        return outcomes

    @staticmethod
    def _hits_for(event: UnifiedEvent) -> list[RuleHit]:
        """一个事件跑当前阶段所有规则。

        删除类用完整命令判（summary 被截断，会漏掉长命令里的 rm）；
        失败类按真实状态码定严重度，不从 summary 文本反解析。
        """
        hits: list[RuleHit] = []

        if event.event == "tool_use" and event.command:
            hit = evaluate_delete(
                event.command,
                agent=event.agent, session=event.session, ts=event.ts,
            )
            if hit is not None:
                hits.append(hit)

        elif event.event == "error":
            if event.source_type == "proxy_request_logs":
                hit = evaluate_failure(
                    status_code=event.status_code,
                    error_message=event.summary,
                    agent=event.agent, session=event.session, ts=event.ts,
                )
            else:
                hit = evaluate_transcript_error(event)
            if hit is not None:
                hits.append(hit)

        return hits

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
            section = await self._watch_config()
            await asyncio.to_thread(self._poll_once, section)
        except Exception as exc:  # noqa: BLE001  一轮失败不能杀死 timer
            self.logger.warning("观测轮询异常（{}）：{}", type(exc).__name__, exc)

    # ── 入口 ───────────────────────────────────────────────────────────────

    @plugin_entry(
        id="status",
        name=tr("entry.status.name", default="插件状态"),
        description=tr("entry.status.description", default="返回同桌猫自身的运行状态。"),
        llm_result_fields=["status", "stage"],
    )
    async def status(self, **_) -> StatusReply:
        """返回插件自身状态；不观测任何 Agent。"""
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
        section = await self._watch_config()
        sources: dict[str, str] = {}
        events: list[UnifiedEvent] = []

        projects_dir = _expand(section.get("claude_projects_dir"))
        if projects_dir.is_dir():
            events.extend(self._recent_transcript_events(projects_dir, count))
            sources["claude_code_transcript"] = str(projects_dir)
        else:
            sources["claude_code_transcript"] = "未配置或目录不存在"

        db_path = _expand(section.get("cc_switch_db"))
        if db_path.is_file():
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

    # ── 一次性读取（不走增量水位） ──────────────────────────────────────────

    def _recent_transcript_events(self, projects_dir: Path, count: int) -> list[UnifiedEvent]:
        """往回扫最近一个转录文件的末尾，解析出事件。"""
        from .adapters.claude_code import iter_transcript_files, read_new_transcript_events

        files = iter_transcript_files(projects_dir)
        if not files:
            self.logger.info("转录目录下没有 jsonl：{}", projects_dir)
            return []
        newest = files[0]
        size = newest.stat().st_size
        offset = max(0, size - TRANSCRIPT_TAIL_BYTES)
        events, _ = read_new_transcript_events(newest, offset)
        self.logger.info("转录 tail：{} 末尾 {} 字节，解析出 {} 条",
                         newest.name, size - offset, len(events))
        return events[-count:]

    def _recent_gateway_events(self, db_path: Path, count: int) -> list[UnifiedEvent]:
        """按 rowid 取最后 count 行（一次性，不动常驻水位）。"""
        from .adapters.gateway import current_watermark, read_new_gateway_events

        watermark = current_watermark(db_path)
        events, _ = read_new_gateway_events(db_path, max(0, watermark - count))
        self.logger.info("网关账本：当前水位 {}，取回 {} 条", watermark, len(events))
        return events[-count:]
