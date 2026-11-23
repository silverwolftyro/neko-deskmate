"""C 批契约层：进度与出错播报的统一信号（纯数据 + 纯函数，零 I/O）。

**这个模块为什么存在**（2026-11-23 定稿；问卷依据见任务书 §一）：

问卷 12 份里最疼的两条是「出错发现晚」7/12、「不知道进度」6/12。
这两个抱怨在用户那里不是两件事，是**同一件事的两面**：

    我能不能移开视线？如果需要我，我会知道吗？

每次派一个 Agent 出去，用户就默默签约了一份「我得时不时回去看看」。
多开几个（问卷补充：有人同时 1–4 个、2–3 个、1–2 个），这份税就是 N 倍。
所以「多 Agent 乱」4/12 不是第四个独立抱怨，是同一个问题的 N>1 版本。

因此本模块的目标不是"多给信息"，是**把"盯着"这个负担从用户身上卸下来**。

**由此推出的唯一设计原则：精度 > 召回。**
宁可漏报一个错误，不可误报一次。漏报损失用户几分钟；误报永久损失用户
对整个系统的信任——一次假的"它卡住了"，会让用户以后每次看到黄灯都先去
确认真假，那等于把警戒税又还给他，还多一层怀疑。
推论：**这是个警报器，不是仪表盘。** 默认全静音，任何一响都有意义。
（B12 删掉「有 N 条待判断」计数播报正是同一原则：那条 0→1→0 的抖动
就是典型的狼来了。）

**契约对所有 Agent 通用**：问卷 Q1 里 Codex 8 > Claude Code 6，结论 5 原话
「工具很分散，要适配器中立，别绑死某一家」。所以本模块不得出现任何
单一 Agent 的概念——`agent` 只是一个不透明的身份字符串，不是"Claude"。

**`Signal` 没有任何能装内容的字段——这是隐私红线的结构性保证，不是注释。**
没有 `command`、没有 `summary`、没有 `content`。适配器要报"它干了什么"，
只能报类别（`kind`，如 `bash` / `edit`）。想加内容字段的人必须先过
`tests/test_progress.py::test_signal_has_no_content_field`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# ── 四个角色 ──────────────────────────────────────────────────────────────
# 与"背后是哪家 Agent"无关。缺角是降级，不是故障：看不到 waiting 的会话
# 永远不会变 🟡，但 🟢/🔴 照报（任务书 §1.3 原则）。

ROLE_ACTION = "action"      # 它动手了吗
ROLE_OUTCOME = "outcome"    # 结果如何（靠 action_id 与 action 配对）
ROLE_BOUNDARY = "boundary"  # 这一轮交接了吗
ROLE_WAITING = "waiting"    # 在等人吗（+ 一句话原因）
ROLES = (ROLE_ACTION, ROLE_OUTCOME, ROLE_BOUNDARY, ROLE_WAITING)


class Role(str, Enum):
    """信号的四种角色。`str` 混基是为了能直接和字符串比大小写敏感地相等。"""

    ACTION = ROLE_ACTION
    OUTCOME = ROLE_OUTCOME
    BOUNDARY = ROLE_BOUNDARY
    WAITING = ROLE_WAITING


# ── 三态灯 ────────────────────────────────────────────────────────────────
# 进度 = **控制权在谁手里**，不是百分比。Agent 自己都不知道百分之多少，
# 任何插件宣称知道都是在编（任务书 §2.1）。

LIGHT_IDLE = "idle"        # 静默：没有未闭环的事
LIGHT_RUNNING = "running"  # 🟢 在跑
LIGHT_WAITING = "waiting"  # 🟡 在等你
LIGHT_ERROR = "error"      # 🔴 出事了
LIGHTS = (LIGHT_IDLE, LIGHT_RUNNING, LIGHT_WAITING, LIGHT_ERROR)


class Light(str, Enum):
    IDLE = LIGHT_IDLE
    RUNNING = LIGHT_RUNNING
    WAITING = LIGHT_WAITING
    ERROR = LIGHT_ERROR


# ── 字段长度上限 ──────────────────────────────────────────────────────────
# 不是为了好看，是为了让"装得下内容"在物理上变困难：
# 一条 shell 命令动辄几百字，类别名不会。超限直接拒收，不截断——
# 截断会把 `export SECRET=abcdef...` 切成一个看似无害的前缀，反而危险。
KIND_MAX = 64
WORKSPACE_MAX = 260        # Windows MAX_PATH 量级
AGENT_MAX = 64
SESSION_MAX = 128
REASON_MAX = 120

# 控制字符一律不收。换行尤其危险：一条通知的第一行必须是结论，
# 注入一个 \n 就能把"失败了"伪装成"已自动放行"。
_FORBIDDEN = tuple(chr(c) for c in range(0x20)) + ("\x7f",)


@dataclass(frozen=True)
class Signal:
    """一条进度信号。**不可变**，且不含任何内容字段。

    字段取舍（2026-11-23 改过一次，理由记在这里免得后人改回去）：

    `agent` 是后加的一等字段。原契约只有 `session` + `workspace`，
    实际用起来会发现两个问题：

    1. **正确性**：状态机按 session 键分桶，两个 Agent 撞了同一个 session id
       就会互相污染状态。所以桶键必须是 `(agent, session)`，不是一个字符串。
    2. **指名**：四个窗口同时开时，一条「它在等你」是废的——用户还得去查
       是哪个，那个"查"的成本正是我们要消灭的东西。用户认识的是
       "哪个 Agent / 哪个目录"，不是一个 UUID。

    没有 `severity` 字段是刻意的：严重度由消费它的规则层决定
    （`rules.py` 已有整套阶梯），契约不预设。同一件事在不同规则下
    可以是 notice 也可以是 critical，塞进契约就等于让契约替规则拍板。
    """

    role: Role
    agent: str
    session: str
    workspace: str | None
    at: float
    action_id: str | None = None
    ok: bool | None = None
    reason: str | None = None
    kind: str = ""

    def __post_init__(self) -> None:
        role = self.role
        if not isinstance(role, Role):
            raise ValueError(f"role 必须是 Role，得到 {role!r}")

        _require_text("agent", self.agent, AGENT_MAX, allow_empty=False)
        _require_text("session", self.session, SESSION_MAX, allow_empty=False)

        if self.workspace is not None:
            _require_text("workspace", self.workspace, WORKSPACE_MAX,
                          allow_empty=False)

        at = self.at
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            raise ValueError(f"at 必须是数字，得到 {at!r}")
        if not math.isfinite(float(at)) or float(at) <= 0:
            raise ValueError(
                f"at 必须是有限正数（epoch 秒），得到 {at!r}；"
                "0 或 NaN 会让超时判断静默失效"
            )

        if self.action_id is not None:
            _require_text("action_id", self.action_id, SESSION_MAX,
                          allow_empty=False)
        if self.reason is not None:
            _require_text("reason", self.reason, REASON_MAX, allow_empty=False)
        _require_text("kind", self.kind, KIND_MAX, allow_empty=True)

        # 字段归属：只有该用它的角色能填。这是"结构即约束"的一部分——
        # 一条 action 上挂个 ok=True 是适配器写错了，让它当场炸，
        # 而不是等到状态机里变成一个解释不了的幽灵状态。
        if role is not Role.OUTCOME and self.ok is not None:
            raise ValueError(f"ok 只有 outcome 有意义，{role.value} 上不许填")
        if self.action_id is not None and role not in (Role.ACTION, Role.OUTCOME):
            raise ValueError(
                f"action_id 只有 action / outcome 有意义，{role.value} 上不许填")
        if role is not Role.WAITING and self.reason is not None:
            raise ValueError(f"reason 只有 waiting 有意义，{role.value} 上不许填")

    # ── 两个派生量 ────────────────────────────────────────────────────────

    def key(self) -> tuple[str, str]:
        """状态机的桶键。**必须是二元组**，理由见 `agent` 字段的说明。"""
        return (self.agent, self.session)

    def who(self) -> str:
        """播报里指名前用的身份。用户不认识 UUID，认识目录和 Agent 名。

        目录优先：四个 Agent 同时开时，用户是靠"哪个项目"定位的，
        Agent 名只是次级线索。两者都拿不出时才退回会话 id（罕见，
        且那种情况下指名本来就不可能，调用方应当降级为不指名）。
        """
        if self.workspace:
            return self.workspace
        return self.agent


def _require_text(name: str, value: Any, limit: int, *, allow_empty: bool) -> None:
    """字段级校验：类型、长度、无控制字符。超限拒收，不截断。"""
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是 str，得到 {type(value).__name__}")
    if not allow_empty and not value:
        raise ValueError(f"{name} 不许为空")
    if len(value) > limit:
        raise ValueError(
            f"{name} 长度 {len(value)} 超过上限 {limit}；"
            "契约不装内容，超限大概率是适配器把命令/输出塞进来了")
    for bad in _FORBIDDEN:
        if bad in value:
            raise ValueError(f"{name} 含控制字符 {bad!r}；换行能伪造通知首行")


# ── 默认开关 ──────────────────────────────────────────────────────────────
# 精度 > 召回的直接落地。**默认开启的集合 = 「需要用户动手」的集合**，
# 一分不多。知会类（action / boundary）默认全关：它们在技术上成立，
# 在注意力上是净损失——每次"它跑完了"都在训练用户忽略这个插件。

@dataclass(frozen=True)
class NotifyDefault:
    role: Role
    enabled: bool
    why: str


NOTIFY_DEFAULTS: tuple[NotifyDefault, ...] = (
    NotifyDefault(
        Role.WAITING, True,
        "插件存在的理由：Agent 完全卡死，只有我们知道它在等人点",
    ),
    NotifyDefault(
        Role.OUTCOME, True,
        "「出错发现晚」是问卷原话；纯本地判定，零 key 可用",
    ),
    NotifyDefault(
        Role.ACTION, False,
        "知会类。它在跑是常态，报它就是滴答式播报",
    ),
    NotifyDefault(
        Role.BOUNDARY, False,
        "知会类且最吵：每一轮结束都响，正是训练用户忽略插件的那个",
    ),
)

# 语音（`respond`）全关：声音是给"要你动手"的，知会类用声音打断是净损失。
VOICE_DEFAULT = False
VOICE_WHY = "声音只配「要你动手」；知会类用声音打断是净损失"

# 汇总永不推送，只在 `status` 里可查。推送汇总 = 滴答式播报。
DIGEST_PUSH = False
DIGEST_WHY = "推送汇总就是定时播报；要查自己用 status 入口"


def notify_enabled(role: Role | str) -> bool:
    """这个角色默认开口吗。纯函数，测试与调用方共用同一份真相。

    用 `==` 而不是 `is`：`Role` 是 `str` 混基枚举，`Role.WAITING == "waiting"`
    为真而 `is` 为假。调用方从配置里读出来的多半是裸字符串，
    用 `is` 会把一个完全合法的 `"waiting"` 推到下面那句报错上——
    那种报错读起来像"你没给这个角色定过开关"，会把人带去找错地方。
    """
    for item in NOTIFY_DEFAULTS:
        if item.role == role:
            return item.enabled
    raise ValueError(
        f"没有为 {role!r} 定过默认开关；新角色必须先在这里表态，"
        "否则它会静默变成不播——或者更坏，播")


# ── 事件 → 信号 映射（CC 适配器）──────────────────────────────────────────
# 放在契约层而不是适配器里，是因为"哪类事件算哪个角色"本身就是契约的
# 一部分：换一家 Agent 时，变的只是"事件从哪来"，不是"这类事算哪个角色"。
#
# 现有四类事件 → 三个角色。`tool_result` 是 C 批新接上的：
# 在它之前，「命令失败了但没报 error」这种最常见的失败一条都看不见
# （实测 30 个转录文件里 161 个 is_error=true 的 tool_result 全被丢弃）。
#
# 注意 **`ok` 只能从 `is_error` 推**，而且推不出就必须丢：
# 无证据判 True 会压掉一次真失败（最坏方向），无证据判 False 会造一次
# 假警报（精度原则禁止的方向）。两条都不如丢。

def signal_from_event(event: Any) -> Signal | None:
    """把一条 `UnifiedEvent` 映射成 Signal。认不出的类型返回 None。

    时间不可信时**返回 None，不造一个时间**：`at` 是超时判断的唯一依据，
    编一个值会让"未回包"和"停滞"两类检测同时静默失效。
    这与本仓「不确定的信息不许编」是同一条硬约束。
    """
    kind = getattr(event, "event", "")
    role = _ROLE_FOR_EVENT.get(kind)
    if role is None:
        return None

    at = _epoch(getattr(event, "ts", ""))
    if at is None:
        return None

    tool = getattr(event, "tool", None) or ""
    if role is Role.ACTION:
        action_id = getattr(event, "tool_use_id", None)
        if not action_id:
            # 没有 id 就无法和 outcome 配对，而配对是 outcome 的全部意义。
            # 硬编一个 id 等于制造一对假配对，比没有更坏。
            return None
        return Signal(
            role=role,
            agent=_text(getattr(event, "agent", "")),
            session=_text(getattr(event, "session", "")),
            workspace=_optional(getattr(event, "working_directory", None)),
            at=at,
            action_id=action_id,
            kind=_clip(tool),
        )

    if role is Role.OUTCOME:
        # 成败判不出就不判。见上面关于 ok 的说明。
        is_error = getattr(event, "is_error", None)
        if is_error is None:
            return None
        return Signal(
            role=role,
            agent=_text(getattr(event, "agent", "")),
            session=_text(getattr(event, "session", "")),
            workspace=_optional(getattr(event, "working_directory", None)),
            at=at,
            action_id=_optional(getattr(event, "tool_use_id", None)),
            ok=not bool(is_error),
            kind=_clip(tool),
        )

    # boundary：不带 action_id、不带 ok。它标记的是"这一轮交接了"，
    # 不是某一次动作的结果。
    return Signal(
        role=role,
        agent=_text(getattr(event, "agent", "")),
        session=_text(getattr(event, "session", "")),
        workspace=_optional(getattr(event, "working_directory", None)),
        at=at,
        kind=_clip(tool),
    )


_ROLE_FOR_EVENT = {
    "tool_use": Role.ACTION,
    "turn_end": Role.BOUNDARY,
    "error": Role.OUTCOME,
    "tool_result": Role.OUTCOME,
}


def _epoch(iso: str) -> float | None:
    """ISO8601 UTC → epoch 秒。解析不出返回 None（不猜）。"""
    text = (iso or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    value = parsed.timestamp()
    return value if math.isfinite(value) and value > 0 else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _clip(value: str) -> str:
    """类别名归一：去首尾空白、压掉内部换行。不截断——超限交给校验拒收。"""
    return " ".join((value or "").split())


def signal_fields() -> tuple[str, ...]:
    """`Signal` 的字段名清单。测试用它钉住"字段集合恰好是这些"。

    加字段之所以是破坏性变更：契约是跨适配器的，多一个字段就意味着
    每一家适配器都要回答"这个字段我填什么"，而填不出来时的默认值
    会悄悄变成语义。宁可加角色，不加字段。
    """
    return tuple(f.name for f in fields(Signal))
