"""会话清单：每个 Agent 现在有哪些会话、最近还在不在动。

与 ``discovery`` 的分工，一句话：

- ``discovery`` 判「这个 Agent 在不在本机」——只判目录存在；
- **这里**判「它有哪些会话、还在动吗」——只读 mtime 和 ``created_at``。

## 为什么需要单独一层（2026-11-25 真机逼出来的）

面板原来的会话列表来自**事件流水线**（``tracker``），而 B4 的设计让适配器
对「插件启动时就存在的转录文件」**跳到末尾**——理由是"不把几个月历史
倒进来播一遍"。这个理由本身没错，错的是它渗到了面板上：

    用户明明开着 agent 在跑  →  面板写「还没看到任何会话」

那不是故障，是**观测面的定义比用户心里那条窄**。用户问的是"本机有没有
会话在跑"，面板答的是"插件启动后来没来新事件"——两个问题被当成一个了。

所以这一层**绕过事件流水线**，直接枚举磁盘/数据库里的会话。
它报的事实和流水线报的事实互相独立，可以对照着看。

## 三条约束

1. **不读任何内容**。只用 ``stat().st_mtime`` / ``st_size`` 和 sqlite 的
   ``MAX(created_at)``。会话里说了什么，这一层一概不知道。
2. **``live`` 是"最近有活动"，不是"进程还活着"**。我们没有 PID，
   Claude Code 的会话目录里也没有心跳文件（实测：只有 ``subagents/``、
   ``tool-results/``、``auto-mode-classifier-error.txt``）。
   mtime 停更可能是跑完了，也可能是模型在思考、可能只是用户走开了——
   **分不出来，所以文案不许写"正在运行"**。
3. **网关那次查询要花钱**。``proxy_request_logs`` 六万行，
   ``GROUP BY session_id`` 每 3 秒全表扫一遍不值得。所以
   ``glance_gateway_sessions`` 默认只回溯 ``GATEWAY_LOOKBACK`` 秒，
   且调用方（面板）本来就不是每轮都调。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 多嫩算「最近有活动」。取 300 秒与插件既有的「未回包 = 300s」阈值对齐——
#: 同一个数字在两处出现，比"各定一个"更容易说清。
LIVE_WINDOW = 300.0

#: 网关侧回溯多久。只影响"列得出哪些历史会话"，不影响 ``live`` 判定。
GATEWAY_LOOKBACK = 24 * 3600.0


@dataclass(frozen=True)
class SessionGlance:
    """一个会话的一瞥。**只有元数据，没有内容。**"""

    agent: str
    #: 会话标识。转录侧是文件名去扩展名（Claude Code 的 session UUID），
    #: 网关侧是 ``proxy_request_logs.session_id``。
    session: str
    #: 最后一次活动的 unix 秒。转录侧 = 文件 mtime。
    last_seen: float
    #: 文件字节数。转录侧有，网关侧 0（不知道）。
    size_bytes: int = 0
    #: 出处。转录侧 = projects 下的子目录名（能看出是哪个工作区）；
    #: 网关侧 = ``app_type``。
    where: str = ""
    #: 最近 ``live_window`` 秒内有没有活动。
    live: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "session": self.session,
            "last_seen": self.last_seen,
            "size_bytes": self.size_bytes,
            "where": self.where,
            "live": self.live,
        }


@dataclass
class AgentSessions:
    """一个 Agent 的会话汇总。"""

    agent: str
    sessions: list[SessionGlance] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sessions)

    @property
    def live(self) -> int:
        return sum(1 for s in self.sessions if s.live)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "total": self.total,
            "live": self.live,
            "sessions": [s.as_dict() for s in self.sessions],
        }


def _short(session: str) -> str:
    """UUID 只留前 8 位——面板上是给人眼扫的，不是给机器对的。"""
    return session[:8] if session else ""


def glance_transcript_sessions(
    projects_dir: Any,
    *,
    agent: str = "claude",
    now: float | None = None,
    live_window: float = LIVE_WINDOW,
    limit: int = 20,
) -> list[SessionGlance]:
    """枚举转录目录下的会话，按最近活动排序。

    **与 ``iter_transcript_files`` 的区别**：那个按 mtime 排，但只被
    适配器用来决定读哪些；这个专门给"盘家底"用，所以带上
    ``size_bytes`` 和 ``live``，且**不限 ``max_files``**——
    家底要盘全，20 个和 200 个看到的结论不一样。
    """
    stamp = time.time() if now is None else float(now)
    root = Path(projects_dir)
    if not root.is_dir():
        return []

    glances: list[SessionGlance] = []
    for path in root.rglob("*.jsonl"):
        # ``<session>/subagents/agent-aa*.jsonl`` 是**子 agent 的运行记录**，
        # 不是会话。不排除的话会话数会虚高，而且它们共享 ``agent-aa`` 前缀，
        # 短显示下看起来像同一个会话重复出现。
        # 实测本机：4 个子 agent 文件 vs 30 个真会话文件。
        if "subagents" in path.parts:
            continue
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            # 单个文件读不到 stat（被锁/权限）不该让整盘家底失败。
            continue
        last_seen = float(stat.st_mtime)
        glances.append(SessionGlance(
            agent=agent,
            session=path.stem,
            last_seen=last_seen,
            size_bytes=int(stat.st_size),
            where=path.parent.name,
            live=(stamp - last_seen) <= live_window,
        ))
    glances.sort(key=lambda g: g.last_seen, reverse=True)
    return glances[:limit] if limit else glances


def gateway_app_types(
    db_path: Any,
    *,
    lookback: float = GATEWAY_LOOKBACK,
    now: float | None = None,
) -> dict[str, int]:
    """网关里各 ``app_type`` 的请求数。

    **这是本模块最重要的一个函数**，理由见下面"网关不按 agent 分账"。

    只做聚合，不读任何内容。返回空 dict 表示"盘不出来"（库不存在）——
    调用方据此把网关侧 Agent 降级，而不是假装它们不存在。

    同样**可能抛 ``sqlite3.Error``**，调用方自己兜。
    """
    stamp = time.time() if now is None else float(now)
    path = Path(db_path)
    if not path.is_file():
        return {}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT app_type, COUNT(*) FROM proxy_request_logs "
            "WHERE created_at >= ? AND app_type IS NOT NULL AND app_type != '' "
            "GROUP BY app_type",
            (stamp - float(lookback),),
        )
        return {str(row[0]): int(row[1]) for row in cursor}
    finally:
        connection.close()


def glance_gateway_sessions(
    db_path: Any,
    *,
    agent: str = "codex",
    app_type: str = "",
    now: float | None = None,
    live_window: float = LIVE_WINDOW,
    lookback: float = GATEWAY_LOOKBACK,
    limit: int = 20,
) -> list[SessionGlance]:
    """从 cc-switch 网关盘会话。

    **``app_type`` 必填才有意义**。网关是**所有 agent 共写一张表**的，
    不按 ``app_type`` 过滤的话，给 Codex 盘出来的会话里排第一的
    很可能是 Claude Code——实测本机正是如此（Claude 会话 706 次请求，
    Codex 只有 3 次）。那又是个"标签比实现说得好听"的坑。

    **可能抛 ``sqlite3.Error``**：库被锁 / 表被改名都在这里冒出去。
    调用方要自己兜（见 ``__init__.panel_context`` 的 try）——
    一个源盘不了家底，不该让另一个源的也盘不了。

    只回溯 ``lookback`` 秒：``proxy_request_logs`` 六万行，
    每 3 秒全表 ``GROUP BY`` 是在拿用户的 CPU 换一个面板数字。
    """
    stamp = time.time() if now is None else float(now)
    path = Path(db_path)
    if not path.is_file():
        return []

    since = stamp - float(lookback)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT session_id, MAX(created_at) AS last_at, COUNT(*) AS hits "
            "FROM proxy_request_logs "
            "WHERE created_at >= ? AND session_id IS NOT NULL AND session_id != '' "
            "  AND app_type = ? "
            "GROUP BY session_id ORDER BY last_at DESC LIMIT ?",
            (since, app_type or agent, limit),
        )
        glances: list[SessionGlance] = []
        for row in cursor:
            last_seen = float(row["last_at"] or 0.0)
            glances.append(SessionGlance(
                agent=agent,
                session=str(row["session_id"]),
                last_seen=last_seen,
                where=str(row["hits"] or 0) + " 次请求",
                live=(stamp - last_seen) <= live_window,
            ))
        return glances
    finally:
        connection.close()


def group_by_agent(glances: Iterable[SessionGlance]) -> dict[str, AgentSessions]:
    """按 agent 归堆。顺序保持传入顺序（调用方已按需排序）。"""
    grouped: dict[str, AgentSessions] = {}
    for glance in glances:
        bucket = grouped.get(glance.agent)
        if bucket is None:
            bucket = AgentSessions(agent=glance.agent)
            grouped[glance.agent] = bucket
        bucket.sessions.append(glance)
    return grouped


def describe(glances: Iterable[SessionGlance], *, live_window: float = LIVE_WINDOW) -> str:
    """一行人话概要。给日志用——面板有自己的渲染，不走这条。"""
    items = list(glances)
    if not items:
        return "没有会话"
    live = sum(1 for g in items if g.live)
    return "{} 个会话，{} 个最近 {} 秒内有活动".format(
        len(items), live, int(live_window))
