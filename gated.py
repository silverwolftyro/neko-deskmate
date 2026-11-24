"""P1c(a)：闸门自己造成的失败，不该记到 Agent 头上。

2026-11-24 真机：审批 570s 到期 → Claude 收到 ``tool_result is_error=true``
→ 120 秒后连发三条「出事了 / Bash 执行失败，它没自己恢复」（三个会话，
文案一字不差）。**失败是插件的闸门造成的，却被报成 Agent 故障。**
用户越忙没点卡，通知越多、越像 Agent 坏了——这正是项目最怕的警报疲劳。

判据只能用插件自己已经知道的事实，**不读内容**（隐私红线：``Signal``
没有 content 字段，tracker 结构上碰不到命令原文）：

  * 一条审批了结时**工具没有跑**（到期没人点 / 用户点拒绝 / 回填被断开）
    → 把 ``(session_id, tool_use_id)`` 记进这本簿子；
  * 同一条 ``tool_result``（``ok=False``）回来时查簿子，命中即判**自伤**：
    tracker 只闭环、不当成 Agent 的失败记账——不点亮 🔴、不开口。

**为什么保留窗口可以给得宽**：精确配对是 ``(session, tool_use_id)``，
宽窗口最多让一条迟到的 tool_result 也被认出来，殃及不到别的调用
（tool_use_id 在父子代理间会重复，但配上 session 之后仍然只指向同一会话里
同一次调用）。窗口的存在只是给这本簿子一个上界，防止无限增长。

**降级路径**（审批侧拿不到 ``tool_use_id``）：见 ``claim`` 的文档。
降级宁可少报、不要误报，而且每次都留日志——**静默的降级等于没有降级**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# 簿子的保留窗口。审批记录本身保留 3600s（``approval.DEFAULT_RETENTION_SECONDS``），
# 这里取 600s：足够覆盖「到期 → tool_result 落进转录 → 下一轮 3 秒轮询读到」
# 这条链（各环节都是秒级），又不拖着十分钟前的旧事。
GATED_FAILURE_WINDOW_SECONDS = 600.0

# 簿子的硬顶。超了丢最旧的——一本只用来配对的簿子不该变成内存泄漏。
GATED_FAILURE_LIMIT = 256

# claim() 的返回值。空串 = 没命中。
MODE_EXACT = "exact"
MODE_DEGRADED = "degraded"


@dataclass(frozen=True)
class GatedFailure:
    """一次「工具没跑成」的审批。

    ``kind`` 是审批时的工具名，**目前只作记录用**（排查时看得出是哪一类
    工具被挡了）：降级路径按「会话 + 窗口」判，不用它。想用它把降级路径
    收窄成「同工具名」的话，得先解决"tool_result 块不带工具名"这件事
    （见 ``events._parse_tool_results``）——那时再收窄，别提前收。
    """

    session: str
    tool_use_id: str
    kind: str          # 审批时的工具名（tool_name）
    at: float          # 记进簿子的时刻


class GatedFailureLog:
    """「闸门造成的失败」登记簿。

    **不加锁**：只在观测轮询那一个线程里被读和写（``_poll_once`` 是同步函数，
    由 ``watch_tick`` 丢进 ``asyncio.to_thread``，同一时刻只有一个实例在跑）。
    真要多线程用，先加锁——不要指望调用方记得。
    """

    def __init__(self, *, window: float = GATED_FAILURE_WINDOW_SECONDS,
                 limit: int = GATED_FAILURE_LIMIT) -> None:
        self._window = float(window)
        self._limit = int(limit)
        self._entries: list[GatedFailure] = []

    def __len__(self) -> int:
        return len(self._entries)

    def record(self, *, session: str, tool_use_id: str, kind: str,
               at: float | None = None) -> bool:
        """登记一条「工具没跑成」。返回 False 表示**没登记**（重复或没有会话）。

        同一 ``(session, tool_use_id)`` 重复登记是空操作：轮询每 3 秒会扫一遍
        已了结的审批，不幂等的话 ``at`` 会被不断刷新，窗口永远不过期，
        簿子跟着无限增长。这是被实测逼出来的——``_approval_blocked_sessions``
        就是同样的每轮全扫，那条路上已经踩过一次。
        """
        session = (session or "").strip()
        if not session:
            # 没有 session 就配不上任何 tracker 桶，登记它没有意义。
            return False
        tool_use_id = (tool_use_id or "").strip()
        for entry in self._entries:
            if (entry.session, entry.tool_use_id) == (session, tool_use_id):
                return False
        moment = time.time() if at is None else float(at)
        self._entries.append(GatedFailure(
            session=session, tool_use_id=tool_use_id,
            kind=(kind or "").strip(),
            at=moment))
        self._prune(moment)
        return True

    def claim(self, *, session: str, tool_use_id: str,
              degraded_window: float, now: float | None = None) -> str:
        """这条失败是闸门造成的吗。返回 ``""`` / ``MODE_EXACT`` / ``MODE_DEGRADED``。

        ``now`` 可注入（同 ``ProgressTracker.tick``）：时钟是判断的唯一依据，
        不可注入就等于没法测"窗口过了会怎样"。

        精确路：``(session, tool_use_id)`` 命中。``tool_result`` 块**必然**带
        ``tool_use_id``——没有它的块在 ``events._parse_tool_results`` 里就被
        跳过了（配对是 outcome 的全部意义）。所以只要审批侧有 id，这条路一定通。
        命中**不删除**条目：它只可能再配到同一次调用，而重复喂同一条
        tool_result（适配器重读、测试里手工喂两遍）不该让第二次变成假警报。

        降级路：**审批侧**没有 id 时走这条。此时精确配对不可能，只能按
        「这个会话刚有一次闸门没放行 + 窗口内的首次失败」判。窗口取
        ``failure_confirm_seconds``——再久就等于在猜"这次失败和那次审批
        有关"，而猜错的方向是**压掉一次真失败**（最坏方向）。
        **一次审批只免一次**：命中即把条目销掉，下一条新审批才重新拿到名额。

        ⚠️ 这条路**不要求调用方也没有 id**。曾经写成"两边都没有 id"，
        结果``tool_result`` 块在 ``events._parse_tool_results`` 里没有 id
        就被整块丢掉——也就是说事件侧恒有 id，那条降级分支在生产里
        **永远走不到**，是死代码。死掉的降级比没有降级更坏：它让
        "hook 负载不带 tool_use_id 的机器上 P1c 静默失效"看起来像是
        已经防住了。
        """
        session = (session or "").strip()
        if not session:
            return ""
        tool_use_id = (tool_use_id or "").strip()
        moment = time.time() if now is None else float(now)
        self._prune(moment)

        # ── 精确路 ──
        # 有 id 就先试着精确配。**配不上不提前返回**——同一个会话可能同时
        # 躺着一条有 id 的和一条没 id 的记录，配不上前者不等于能排除后者。
        if tool_use_id:
            for entry in self._entries:
                if (entry.session, entry.tool_use_id) == (session, tool_use_id):
                    return MODE_EXACT

        # ── 降级路 ──
        # 只认"审批侧也没拿到 id"的条目。一次审批只免一次（见上面的 ⚠️）。
        for index, entry in enumerate(self._entries):
            if entry.session != session or entry.tool_use_id:
                continue
            if moment - entry.at <= degraded_window:
                # 用掉即销。不销的话这个会话在整段窗口里只有一次免罪票，
                # 而那次可能早就用在一件无关的事上。
                del self._entries[index]
                return MODE_DEGRADED
        return ""

    def _prune(self, now: float) -> None:
        """过期条目出库。"""
        cutoff = now - self._window
        kept = [entry for entry in self._entries if entry.at >= cutoff]
        if len(kept) > self._limit:
            del kept[: len(kept) - self._limit]        # 丢最旧的
        self._entries = kept
