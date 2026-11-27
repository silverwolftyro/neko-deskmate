"""AgentHUD 任务卡（0.6.26）。**照着使魔任务 HUD 的形状做，但 entirely 我们自己的实现。**

## 参考的是什么

用户在 2026-11-27 给了两张官方截图，第二张（使魔任务 HUD）是真正的原本：

    🐾 使魔   ● 2  ◐ 0
    ├─ 跑单元测试 · agent-1      [运行中]
    │    运行命令 npm test / 在目录 D:/work/neko
    │    [始终允许] [这件事里都允许] [只这一次] [拒绝]
    ├─ 改文档 · agent-2          [等你回话]
    └─ 查资料：同类项目怎么写…    [等你回话]

**关键在结构：任务才是单位，提问挂在任务下面。** 我们第一版把待决审批平铺成一列，
只回答"有几件在等我"；这个形状同时回答"谁在跑、在跑什么、跑了多久"——
上下文回来了，而那才是这个东西的价值。

## 为什么不能直接用官方组件

链条在前端监听器里写死，没有插件能挂进去的地方：

    NekoPowerAskBlock.tsx:187  give() → onAction({action:'nekopower_ask_answer',
                                                   payload:{taskId, questionId,...}})
    app-nekopower-ask.js:840   全前端只有这一个监听器接它
    app-nekopower-ask.js:196   fetch('/api/agent/commissions/' + taskId + '/answer')
    agent_router.py:717        代理到 NekoPower agent 进程的 /console/commissions/{id}

发一个 ``nekopower_ask`` block，卡渲染得出来，但点下去 POST 到一个不存在的
commission id → 404 → 没反应。**比不做更糟**，所以形状照抄、实现自己做。

## 三条从官方那儿学到、但按我们的数据落地的

1. **按会话分组。** 数据本来就有：``snapshot()`` 每行带 ``agent`` / ``session_id``，
   tracker 的 ``SessionState`` 带四态灯、轮次、类别计数。不需要新数据源。
2. **每行四个按钮，``args`` 带各自的 ``request_id``。** 按钮 id 必须全局唯一
   （``actions`` 是字典，两个审批共用 "allow_once" 会互相覆盖），
   所以前缀上 request_id。
3. **没有输入框。** 官方那个「补一句」是原生 React 组件的能力；
   我们的 ``html_card`` 是禁脚本 iframe + 固定 JSON args，
   文档原话「不提供通用表单、数据绑定或 JavaScript 运行时」。
   拒绝理由走 ``approval.build_decision()`` 的 deny ``message`` 字段（已有），
   用户在 Claude Code 自己的输入框里补充——那本来就是它的通道。

## 什么时候显示 / 收起

一个会话满足任一条件就显示：**有待决审批** / **灯不是 IDLE** /
**最近 300 秒内有过事件**。全都不满足 → 整个面板收起。

300 秒取 ``tracker`` 的 ``unclosed_action_seconds`` 默认值——同一个"多久没动静
算结束"的判据，不另立一套。
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from .allowrules import BUTTON_SCOPES
from .commands import COMMAND_DISPLAY_MAX

#: 面板标题。窗口内直接以视图标题显示页签，与「任务」并列。
TITLE = "同桌猫 · 任务卡"

#: 会话多久没动静就从面板上消失。与 ``tracker`` 的 ``unclosed_action_seconds``
#: 默认值同义——"多久没动静算结束"只该有一个判据。
IDLE_AFTER_SECONDS = 300.0

#: 一个视图里最多几个会话。再多就截断——这个面板是"看一眼现在什么情况"，
#: 不是监控墙。真有人同时开十几个会话，他需要的是另一个工具。
MAX_SESSIONS = 5

#: 最近事件流水上限。
MAX_NOTICES = 5

WAITING_CSS = """
section { padding: 12px; font-size: 13px; line-height: 1.5; }
.head { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; }
.count { color: #888; font-size: 12px; }
.session { border: 1px solid #e4e4e7; border-radius: 8px;
           padding: 8px 10px; margin: 0 0 8px; }
.session.waiting { border-color: #f0b429; }
.session.error { border-color: #e11d48; }
.who { font-weight: 600; margin: 0 0 2px; }
.badge { display: inline-block; font-size: 11px; padding: 1px 6px;
         border-radius: 999px; margin-left: 6px; vertical-align: middle; }
.badge.run { background: #dcfce7; color: #166534; }
.badge.wait { background: #fef3c7; color: #92400e; }
.badge.err { background: #ffe4e6; color: #9f1239; }
.badge.idle { background: #f4f4f5; color: #52525b; }
.meta { color: #71717a; font-size: 12px; margin: 0 0 6px; }
.ask { background: #fafafa; border-radius: 6px; padding: 6px 8px;
       margin: 6px 0 0; }
.ask p { margin: 0 0 4px; }
.ask .dir { color: #71717a; font-size: 12px; }
.row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.row button { font-size: 12px; padding: 4px 10px; cursor: pointer; }
.row button.deny { color: #be123c; }
.log { color: #52525b; font-size: 12px; margin: 8px 0 0; }
.log ul { margin: 4px 0 0; padding-left: 18px; }
.log li { margin: 0 0 2px; }
"""

#: 四态灯 → 徽标文案与 class。**给用户看的是人话，不是枚举名**。
_STATUS = {
    "running": ("运行中", "run"),
    "waiting": ("等你回话", "wait"),
    "error": ("出事了", "err"),
    "idle": ("闲着", "idle"),
}


@dataclass(frozen=True)
class ApprovalRow:
    """一条待决审批。"""

    request_id: str
    text: str
    working_directory: str
    #: 这条已经等了多久。**不是会话的"最近动静"**——会话可能刚跑完一步，
    #: 而那张卡从一步之前就挂在那儿了。用户问的是"这事等了我多久"。
    wait_age: float = 0.0

    @property
    def buttons(self) -> dict[str, dict[str, object]]:
        """四个按钮。**id 前缀 request_id**——``actions`` 是字典，
        两条审批共用 "allow_once" 会互相覆盖，屏幕上只有一个能点。"""
        rid = self.request_id
        return {
            f"always_{rid}": {"entry": "approval_decide",
                              "args": {"request_id": rid,
                                       "decision": "allow",
                                       "scope": BUTTON_SCOPES["allow_always"]}},
            f"session_{rid}": {"entry": "approval_decide",
                               "args": {"request_id": rid,
                                        "decision": "allow",
                                        "scope": BUTTON_SCOPES["allow_session"]}},
            f"once_{rid}": {"entry": "approval_decide",
                            "args": {"request_id": rid,
                                     "decision": "allow",
                                     "scope": "once"}},
            f"deny_{rid}": {"entry": "approval_decide",
                            "args": {"request_id": rid,
                                     "decision": "deny"}},
        }


@dataclass(frozen=True)
class SessionCard:
    """一个会话的一格。"""

    agent: str
    session: str
    status: str                 # running / waiting / error / idle
    elapsed: float
    summary: str                # 这一轮在干什么（类别概括）
    approvals: tuple[ApprovalRow, ...]
    workspace: str = ""

    @property
    def waiting(self) -> bool:
        return bool(self.approvals)

    def signature(self) -> tuple:
        return (self.agent, self.session, self.status,
                tuple(a.request_id for a in self.approvals),
                tuple(a.text for a in self.approvals))


@dataclass(frozen=True)
class WaitingState:
    """面板的一次快照。纯数据，便于单测。"""

    cards: tuple[SessionCard, ...]
    notices: tuple[str, ...]

    @property
    def count(self) -> int:
        return sum(len(c.approvals) for c in self.cards)

    @property
    def oldest_age(self) -> float:
        """最久那条**审批**等了多久。

        刻意不用 ``SessionCard.elapsed``（会话的"最近动静"）——会话可能刚
        跑完一步，而卡从一步之前就挂着。用户问的是"这事等了我多久"。
        """
        ages = [row.wait_age for c in self.cards for row in c.approvals]
        return max(ages) if ages else 0.0

    def signature(self) -> tuple:
        """什么算「真变了」。

        计数**单独一项**：它变了是真事件，该重新 create 让面板再亮一次
        （用户可能刚手动关掉它，见 ``_sync_waiting_view``）。其余变动
        （加了条流水、徽标换了、分钟数跳了）只 update，不重建——
        每分钟重建一次就是每分钟亮一次圆点。
        """
        return (self.count, tuple(c.signature() for c in self.cards),
                self.notices)


# ── 纯函数层：快照 → 面板状态 ──────────────────────────────────────────

def session_cards(*, approvals: list[dict], sessions: list, now: float,
                  max_sessions: int = MAX_SESSIONS) -> tuple[SessionCard, ...]:
    """审批快照 + tracker 会话状态 → 分组后的面板卡。

    ``approvals`` 是 ``ApprovalGateway.snapshot()``（已含已了结的，这里只取
    ``state == "pending"``）。``sessions`` 是 ``ProgressTracker.sessions()``。

    **两侧的会话都进来**：tracker 知道"谁在跑"，快照知道"谁在等"。
    只有一边有的照样显示——在跑但没待决的是大多数，只显示有待决的就把
    这个面板退化成了待办清单，而那正是我们第一版的问题。
    """
    pending: dict[tuple[str, str], list[ApprovalRow]] = {}
    for row in approvals:
        if row.get("state") != "pending":
            continue
        key = (str(row.get("agent") or ""), str(row.get("session_id") or ""))
        created = float(row.get("created_at") or 0)
        pending.setdefault(key, []).append(ApprovalRow(
            request_id=str(row.get("request_id") or ""),
            text=_clip(str(row.get("tool_summary") or "")),
            working_directory=str(row.get("working_directory") or ""),
            wait_age=max(0.0, now - created) if created else 0.0,
        ))

    known: dict[tuple[str, str], object] = {}
    for state in sessions or []:
        known[(str(getattr(state, "agent", "")),
               str(getattr(state, "session", "")))] = state

    cards: list[SessionCard] = []
    for key in sorted(set(pending) | set(known)):
        state = known.get(key)
        rows = tuple(pending.get(key, ()))
        if state is None:
            # 只在快照里、tracker 没见过的会话：仍要显示（它在等），
            # 但状态只能猜——按"在等"算，不假装知道更多。
            cards.append(SessionCard(agent=key[0], session=key[1],
                                     status="waiting", elapsed=0.0,
                                     summary="", approvals=rows))
            continue
        elapsed = max(0.0, now - float(getattr(state, "last_event_at", 0) or 0))
        light = str(getattr(getattr(state, "light", ""), "value",
                            getattr(state, "light", "")) or "idle")
        if rows:
            status = "waiting"
        else:
            status = light if light in _STATUS else "idle"
        if not rows and status == "idle" and elapsed > IDLE_AFTER_SECONDS:
            continue                     # 什么都不做的会话不占版面
        cards.append(SessionCard(
            agent=key[0], session=key[1], status=status, elapsed=elapsed,
            summary=_round_summary(state), approvals=rows,
            workspace=str(getattr(state, "workspace", "") or ""),
        ))

    # 有待决的排前面——这个面板首先要回答"要不要现在去点"。
    cards.sort(key=lambda c: (not c.waiting, -c.elapsed))
    return tuple(cards[:max_sessions])


def _clip(text: str) -> str:
    return text[:COMMAND_DISPLAY_MAX]


def _round_summary(state: object) -> str:
    """这一轮在干什么。**只报类别**，和 tracker 的 boundary 通知同一套话——
    不能面板一套、播报一套。"""
    kinds = getattr(state, "round_kinds", None) or {}
    ranked = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranked:
        return ""
    tally = ", ".join(f"{name} {count} 次" for name, count in ranked[:3])
    return f"这一轮 {tally}"


def _elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return "不到 1 分钟"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


# ── 渲染 ──────────────────────────────────────────────────────────────

def build_waiting_html(state: WaitingState) -> str:
    """面板正文。**动态内容全部转义**：工具摘要里有命令原文。"""
    if not state.cards:
        return ("<section><p class=\"meta\">"
                "现在没有在跑的编码 Agent 会话。</p></section>")
    head = (f"{state.count} 个审批在等你"
            if state.count else "在跑的会话")
    if state.count and state.oldest_age:
        head += f" · 最久的已等 {_elapsed(state.oldest_age)}"
    body = "".join(_session_html(card) for card in state.cards)
    log = _log_html(state.notices)
    return (f"<section><p class=\"head\">{html.escape(head)}</p>"
            f"{body}{log}</section>")


def _session_html(card: SessionCard) -> str:
    label, css = _STATUS.get(card.status, _STATUS["idle"])
    who = f"{html.escape(card.agent or '未知')}"
    short = card.session[:8]
    if short:
        who += f" · {html.escape(short)}"
    meta = []
    if card.elapsed:
        meta.append(f"最近动静 {_elapsed(card.elapsed)}前")
    if card.summary:
        meta.append(html.escape(card.summary))
    meta_html = (f'<p class="meta">{html.escape(" · ".join(meta))}</p>'
                 if meta else "")
    asks = "".join(_ask_html(row) for row in card.approvals)
    return (f'<div class="session {css}">'
            f'<p class="who">{who}'
            f'<span class="badge {css}">{label}</span></p>'
            f"{meta_html}{asks}</div>")


def _ask_html(row: ApprovalRow) -> str:
    line = html.escape(row.text or "未知工具")
    where = (f'<p class="dir">在目录 {html.escape(row.working_directory)}</p>'
             if row.working_directory else "")
    buttons = (
        '<div class="row">'
        '<button data-neko-action="always_%s">始终允许</button>'
        '<button data-neko-action="session_%s">这件事里都允许</button>'
        '<button data-neko-action="once_%s">只这一次</button>'
        '<button class="deny" data-neko-action="deny_%s">拒绝</button>'
        "</div>" % (row.request_id, row.request_id,
                    row.request_id, row.request_id)
    )
    return f'<div class="ask"><p>{line}</p>{where}{buttons}</div>'


def _log_html(notices: tuple[str, ...]) -> str:
    if not notices:
        return ""
    items = "".join(f"<li>{html.escape(line)}</li>" for line in notices)
    return (f'<div class="log">最近发生<ul>{items}</ul></div>')


def build_waiting_actions(state: WaitingState) -> dict[str, dict[str, object]]:
    """整个面板的按钮映射。**每个按钮带各自那条的 request_id。**"""
    out: dict[str, dict[str, object]] = {}
    for card in state.cards:
        for row in card.approvals:
            out.update(row.buttons)
    return out


def build_waiting_summary(state: WaitingState) -> str:
    """复制/导出用的那一行。"""
    if state.count:
        return (f"{state.count} 个审批在等你 · 最久的已等 "
                f"{_elapsed(state.oldest_age)}")
    running = len(state.cards)
    return f"{running} 个会话在跑" if running else "没有在跑的会话"
