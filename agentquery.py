"""让 Yui 查问本机编码 Agent 的活动（0.6.27）。

## 它回答什么问题

用户问「我的 CC 刚才在干嘛」「有没有在等我」「跑完了吗」的时候，
Yui 调这个工具拿一份摘要，再用她自己的话回答。

在那之前，观测层采了一堆数据但**用户没法问**——想知道只能翻日志或翻转译。
这是「替你看着 Agent」真正兑现的那一半：前面做的都是"看"，这个是"能说"。

## 为什么用 ``@llm_tool`` 而不是 ``@plugin_entry``

2026-11-27 查证过（完整取舍见
``05-任务与实验/TASK-2026-11-27-Yui查问编码Agent活动-取舍待拍板.md``）：

- ``@plugin_entry`` 的消费者不含主对话模型——它要经喵能力分派
  （用户问 → Yui 调 ``dispatch_task`` → 喵能力侧模型挑入口 → 才轮到我们）。
  本机日志佐证：Yui 的工具表里 ``agent_deskmate`` 出现 0 次。
- 而这个用例正是官方文档那句「克制使用」的例外里说的
  「**必须**让主对话模型直接调用的能力」：用户当场问、Yui 当场答。

代价也认：每请求多一份 schema（那张表已经有 30+ 个工具，边际相对小），
以及 **main_server 重启后失效且插件探不到**——恢复靠重载插件。

## 三条硬规矩

1. **只收一句 ``question``，不收 id。** ``prompts_task_tools.py`` 的 docstring
   写得很明白：主对话会话**几轮就换一次**，给它 id 它记不住。
2. **描述写窄。** 明写"不是喵能力/使魔任务"——这是防误调最强的杠杆，
   因为 ``query_tasks`` 已经在答"在办什么"了，两条工具挨得太近。
3. **返回前脱敏。** ``tool_summary`` 里是命令原文，可能含密钥；
   用户目录路径也不该出现在送给模型的文本里（``redact`` + 家目录替换）。

## 数据从哪儿来

**只用内存里已有的东西，不新开 I/O**：tracker 的会话状态（谁在跑、灯是什么、
这一轮用了什么）、审批快照（谁在等）、最近通知流水（刚发生了什么）。

v1 **不做** jsonl 冷数据路径。理由：上面的三样已经覆盖"在跑 / 在等 /
刚发生"，而"三天前干了什么"是另一个问题，需要一份懒建的索引——
那是独立一块工作，混进来只会让这版两边都验不透。
"""

from __future__ import annotations

import os
from pathlib import Path

from .broadcast import redact

#: 工具名。``[A-Za-z0-9_.-]{1,64}`` 且不能全是点——拼进 callback URL 路径段。
TOOL_NAME = "ask_coding_agents"

#: 给模型看的描述。**窄是故意的**：``query_tasks`` 已经在答"在办什么"，
#: 两条工具挨得太近就会互相误调。所以这里把边界说到不留缝。
# 描述里那句「不要为此派任务」是**必须的**，不是修辞。
#
# 2026-11-27 真机：问「我的 CC **刚才**在干嘛」她调这个工具（5 次全是过去时），
# 问「我的 CC **在干嘛**」她改走 ``dispatch_task`` 派了个任务去跑 PowerShell。
# 根因在宿主侧——``prompts_task_tools.py`` 的 ``DISPATCH_TOOL_DESCRIPTION``
# 原话是「查资料、**看状态也算**」，它明说了"看状态"可以派给它。
# 于是"现在时"被她判成需要现场查，"过去时"才落到我们这儿。
#
# 而派任务是错的：慢（起任务 + 一连串审批）、要人点、
# 而且答案我们早就观测到了。所以描述里必须把这一格明确抢回来。
TOOL_DESCRIPTION = (
    "查问用户自己电脑上开着的 Claude Code 在跑什么、跑完了没有、"
    "有没有在等人、刚才出了什么事。"
    "**用户问 CC 的状态/在干什么/跑完没有时，用这个工具，不要为此派任务**"
    "——它读的是插件已经观测到的数据，派任务是白费一遍还要人批准。"
    "**目前只看得到 Claude Code**——喵能力/使魔那些任务它看不见，"
    "那些要用 query_tasks 问。"
    "只用于用户问自己机器上的 Claude Code 项目与进展；闲聊不调。"
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "用户想问的那句话，例如「我的 CC 刚才在干嘛」。"
                           "留空就返回最近在跑的概况。",
        },
    },
}

#: 单次调用超时。这个工具只读内存，20 秒是给"宿主转发 + 模型解析"留的余量。
TOOL_TIMEOUT = 20.0

#: 摘要长度上限。**几百字**——再长模型会开始复述而不是概括，
#: 而用户等的是"一句话说清"。
SUMMARY_MAX = 400

#: 灯 → 人话。**不把枚举名送给模型**——它不认识，而且会照着念出来。
_STATUS_WORDS = {
    "running": "在跑",
    "waiting": "在等人",
    "error": "出事了",
    "idle": "闲着",
}


def _home() -> str:
    """用户主目录。拿不到就返回空串（调用方按"不替换"处理）。"""
    try:
        return str(Path(os.path.expanduser("~")))
    except Exception:  # noqa: BLE001 —— 取不到主目录不该让工具炸
        return ""


def _scrub(text: str, home: str) -> str:
    """送给模型之前的最后一道处理。

    两步，都是必需的：

    1. ``redact()`` —— 密钥形状。``tool_summary`` 里是**命令原文**，
       ``export TOKEN=xxx`` 这种就在里面。
    2. **家目录替换成 ``~``** —— 用户目录路径不该出现在送给模型的文本里，
       而且它又长又占字数，替换后摘要能多说一件事。
    """
    safe = redact(text or "")
    if home:
        # **两个方向都要换**。Windows 上 expanduser 给反斜杠，
        # 而命令行里常写成正斜杠。只换一边，另一半原样送给模型——
        # 2026-11-27 第一版正是这么漏的。
        for spelling in {home, home.replace("\\", "/"),
                        home.replace("/", "\\")}:
            if spelling:
                safe = safe.replace(spelling, "~")
    return safe


def _elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return "不到 1 分钟"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


def summarize(*, sessions: list, approvals: list, notices: list,
              now: float, home: str = "") -> str:
    """内存里的三样 → 一份几百字的摘要。**纯函数**，便于单测。

    ``sessions`` 是 ``ProgressTracker.sessions()``，
    ``approvals`` 是 ``ApprovalGateway.snapshot()``，
    ``notices`` 是最近的 ``Notice.headline``。
    """
    home = home or _home()
    lines: list[str] = []

    pending = [row for row in approvals if row.get("state") == "pending"]

    # ── 在跑的会话 ──
    # 只列有活动的： tracker 会留着彻底闲着的会话，全列出来就是浪费字数。
    active = [s for s in sessions
              if _light_of(s) != "idle"
              or now - float(getattr(s, "last_event_at", 0) or 0) <= 300]
    active.sort(key=lambda s: -float(getattr(s, "last_event_at", 0) or 0))

    if active:
        lines.append(f"我这边看得见的 Claude Code 会话，有 {len(active)} 个有活动：")
        for state in active[:5]:
            agent = str(getattr(state, "agent", "") or "未知")
            light = _light_of(state)
            status = _STATUS_WORDS.get(light, light)
            age = now - float(getattr(state, "last_event_at", 0) or 0)
            kinds = getattr(state, "round_kinds", None) or {}
            tally = ", ".join(
                f"{name} {count} 次"
                for name, count in sorted(kinds.items(),
                                          key=lambda kv: (-kv[1], kv[0]))[:3])
            # 短会话 id：**两个 claude-code 分会话不带它就读不出谁是谁**。
            # 披露级别和 AgentHUD 卡片一样（那儿也显示 8 位），不是新泄。
            short = str(getattr(state, "session", "") or "")[:8]
            who = f"{agent} · {short}" if short else agent
            bit = f"- {who}：{status}"
            if age > 0:
                bit += f"，最近动静 {_elapsed(age)}前"
            if tally:
                bit += f"，这一轮 {tally}"
            lines.append(bit)
    else:
        lines.append("我这边没有在跑的 Claude Code 会话。")

    # ── 在等人的 ──
    if pending:
        lines.append("")
        lines.append(f"有 {len(pending)} 个 Claude Code 的审批在等你：")
        for row in pending[:5]:
            text = _scrub(str(row.get("tool_summary") or ""), home)
            age = now - float(row.get("created_at") or now)
            lines.append(f"- {text}（已等 {_elapsed(age)}）")

    # ── 最近发生 ──
    if notices:
        lines.append("")
        lines.append("最近发生：")
        for line in list(notices)[-5:]:
            lines.append(f"- {_scrub(str(line), home)}")

    text = "\n".join(lines)
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 1] + "…"
    return text


def _light_of(state: object) -> str:
    """``Light`` 枚举 → 字符串。两种形态都要认（真枚举 / 测试里的裸串）。"""
    light = getattr(state, "light", "")
    return str(getattr(light, "value", light) or "idle")


def empty_answer() -> str:
    """一条活动都没有时给模型的话。

    **必须明确说"没有"**：让 Yui 能直接答"没有"，而不是拿着一份空摘要编。
    这是用户点名的验收项之一——误报和漏报在这个工具上一样贵。
    """
    return ("我这边没有在跑的 Claude Code 会话，也没有在等的审批。"
            "喵能力/使魔那边的任务我看不见，要那些得另问。")
