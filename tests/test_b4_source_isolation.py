"""B4：一个观测源坏了，不能把另一个源一起拖死。

真机上会发生的三件事，全部指向同一个结构缺陷：
  1. 网关库被锁 / 表改名 / WAL 缺 -shm → ``sqlite3.OperationalError``
     冒到 ``watch_tick``，**本轮已经读到的转录事件整批丢掉**——删除播报首当其冲；
  2. ``GatewayAdapter.__init__`` 取水位就抛 → ``_ensure_watchers`` 每轮失败，
     转录适配器永远建不起来：**网关坏了，删了什么都不播**；
  3. 运行期新开的会话，它的转录文件第一次被见到时 offset 直接设到末尾
     ——新会话开头的事件全丢。

修法：两个源各自 try/except（失败只降级自己 + 限频日志）、
构造各自兜住、网关只 SELECT 用得上的列、新文件区分"启动时已在"与"运行期新增"。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import plugin.plugins.agent_deskmate as module
from plugin.plugins.agent_deskmate import (
    SOURCE_ERROR_LOG_EVERY,
    AgentDeskmatePlugin,
    _hits_for_event,
)
from plugin.plugins.agent_deskmate.adapters.claude_code import ClaudeCodeAdapter
from plugin.plugins.agent_deskmate.adapters.gateway import (
    GATEWAY_COLUMNS,
    GatewayAdapter,
    GatewayReadError,
    current_watermark,
    read_new_gateway_events,
)
from plugin.plugins.agent_deskmate.tracker import ProgressTracker

GATEWAY_SCHEMA = """
CREATE TABLE proxy_request_logs (
    request_id TEXT, app_type TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    total_cost_usd TEXT, latency_ms INTEGER,
    status_code INTEGER, error_message TEXT,
    session_id TEXT, created_at INTEGER
)
"""


class ListLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: object) -> None:
        try:
            rendered = str(message).format(*args) if args else str(message)
        except (IndexError, KeyError, ValueError):
            rendered = f"{message} | {args!r}"
        self.lines.append((level, rendered))

    def info(self, message: str, *args: object) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log("warning", message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log("error", message, *args)

    def texts(self, level: str = "") -> list[str]:
        return [text for lv, text in self.lines if not level or lv == level]


class RecordingPush:
    """push_message 的记录桩。返回值形状与 SDK 一致（submitted=True）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return {"submitted": True}


class WatcherHost:
    """只借 ``AgentDeskmatePlugin`` 的那几个方法，其余属性手搭。

    不构造真插件：那需要宿主上下文，而这里要测的只是
    "两个源互不拖累"这一条逻辑。借方法而不是抄一遍，
    改动会立刻反映到测试上。
    """

    _ensure_watchers = AgentDeskmatePlugin._ensure_watchers
    _poll_source = AgentDeskmatePlugin._poll_source
    _poll_once = AgentDeskmatePlugin._poll_once
    _sync_tracker_config = AgentDeskmatePlugin._sync_tracker_config
    _feed_tracker = AgentDeskmatePlugin._feed_tracker
    _adapter_status = AgentDeskmatePlugin._adapter_status
    _ADAPTER_SOURCES = AgentDeskmatePlugin._ADAPTER_SOURCES

    def __init__(self) -> None:
        self._broadcaster = None
        self._transcript_adapter = None
        self._gateway_adapter = None
        self._source_error_at: dict[str, float] = {}
        self._source_state: dict[str, str] = {}
        # C 批 §三：真插件这两个字段由 _reset_watch_state 建，这里手搭一份，
        # 否则 _poll_once 第一轮就 AttributeError。
        self._tracker = ProgressTracker()
        self._tracker_cfg = None
        self._pending_pushes: list = []
        self.logger = ListLogger()
        self.push_message = RecordingPush()


def _tool_line(command: str, session: str = "sess-1") -> str:
    return json.dumps({
        "type": "assistant",
        "sessionId": session,
        "isSidechain": False,
        "timestamp": "2026-11-22T06:00:00.000Z",
        "cwd": r"D:\workspace\proj",
        "message": {
            "role": "assistant",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                         "input": {"command": command}}],
        },
    }, ensure_ascii=False)


def _transcript(tmp_path: Path, commands: list[str]) -> Path:
    target = tmp_path / "projects" / "proj.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(_tool_line(c) for c in commands) + "\n",
                      encoding="utf-8")
    return target


def _gateway_db(tmp_path: Path, rows: int = 2) -> Path:
    db = tmp_path / "cc-switch.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute(GATEWAY_SCHEMA)
        for index in range(rows):
            connection.execute(
                "INSERT INTO proxy_request_logs (app_type, status_code, created_at) "
                "VALUES ('claude', 200, ?)", (1795193997 + index,))
        connection.commit()
    finally:
        connection.close()
    return db


def _section(tmp_path: Path) -> dict:
    return {"coalesce_seconds": 60,
            "watch": {"claude_projects_dir": str(tmp_path / "projects"),
                      "cc_switch_db": str(tmp_path / "cc-switch.db")}}


def _broken_db(tmp_path: Path, *, name: str = "broken.db") -> Path:
    """一个能连上但**没有那张表**的库——最接近"表被改名"的真实故障。"""
    db = tmp_path / name
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE something_else (x INTEGER)")
        connection.commit()
    finally:
        connection.close()
    return db


# ── 一个源失败，另一个源照常播 ────────────────────────────────────────────

def test_a_gateway_failure_does_not_eat_the_transcript_events(tmp_path: Path) -> None:
    """任务书 B4 测试第一条：网关抛异常 → 当轮转录事件仍然播报。

    老结构是顺序两个 poll，网关一抛，本轮已读到的转录事件整批跟着丢——
    异常冒到 watch_tick，整轮作废。
    """
    _transcript(tmp_path, ["ls -la"])
    _gateway_db(tmp_path)
    host = WatcherHost()
    host._ensure_watchers(_section(tmp_path))
    assert host._gateway_adapter is not None
    host._poll_once(_section(tmp_path))      # 首轮：记水位，别把后面的算成历史

    # 把网关这一轮弄坏：表被改名（真实故障形态之一）
    host._gateway_adapter.db_path = _broken_db(tmp_path)
    target = tmp_path / "projects" / "proj.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf build/") + "\n")

    outcomes = host._poll_once(_section(tmp_path))
    # 转录的删除播报出去了；网关坏掉只让它自己这一轮空手
    assert host.push_message.calls, "网关故障把转录播报一起吞了"
    assert any("删除" in call["parts"][0]["text"]
               for call in host.push_message.calls)
    assert outcomes
    # 网关那一轮有记账（限频之后只有一条）
    assert any("网关观测源本轮读取失败" in text
               for text in host.logger.texts("warning"))


def test_a_gateway_failure_is_rate_limited_in_the_log(tmp_path: Path) -> None:
    """持续故障不能每 3 秒刷一条 warning——那会把删除记录冲出日志。"""
    _gateway_db(tmp_path)
    host = WatcherHost()
    host._ensure_watchers(_section(tmp_path))
    host._gateway_adapter.db_path = _broken_db(tmp_path)

    for _ in range(4):
        host._poll_source("网关", host._gateway_adapter)
    warnings = [text for text in host.logger.texts("warning")
                if "网关观测源本轮读取失败" in text]
    assert len(warnings) == 1, f"限频没生效，刷了 {len(warnings)} 条"


def test_the_rate_limit_opens_up_again_after_the_window(tmp_path: Path) -> None:
    """限频不是静音：过了窗口要再说一次，否则故障恢复了也没人知道。"""
    _gateway_db(tmp_path)
    host = WatcherHost()
    host._ensure_watchers(_section(tmp_path))
    host._gateway_adapter.db_path = _broken_db(tmp_path)

    host._poll_source("网关", host._gateway_adapter)
    # 把记账拨到窗口之前，模拟"已经过去很久了"
    host._source_error_at["网关"] = time.monotonic() - SOURCE_ERROR_LOG_EVERY - 1
    host._poll_source("网关", host._gateway_adapter)
    warnings = [text for text in host.logger.texts("warning")
                if "网关观测源本轮读取失败" in text]
    assert len(warnings) == 2


def test_the_watermark_does_not_move_when_a_read_fails(tmp_path: Path) -> None:
    """失败时水位原地不动——否则那一段增量被永久跳过。"""
    db = _gateway_db(tmp_path, rows=2)
    adapter = GatewayAdapter(db)
    assert adapter.watermark == 2
    adapter.db_path = _broken_db(tmp_path)
    with pytest.raises(GatewayReadError):
        adapter.poll()
    assert adapter.watermark == 2
    # 修好之后，同一段增量还能读到（不是被跳过）
    adapter.db_path = db
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "INSERT INTO proxy_request_logs (app_type, status_code, created_at) "
            "VALUES ('claude', 200, 1795194000)")
    connection.close()
    assert len(adapter.poll()) == 1
    assert adapter.watermark == 3


# ── 构造失败不阻塞另一个源 ────────────────────────────────────────────────

def test_a_broken_gateway_does_not_stop_the_transcript_source(tmp_path: Path) -> None:
    """任务书 B4 第二条：网关建不起来，转录照样起来。

    老实现里 ``GatewayAdapter(db)`` 构造时就取水位，一抛，
    ``_ensure_watchers`` 整轮返回 None——**网关坏了，删了什么都不播**。
    """
    _transcript(tmp_path, ["ls -la"])
    _broken_db(tmp_path, name="cc-switch.db")     # 配置指向的那个路径
    host = WatcherHost()
    broadcaster = host._ensure_watchers(_section(tmp_path))
    assert broadcaster is not None, "网关建不起来把转录一起挡住了"
    assert host._transcript_adapter is not None
    assert host._gateway_adapter is None
    assert any("网关观测源建不起来" in text for text in host.logger.texts("warning"))

    # 转录真的能工作（先跑一轮记水位，再把新事件追加进去）
    host._poll_once(_section(tmp_path))
    target = tmp_path / "projects" / "proj.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf D:/other/x.txt") + "\n")
    outcomes = host._poll_once(_section(tmp_path))
    assert outcomes
    assert host.push_message.calls


# ── 网关只 SELECT 用得上的列 ──────────────────────────────────────────────

def test_only_the_needed_columns_are_selected() -> None:
    """``SELECT *`` 换成显式列名：依赖写在明面上。"""
    assert "app_type" in GATEWAY_COLUMNS
    assert "status_code" in GATEWAY_COLUMNS
    assert "total_cost_usd" in GATEWAY_COLUMNS
    # 用不到的不取（原表 28 列）
    assert "request_id" not in GATEWAY_COLUMNS
    assert len(GATEWAY_COLUMNS) < 20


def test_an_unexpected_extra_column_does_not_break_the_read(tmp_path: Path) -> None:
    """别的程序给表加了列，我们照常读——``*`` 会把形状搅变。"""
    db = _gateway_db(tmp_path, rows=1)
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("ALTER TABLE proxy_request_logs ADD COLUMN extra TEXT")
        connection.execute("UPDATE proxy_request_logs SET extra = 'x'")
    connection.close()
    events, watermark = read_new_gateway_events(db, 0)
    assert len(events) == 1
    assert watermark == 1


def test_a_missing_column_fails_loudly(tmp_path: Path) -> None:
    """少一个我们依赖的列 → 当场报错，而不是安静地少一列数据。

    这正是显式列名的好处：``SELECT *`` 在列被删时会把 None 塞进来，
    读出来的东西看着正常其实缺字段。
    """
    db = tmp_path / "narrow.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE proxy_request_logs (app_type TEXT)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(sqlite3.Error):
        read_new_gateway_events(db, 0)


# ── 运行期新增的转录文件：首批事件不丢 ────────────────────────────────────

def test_a_transcript_file_that_appears_at_runtime_is_read_from_the_start(
        tmp_path: Path) -> None:
    """任务书 B4 测试第二条：运行期新建转录文件 → 首批事件不丢。

    老实现"首次见到就把 offset 设到末尾"，于是插件加载之后新开的会话，
    它开头的事件（含第一次删除）整段消失。
    """
    _transcript(tmp_path, ["ls -la"])
    adapter = ClaudeCodeAdapter(tmp_path / "projects")
    assert adapter.poll() == []          # 启动时已有的：只记水位

    # 运行期新会话
    fresh = tmp_path / "projects" / "new-session.jsonl"
    fresh.write_text(_tool_line("rm -rf build/") + "\n", encoding="utf-8")
    events = adapter.poll()
    assert [event.command for event in events] == ["rm -rf build/"]


def test_a_file_that_existed_at_startup_still_starts_at_the_end(
        tmp_path: Path) -> None:
    """反过来也成立：启动时就在的文件不把几个月历史倒进来播。"""
    _transcript(tmp_path, ["rm -rf old-history", "rm -rf older-history"])
    adapter = ClaudeCodeAdapter(tmp_path / "projects")
    assert adapter.poll() == []
    assert adapter.poll() == []

    target = tmp_path / "projects" / "proj.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf fresh") + "\n")
    events = adapter.poll()
    assert [event.command for event in events] == ["rm -rf fresh"]


def test_a_runtime_file_that_grows_is_read_incrementally(tmp_path: Path) -> None:
    """新文件也不是每轮全量重读：offset 照常前移。"""
    _transcript(tmp_path, ["ls -la"])
    adapter = ClaudeCodeAdapter(tmp_path / "projects")
    adapter.poll()

    fresh = tmp_path / "projects" / "new.jsonl"
    fresh.write_text(_tool_line("rm -rf a") + "\n", encoding="utf-8")
    assert [e.command for e in adapter.poll()] == ["rm -rf a"]
    with fresh.open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf b") + "\n")
    assert [e.command for e in adapter.poll()] == ["rm -rf b"]


def test_a_transcript_read_failure_does_not_stop_the_gateway(tmp_path: Path) -> None:
    """对称的一条：转录这边炸了，网关那边照常。"""

    class ExplodingAdapter:
        def poll(self, *args: object, **kwargs: object) -> list:
            raise OSError("文件被占用")

    host = WatcherHost()
    host._ensure_watchers(_section(tmp_path))
    host._transcript_adapter = ExplodingAdapter()
    with pytest.raises(OSError):
        host._poll_source("转录", host._transcript_adapter)


# ── 水位与诊断的一致性 ───────────────────────────────────────────────────

def test_current_watermark_on_a_broken_db_is_a_clear_error(tmp_path: Path) -> None:
    """构造期失败要能被认出来（GatewayReadError 的上游就是它）。"""
    db = _broken_db(tmp_path)
    with pytest.raises(sqlite3.Error):
        current_watermark(db)


def test_diagnose_still_works_when_the_gateway_is_broken(tmp_path: Path) -> None:
    """诊断入口读网关尾部也要能活——它跟常驻轮询是两个入口，但同一批故障。"""
    _transcript(tmp_path, ["rm -rf build/"])
    _broken_db(tmp_path)
    reply = module.diagnose(_section(tmp_path))
    assert reply.events_read >= 1
    assert reply.hits >= 1


def test_hits_for_event_is_untouched_by_source_isolation(tmp_path: Path) -> None:
    """隔离只发生在取数据那一层，规则层照旧。"""
    _transcript(tmp_path, ["rm -rf D:/other/x.txt"])
    adapter = ClaudeCodeAdapter(tmp_path / "projects")
    adapter.poll()
    target = tmp_path / "projects" / "proj.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_tool_line("rm -rf D:/other/x.txt") + "\n")
    hits = [hit for event in adapter.poll() for hit in _hits_for_event(event)]
    assert len(hits) == 1
    assert hits[0].rule == "delete"
    assert hits[0].severity == "critical"
