"""「在等你」AgentHUD 常驻面板（0.6.25）。

## 它回答什么问题

你离开一会儿回来，不确定有没有事在等你——只能翻聊天记录找卡片。
这个面板让那个问题有个固定答案：**AgentHUD 里常驻一行，N 个在等你、
最久的已等多久**。N 归零它自己关掉。

这是 slogan「替你看着 Agent，大事问你」的另一半：前半句是观测，
这半句是**别让你该点的那一下被聊天流冲走**。

## 为什么用 AgentHUD 视图而不是聊天播报

2026-11-27 真机实测（0.6.24 视图探针）：**重复 create 不抢焦点**——
只换探针编号，用户当前页签与折叠状态不动，未查看的只亮个小圆点。
这条决定了它能放**常驻状态**：换一次性通知早就把用户正在看的标签页
抢走了。而聊天播报做不到常驻——消息会被后续消息推走。

## 三条设计约束

1. **只读，没有按钮。** 面板不是审批状态机的第二个写入方。
   要能在面板里直接批/拒，先得想清楚怎么保证只有一处能决定——
   那是另一轮，不该混进来。（真要加，见文件末尾的备注。）
2. **N ≥ 1 才显示，N 归零自动关。** 常驻面板最怕变成又一块永远亮着的
   装饰——那正是用户说的「麻木」。
3. **不每秒刷新。** 见 ``signature()``：只有计数变化或等待分钟数变化
   才算一次真变动。年龄每秒都在变，跟着刷就是每秒一次 host 投递。

## 关于「用户手动关掉之后」

宿主语义：关掉之后 ``update()`` 不会重新打开。所以计数变化时
**重新 create**（会亮圆点、不抢焦点），只有分钟数变化才 update。
这样"用户嫌烦关掉了、然后真来了一件新的"仍然会被看见。
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from .commands import COMMAND_DISPLAY_MAX

#: 面板标题。窗口内直接以视图标题显示页签，与「任务」并列。
TITLE = "同桌猫 · 在等你"

#: 一个视图里最多列几条。再多就折叠成「还有 N 条」——
#: 列长了没人读，而读这个面板的人只想知道"要不要现在去点"。
MAX_ROWS = 3

WAITING_CSS = """
section { padding: 12px; font-size: 13px; line-height: 1.6; }
h3 { margin: 0 0 8px; font-size: 14px; }
p { margin: 0 0 10px; }
.head { font-weight: 600; }
ul { margin: 0; padding-left: 18px; }
li { margin: 0 0 4px; }
.more { color: #888; font-size: 12px; margin: 8px 0 0; }
"""


@dataclass(frozen=True)
class WaitingState:
    """「在等你」面板的一次快照。纯数据，便于单测。"""

    count: int
    #: 最久那条已经等了多久（秒）。count 为 0 时是 0。
    oldest_age: float
    #: 最多 MAX_ROWS 行摘要，最新的在前。
    rows: tuple[str, ...]

    def signature(self) -> tuple:
        """什么算「真变了」。

        **分钟数取整**：年龄每秒都在变，跟着刷就是每秒一次 host 投递。
        而「已等 3 分钟」变「已等 4 分钟」是人能感知的变化，
        每秒一次的「已等 3 分 01 秒」不是。

        计数**单独一项**：它变了就是真事件，该重新 create 让面板再亮一次
        （用户可能刚手动关掉它，见模块 docstring 第 3 条）。
        """
        return (self.count, int(self.oldest_age // 60), self.rows)


def waiting_state(snapshot: list[dict], now: float) -> WaitingState:
    """审批快照 → 面板状态。纯函数。

    ``snapshot`` 是 ``ApprovalGateway.snapshot()``：未决 + 已了结（未删的），
    最新在前。这里只取 ``state == "pending"`` 的——已了结的不该再算"在等你"，
    但它们仍要留在快照里供事后查，所以过滤在这儿做而不是改 gateway。
    """
    pending = [row for row in snapshot if row.get("state") == "pending"]
    if not pending:
        return WaitingState(count=0, oldest_age=0.0, rows=())

    ages = [now - float(row.get("created_at") or now) for row in pending]
    rows = tuple(_row_text(row) for row in pending[:MAX_ROWS])
    return WaitingState(count=len(pending), oldest_age=max(ages), rows=rows)


def _row_text(row: dict) -> str:
    """一行的摘要。**原样用 ``tool_summary``，不过 ``fact_line``。**

    ``tool_summary`` 是 ``approval._tool_summary()`` 造的，形状已经是
    ``"Bash: rm -rf build/"``——一句人能看懂的话，而且已经截过长度。
    审批卡的主语行用的就是它，所以面板和卡片天然是同一套话。

    **为什么不过 ``fact_line``**（0.6.25 开发时踩过，值得记）：

    1. ``fact_line`` 吃的是**原始命令**，不是摘要。喂 ``tool_summary``
       进去，它解析不出动作，退回「未识别的命令：Bash: rm -rf build/
       （看不出它会影响什么）」——又长又全是空话。
    2. 而「未识别的命令」这句**正是卡片自己删掉的那类内容**：
       ``build_card_html`` 的注释写着，命令认不出时事实行宁可不显示，
       「同一件事说两遍、其中一遍还是空话，正是 B7 从卡片上删掉的」。
       我这一过，把它从面板侧又请回来了。
    3. ``ApprovalGateway.snapshot()`` 里**没有原始命令**（只有
       ``tool_summary`` / ``tool_name``），要做得改 ``as_dict()`` 的
       输出形状——为一行摘要动那个不值。

    结论：面板只该显示"是哪条在等"，至于"它到底要干什么"，
    点开卡片看——那条路有完整命令、有事实行、有「这是什么」。
    """
    summary = str(row.get("tool_summary") or "").strip()
    if summary:
        return summary[:COMMAND_DISPLAY_MAX]
    return str(row.get("tool_name") or "未知工具")


def _elapsed(seconds: float) -> str:
    """「4 分钟」「1 小时 3 分钟」。不到一分钟说「不到 1 分钟」。"""
    total = max(0, int(seconds))
    if total < 60:
        return "不到 1 分钟"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


def build_waiting_html(state: WaitingState) -> str:
    """面板正文。**动态内容全部转义**：工具摘要里有命令原文，
    而 HTML 跑在禁脚本 iframe 里，转义是默认动作不是可选优化。"""
    head = f"{state.count} 个审批在等你 · 最久的已等 {_elapsed(state.oldest_age)}"
    items = "".join(f"<li>{html.escape(row)}</li>" for row in state.rows)
    more = state.count - len(state.rows)
    tail = (f'<p class="more">还有 {more} 条，去聊天窗里点</p>'
            if more > 0 else "")
    return (
        "<section>"
        f'<h3 class="head">{html.escape(head)}</h3>'
        f"<ul>{items}</ul>{tail}"
        "<p class=\"more\">这一栏只是提醒，不代替卡片——"
        "批还是不批，回聊天窗点原来的卡片。</p>"
        "</section>"
    )


def build_waiting_summary(state: WaitingState) -> str:
    """复制/导出用的那一行。"""
    return f"{state.count} 个审批在等你 · 最久的已等 {_elapsed(state.oldest_age)}"
