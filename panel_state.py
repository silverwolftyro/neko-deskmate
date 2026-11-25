"""设置面板的状态计算。**纯函数**——所有输入由调用方（插件方法）喂进来，
这里不碰文件系统、不读配置、不问审批服务。可单测，也不需要宿主。

设计约束（来自 `05-任务与实验/TASK-2026-11-25-设置面板.md`）：

- 面板是**控制面**，不是观测面。所以这里不给实时事件流，只给
  「需要你注意」+「最近几条」两个有限的列表。
- **宁少不猜**。取不到的值就不放进 state，让面板显示"未知"，
  而不是塞一个看起来像数据的占位符。
"""

from __future__ import annotations

from typing import Any

# 「正在观测的会话」最多列几个。同上。
SESSION_LIMIT = 8
# 最近审批记录最多列几条。
APPROVAL_LIMIT = 5


def _short(session: str) -> str:
    return session[:8] if session else ""


def build_panel_state(
    *,
    running: bool,
    hook: dict[str, Any],
    roots: list[str],
    api_key_set: bool,
    auto_review_enabled: bool,
    authorized_scopes: list[str],
    explain_verbosity: str,
    warning_emphasis: str,
    warning_first_n: int,
    sessions: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    now: float,
    agents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把插件的内部状态压成面板要的那一份。

    ``hook`` 是 ``hookhealth.HookHealth.as_dict()`` 的结果。
    ``sessions`` / ``approvals`` 由调用方从 tracker / approval 快照转换，
    这里只做裁剪与排序，不重新解释它们的含义。
    ``agents`` 是 ``discovery.discover_agents()`` 的结果（``as_dict`` 后的），
    缺省 ``None`` 表示调用方没做发现——面板据此不渲染那张卡，
    而不是渲染一个空的"没检测到 Agent"吓唬人。
    """
    attention: list[dict[str, Any]] = []

    if not running:
        attention.append({
            "ts": now,
            "tone": "danger",
            "title": "插件已停止",
            "detail": "面板上的按钮点不动，观测也停了。"
                      "在插件管理器里启动它。",
            "action": None,
        })

    # hook 的问题排在 roots 前面：它让整个审批功能失效，后果更硬。
    if hook.get("state") not in (None, "ok"):
        attention.append({
            "ts": now,
            "tone": hook.get("tone", "danger"),
            "title": hook.get("title", "审批功能有问题"),
            "detail": hook.get("detail", ""),
            "action": hook.get("action"),
        })

    if not roots:
        attention.append({
            "ts": now,
            "tone": "warning",
            "title": "没声明你自己的目录",
            "detail": "高危通知的主语会显示「发起者未知」——不是插件坏了，"
                      "是它不知道该不该说「你自己开的」。在「设置」页声明"
                      "你常开会话的目录即可。",
            "action": None,
        })

    # 自动审查开了但没签字：这个状态必须显式，不能静默失效
    # （配置开成 true 而功能不生效，用户会以为面板的开关是坏的）。
    if auto_review_enabled and "auto_review" not in authorized_scopes:
        attention.append({
            "ts": now,
            "tone": "danger",
            "title": "自动审查已启用但未确认授权",
            "detail": "配置里开着，但你还没在「确认授权」里签过字，"
                      "所以它不会替你放行任何东西。去签字，或者关掉它。",
            "action": "approval_authorize",
        })

    # ── 会话列表 ───────────────────────────────────────────────────────────
    session_rows = []
    for state in sorted(
        sessions,
        key=lambda s: float(s.get("last_event_at") or 0.0),
        reverse=True,
    )[:SESSION_LIMIT]:
        session_rows.append({
            "agent": state.get("agent", ""),
            "short": _short(state.get("session", "")),
            "workspace": state.get("workspace") or "",
            "light": state.get("light", "idle"),
            "rounds": int(state.get("rounds") or 0),
            "last_event_at": state.get("last_event_at") or 0.0,
            "last_broadcast_at": state.get("last_broadcast_at") or 0.0,
        })

    # 「需要你注意」与「最近发生」是**两份不同的列表**。
    # 曾经把 attention 也塞进 log，结果日志页同一句出现两遍
    # （2026-11-25 真机截图：roots 提示在上下两区各显示一次）。
    recent: list[dict[str, Any]] = []
    for row in approvals[:APPROVAL_LIMIT]:
        recent.append({
            "ts": float(row.get("created_at") or 0.0),
            "tone": _approval_tone(row),
            "text": _approval_text(row),
        })

    # 最近一次播报 = 所有会话的 fingerprint 计时里的最大值。
    # 取不到就留 0，面板显示"—"——不拿"最近事件"冒充"最近播报"。
    last_broadcast = 0.0
    for state in sessions:
        try:
            value = float(state.get("last_broadcast_at") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > last_broadcast:
            last_broadcast = value

    # ── 本机 Agent 清单 ─────────────────────────────────────────────────────
    # 只收 ``reported`` 的（absent = 本机没装，说出来是噪音）。
    # 顺序按状态排：正在观测的靠前，blind 靠后——用户第一眼该看到
    # "插件在干活"，而不是一列"不支持"。
    _STATE_ORDER = {
        "observable": 0, "conditional": 1, "feasible": 2, "blind": 3,
    }
    agent_rows: list[dict[str, Any]] = []
    for row in agents or []:
        if not row.get("reported", True):
            continue
        agent_rows.append({
            "id": str(row.get("id") or ""),
            "label": str(row.get("label") or row.get("id") or ""),
            "state": str(row.get("state") or ""),
            "detail": str(row.get("detail") or ""),
            # 家目录只给"在哪"，不给内容——用户需要知道数据在哪个盘，
            # 而发现层本来也只判存在、不读内容。
            "home": str(row.get("home") or ""),
        })
    agent_rows.sort(key=lambda r: (_STATE_ORDER.get(r["state"], 9), r["label"]))

    return {
        "running": running,
        "last_broadcast_at": last_broadcast,
        "hook": hook,
        "roots": list(roots),
        "api_key_set": api_key_set,
        "auto_review": {
            "enabled": auto_review_enabled,
            "signed": "auto_review" in authorized_scopes,
            "advice_signed": "ai_judgement" in authorized_scopes,
            "auto_signed": "auto_decide" in authorized_scopes,
        },
        "explain_verbosity": explain_verbosity,
        "warning_emphasis": warning_emphasis,
        "warning_first_n": warning_first_n,
        "session_count": len(sessions),
        "sessions": session_rows,
        "attention": attention,
        "recent": recent,
        "agents": agent_rows,
        # 顶部「本机 N 个 Agent，观测中 M 个」用的计数。
        # 自己数而不复用 discovery.summarize：那边吃 AgentProbe 对象，
        # 这边已经转成 dict 了，为了复用再转回去是纯浪费。
        "agent_summary": _count_states(agent_rows),
    }


_APPROVAL_LABEL = {
    "approve": "允许",
    "deny": "拒绝",
    "expired": "到期未决定",
}


def _count_states(rows: list[dict[str, Any]]) -> dict[str, int]:
    """按发现状态计数。只数四种要报告的状态，``absent`` 不算。"""
    counts = {state: 0 for state in
              ("observable", "conditional", "feasible", "blind")}
    for row in rows:
        state = row.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def _approval_tone(row: dict[str, Any]) -> str:
    decision = str(row.get("decision") or "")
    if decision == "deny":
        return "warning"
    if decision in ("expired", ""):
        return "default"
    return "success"


def _approval_text(row: dict[str, Any]) -> str:
    """一条审批记录的人话。**只用具名字段，不猜**。"""
    decision = str(row.get("decision") or "")
    label = _APPROVAL_LABEL.get(decision, decision or "未决定")
    tool = str(row.get("tool_name") or "某个工具")
    summary = str(row.get("tool_summary") or "").strip()
    head = summary[:40] + ("…" if len(summary) > 40 else "") if summary else ""
    session = _short(str(row.get("session_id") or ""))
    where = " 会话 {}".format(session) if session else ""
    if head:
        return "你{label}了 {tool}（{head}）{where}".format(
            label=label, tool=tool, head=head, where=where)
    return "你{label}了 {tool}{where}".format(label=label, tool=tool, where=where)
