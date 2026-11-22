"""M0-lite-5 审批中继：Claude Code PermissionRequest hook → 本地 HTTP → 卡片 → 回填。

**核心原则（2026-11-21 修订）：插件永不代替用户做判断，除非用户显式授权。**

三层门控（规格《审批自动化-授权层级》§1）：

* **L0 转发**——无任何配置即可用：只转发审批、常驻提醒未决，不解释不判断；
* **L1 建议**——``ai_judgement = true`` **且** ``api_key`` 非空：给出
  「建议 + 理由 + 依据」并摆在卡片上，仍然等用户点；
* **L2 自动**——L1 两条 + ``auto_decide = "low_risk_only"`` + 风险 ≤ 阈值：
  在低风险动作上替用户点，并留可审计记录。

**任何一层都不能跳级**：L2 必须建立在 L1 之上——没解释能力的自动决定等于黑箱代办。
删除类/危险写**永远不进 L2**，无论阈值怎么设。

到期（``timeout_mark_expired``，默认 570s = hook 600s 前 30s）时：
用户没点 → **只把 pending 标成 ``expired``**——不拒绝、不回填、不记成"已拒绝"，
响应体里不得出现任何 decision。用 503 + 无 decision 的响应体告诉宿主"插件没有
决定"；真正拦住这次调用的是宿主自己的策略（``hooks.json`` 里的
``onFailure:"block"``）。文案只写「宿主已自行处理，本插件未做判断」。

**L2 自动放行的时机是事件发生的那一刻，不是等到期。** 四条全中才自动 allow：
用户已在 ``approval_authorize`` 确认授权文案、配置里 L1/L2 全开、风险 ≤ 阈值、
判断后端就绪。自动放行后立刻发一条 blind 通知 + 写一条含授权人/规则/模型/理由的
审计。其余情况一律不自动决定——**包括后端说 deny**（永不 auto-deny）、后端异常、
没授权、没 key。没接后端时也不自己拍一个，把请求留给用户，走到期。

这是三条红线里第 2 条的执行体：**审批交互只能走 create_card + data-neko-action**。

设计要点（都来自官方资料或踩过的坑，不是拍的）：

1. **HTTP 服务跑在守护线程里**，不是 startup 里的 asyncio 任务。
   AGENTS.md 明确写着"startup 返回时它创建的任务会被取消"——任务活不到
   用户点按钮那一刻，线程可以。
2. **回填靠"还挂着的那条连接"，不靠 chain_id 推断身份。** hook 是"挂起等响应"
   语义：handler 线程一直阻塞到用户决定或到期，身份由连接本身保证。
   所以 chain_id 只用于显示，不参与回填（2026-11-21 归因结论 2）。
3. **pending 键是 (session_id, request_id)，request_id 由我们生成。**
   sessionId 在父子代理之间是同一个值（实测：子代理记录的 sessionId 恒等于父会话），
   拿它区分不了父子；但回填不需要区分，所以不影响正确性。
4. **pending 到期不删**，标 ``expired`` 后按 ``retention_seconds`` 保留，
   以便事后查"当时到底问过什么"。
5. **hooks.json 必须带 ``onFailure:"block"``。** 否则插件没起来、返回非 2xx
   或超时，Claude Code 按"非阻断错误"处理 → **直接放行**。这是 fail-open，
   对审批是灾难。我们到期时正是靠它把"没有决定"变成"拦住"。

官方回填格式（03-调研证据/06 §3.3）：::

    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                            "decision": {"behavior": "allow"|"deny",
                                         "updatedInput": {}, "updatedPermissions": []}}}

deny 可另带 ``message`` 与 ``interrupt``。``interrupt`` 默认 False——
拒绝这一次调用就好，不要顺手掐掉整个回合（那会让 Claude 中途闭嘴）。

本模块**不 import 任何 SDK**：状态机与 HTTP 服务是纯标准库，
才能在没宿主的环境里单测（允许/拒绝/到期/连接断开/无会话/门控）。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

# redact 只有一份，在 broadcast 里（A 批 A4：三条出口共用一套形状）。
# 审批自己不再长第二套——两套脱敏规则等于没有规则。
from .broadcast import redact
from .commands import (
    CONCLUSION_ALLOW,
    CONCLUSION_UNSURE,
    CONCLUSIONS,
    MAX_CARD_ACTIONS,
    TIER_EXTREME,
    TIER_UNKNOWN,
    all_action_phrases,
    card_lines,
    command_body_unknown,
    command_display_note,
    conclusion_for,
    severity_for_tier,
    tier_for_command,
    translate,
)
from .rules import delete_tier, l2_readonly_allowed, match_delete
from .warncount import WarningCounter

# 决策取值。只有用户点按钮、或 L2 在用户显式授权下自动放行，才能产生这两个值。
APPROVE_ALLOW = "allow"
APPROVE_DENY = "deny"
DECISIONS = (APPROVE_ALLOW, APPROVE_DENY)

# decided_by 的取值。
DECIDED_BY_USER = "user"                # 用户点了 → 回填
DECIDED_BY_EXPIRED = "expired"          # 我们先到期 → **不回填、不记成拒绝**
DECIDED_BY_HOST_TIMEOUT = "host_timeout"  # 连接被宿主断开 → **不回填**
DECIDED_BY_AI = "ai"                    # L2 自动放行 → 回填，但必须可审计
# 只出现在 kind="authorization" 的审计行上：那是"谁确认了授权"，
# 不是一次审批决定。决定类的 decided_by 永远只有上面四个值。
DECIDED_BY_AUTHORIZATION = "authorization"

# ── 显式授权（规格 §2 硬规则 2） ────────────────────────────────────────────
# ai_judgement / auto_decide 只能由用户写配置打开；但**光写配置还不够**——
# 首次开启前必须先让用户确认授权文案。确认记录写进审计（时间、范围、授权版本）。
# 插件没有任何方法能在运行期自行打开授权。

AUTHORIZATION_VERSION = 1
AUTH_SCOPE_ADVICE = "ai_judgement"
AUTH_SCOPE_AUTO = "auto_decide"
AUTH_SCOPES = (AUTH_SCOPE_ADVICE, AUTH_SCOPE_AUTO)

AUTHORIZATION_TITLE = "同桌猫 · 授权确认"
# 授权文案。必须包含"模型判断可能出错"与"决定由你授权并承担后果"——
# 这是用户签字前该看到的话，测试逐句盯着。
# **全文人话**：原来这里写着"命中的 L0 规则"，那是内部词，
# 用户在签字这一刻不该去猜 L0 是什么（C5 同类缺陷）。
AUTHORIZATION_TEXT = (
    "开启后，插件会把「命令原文、工作目录、本地规则判定的结果、会话标识」"
    "交给你的配置里指定的模型，由它给出建议，"
    "或者在你授权的低风险动作上替你放行。"
)
AUTHORIZATION_HEADLINE = "模型判断可能出错，决定由你授权并承担后果"
AUTHORIZATION_FOOTER = (
    "插件不会在运行时自行开启以上任何一项；想关闭只需把配置改回 "
    "关闭 / 不自动决定。确认记录会写进审批审计（含时间、范围、授权版本）。"
)

HOOK_PATH = "/hook/permission"
# 探活端点。中继转发之前先打这里；插件没起来时中继据此交还原生权限流程。
HEALTH_PATH = "/health"
MAX_BODY_BYTES = 256 * 1024
# 同时挂起的审批请求上限。每个请求占一个 handler 线程，而且会一直阻塞到
# 用户点按钮或到期（默认 570s）——不设上限的话，一个乱发的客户端就能
# 把线程吃光。超出的请求回 503，中继把它当"没有决定"交还原生流程。
MAX_INFLIGHT = 32
# 探活应答里的身份标识。**中继靠它确认"对面是自己这一版插件"**：
# 端口上住的可能是别的程序（A 批 A3：插件没启动时若其它进程占了 48921，
# 中继会把完整命令发给它，还会把它返回的任意内容转交 Claude Code）。
# 只答 "ok" 是不够的——那只能证明"有个 HTTP 服务在听"。
HEALTH_SERVICE = "agent_deskmate"
HEALTH_TOKEN = "deskmate-approval-v1"

# 默认到期时间。必须**明显小于** hooks.json 里的 hook timeout（600s），
# 否则我们还没到期宿主就先断了。留 30 秒余量。
DEFAULT_TIMEOUT_SECONDS = 570.0
# 到期后的记录保留多久，供 pending_list 事后查。
DEFAULT_RETENTION_SECONDS = 3600.0
# 审计记录的内存条数上限。超了就丢最旧的——内存不能无限涨。
AUDIT_MEMORY_LIMIT = 500

# ── 三层门控（规格 §1） ────────────────────────────────────────────────────

AUTO_OFF = "off"
AUTO_LOW_RISK_ONLY = "low_risk_only"
AUTO_DECIDE_VALUES = (AUTO_OFF, AUTO_LOW_RISK_ONLY)

# ── 动作类别：不可恢复 vs 可恢复 ────────────────────────────────────────────
# 闸门的排除判据是**动作类别**，不是规则名（规格 §16.3 C3）。
#
# 为什么不能按规则名排：规则名会随规则库增删而变——C1 之后"删除类"很可能拆成
# disk_root / system_dir / shadow_copy 好几条。靠 `rule == "delete"` 排除，
# 就等于每加一条规则都得回来改一遍门控；漏改的那条，配上
# `auto_decide_max_severity = "critical"` 就被 L2 自动放行了。按类别排，
# 新规则只要声明自己"不可恢复"就自动继承排除，不需要有人记得改门控。
ACTION_CLASS_RECOVERABLE = "recoverable"      # 可恢复：删了重建得回来
ACTION_CLASS_IRREVERSIBLE = "irreversible"    # 不可恢复：删盘 / 格式化 / 引导项…
# 只列"不可恢复"这一类。**未判定时按可恢复算**——宁可漏，不可错杀，
# 与闸门"精度优先"同向；这也意味着将来新规则要显式声明自己不可恢复。
IRREVERSIBLE_CLASSES = frozenset({ACTION_CLASS_IRREVERSIBLE})
DEFAULT_ACTION_CLASS = ACTION_CLASS_RECOVERABLE

# 严重度阶梯。与 rules.py 的取值一致，这里只用来比大小。
SEVERITY_ORDER = {"info": 0, "notice": 1, "warn": 2, "critical": 3}
# B1/B2 的兜底档：归一化之后仍看不懂的命令、以及解析不出目标的删除，
# 一律落到 warn——"没见过的默认待定"，绝不进低风险。
SEVERITY_WARN = "warn"

# **极高危档本身不可被自动放行**（规格 §3 表：极高危"任何配置不得自动放行"）。
# 这是独立于动作类别的第二道墙：万一将来某条规则忘了声明类别，
# severity=critical 也照样挡住。于是 auto_decide_max_severity 这个旋钮
# 最高只能开到 warn——开到 critical 会被压回 warn 并在启动日志里说明。
AUTO_DECIDE_MAX_SEVERITY_CEILING = "warn"
DEFAULT_MAX_SEVERITY = "notice"

# 风险判定用的默认档：L0 规则没命中就当低风险。
DEFAULT_RISK_SEVERITY = "notice"


# 风险判定用的默认档：L0 规则没命中就当低风险。
DEFAULT_RISK_SEVERITY = "notice"

# 审计里的规则名。**都是 Plain words，不许出现内部术语**——审计行会经
# audit_list 显示给用户看，`irreversible` / `action_class` 这种词上过
# A 批的禁用词清单，不能从后门绕回来。
RULE_DELETE = "delete"        # 删除类（rm / del / rmdir / git clean …）
RULE_DISCARD = "discard"      # 词典认得的不可恢复动作（git reset --hard …）
RULE_UNCLEAR = "unclear"      # 归一化之后仍看不懂这条命令


def _command_of(tool_input: Any) -> str:
    """从 tool_input 里取命令原文。只认 ``command`` 一个键——不猜。"""
    if isinstance(tool_input, dict):
        raw = tool_input.get("command")
        if isinstance(raw, str):
            return raw
    return ""


def _class_for_tier(tier: str) -> str:
    """目标档位 → 动作类别。只有极高危显式声明"不可恢复"，
    其余按可恢复算（与"未判定时按可恢复"同一理由：宁可漏，不可错杀）。"""
    return (ACTION_CLASS_IRREVERSIBLE if tier == TIER_EXTREME
            else DEFAULT_ACTION_CLASS)


def _classify(command: str, working_directory: str | None
              ) -> tuple[str, str, str, str]:
    """一条 Bash 命令的完整判定。返回 ``(severity, rule, action_class, tier)``。

    B2 之后按**目标档位**分，不再"命令里出现删除 token 就一律 critical"：
    先解析要删哪些路径，再按路径所在位置定档。删 `node_modules` 与删 `~`
    顶同一根红条，结果是用户见到红条就点确认，闸门就是这么贬值的。

    闸门与词典现在共用同一套目标解析（``commands.tier_for_command``）——
    老结构是两套判据，同一条 `git reset --hard` 词典认、闸门不认。
    """
    if match_delete(command):
        # 删除类：目标解析不出来就抬到极高危，那条规则在 ``rules.delete_tier``
        # 里——播报与闸门共用，避免两侧漂移。
        tier = delete_tier(command, working_directory)
        return severity_for_tier(tier), RULE_DELETE, _class_for_tier(tier), tier

    # 词典认得的不可恢复动作：`git reset --hard` / `drop table` / `curl | sh`。
    # 闸门原来完全不认它们——用户看到的是"低风险"，词典说的是"找不回来"。
    facts = translate(command)
    if facts.known and facts.irreversible:
        tier = tier_for_command(command, working_directory)
        return severity_for_tier(tier), RULE_DISCARD, _class_for_tier(tier), tier

    # B1 兜底：归一化之后仍看不懂这条命令 → 至少 warn，绝不进低风险。
    if command_body_unknown(command):
        return SEVERITY_WARN, RULE_UNCLEAR, DEFAULT_ACTION_CLASS, TIER_UNKNOWN
    return DEFAULT_RISK_SEVERITY, "", DEFAULT_ACTION_CLASS, TIER_UNKNOWN


def assess_risk(tool_name: str, tool_input: Any,
                working_directory: str | None = None) -> tuple[str, str, str]:
    """评估一次工具调用。纯函数，不接模型。

    返回 ``(severity, rule, action_class)``。档位另由 ``assess_tier`` 取——
    两者同一份判定，只是视图不同，不会打架。

    ``action_class`` 是**独立于规则名**的一维：规则名会随规则库变，类别不会。
    门控只认类别与极高危档，所以新规则只要声明自己不可恢复就自动继承排除，
    不需要有人记得改门控（规格 §16.3 C3）。

    刻意不看 ``tool_input`` 里除命令以外的字段：猜动作是归因错的高发区。
    """
    command = _command_of(tool_input)
    if tool_name == "Bash" and command:
        severity, rule, action_class, _ = _classify(command, working_directory)
        return severity, rule, action_class
    return DEFAULT_RISK_SEVERITY, "", DEFAULT_ACTION_CLASS


def assess_tier(tool_name: str, tool_input: Any,
                working_directory: str | None = None) -> str:
    """这次请求的**目标档位**。闸门与警示计数共用它（B2）。

    不是删除、也不是词典认得的不可恢复动作时返回 ``TIER_UNKNOWN``——
    那种请求根本不亮红条，给它一个具体档位只会让计数键无意义地散开。
    """
    command = _command_of(tool_input)
    if tool_name == "Bash" and command:
        return _classify(command, working_directory)[3]
    return TIER_UNKNOWN


def _command_of_record(record: Any) -> str:
    """从挂起的审批记录里取命令原文。取不到就空串（→ 不进白名单）。"""
    request = getattr(record, "request", None)
    return str(getattr(request, "full_command", "") or "")


def effective_max_severity(configured: str) -> str:
    """把配置里的 L2 阈值夹到合法上限。纯函数，便于单测。

    极高危档不可被自动放行（规格 §3），所以阈值开到 ``critical`` 是没有意义的——
    它要么是用户没理解这个旋钮管什么，要么是想绕过极高危墙。压回 ``warn``
    并让调用方打日志，比默默接受一个"看起来开了其实开不动"的配置好。
    """
    wanted = str(configured or "").strip().lower()
    if wanted not in SEVERITY_ORDER:
        return DEFAULT_MAX_SEVERITY
    if SEVERITY_ORDER[wanted] > SEVERITY_ORDER[AUTO_DECIDE_MAX_SEVERITY_CEILING]:
        return AUTO_DECIDE_MAX_SEVERITY_CEILING
    return wanted


DEFAULT_MAX_SEVERITY = "notice"


def auto_decide_gate(*, severity: str, ai_judgement: bool,
                     has_key: bool, auto_decide: str,
                     max_severity: str,
                     authorized_scopes: tuple[str, ...] = (),
                     action_class: str = DEFAULT_ACTION_CLASS,
                     readonly: bool = False) -> tuple[bool, str]:
    """L2 门控。返回 ``(是否放行, 原因)``。纯函数，便于单测。

    六条全中才放行，缺任何一条都给出**具体原因**——原因要能写进审计，
    不能只回一个 False 让人猜。授权检查排在 ai_judgement 之后、key 之前：
    配置说"要开"只是意愿，用户在授权入口签了字才算数。

    排除判据按**动作类别**与**极高危档**，不按规则名（规格 §16.3 C3）：
    规则名会随规则库增删而变，靠它排除就意味着每加一条规则都得回来改门控。
    所以这个函数**刻意不收 ``rule`` 参数**——收了就会有人顺着它写规则名判断。
    ``readonly`` 收的是一个**已算好的事实**（``rules.l2_readonly_allowed``
    的结果），不是命令字符串：解析留在 rules 里单测，门控只消费结论。

    ``readonly`` 默认 False（**不批**）：B2 之后删除类会按目标分档，
    `rm -rf build/` 落在可恢复档、阈值之内，没有这道白名单它就会被自动放行。
    默认不批意味着将来哪个调用点忘了算这个事实，L2 静默不生效——
    那是安全的失败方向。
    """
    if auto_decide != AUTO_LOW_RISK_ONLY:
        return False, f"auto_decide={auto_decide!r} 未开启"
    if not ai_judgement:
        # 不能跳级：没开 L1 就没有解释能力，此时的自动决定是黑箱代办。
        return False, "ai_judgement 未开启（L2 必须建立在 L1 之上）"
    if AUTH_SCOPE_ADVICE not in authorized_scopes:
        return False, "未确认 ai_judgement 授权（见 approval_authorize）"
    if AUTH_SCOPE_AUTO not in authorized_scopes:
        return False, "未确认 auto_decide 授权（见 approval_authorize）"
    if not has_key:
        return False, "未配置 api_key（没 key 不判断）"
    # 第一道墙：动作类别。不可恢复的一律排除，阈值开到顶也一样。
    if action_class in IRREVERSIBLE_CLASSES:
        return False, f"{action_class} 类动作永不进入 L2（无论阈值怎么设）"
    # 第二道墙：极高危档。独立于类别，防将来某条规则忘了声明类别。
    if SEVERITY_ORDER.get(severity, 99) >= SEVERITY_ORDER["critical"]:
        return False, f"风险 {severity} 属极高危档，任何配置不得自动放行"
    if SEVERITY_ORDER.get(severity, 99) > SEVERITY_ORDER.get(max_severity, 0):
        return False, f"风险 {severity} 超过阈值 {max_severity}，回落为 L1"
    # 第三道墙：只读白名单（B2）。可恢复 + 阈值之内也未必能自动放行——
    # 代办范围必须收敛到"看了不会出事"的命令。
    if not readonly:
        return False, "不在 L2 只读白名单内，回落为 L1"
    return True, "L2 门控通过"


def advice_state(*, ai_judgement: bool, has_key: bool, backend_ready: bool,
                 authorized: bool = True) -> str:
    """卡片上"建议"那块显示成什么样。四种状态，都不许假装成功。

    ``authorized`` 单独一档：配置开了但用户没签字时，卡片必须说"未确认授权"，
    不能显示成"没有 key"——那是两件事，混起来用户不知道该去哪补。
    """
    if not ai_judgement:
        return ADVICE_STATE_FALLBACK
    if not authorized:
        return "unauthorized"
    if not has_key:
        return "no_key"
    if not backend_ready:
        return "pending_backend"
    return ADVICE_STATE_READY


ADVICE_TEXT = {
    # 全部是**陈述句**：只说现在是什么状态，不指挥用户去配什么。
    # 规格 §16.1「覆盖 §12.1」三条硬要求：
    #   1. 严禁「填写 API key 即可…」这类索取式文案；
    #   2. 不准暗示零 key 时闸门不工作——安全兜底与模型无关；
    #   3. 不许把配置键名（ai_judgement）或内部入口名（approval_authorize）
    #      泄到用户界面，用户不知道那是什么（这是 698d4a9 的 C5 缺陷）。
    "disabled": "当前只做提醒，未接入模型。安全兜底与模型无关。",
    "unauthorized": "尚未确认授权，暂不给出建议。安全兜底与模型无关。",
    "no_key": "当前只做提醒，未接入模型。安全兜底与模型无关。",
    "pending_backend": "当前只做提醒，模型能力尚未接入。安全兜底与模型无关。",
}

# ── 卡片侧「建议块」的唯一词表 ────────────────────────────────────────────
# **只有本模块拥有这套名字。** model_advice.py 里的 STATE_* 是"调用结果码"
# （ok / timeout / auth_failed…），那是模型客户端自己的事；两套词曾经有重叠
# 又不完全重叠（no_key / disabled / unauthorized 撞名，其余不撞），结果
# _advice_for_card 把调用结果码当成展示状态直接传进 build_card_html，
# 模型一成功就 ADVICE_TEXT["ok"] → KeyError → 整张卡片崩掉（真机实测）。
#
# 修法不是"让卡片多认一个 ok"——那只会把词表改大，下一个人再加第三种。
# 修法是：展示状态只在这里定义，_advice_for_card 是唯一的翻译点，
# 并且它翻译出来的东西必须落在这三个名字里，否则按未知处理。
ADVICE_STATE_READY = "ready"
# 未知状态的兜底文案。取 disabled：它是一句纯粹的"现在没有建议"，
# 不指向任何需要用户去补的动作，也不会暗示闸门不工作。
ADVICE_STATE_FALLBACK = "disabled"
# 合法的展示状态集合。_advice_for_card 与 build_card_html 都拿它自检。
# **必须含 ADVICE_STATE_READY**：ready 态不渲染 ADVICE_TEXT 里的句子，
# 它渲染「建议 + 理由 + 依据」三行，所以它不在 ADVICE_TEXT 的键里——
# 只拿 ADVICE_TEXT 的键当合法集合，就会把 ready 误判成未知状态，
# 模型成功给出建议时反而降级成"没有建议"（写这一段时真犯过，被测试抓住）。
ADVICE_STATES = frozenset(ADVICE_TEXT) | {ADVICE_STATE_READY}


def advice_text(state: str) -> str:
    """取某个展示状态对应的文案；**未知状态不抛异常**。

    建议是锦上添花，卡片是核心功能。建议出任何问题都不能让卡片不出现——
    所以这里永远有值可返回，调用方不需要 try。
    """
    return ADVICE_TEXT.get(state, ADVICE_TEXT[ADVICE_STATE_FALLBACK])


def is_known_advice_state(state: Any) -> bool:
    return state in ADVICE_STATES


# 模型建议与风险档冲突时，写进审计的那句话。
ADVICE_CONFLICT_NOTE = "模型建议与风险档冲突，已按无法判断呈现"


def safe_conclusion(suggestion: str, *, risk: str,
                    action_class: str) -> tuple[str, str]:
    """把模型的建议收拢到安全区间。返回 ``(结论, 冲突说明)``。

    **critical / 不可恢复的请求永不显示「建议允许」**（规格 §2 硬规则 3）：
    模型硬说 allow 也降级成「无法判断」。理由很直白——这类动作一旦放行
    就没有第二次机会，而模型会错；一个没有依据的"建议允许"会替用户
    把最不该省的那一步省掉。

    注意它**只压 allow**：模型说 deny 时照样显示"建议拒绝"，说 unsure
    时照样显示"无法判断"。这是单向阀门，不是把建议整条关掉。

    放在这个模块而不是 model_advice：判据（风险档、动作类别）在这边，
    而建议客户端不该知道规则库怎么分级。
    """
    conclusion = conclusion_for(suggestion)
    high_risk = (action_class in IRREVERSIBLE_CLASSES
                 or SEVERITY_ORDER.get(risk, 0) >= SEVERITY_ORDER["critical"])
    if high_risk and conclusion == CONCLUSION_ALLOW:
        return CONCLUSION_UNSURE, ADVICE_CONFLICT_NOTE
    return conclusion, ""


class ApprovalRequest(BaseModel):
    """一次审批请求。request_id 由我们生成，不用 Claude 的 tool_use_id——
    那个 id 在父子代理间会重复，而我们可能要同时挂多个请求。"""

    session_id: str = Field(default="", description="Claude Code 的 session_id")
    request_id: str = Field(description="我们生成的请求 id")
    tool_name: str = Field(default="", description="要执行的工具名")
    tool_summary: str = Field(default="", description="工具调用的一行摘要（给人看）")
    # 完整命令/目标原文，**不截断**。存在的理由（2026-11-22 A 批）：
    # 风险判定用的是完整命令，而卡片与模型原来都只有截到 120 字的
    # tool_summary——一条 150 字的命令在末尾挂 `&& rm -rf ~`，
    # 判定看得见、人和模型都看不见。所以这里单独存一份全文，
    # 卡片/解释卡/模型 prompt 统一用它，不再从 summary 反推。
    # **只在内存里流转**：任何出口（日志/审计）都要先过 broadcast.for_log。
    full_command: str = Field(default="", description="未经截断的命令原文，仅内存流转")
    working_directory: str | None = Field(default=None, description="会话 cwd")
    agent: str = Field(default="claude-code", description="来源 Agent")
    # 上游进程链：谁拉起了这个 Agent。由中继在转发时爬进程树填
    # （规格 §11：**只展示，不参与任何决策**）。空 = 没爬到 → 显示「上游未知」。
    upstream: list[dict[str, str]] = Field(
        default_factory=list, description="上游进程链 [{name, path}]，从近到远")
    received_at: float = Field(default_factory=time.time)


class PendingApproval:
    """一条审批记录。

    ``decided_by`` 为 None 表示还没决定（pending）；有值表示已了结。
    了结不等于删除——记录按 ``retention_seconds`` 保留，供事后查。

    ``auto`` 为 True 表示这条是 L2 在用户授权下自动放行的：它**不该弹卡片**
    （自动放行还要人点就失去意义），只要一条 blind 通知 + 一条审计。
    """

    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.event = threading.Event()
        self.decision: str | None = None       # allow / deny，仅用户或 L2 能填
        self.decided_by: str | None = None
        self.card_id: str | None = None
        self.card_announced = False
        # 风险、规则命中与**动作类别**：建请求时就由 L0 定好。
        # 类别是独立于规则名的一维，门控与红条都只认它（规格 §16.3 C3）。
        self.severity: str = DEFAULT_RISK_SEVERITY
        self.rule: str = ""
        self.action_class: str = DEFAULT_ACTION_CLASS
        # **目标档位**（B2）：recoverable / normal / extreme / unknown。
        # severity 与 action_class 都由它推出来，但推不回去——normal 与 unknown
        # 都落 warn/recoverable，红字计数却要分开算，所以单独存一份。
        self.risk_tier: str = TIER_UNKNOWN
        # L2 决定时填。
        self.judge_model: str = ""
        self.judge_reason: str = ""
        # 生效中的授权（"v1:ai_judgement,auto_decide by=user"）——L2 审计要能
        # 追溯到"是谁授权的"，不能只写"AI 决定的"。
        self.authorization: str = ""
        # 同一件事的**人话版**，给卡片与通知用。审计要精确到配置键名，
        # 用户屏幕上不能出现键名（C5 同类缺陷）——两个字段各管一头，
        # 谁也不许替谁说话。
        self.authorization_card: str = ""
        # L2 自动放行标记 + 通知是否已发。
        self.auto = False
        self.auto_announced = False
        # 已经进内存的审计行。投递失败时要把它改成最终状态（见 note_host_timeout）。
        self.audit_entry: AuditRecord | None = None
        # 这一条拿到的模型建议。**只给「这是什么」卡用**——主卡只显示结论行，
        # 理由与依据按规格 P3 折叠进解释卡，不让它们干扰扫读。
        self.advice: dict[str, str] = {}
        # 「这是什么」说明卡的 id。它是**追加**的一条新消息，不占审批卡
        # （2026-11-22 真机反馈：原来解释卡替换审批卡，按钮全没了）。
        # 记下 id 是为了重复点时更新同一张，而不是越点越多。
        self.explain_card_id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.request.session_id, self.request.request_id)

    def is_pending(self) -> bool:
        return self.decided_by is None

    def settled(self) -> bool:
        return self.decided_by is not None

    def needs_card(self) -> bool:
        return self.card_id is None and not self.auto

    def needs_announce(self) -> bool:
        return self.card_id is not None and self.settled() and not self.card_announced

    def as_dict(self) -> dict[str, Any]:
        """pending_list 的一行。"""
        return {
            "request_id": self.request.request_id,
            "session_id": self.request.session_id,
            "tool_name": self.request.tool_name,
            "tool_summary": self.request.tool_summary,
            "working_directory": self.request.working_directory,
            "created_at": self.request.received_at,
            "created_time": _iso(self.request.received_at),
            "state": "pending" if self.is_pending() else "settled",
            "risk": self.severity,
            "rule": self.rule,
            "action_class": self.action_class,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "auto": self.auto,
        }


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(tool_name: str, tool_input: Any) -> str:
    """从 tool_input 里取一句人能看懂的话。

    只取一个最有信息量的字段，不把整个 input 倒出来（可能含密钥/长文本）。
    取不到的就只写工具名——**不猜它要干什么**。
    """
    key = _primary_key(tool_input)
    if key is None:
        return tool_name or "未知工具"
    return f"{tool_name}: {_clip(tool_input[key])}"


def _primary_key(tool_input: Any) -> str | None:
    """tool_input 里"最有信息量的那个字段"的键名。取不到返回 None。

    ``_tool_summary`` 与 ``_full_command`` **共用这一个顺序表**：
    两份各自维护一份的话，摘要显示 A 字段、全文取 B 字段，
    卡片上就会出现"看得见的不完整、完整的不对应"。
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "path", "pattern", "query", "url",
                "prompt", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return key
    return None


def _full_command(tool_name: str, tool_input: Any) -> str:
    """未经截断的命令/目标原文。取不到就空串——**不猜、不补**。

    刻意保留换行：多行脚本在等宽块里才读得下去，压成一行会把
    ``if …; then rm -rf x; fi`` 这种结构读错。清洗只去掉首尾空白。
    """
    key = _primary_key(tool_input)
    if key is None:
        return ""
    value = tool_input[key]
    return str(value).strip()


def build_request(payload: Any) -> ApprovalRequest:
    """把 hook 的 JSON 体转成 ApprovalRequest。

    容忍缺字段：``session_id`` 可能是空的（后台/无会话场景），
    那时键变成 ``("", request_id)``，照样能工作——只是定位信息少一截。
    ``upstream`` 由中继爬进程树填，**不是 hook 原生字段**；直接打
    ``/hook/permission`` 的请求没有它，那时显示「上游未知」而不是猜一个。
    """
    data = payload if isinstance(payload, dict) else {}
    tool_name = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input")
    return ApprovalRequest(
        session_id=str(data.get("session_id") or ""),
        request_id=uuid.uuid4().hex[:12],
        tool_name=tool_name,
        tool_summary=_tool_summary(tool_name, tool_input),
        # 全文单独存一份：卡片、解释卡、模型 prompt 都用它，
        # 不再有人从截断的 summary 里反推命令。
        full_command=_full_command(tool_name, tool_input),
        working_directory=(str(data["cwd"]) if data.get("cwd") else None),
        upstream=_clean_upstream(data.get(UPSTREAM_FIELD)),
    )


def _clean_upstream(raw: Any) -> list[dict[str, str]]:
    """把中继报上来的进程链洗干净：只留 ``{name, path}``，最多三条。

    中继是独立脚本，它读到的进程名/路径不能当可信内容——拼进 HTML 前
    必须过一遍类型与长度。形状不对就当没有（显示「上游未知」），
    **不猜、不补**：猜错归因比没有归因更危险。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    cleaned: list[dict[str, str]] = []
    for entry in raw:
        if len(cleaned) >= UPSTREAM_MAX:
            break
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        # **只收字符串**：数字/None 说明中继那头出了问题，那种条目显示出来
        # 也读不懂（"7"是谁？），不如当没有。宽容留给展示层，严格留给边界。
        if not isinstance(name, str) or not name.strip():
            continue
        path = entry.get("path")
        cleaned.append({"name": _clip(name, 64).strip(),
                        "path": _clip(path, 260).strip()
                        if isinstance(path, str) else ""})
    return cleaned


def _port_in_use(port: int) -> bool:
    """127.0.0.1:port 上有没有别的东西在 listen。

    只连不听：连得上说明已被占用。故意不 accept/不发数据——
    那会干扰对方（可能是另一个插件实例）。
    """
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0


def hook_response(behavior: str, *, message: str = "") -> dict:
    """按官方 schema 组装回填体。

    ``message`` / ``interrupt`` 是 **deny 专属字段**（官方文档：deny 可带
    message/interrupt）。给 allow 挂 message 不但是无用信息，还是 schema 外的
    字段——L2 自动放行的说明走通知与审计，不回填。

    **allow 时刻意不带 ``updatedInput`` / ``updatedPermissions``**
    （规格 P5.2）：实测无害，但某些版本可能把空对象理解成"把参数清空"。
    我们没有要改写的参数，那就什么都别带——少给一个字段比多给一个
    可能被误解的字段安全。deny 仍按官方示例带齐。
    """
    decision: dict[str, Any] = {"behavior": behavior}
    if behavior != APPROVE_ALLOW:
        decision["updatedInput"] = {}
        decision["updatedPermissions"] = []
    if message:
        decision["message"] = message
        # 刻意不设 interrupt=True：拒绝这一次调用就好，
        # 掐掉整个回合会让 Claude 中途闭嘴，用户还得重新发起。
        decision["interrupt"] = False
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def no_decision_response(*, reason: str, timeout_seconds: float) -> dict:
    """到期时的响应体：**不含任何 decision**。

    配 503 一起用。宿主看到非 2xx 且 ``onFailure:"block"`` 已配置，
    就会按它自己的失败策略处理（拦住这次调用）——那是宿主的决定，不是我们的。
    文案只写「宿主已自行处理，本插件未做判断」，不许出现"拒绝"字样。

    **F4（2026-11-21 真机实测）**：只写上面两句时，Claude Code 把到期理解成
    "hook 服务挂了"，反过来建议用户去查端口——用户白担心一场。所以补一句
    人话说明"这是在等你，不是它坏了"。刻意不出现端口号、地址、
    ``onFailure`` 这类内部词：那是给我们排查用的，写进给 CC 看的话里只会添乱。
    """
    return {
        "ok": False,
        "submitted": False,
        "reason": reason,
        "message": (
            f"同桌猫：{timeout_seconds:g} 秒内无人决定。"
            "宿主已自行处理本次调用，本插件未做判断。"
            "（这不是故障——同桌猫一直在等你做决定，没有代替你作答；"
            "超时后由 Claude Code 自己接手这次权限请求。）"
        ),
    }


# 审批按钮的 action id。写死在这里：HTML 里的 data-neko-action 与
# actions 字典的键必须一致，两边各写一遍字符串迟早对不上。
ACTION_APPROVE = "deskmate_approve"
ACTION_DENY = "deskmate_deny"
ACTION_EXPLAIN = "deskmate_explain"
# 「这是什么」讲多细（规格 P5.3）。三档，默认 standard。
#   brief     只讲结论与最坏结果
#   standard  加上范围与规则判据
#   detailed  再叠加模型给的理由与依据
VERBOSITY_BRIEF = "brief"
VERBOSITY_STANDARD = "standard"
VERBOSITY_DETAILED = "detailed"
VERBOSITIES = (VERBOSITY_BRIEF, VERBOSITY_STANDARD, VERBOSITY_DETAILED)
DEFAULT_VERBOSITY = VERBOSITY_STANDARD
ACTION_AUTH_CONFIRM = "deskmate_authorize_confirm"
ACTION_AUTH_CANCEL = "deskmate_authorize_cancel"
# 承载这些按钮的入口 id。
ENTRY_DECIDE = "approval_decide"
ENTRY_EXPLAIN = "approval_explain"
ENTRY_AUTHORIZE = "approval_authorize"

# ── 高风险警示的强调档位（2026-11-22 真机反馈）────────────────────────────
# 原实现每条请求都顶一整行红字。用户原话：前几次有用，之后价值像反比函数
# 一样暴跌——**看多了就等于没有**，而真到该被叫住的那一刻也被一起跳过。
#
# 所以红字按命中次数递减：
#   always    每条都整行红字
#   first_n   前 n 次整行红字，第 n+1 次起缩成一个徽标（默认）
#   never     永远只显示徽标
#
# **这只改长什么样，不改行为。** 极高危依然永不自动放行、依然必须人点，
# 徽标态与红字态走的是同一条门控（有测试盯死这件事：视觉降级 ≠ 安全降级）。
WARNING_ALWAYS = "always"
WARNING_FIRST_N = "first_n"
WARNING_NEVER = "never"
WARNING_MODES = (WARNING_ALWAYS, WARNING_FIRST_N, WARNING_NEVER)
DEFAULT_WARNING_MODE = WARNING_FIRST_N
DEFAULT_WARNING_FIRST_N = 3
WARNING_BANNER = ("高风险动作：任何配置都不会让它自动放行，"
                  "必须由你亲自点允许或拒绝。")
WARNING_BADGE = "⚠ 高风险"


def normalize_warning_mode(value: object) -> str:
    """配置里的档位名 → 合法取值。认不出按默认（first_n）处理，不抛。"""
    mode = str(value or "").strip().lower()
    return mode if mode in WARNING_MODES else DEFAULT_WARNING_MODE


@dataclass(frozen=True)
class WarningPolicy:
    """这一次的警示该长什么样。**只决定外观，不决定放不放行。**"""

    mode: str = DEFAULT_WARNING_MODE
    first_n: int = DEFAULT_WARNING_FIRST_N
    # 同一类别第几次命中（含这次）。计数落在数据目录，重启不丢。
    seen: int = 1

    def kind(self) -> str:
        """``banner`` | ``badge``。

        ``first_n`` 为 0 或负数时永远只给徽标——那是用户明写"一次红字都不要"，
        偷偷给一次反而是没听懂配置。（徽标仍在，安全提示不会因为看腻了就消失。）
        """
        if self.mode == WARNING_ALWAYS:
            return "banner"
        if self.mode == WARNING_NEVER:
            return "badge"
        return "banner" if self.seen <= self.first_n else "badge"


# ── 上游进程链（规格 §11：只展示，不参与任何决策）────────────────────────
# 真实链路常常是：DSH 的 agent → 起 pwsh → 起 `claude -p` → 它调 Bash。
# 卡片原来只写「claude-code 请求执行」，用户看不到"是谁拉起的 claude"。
# 中继自己就在这条链上，所以由它往上爬进程树，把 [{name, path}] 随请求
# 报给插件；插件只负责怎么说。
#
# **爬不到就说不知道**：空链一律「上游未知」，绝不拿当前进程名凑一个。
UPSTREAM_UNKNOWN = "上游未知"
# 最多显示几个节点。与中继的爬取深度一致（B5 从 3 提到 5）：
# 老口径 3 层，多套一层 shell 就断在 bash.exe ← bash.exe ← claude.exe，
# 看不到真正的发起者（真机实测）。过滤 shell 之后仍要留得下 3 个有效节点，
# 所以这里也跟着放到 5。
UPSTREAM_MAX = 5
# 纯 shell 层：**展示时跳过**（B5）。它们只是"命令怎么被递到的"，
# 不是"谁发起的"。DSH → pwsh → claude 这条链上，用户要知道的是 DSH，
# 不是中间那层 pwsh。爬取时仍然要爬——不爬过去就找不到更上面那个。
UPSTREAM_SHELLS = frozenset({
    "bash", "sh", "cmd", "pwsh", "powershell", "dash", "zsh",
})
# 中继写进请求体的键名。**必须与 hook_relay.py 的 UPSTREAM_FIELD 一致**，
# 一致性由 tests/test_upstream.py 里的一条测试盯着。
# 带前缀是因为请求体本身是 Claude Code 的：将来它加一个叫 ``upstream``
# 的字段，不带前缀的我们就会把它的内容当成进程链显示给用户——错归因。
UPSTREAM_FIELD = "deskmate_upstream"
# 可执行路径里能认出来的发起者 → 友好名。**按整词匹配、长词优先**：
# 短词（``desk`` / ``dsh``）是给误判留的门，所以先试长的，
# 而且必须整个词命中——``nodesk.exe`` 不许算命中 ``desk``。
# 认不出来的显示 exe 名本身：宁可少说，不错说。
UPSTREAM_FRIENDLY = (
    ("deepseek", "DeepSeek Harness"),
    ("dsh", "DeepSeek Harness"),
    ("mcode", "mcode"),
    ("desk", "desk"),
)
# 长词优先：短词恰好是长词开头时（``dsh`` / ``dsharness``），
# 先匹配长的那个，否则短词会抢走本该归给长词的条目。
_FRIENDLY_BY_LENGTH = tuple(sorted(UPSTREAM_FRIENDLY,
                                   key=lambda item: -len(item[0])))


def _exe_stem(name: str) -> str:
    """``pwsh.exe`` → ``pwsh``。只剥可执行后缀，别的一概不动。"""
    text = str(name or "").strip().lower()
    for suffix in (".exe", ".com", ".bat", ".cmd", ".ps1"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _is_shell_exe(name: str) -> bool:
    """这一层是纯 shell 吗（bash/sh/cmd/pwsh…）。展示时跳过它。"""
    return _exe_stem(name) in UPSTREAM_SHELLS


def _path_words(path: str) -> set[str]:
    """把路径切成**词**：按非字母数字切开。

    用词匹配而不是 ``\\b``：``\\b`` 把下划线当词字符，于是 ``dsh_tools``
    不匹配而 ``dsh.tools`` 匹配——同一个意思两种结果，全看用户用什么分隔符。
    按"非字母数字即分隔"切，两条路径的答案就一致了。
    """
    lowered = str(path or "").lower().replace("\\", "/")
    return {word for word in re.split(r"[^0-9a-z]+", lowered) if word}


def _friendly_exe(path: str, name: str) -> str:
    words = _path_words(path)
    for needle, friendly in _FRIENDLY_BY_LENGTH:
        if needle in words:
            return f"{name}（{friendly}）"
    return name


def describe_upstream(upstream: Any) -> str:
    """进程链 → 一行人话：``node.exe（DeepSeek Harness）``。

    链按"谁拉起了谁"从近到远排，所以用 ``←`` 连接。
    **这不是证据链，只是线索**：Windows 上父进程可能已退出、中间常隔一层
    shell，所以它只用来回答"这是我自己开的吗"，不参与任何门控判断。

    B5：纯 shell 层（bash/sh/cmd/pwsh…）在展示前滤掉，**且不计入条数上限**——
    先截断再过滤的话，三条 shell 就能把真正的发起者挤掉，
    结果是"明明知道却显示上游未知"。
    """
    labels: list[str] = []
    if isinstance(upstream, (list, tuple)):
        for entry in upstream:
            if len(labels) >= UPSTREAM_MAX:
                break
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name or _is_shell_exe(name):
                continue
            labels.append(_friendly_exe(str(entry.get("path") or ""), name))
    return " ← ".join(labels) if labels else UPSTREAM_UNKNOWN


def warning_mode_for_tier(tier: str, configured: str) -> str:
    """极高危档的警示模式：**强制常亮，永不递减**（B2）。

    连点 3 次删 `node_modules` 之后，第 4 次的 `rm -rf ~` 仍要整行红字。
    这里覆盖用户的递减配置——和 ``auto_decide_gate`` 的封顶阈值同一个理由：
    安全件的下限不该被体验旋钮调没（用户配的 "never" 只对非极高危生效）。

    抽成纯函数是为了能单测：原来这段逻辑塞在插件的 ``_warning_policy`` 里，
    "极高危永不递减"这条就没法在不起宿主的条件下验证。
    """
    if str(tier or "") == TIER_EXTREME:
        return WARNING_ALWAYS
    return normalize_warning_mode(configured)


def warning_key(risk_tier: str) -> str:
    """警示计数的归类键：**同一档才算一次重复**（B2 改为按目标档位计）。

    老口径按 action_class 计，坑在：可恢复但极高危的请求会和所有低风险请求
    共用 ``recoverable`` 这个键，点几次低风险之后，那条极高危的红字就被
    递减掉了——用错阈值比没有阈值危险。改成按目标档位计，四档各一个键。

    **极高危档永不进入递减**：那件事不在这个函数里做，而在
    ``_warning_policy``——档位是 extreme 时强制常亮红字。
    放在那里是因为"要不要递减"是外观决策，``WarningPolicy`` 才是外观的唯一入口；
    计数这边只负责回答"同一档第几次见了"。
    """
    tier = str(risk_tier or "").strip() or TIER_UNKNOWN
    return f"tier:{tier}"

CARD_CSS = (
    "section { padding: 12px; font-size: 13px; }"
    "strong { display: block; margin-bottom: 6px; }"
    "p { margin: 4px 0; }"
    ".loc { color: #888; font-size: 12px; }"
    # 动作行：第一行，说得最直白，稍微大一点
    ".act { font-size: 14px; margin: 8px 0 6px; }"
    # 结论行：独立成行、加粗、置上（规格 P2）
    ".verdict { font-size: 15px; margin: 8px 0 2px; }"
    ".verdict strong { display: inline; font-size: 15px; }"
    ".risk { color: #c0392b; font-size: 13px; margin: 6px 0; }"
    # 徽标：红字递减之后的形态。单行、小号、不占正文——用户在第二次之后
    # 就不再逐字读它，但它得一直在（安全提示不能因为看腻了就消失）。
    ".badge { display: inline-block; color: #c0392b; font-size: 12px;"
    " border: 1px solid #c0392b; border-radius: 3px; padding: 0 6px;"
    " margin: 6px 0 0; }"
    ".src { color: #888; font-size: 12px; margin: 8px 0 2px; }"
    ".hint { color: #aaa; font-size: 12px; margin-top: 8px; }"
    # 完整命令：等宽 + 保留换行 + 自动换行。它是「这是什么」里唯一一段
    # 原文，长命令可能几百字，不能把它撑破卡片。
    ".cmd { font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
    " white-space: pre-wrap; word-break: break-all; background: #f5f5f5;"
    " border-left: 3px solid #ccc; padding: 6px 8px; margin: 6px 0;"
    " max-height: 240px; overflow: auto; }"
    "button { margin: 8px 8px 0 0; padding: 6px 18px; }"
)


def _escape(text: str) -> str:
    """卡片 HTML 转义。工具参数是 Agent 产的，不能当可信 HTML 直接拼。"""
    import html

    return html.escape(str(text), quote=True)


def _minutes(seconds: float) -> str:
    """把秒数说成人话：570s → 「约 10 分钟」，30s → 「约 1 分钟」。"""
    minutes = max(1, round(seconds / 60.0))
    return f"约 {minutes} 分钟"


def build_card_html(*, subject: str, tool_summary: str, session_id: str,
                    working_directory: str | None,
                    advice_state: str = ADVICE_STATE_FALLBACK,
                    risk: str = DEFAULT_RISK_SEVERITY,
                    action_class: str = DEFAULT_ACTION_CLASS,
                    command: str = "", conclusion: str = "",
                    upstream: Any = (), warning: WarningPolicy | None = None,
                    logger: Any = None) -> str:
    """审批卡片的 HTML。纯函数——卡片文案是验收项，必须能单测。

    **只放决策需要的东西**（2026-11-22 真机反馈：原来半屏是免责声明与
    术语解释，结论被埋在中间）：结论、动作、后果、范围、三个按钮，
    外加一行小字的执行者与上游进程链。会话 id / 工作目录 / 超时说明 /
    免责声明全部搬到「这是什么」与配置页。

    ``subject`` 由调用方用归因语法生成（``describe_actor``），
    这样播报和卡片说的是同一套话。``upstream`` 是中继爬进程树的结果。

    **任何 ``advice_state`` 都不能让这个函数抛异常。** 模型建议是锦上添花，
    卡片是核心功能——建议挂了也必须能正常审批。所以未知状态走
    ``advice_text`` 的兜底文案；传了 ``logger`` 就顺手记一条 warning，
    让"为什么这条没有结论"有据可查（不传也不影响渲染）。
    """
    if not is_known_advice_state(advice_state):
        # 未知状态：兜底 + 记账。绝不向上抛——那会让整张卡片消失。
        if logger is not None:
            logger.warning(
                "审批卡片收到未知的建议状态 {!r}，按「无建议」渲染（请求 {}）",
                advice_state, session_id or "未知",
            )
        advice_state = ADVICE_STATE_FALLBACK

    # 高风险警示（规格 §2 硬规则 3 + §17.2）。判据是**动作类别或极高危档**，
    # 不是规则名（C3）——规则名会随规则库变，警示不能跟着漏。
    # ``WarningPolicy`` 只决定它长成红字还是徽标，**不决定放不放行**。
    high_risk = (action_class in IRREVERSIBLE_CLASSES
                 or SEVERITY_ORDER.get(risk, 0) >= SEVERITY_ORDER["critical"])
    warn_kind = (warning or WarningPolicy()).kind() if high_risk else "none"
    if warn_kind == "banner":
        warn_html = (f'<p class="risk"><strong>{_escape(WARNING_BANNER)}'
                     "</strong></p>")
    elif warn_kind == "badge":
        warn_html = f'<p><span class="badge">{_escape(WARNING_BADGE)}</span></p>'
    else:
        warn_html = ""

    # 主卡正文固定四行（规格 P3）：结论 / 动作 / 后果 / 范围。
    # 前三行由命令翻译词典产出（本地、零 key），结论行由调用方填——
    # 词典只描述事实，不给结论。
    lines = card_lines(command=command or tool_summary,
                       working_directory=working_directory)
    # 命令超过展示上限时那一行提示（A 批 A1）。放在四行正文之后、
    # 执行者信息之前：它是关于"你看全了吗"的警告，得在按钮之前。
    note = command_display_note(command or tool_summary)
    note_html = f'<p class="src">{_escape(note)}</p>' if note else ""
    # B3：一条命令里有多个动作时（`rm a && git reset --hard`），**每一段都要
    # 列出来**，最危险的在前。老实现只列第一个——没被描述的动作等于被默认
    # 批准，用户点"允许"时根本不知道还有第二个。超出的进「这是什么」。
    action_html = "".join(f'<p class="act">{_escape(text)}</p>'
                          for text in lines["actions"]) or \
        f'<p class="act">{_escape(lines["action"])}</p>'
    # 结论行的取值受 advice_state 约束：**只有真的拿到了模型建议，
    # 结论才可能是"建议允许/建议拒绝"**。建议层降级时（没 key、没签字、
    # 调用失败、状态未知）一律落最保守的第三态——哪怕调用方误传了一个结论，
    # 也不能让一个没有依据的"建议允许"出现在卡片上。
    if advice_state == ADVICE_STATE_READY and conclusion in CONCLUSIONS:
        conclusion_text = conclusion
    else:
        conclusion_text = CONCLUSION_UNSURE
    body_lines = [
        # 结论行**置于最上**（规格 P2）：快速扫读时眼睛先落在哪里，
        # 结论就该在那里。P3 的四行样例把结论排在最后，与 P2 的"置于最上"
        # 冲突；这里按 P2 排，因为 P2 给了理由（误读），P3 只给了内容清单。
        f'<p class="verdict"><strong>{_escape(conclusion_text)}</strong></p>',
        action_html,
        f"<p>{_escape(lines['consequence'])}</p>",
        f"<p>{_escape(lines['scope'])}</p>",
        warn_html,
        note_html,
    ]

    # 执行者与上游：压成两行小字放在最下面。它们回答"这是我自己开的吗"，
    # 属于定位信息，不该挤占结论的位置（规格 Q2）。
    short_session = (session_id or "")[:8]
    who = f"执行：{subject}" + (f" 会话 {short_session}" if short_session else "")
    return (
        "<section>"
        "<strong>同桌猫 · 审批请求</strong>"
        + "".join(body_lines)
        + f"<p class=\"src\">{_escape(who)}</p>"
        + f"<p class=\"src\">上游：{_escape(describe_upstream(upstream))}</p>"
        + f"<button data-neko-action=\"{ACTION_APPROVE}\">允许</button>"
        + f"<button data-neko-action=\"{ACTION_DENY}\">拒绝</button>"
        + f"<button data-neko-action=\"{ACTION_EXPLAIN}\">这是什么</button>"
        + "</section>"
    )


def explain_request(*, tool_name: str, tool_summary: str,
                    working_directory: str | None, rule: str,
                    severity: str, command: str = "",
                    model_reason: str = "", basis: str = "",
                    model_note: str = "", session_id: str = "",
                    full_command: str = "",
                    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                    verbosity: str = DEFAULT_VERBOSITY) -> dict[str, str]:
    """「这是什么」按钮的落点：解释，只说事实，不猜意图。

    主卡的正文只有四行（规格 P3），所以**会话 id、工作目录、超时说明、
    模型的理由与依据全放这里**——「这是什么」就是看细节的地方。

    ``basis`` 那行按规格 P3 整行来自动生成（工具 / 目录 / 风险档 / 模型名），
    不许手写：手写的内容迟早和实际字段不一致。

    **全文人话，不出现内部词**（2026-11-22 真机反馈：解释卡上出现过
    ``L0`` / ``critical`` / ``L2`` / ``onFailure``——那是 C5 同类缺陷，
    用户不知道那是什么）。风险档只用来分支措辞，**不把档位名印出来**。
    """
    if rule == "delete":
        verdict = "这是一次删除文件的操作，属于高风险。"
        extra = "这类操作永远不会自动放行，必须你亲自点允许或拒绝。"
    elif rule:
        verdict = "本地规则命中了这次操作，按高风险处理。"
        extra = "是否放行由你决定，插件只转发、不代判。"
    else:
        verdict = "本地规则没有命中，按低风险处理。"
        extra = ("即使风险低，只要没开自动决定，插件仍然只转发、不代判；"
                 "你一直不决定的话，到期后由 Claude Code 自己接手。")

    # 主卡上原来那段"允许后…/拒绝则…/约 N 分钟后…"整块搬到这里
    # （规格 Q2：卡片上不重复免责说明）。**到期不等于拒绝**——
    # 这句话必须说清楚，否则用户会以为不点就是不同意。
    timeout_line = (
        f"允许后 Claude Code 继续执行；拒绝则中止这次调用"
        f"（不会掐掉整个回合）。{_minutes(timeout_seconds)}后无人决定，"
        "这条视为未决定，由 Claude Code 自己接手——插件不代你判断，"
        "也不把「一直没点」记成拒绝。")

    short_session = (session_id or "")[:8]
    where = working_directory or "工作目录未知"
    location = (f"会话 {short_session} 的工作目录：{where}"
                if short_session else f"工作目录：{where}")

    explanation = {
        "title": "这是什么",
        # ``tool_summary`` 本身已经是「Bash: rm -rf build/」这种形状，
        # 再把 tool_name 拼一遍就是「请求执行 Bash：Bash: rm -rf build/」——
        # 工具名出现两次，看着像插件复读。所以摘要已经有前缀就不重复。
        "what": (f"Claude Code 请求执行 "
                 f"{tool_summary or tool_name or '某个工具'}"),
        "verdict": verdict,
        "extra": extra,
        "timeout": timeout_line,
        "location": location,
        "note": ("以上是插件能讲准的事实。看不出来的地方不会替你猜——"
                 "要不要放行由你定。"),
    }
    # 完整命令原文（A 批 A1）：主卡只显示前 80 字，**全文在这里**。
    # 等宽块 + 保留换行：多行脚本要照着原样读才判断得准。
    if full_command.strip():
        explanation["full_command"] = full_command
    # B3：一条命令里有多个动作时，主卡只列最危险的 ``MAX_CARD_ACTIONS`` 条，
    # **剩下的在这里一条不少地列全**。主卡装不下不等于可以不知道——
    # 那是"没被描述的动作被默认批准"的另一种形式。
    if command.strip():
        everything = all_action_phrases(command, working_directory)
        if len(everything) > MAX_CARD_ACTIONS:
            explanation["all_actions"] = "；".join(
                text.lstrip("⚠️ ") for text in everything)
    # 讲多细由配置决定（规格 P5.3）。brief 只留结论与后果；standard 加范围
    # 与规则判据；detailed 再叠加模型给的理由与依据。
    # **full_command 不在这三个档次里被裁掉**：它是主卡看不全的那部分，
    # 删了它，「尾部未显示」那句提示就变成了死路。
    if verbosity == VERBOSITY_BRIEF:
        for key in ("location", "extra", "timeout"):
            explanation.pop(key, None)
    elif verbosity != VERBOSITY_DETAILED:
        model_reason, basis = "", ""
    if model_note:
        # 没接入模型时的那句陈述。放在解释卡而不是主卡：主卡正文是四行，
        # 而这句话属于"细节"，正是「这是什么」该放的地方。
        explanation["model_note"] = model_note
    if model_reason:
        explanation["model_reason"] = f"模型给的理由：{model_reason}"
    if basis:
        explanation["basis"] = f"依据：{basis}"
    return explanation


def build_explain_html(explanation: dict[str, str]) -> str:
    """把 explain_request 的结果渲染成卡片用的 HTML。

    **纯说明卡，一个按钮都没有**（2026-11-22 第二轮真机反馈）：原来它
    *替换*审批卡，把按钮整个抢走，而卡上的「返回审批」在宿主侧报
    ``UI action ... is not exposed by surface ...``，用户卡在中间出不去。
    现在改成**追加一条新消息**，原审批卡原地不动、三个按钮始终可点，
    所以这里既不需要按钮，也不需要"返回"。
    """
    return (
        "<section>"
        f"<strong>{_escape(explanation['title'])}</strong>"
        f"<p>{_escape(explanation['what'])}</p>"
        f"<p>{_escape(explanation['verdict'])}</p>"
        f"<p>{_escape(explanation['extra'])}</p>"
        f"<p class=\"loc\">{_escape(explanation['location'])}</p>"
        # 完整命令：等宽块。放在位置信息之后、模型理由之前——
        # 先看清事实，再看别人的意见。
        + (f"<pre class=\"cmd\">{_escape(explanation['full_command'])}</pre>"
           if explanation.get("full_command") else "")
        # B3：主卡列不下的其余动作，一条不少地列在这里。
        + (f"<p>这条命令里还有这些动作：{_escape(explanation['all_actions'])}</p>"
           if explanation.get("all_actions") else "")
        + (f"<p class=\"src\">{_escape(explanation['model_note'])}</p>"
           if explanation.get("model_note") else "")
        + (f"<p><strong>{_escape(explanation['model_reason'])}</strong></p>"
           if explanation.get("model_reason") else "")
        + (f"<p>{_escape(explanation['timeout'])}</p>"
           if explanation.get("timeout") else "")
        + (f"<p class=\"hint\">{_escape(explanation['basis'])}</p>"
           if explanation.get("basis") else "")
        + f"<p class=\"hint\">{_escape(explanation['note'])}</p>"
        + "</section>"
    )


def build_card_actions(request_id: str) -> dict[str, dict[str, Any]]:
    """三个按钮 → 两个入口，用 args 区分。"""
    return {
        ACTION_APPROVE: {"entry": ENTRY_DECIDE,
                         "args": {"request_id": request_id, "decision": APPROVE_ALLOW}},
        ACTION_DENY: {"entry": ENTRY_DECIDE,
                      "args": {"request_id": request_id, "decision": APPROVE_DENY}},
        ACTION_EXPLAIN: {"entry": ENTRY_EXPLAIN,
                         "args": {"request_id": request_id}},
    }


def build_authorize_actions(scopes: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """授权卡的两个按钮 → 同一个入口，用 args 区分签不签字。"""
    return {
        ACTION_AUTH_CONFIRM: {"entry": ENTRY_AUTHORIZE,
                             "args": {"scopes": ",".join(scopes), "confirm": True}},
        ACTION_AUTH_CANCEL: {"entry": ENTRY_AUTHORIZE,
                             "args": {"scopes": ",".join(scopes), "confirm": False}},
    }


AUTH_SCOPE_TEXT = {
    AUTH_SCOPE_ADVICE: "模型建议",
    AUTH_SCOPE_AUTO: "低风险时自动放行",
}


def scope_text(scopes: tuple[str, ...]) -> str:
    """授权范围 → 人话。**不把配置键名印在卡上**。

    原来这里直接 ``'、'.join(scopes)``，卡片上于是出现
    「本次确认范围：ai_judgement、auto_decide」——用户在签字这一刻
    不该去猜那两个词是什么（C5 同类缺陷：内部词泄到界面）。
    认不出的范围原样给：那是新加的 scope，宁可显示原名也不编一个说法。
    """
    return "、".join(AUTH_SCOPE_TEXT.get(str(scope), str(scope))
                     for scope in scopes)


def build_authorize_html(*, scopes: tuple[str, ...]) -> str:
    """授权确认卡。文案固定，不掺变量——用户签字前看到的话不该随上下文变。"""
    return (
        "<section>"
        f"<strong>{_escape(AUTHORIZATION_TITLE)}</strong>"
        f"<p>{_escape(AUTHORIZATION_TEXT)}</p>"
        f"<p class=\"risk\">{_escape(AUTHORIZATION_HEADLINE)}</p>"
        f"<p>本次确认范围：{_escape(scope_text(scopes))}（授权版本 "
        f"v{AUTHORIZATION_VERSION}）</p>"
        f"<p class=\"hint\">{_escape(AUTHORIZATION_FOOTER)}</p>"
        f"<button data-neko-action=\"{ACTION_AUTH_CONFIRM}\">确认授权</button>"
        f"<button data-neko-action=\"{ACTION_AUTH_CANCEL}\">暂不授权</button>"
        "</section>"
    )


def build_result_html(note: str) -> str:
    """结果态卡片：只留一句话，按钮已清掉。"""
    return f"<section><p>{_escape(note)}</p></section>"


class AuthorizationRecord(BaseModel):
    """用户对 L1/L2 的显式授权确认。

    写在磁盘上，插件重启后恢复——恢复的不是"自动开启"：配置里
    ``ai_judgement=false`` / ``auto_decide="off"`` 时，授权记录在手也不会有
    任何动作。它只是避免重启把用户已经做过的确认吞掉。
    """

    version: int = AUTHORIZATION_VERSION
    scopes: tuple[str, ...] = ()
    confirmed_at: float = Field(default_factory=time.time)
    confirmed_by: str = Field(default="user")
    note: str = Field(default="")

    def as_audit_text(self) -> str:
        """审计里的授权溯源：版本 + 范围 + 谁签的字。

        **这一版给审计用**：键名（``ai_judgement``）在审计里是有用的
        精确信息，事后对账要靠它。卡片上看的是 ``as_card_text``——
        同一个事实的两种写法，别混用。
        """
        return f"v{self.version}:{','.join(self.scopes) or '-'} by={self.confirmed_by}"

    def as_card_text(self) -> str:
        """卡片上的授权溯源：同一件事的人话版。

        结果卡上原来直接印 ``as_audit_text()``，于是用户看到
        「授权 v1:ai_judgement,auto_decide by=user」——那是在让用户
        猜配置键名（C5 同类缺陷）。审计要精确，卡片要能读。
        """
        return f"{scope_text(tuple(self.scopes))}（v{self.version} 已确认）"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scopes": list(self.scopes),
            "confirmed_time": _iso(self.confirmed_at),
            "confirmed_by": self.confirmed_by,
            "note": self.note,
        }


def load_authorization(path: Any) -> AuthorizationRecord | None:
    """从磁盘恢复授权确认。文件不存在/读不懂/校验不过都当没有——不猜。"""
    if path is None:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return AuthorizationRecord.model_validate(raw)
    except ValidationError:
        return None


class AuditRecord(BaseModel):
    """一条审计行。每次决定都写一条，含 L2 的授权溯源、模型与理由。"""

    # kind = decision（一次审批决定）| authorization（一次授权确认）。
    # 两者共用一张表：授权是决定的依据，拆开就查不到"这条自动放行凭的是什么"。
    kind: str = Field(default="decision")
    ts: float = Field(default_factory=time.time)
    request_id: str = ""
    session_id: str = ""
    tool_summary: str = ""
    decision: str | None = Field(default=None, description="allow / deny；None = 没发决定")
    decided_by: str = Field(description="user / ai / expired / host_timeout；授权行为 authorization")
    model: str = Field(default="", description="给出该决定的模型；空 = 非模型决定")
    reason: str = Field(default="", description="一句话理由")
    rule: str = Field(default="", description="命中的 L0 规则；空 = 未命中")
    severity: str = Field(default="", description="L0 风险档")
    action_class: str = Field(
        default="", description="动作类别：recoverable / irreversible；空 = 未涉及")
    authorization: str = Field(default="", description="生效中的授权溯源；空 = 未涉及授权")
    # 模型建议被风险档压掉时的那句说明（2026-11-22 A 批 A2）。
    # **必须落审计**：卡片上只看得到"无法判断"，看不到"模型本来想说 allow"——
    # 而事后复盘时这两者的差别很大（一个是模型含糊，一个是模型说反了）。
    advice_note: str = Field(default="", description="模型建议与风险档的冲突说明；空 = 无冲突")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "time": _iso(self.ts),
            "request_id": self.request_id,
            "session_id": self.session_id,
            # tool_summary 里带命令原文，**这里是原文**：审计列表是给用户
            # 自己对账看的，看不清当时放行了什么就没法对账（与卡片同理）。
            # 落盘那一份由 ``audit_row_for_disk`` 脱敏——两件事分开做，
            # 因为"用户要看全文"和"磁盘不许留密钥"是两个方向的要求。
            "tool_summary": self.tool_summary,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "model": self.model or "-",
            "reason": self.reason,
            "rule": self.rule or "-",
            "severity": self.severity or "-",
            "action_class": self.action_class or "-",
            "authorization": self.authorization or "-",
            "advice_note": self.advice_note or "-",
        }

    def as_text(self) -> str:
        note = f" | {self.advice_note}" if self.advice_note else ""
        return (f"{_iso(self.ts)} | {self.kind} | {self.decision or '无决定'} | "
                f"by={self.decided_by} | rule={self.rule or '-'} | "
                f"class={self.action_class or '-'} | "
                f"auth={self.authorization or '-'} | model={self.model or '-'} | "
                f"req={self.request_id or '-'} | "
                f"session={self.session_id or '无'} | {self.tool_summary}"
                f" | {self.reason}{note}")


def audit_row_for_disk(entry: AuditRecord) -> dict[str, Any]:
    """审计行的**落盘版**：每个字符串字段都过一遍脱敏。

    A 批 A4 第 4 条。审计是唯一一份"用户没点也会落盘"的记录（到期同样
    写一行），所以它的泄露面比日志还大——``tool_summary`` 里带着命令
    原文，命令里就可能带着 ``GITHUB_TOKEN=…``。

    刻意只在这一层脱敏：``as_dict`` / ``as_text`` 保持原文，因为用户
    在 ``audit_list`` 里要看全文才能对账。把脱敏塞进 ``as_dict`` 会让
    用户自己的屏幕也变成马赛克，而那是另一种失败。
    """
    row = entry.as_dict()
    for key, value in row.items():
        if isinstance(value, str):
            row[key] = redact(value)
    return row


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


# decided_by → stats() 里的计数字段名。**user 有两个字段**（allow / deny），
# 所以不能只按 decided_by 查表，得连 decision 一起看（见 _count_field）。
_COUNT_FIELDS = {
    DECIDED_BY_AI: "stats_ai",
    DECIDED_BY_EXPIRED: "stats_expired",
    DECIDED_BY_HOST_TIMEOUT: "stats_host_timeout",
}


class ApprovalGateway:
    """本地 HTTP 服务 + pending 状态机。线程安全，不依赖宿主。"""

    def __init__(self, logger: Any, *, port: int = 48921,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 retention_seconds: float = DEFAULT_RETENTION_SECONDS,
                 ai_judgement: bool = False,
                 auto_decide: str = AUTO_OFF,
                 auto_decide_max_severity: str = "notice",
                 has_api_key: bool = False,
                 audit_path: Any = None,
                 authorization_path: Any = None,
                 warning_counts_path: Any = None,
                 advice_backend_ready: bool = False,
                 judge: Any = None) -> None:
        self._logger = logger
        self._port = int(port)
        self._timeout_seconds = float(timeout_seconds)
        self._retention_seconds = float(retention_seconds)
        # 门控开关：**只能由用户写配置**，运行期不可改（见 policy_snapshot 的测试）。
        self._ai_judgement = bool(ai_judgement)
        self._auto_decide = str(auto_decide)
        # 极高危档不可被自动放行（规格 §3），所以阈值开到 critical 是无意义的，
        # 夹到 warn 并留一条日志——默默接受一个"开了却开不动"的配置更难查。
        self._auto_decide_max_severity = effective_max_severity(
            auto_decide_max_severity)
        self._has_api_key = bool(has_api_key)
        self._audit_path = audit_path
        self._authorization_path = authorization_path
        # 用户的显式授权确认。默认 None = 没签字 → L1/L2 全不可用。
        self._authorization = load_authorization(authorization_path)
        # 红字递减的命中计数。**刻意与门控分开存**：它只影响警示长什么样，
        # 不影响任何放行判断（规格 Q3：视觉降级绝不改变行为）。
        self._warnings = WarningCounter(warning_counts_path)
        # **L1 建议后端真的能构造出来吗**。2026-11-22 口径 bug：原来这里
        # 借的是 L2 的 judge（恒为 None），于是模型建议明明在工作，
        # 启动行却显示「生效=建议否」。建议层和自动决定层是两件事，
        # 判据必须分开（规格 Q4）。
        self._advice_backend_ready = bool(advice_backend_ready)
        # 同时挂起的请求数（A 批 A3）。``acquire_slot`` / ``release_slot``
        # 用它把并发挡住 MAX_INFLIGHT，保护的是线程，不是业务逻辑。
        self._inflight = 0
        # 判断后端。5a 恒为 None——门控算得出来，但没有模型就不自动决定。
        self._judge = judge
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], PendingApproval] = {}
        self._audit: list[AuditRecord] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.stats_allow = 0
        self.stats_deny = 0
        self.stats_ai = 0
        self.stats_expired = 0
        self.stats_host_timeout = 0
        self.stats_authorizations = 0

    # ── 生命周期 ───────────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        return self._port

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    # ── 并发额度（A 批 A3） ─────────────────────────────────────────────────

    def acquire_slot(self) -> bool:
        """占一个挂起额度。满了返回 False，调用方应回 503。

        每个审批请求会占一个 handler 线程并阻塞到用户决定或到期，
        所以这个计数就是"同时能等多少人"。计数只增不减地涨到上限，
        之后的请求一个都进不来——那正是要的效果。
        """
        with self._lock:
            if self._inflight >= MAX_INFLIGHT:
                return False
            self._inflight += 1
            return True

    def release_slot(self) -> None:
        """还回一个额度。**只减不增**：重复释放会把额度越放越多。"""
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def running(self) -> bool:
        return self._server is not None

    def start(self) -> bool:
        """绑 127.0.0.1 并起守护线程。端口占用等原因失败返回 False，不抛异常。

        失败只是"审批中继不可用"，观测与播报照常跑——不能因为一个端口
        把整个插件拖死。调用方负责把失败写进日志。
        """
        if self._server is not None:
            return True
        if _port_in_use(self._port):
            # 不能只靠 bind 报错：HTTPServer 默认开 SO_REUSEADDR，Windows 上
            # 它允许两个 socket 绑同一个端口，结果第二个服务起得来却永远收不到
            # hook——那种静默失败比直接报错难查得多。所以先探一次。
            self._logger.error(
                "审批服务启动失败：端口 {}（127.0.0.1）已被其它程序占用。"
                "PermissionRequest hook 会打不进来，请改 approval_port 并同步改 hooks.json",
                self._port,
            )
            return False
        gateway = self

        class Handler(_HookHandler):
            pass

        Handler.gateway = gateway  # type: ignore[attr-defined]
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        except OSError as exc:
            self._logger.error(
                "审批服务启动失败，端口 {} 绑不上（{}）：{}。"
                "PermissionRequest hook 将打不进来，请改 approval_port 并同步改 hooks.json",
                self._port, type(exc).__name__, exc,
            )
            return False
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="deskmate-approval", daemon=True,
        )
        self._thread.start()
        # 状态行拆成「配置怎么写」与「实际生不生效」两列（规格 P5.1），
        # 且**两层的判据分开**（规格 Q4）：
        #   「生效=建议」看 L1 建议客户端（配置 + key + 授权 + 客户端可构造）
        #   「生效=自动」看 L2 判断后端（配置 + 授权 + judge 在）
        # 原来两列都借 judge 算，于是建议明明在工作也显示「否」。
        # 最后一栏特意叫「自动判断后端」而不是「后端」——建议客户端也是后端，
        # 两个词混在一起就是这次口径 bug 的由来。
        self._logger.info(
            "审批服务就绪 http://127.0.0.1:{}{} 到期={}s 保留={}s "
            "配置=建议{}/自动{} 生效=建议{}/自动{} 授权={} 自动判断后端={}",
            self._port, HOOK_PATH, self._timeout_seconds, self._retention_seconds,
            "开" if self._ai_judgement else "关",
            self._auto_decide,
            "是" if self._l1_ready() else "否",
            "是" if self._auto_decide_live() else "否",
            "已确认" if self._authorization is not None else "未确认",
            "已接入" if self._judge is not None else "未接入",
        )
        if self._authorization is None and (self._ai_judgement
                                            or self._auto_decide != AUTO_OFF):
            # 配置开了但没签字：不说清楚，用户会以为已经生效了。
            self._logger.warning(
                "approval.ai_judgement / auto_decide 已配置，但用户尚未确认授权，"
                "L1/L2 均不生效。调用 approval_authorize 确认授权文案后才可用",
            )
        return True

    def stop(self) -> None:
        server, self._server, self._thread = self._server, None, None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception as exc:  # noqa: BLE001  关闭失败不能炸掉 shutdown
            self._logger.warning("审批服务关闭异常（{}）：{}", type(exc).__name__, exc)
        with self._lock:
            for record in self._records.values():
                if record.decided_by is None:
                    # 关服务时还挂着的请求：我们不代用户决定，只标记，
                    # 让宿主的 onFailure 策略去处理。
                    record.decided_by = DECIDED_BY_EXPIRED
                    entry = self._write_audit_locked(record)
                    self._count_locked(record)
                    # 关服务没有"投递"这一步，直接落盘即可。
                    self._append_audit_file(entry)
                record.event.set()
        self._logger.info("审批服务已关闭")

    # ── hook 侧 ────────────────────────────────────────────────────────────

    def submit(self, payload: Any, *, respond: Any = None) -> dict | None:
        """HTTP handler 调入口：登记 → 事件发生时判 L2 → 等用户 → 回填或**不回填**。

        返回值是「要回填给宿主的响应体」；``None`` 表示**没有决定**，
        调用方应回 503 且不带任何 decision。

        ``respond`` 是可选的投递回调：传了就在拿到结果时立刻投递，
        并由这里统一捕获"连接已断开"（记 ``host_timeout``）。
        不传则只返回结果，由调用方自己投递（单测走这条）。

        这个函数会**阻塞调用它的线程**（每个 hook 请求一个线程），
        所以绝不能在主循环里调。
        """
        request = build_request(payload)
        record = PendingApproval(request)
        tool_input = (payload or {}).get("tool_input") if isinstance(payload, dict) else None
        # 判定跑一遍、取两个视图：severity/rule/action_class 进门控与审计，
        # risk_tier 进警示计数（B2：红字按目标档位计，极高危永不递减）。
        # 两份视图出自同一个 ``_classify``，不会打架。
        record.severity, record.rule, record.action_class = assess_risk(
            request.tool_name, tool_input, request.working_directory)
        record.risk_tier = assess_tier(
            request.tool_name, tool_input, request.working_directory)
        with self._lock:
            self._records[record.key] = record
            self._prune_locked()
        self._logger.info(
            "收到审批请求 {}（会话 {} 工具 {} 风险 {}）", request.request_id,
            request.session_id or "无", request.tool_name or "未知", record.severity,
        )

        # L2 在**事件发生的那一刻**判，不是等到期（规格 §2 硬规则 4）。
        # 放在锁外：判断后端可能慢（网络），不能堵住 resolve / HUD / 建卡。
        self._maybe_auto_allow(record)

        record.event.wait(timeout=self._timeout_seconds)

        with self._lock:
            if record.decided_by is None:
                # 到期：**只标记，不发任何决定、不记成拒绝**（规格 §2 硬规则 1）。
                # 拦住这次调用的是宿主自己的 onFailure:"block"，不是我们的判断。
                self._mark_expired_locked(record)

        # 审计行与计数都必须在**投递之前**进内存：客户端一收到回填就可能来查，
        # "先回客户端后记账"会让 audit_rows() 偶发少一行——实测 CI seed 下
        # 约 1/4 概率红一次。stats() 是同道理的第二次：它晚一步落地的话，
        # 状态行会短那一刻显示"allow=0"，而审计里明明躺着一条 allow。
        # 落盘仍必须晚于投递：host_timeout 是投递失败才知道的，
        # JSONL 是追加写的，只能写最终状态。
        with self._lock:
            entry = self._write_audit_locked(record)
            self._count_locked(record)

        result = self._response_for(record)
        if respond is not None:
            try:
                respond(result)
            except (ConnectionError, TimeoutError) as exc:
                # 连接类失败一律算"没送达"：Windows 上客户端提前离开时抛的是
                # ConnectionAbortedError(10053)，它**不是** BrokenPipeError 的子类，
                # 只捕后两者会漏——实测踩过。不重试：重试等于发两条。
                self._logger.info(
                    "回填时连接已断开（{}），请求 {} 记为 host_timeout，未送达",
                    type(exc).__name__, request.request_id,
                )
                self.note_host_timeout(request.request_id)

        with self._lock:
            self._append_audit_file(entry)
            # **不删记录**：过期条目要留在 _records 里供 pending_list 事后查，
            # 由 _prune_locked 按保留期统一清理。删在这里就等于"过期即消失"，
            # 而那正是审计最需要看到的几条。
        return result

    # ── L2 自动放行（事件发生时；规格 §2 硬规则 2/3/4） ─────────────────────

    def _maybe_auto_allow(self, record: PendingApproval) -> bool:
        """门控 + 后端都过，才替用户点一次"允许"。返回是否自动放行了。

        **永不 auto-deny**：后端说 deny 也不代用户拒绝，照样把卡片摆出来让人点。
        没授权 / 没 key / 没后端 / 后端异常 / 后端说 deny / 无效返回 → 一律不自动
        决定，记录留给用户，走到期。也就是说：这条路只有一个可能的产出是 allow。
        """
        with self._lock:
            authorization = self._authorization
        scopes = authorization.scopes if authorization is not None else ()
        eligible, why = auto_decide_gate(
            severity=record.severity, action_class=record.action_class,
            ai_judgement=self._ai_judgement, has_key=self._has_api_key,
            auto_decide=self._auto_decide,
            max_severity=self._auto_decide_max_severity,
            authorized_scopes=scopes,
            # 只读白名单（B2）在 rules 里算，这里只消费结论。
            readonly=l2_readonly_allowed(_command_of_record(record)),
        )
        if not eligible:
            # 绝大多数请求走这条（默认全关）。不打日志：每次都打就是噪音，
            # 原因在卡片与审计里都能看到。
            return False
        if self._judge is None:
            # 门控算得过但没接判断后端（5a）。**不因为门控通过就自己拍一个**——
            # 那是插件在替用户判断。把请求留给用户，走到期。
            self._logger.info(
                "请求 {} 的 L2 门控通过（{}），但判断后端未接入（5b），"
                "不自动决定，转交用户判断",
                record.request.request_id, why,
            )
            return False

        try:
            verdict = self._judge(record)
        except Exception as exc:  # noqa: BLE001  后端炸了不能炸掉审批
            self._logger.warning(
                "请求 {} 的 L2 判断后端异常（{}），不自动决定，转交用户判断",
                record.request.request_id, type(exc).__name__,
            )
            return False
        if not isinstance(verdict, dict) or verdict.get("decision") not in DECISIONS:
            self._logger.warning(
                "请求 {} 的 L2 判断没给出有效决定，不自动决定，转交用户判断",
                record.request.request_id,
            )
            return False
        if verdict["decision"] != APPROVE_ALLOW:
            # 永不 auto-deny（规格 §2 硬规则 3）：AI 说"拒"也只是个建议，
            # 拒绝必须由用户亲手点。
            self._logger.info(
                "请求 {} 的 L2 判断建议 deny，但插件永不自动拒绝，转交用户判断",
                record.request.request_id,
            )
            return False

        with self._lock:
            # 用户可能在这一瞬间点了按钮——那种情况以用户为准，不覆盖。
            if record.decided_by is not None:
                return False
            record.decision = APPROVE_ALLOW
            record.decided_by = DECIDED_BY_AI
            record.judge_model = str(verdict.get("model") or "")
            record.judge_reason = str(verdict.get("reason") or "")
            record.authorization = (authorization.as_audit_text()
                                    if authorization is not None else "")
            record.authorization_card = (authorization.as_card_text()
                                         if authorization is not None else "")
            record.auto = True
            # 自动放行不该再弹卡片等人点——但要发一条 blind 通知 + 一条审计。
            # card_id 置成占位空串，timer 就不会为它建卡。
            record.card_id = ""
            record.event.set()
        self._logger.warning(
            "请求 {} 由 L2 自动放行（用户已授权 {}，模型 {}，理由 {}）",
            record.request.request_id,
            authorization.as_audit_text() if authorization is not None else "-",
            record.judge_model or "-", record.judge_reason or "-",
        )
        return True

    def _response_for(self, record: PendingApproval) -> dict | None:
        """记录 → 回填体。``None`` = 没有决定，调用方回 503。"""
        if record.decided_by == DECIDED_BY_EXPIRED:
            return None
        if record.decided_by == DECIDED_BY_AI:
            # L2 只可能自动 allow；且 **allow 不带 message**（deny 专属字段）。
            # 自动放行的说明走 blind 通知、卡片与审计，不回填给宿主。
            return hook_response(APPROVE_ALLOW)
        # message 是 deny 专属字段：allow 时带上"你拒绝了"会自相矛盾
        # （这个 bug 在端到端跑第一遍时撞见过一次，重构后又差点回来，
        #  所以有单测盯着）。
        if record.decision == APPROVE_DENY:
            return hook_response(APPROVE_DENY, message=_user_deny_message())
        return hook_response(APPROVE_ALLOW)

    def _mark_expired_locked(self, record: PendingApproval) -> None:
        """到期：只标记 expired，**不发任何决定**。调用方必须已持有锁。

        这里刻意不碰门控、不调判断后端——L2 的时机是事件发生的那一刻
        （``_maybe_auto_allow``），到期时再判就等于让用户白等 570 秒。
        到期只做一件事：把状态写成"没人决定"。
        """
        record.decided_by = DECIDED_BY_EXPIRED
        self._logger.warning(
            "审批到期（{}s）无人决定：宿主已自行处理，本插件未做判断，"
            "请求 {} 标记 expired 且不回填",
            self._timeout_seconds, record.request.request_id,
        )

    def _count_locked(self, record: PendingApproval) -> None:
        """按最终 decided_by 记一次数。调用方必须已持有锁。

        集中在一处：早期版本把计数写在各个分支里，结果 stats_ai 永远是 0——
        因为那时 if/elif 已经走完，加在分支里的计数没人执行。

        字段名由 ``_count_field`` 给，所以"往哪一格加"也只有一处知道。
        """
        field = self._count_field(record)
        if field is not None:
            setattr(self, field, getattr(self, field) + 1)

    def _count_field(self, record: PendingApproval) -> str | None:
        """这条记录该进 stats() 的哪一格。认不出返回 None（不计数）。"""
        if record.decided_by == DECIDED_BY_USER:
            return ("stats_allow" if record.decision == APPROVE_ALLOW
                    else "stats_deny")
        return _COUNT_FIELDS.get(record.decided_by)

    def _shift_count_locked(self, previous: str | None,
                            current: str | None) -> None:
        """把一笔计数从一格挪到另一格。总数不变，一条请求始终只计一次。

        只有 ``note_host_timeout`` 用它：投递失败时状态从"用户点了允许"
        变成"没送达"，而计数早就按前一个状态记过了。
        **只减不增到负数**：重复调用的后果是多减几次，不是计出个负数来。
        """
        if previous == current:
            return
        for field, delta in ((previous, -1), (current, 1)):
            if field is None:
                continue
            value = getattr(self, field, 0) + delta
            setattr(self, field, max(0, value))

    def _write_audit_locked(self, record: PendingApproval) -> AuditRecord:
        """建一条审计行并放进内存，返回它。调用方必须已持有锁。

        **只进内存，不落盘、不计数**：落盘要等投递结果（host_timeout 是投递
        失败才知道的），计数要按最终状态记一次，两件事都由 submit 收尾。
        """
        entry = AuditRecord(
            kind="decision",
            request_id=record.request.request_id,
            session_id=record.request.session_id,
            tool_summary=record.request.tool_summary,
            decision=record.decision,
            decided_by=record.decided_by or DECIDED_BY_EXPIRED,
            model=record.judge_model,
            rule=record.rule,
            severity=record.severity,
            action_class=record.action_class,
            authorization=record.authorization,
            reason=self._audit_reason(record),
            # 模型建议被风险档压掉时要能事后查到（A 批 A2 第 3 条）。
            advice_note=str(getattr(record, "advice", {}).get("conflict", "") or ""),
        )
        self._audit.append(entry)
        if len(self._audit) > AUDIT_MEMORY_LIMIT:
            del self._audit[: len(self._audit) - AUDIT_MEMORY_LIMIT]
        # 记在记录上：投递失败时 note_host_timeout 要能把这一条改成最终状态。
        record.audit_entry = entry
        return entry

    def _append_audit_locked(self, entry: AuditRecord) -> None:
        """进内存 + 落盘。调用方必须已持有锁。"""
        self._audit.append(entry)
        if len(self._audit) > AUDIT_MEMORY_LIMIT:
            del self._audit[: len(self._audit) - AUDIT_MEMORY_LIMIT]
        self._append_audit_file(entry)

    def _audit_reason(self, record: PendingApproval) -> str:
        if record.decided_by == DECIDED_BY_USER:
            return "用户在 N.E.K.O 里点了按钮"
        if record.decided_by == DECIDED_BY_AI:
            return (getattr(record, "judge_reason", "")
                    or "L2 在用户授权下自动放行（低风险）")
        if record.decided_by == DECIDED_BY_HOST_TIMEOUT:
            return "回填时连接已被宿主断开，决定未送达"
        # 到期：**不许出现"拒绝"字样**——我们没拒绝，是宿主自己处理的。
        return "到期无人决定；宿主已自行处理，本插件未做判断"

    def _append_audit_file(self, entry: AuditRecord) -> None:
        """审计落一份 JSONL，插件重启后仍可查。写失败只记日志，不丢内存里的那份。

        写出去的是 ``audit_row_for_disk``——**脱敏版**。内存里那份保持原文，
        用户查 ``audit_list`` 时要看全文。
        """
        if self._audit_path is None:
            return
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(audit_row_for_disk(entry),
                                        ensure_ascii=False) + "\n")
        except OSError as exc:
            self._logger.warning("审计写入失败（{}）：{}", type(exc).__name__, exc)

    # ── 显式授权（规格 §2 硬规则 2） ─────────────────────────────────────────

    def grant_authorization(self, scopes: Any, *,
                            confirmed_by: str = "user") -> AuthorizationRecord:
        """记录用户的授权确认。**只有用户在卡片上点了「确认授权」才能走到这。**

        插件自身没有任何路径调用它：没有定时任务、没有 hook、没有配置变更会
        触发授权（``on_config_change`` 刻意不碰它，见 ``__init__.py``）。
        """
        clean = tuple(dict.fromkeys(
            str(scope) for scope in (scopes or ()) if str(scope) in AUTH_SCOPES))
        if not clean:
            raise ValueError(f"没有可授权的范围（可用：{'/'.join(AUTH_SCOPES)}）")
        record = AuthorizationRecord(scopes=clean, confirmed_by=confirmed_by or "user")
        with self._lock:
            self._authorization = record
            self.stats_authorizations += 1
            self._persist_authorization_locked(record)
            self._append_audit_locked(AuditRecord(
                kind="authorization",
                tool_summary="授权确认",
                decision=None,
                decided_by=DECIDED_BY_AUTHORIZATION,
                authorization=record.as_audit_text(),
                reason=f"用户确认授权文案：{AUTHORIZATION_HEADLINE}",
            ))
        self._logger.warning(
            "用户已确认授权：范围={} 版本=v{}。{}",
            ",".join(clean), record.version, AUTHORIZATION_HEADLINE,
        )
        return record

    def _persist_authorization_locked(self, record: AuthorizationRecord) -> None:
        """授权确认落盘，重启后恢复。写失败只记日志——不影响本次生效。"""
        if self._authorization_path is None:
            return
        try:
            self._authorization_path.parent.mkdir(parents=True, exist_ok=True)
            self._authorization_path.write_text(
                json.dumps(record.model_dump(), ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self._logger.warning("授权确认写入失败（{}）：{}", type(exc).__name__, exc)

    def authorized_scopes(self) -> tuple[str, ...]:
        with self._lock:
            record = self._authorization
        return record.scopes if record is not None else ()

    def authorization_snapshot(self) -> dict[str, Any]:
        """授权状态快照（只读）。给 status 入口和授权卡片用。"""
        with self._lock:
            record = self._authorization
        if record is None:
            return {
                "granted": False, "version": 0, "scopes": [],
                "confirmed_time": None, "confirmed_by": "",
                "text": AUTHORIZATION_TEXT, "headline": AUTHORIZATION_HEADLINE,
            }
        snapshot = record.as_dict()
        snapshot.update({
            "granted": True,
            "text": AUTHORIZATION_TEXT,
            "headline": AUTHORIZATION_HEADLINE,
        })
        return snapshot

    def note_host_timeout(self, request_id: str) -> None:
        """投递失败：决定可能做了但没送到宿主。

        把 ``decided_by`` 覆盖成 ``host_timeout``——从宿主的视角看，
        这条请求**没有发生过任何决定**（它没收到），所以审计必须这么记。
        原本的决定留在 ``decision`` 字段里，人和审计都能看出"用户选了允许但没送达"。

        **同时改已经写进内存的那条审计行**：它比投递更早落（见 submit 里的注释），
        不改的话内存里会留着"by=user"，而落盘的 JSONL 是"by=host_timeout"——
        同一份审计两种说法，比没有审计更糟。

        **计数也跟着搬**：计数现在与审计行同时落（早于投递），所以这里要把
        已经记下的那一笔从原状态挪到 ``host_timeout``。不搬的话这条请求会被
        计两次（一次 allow、一次 host_timeout），而"一条请求只计一次、
        且按最终状态计"是 stats() 的语义。挪而不是重计，所以总数不变。
        """
        with self._lock:
            for record in self._records.values():
                if record.request.request_id != request_id:
                    continue
                previous = self._count_field(record)
                if record.decided_by != DECIDED_BY_HOST_TIMEOUT:
                    record.decided_by = DECIDED_BY_HOST_TIMEOUT
                entry = record.audit_entry
                if entry is not None:
                    entry.decided_by = DECIDED_BY_HOST_TIMEOUT
                    entry.decision = record.decision
                    entry.reason = self._audit_reason(record)
                self._shift_count_locked(previous, self._count_field(record))
                return

    def _prune_locked(self) -> None:
        """清掉超过保留期的记录。调用方必须已持有锁。"""
        cutoff = time.time() - self._retention_seconds
        stale = [key for key, record in self._records.items()
                 if record.request.received_at < cutoff]
        for key in stale:
            del self._records[key]

    # ── 插件侧（timer / UI 动作 / 入口） ───────────────────────────────────

    def take_new(self) -> list[PendingApproval]:
        """取走还没建卡片的记录（取走即占位，避免重复建卡）。"""
        with self._lock:
            self._prune_locked()
            fresh = [r for r in self._records.values() if r.needs_card()]
            for record in fresh:
                record.card_id = ""      # 占位：表示"已取走，正在建"
            return fresh

    def take_auto_allowed(self) -> list[PendingApproval]:
        """取走已自动放行、还没发过通知的记录（取走即占位，避免重复通知）。

        自动放行不弹卡片，但**必须让用户知道**：看不见的放行等于后门。
        通知由插件的 approval timer 发（push_message 是 async SDK 调用）。
        """
        with self._lock:
            fresh = [r for r in self._records.values() if r.auto and not r.auto_announced]
            for record in fresh:
                record.auto_announced = True
            return fresh

    def attach_card(self, request_id: str, card_id: str) -> None:
        with self._lock:
            for record in self._records.values():
                if record.request.request_id == request_id:
                    record.card_id = card_id
                    return

    def settle_pending_cards(self) -> list[PendingApproval]:
        """已建卡且已了结、但还没在卡片上显示的记录。"""
        with self._lock:
            return [r for r in self._records.values() if r.needs_announce()]

    def mark_announced(self, request_id: str) -> None:
        with self._lock:
            for record in self._records.values():
                if record.request.request_id == request_id:
                    record.card_announced = True
                    return

    def resolve(self, request_id: str, decision: str, *,
                decided_by: str = DECIDED_BY_USER) -> str:
        """UI 按钮的落点。返回 ``resolved`` / ``already-resolved`` / ``expired`` / ``unknown``。

        **幂等**：重复点击（用户连点、多窗口各点一次）只有第一次算数。
        宿主的按钮代理本来就会忽略执行中按钮的重复点击，但那是 UI 层的
        尽力而为，业务层必须自己保证——跨窗口的重复它管不了。
        """
        if decision not in DECISIONS:
            return "unknown"
        with self._lock:
            for record in self._records.values():
                if record.request.request_id != request_id:
                    continue
                if record.decided_by == DECIDED_BY_EXPIRED:
                    # 过期的卡片按钮：不回填，明确告诉用户这条已经不算数了。
                    return "expired"
                if record.decided_by is not None:
                    return "already-resolved"
                record.decision = decision
                record.decided_by = decided_by
                record.event.set()
                self._logger.info(
                    "审批已决定（{} by {}）：请求 {}", decision, decided_by, request_id,
                )
                return "resolved"
        return "unknown"

    def policy_snapshot(self) -> dict[str, Any]:
        """当前门控开关与授权状态的快照。

        **只读**：这些值只能来自用户配置（+ 用户在授权卡上签字），
        运行期没有任何方法能改它们。测试靠这一点守住规格 §2 硬规则 2
        「没有显式授权就不执行任何明确操作」。
        """
        return {
            "ai_judgement": self._ai_judgement,
            "auto_decide": self._auto_decide,
            "auto_decide_max_severity": self._auto_decide_max_severity,
            "has_api_key": self._has_api_key,
            "judge_connected": self._judge is not None,
            "authorized_scopes": list(self.authorized_scopes()),
        }

    def advice_state(self) -> str:
        return advice_state(
            ai_judgement=self._ai_judgement,
            has_key=self._has_api_key,
            backend_ready=self._advice_backend_ready,
            authorized=bool(self.authorized_scopes()),
        )

    def note_warning(self, key: str) -> int:
        """记一次该类别的命中，返回**含这次**的次数。

        计数挂了就返回 1（当成第一次）——多显示一次红字远比
        因为计数失败少显示一次安全。
        """
        try:
            return self._warnings.bump(key)
        except Exception as exc:  # noqa: BLE001  计数失败不影响审批
            self._logger.warning("警示计数失败（{}）：{}", type(exc).__name__, exc)
            return 1

    def _auto_decide_live(self) -> bool:
        """L2 现在是否真的能自动放行（配置开 + 授权签了 + 有判断后端）。

        **刻意不看建议层**：L2 要的是判断后端，不是建议客户端。
        这两层曾经共用一个 readiness，结果启动行把"建议在工作"显示成
        "没生效"，也把"没有判断后端"显示成"建议没生效"（规格 Q4）。
        """
        if self._auto_decide != AUTO_LOW_RISK_ONLY or self._judge is None:
            return False
        scopes = self._authorization.scopes if self._authorization else ()
        return AUTH_SCOPE_AUTO in scopes

    def _l1_ready(self) -> bool:
        """L1 建议现在是否真的给得出建议。

        判据与 ``advice_state()`` **完全同一处**：配置开 + 有 key + 签了字
        + 建议客户端构造得出来。状态行只是它的展示，不另算一套——
        再算一套就会出现"行上说没生效、卡片上却在给建议"的自相矛盾
        （2026-11-22 真机反馈：卡片 3 秒建出且带理由，启动行却说 建议否）。
        """
        return self.advice_state() == ADVICE_STATE_READY

    def undecided_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.is_pending())

    def snapshot(self) -> list[dict[str, Any]]:
        """pending_list 的数据：未决 + 已了结（未删的）。最新在前。"""
        with self._lock:
            self._prune_locked()
            rows = [r.as_dict() for r in self._records.values()]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows

    def audit_rows(self) -> list[dict[str, Any]]:
        """audit_list 的数据。最新在前。"""
        with self._lock:
            rows = [entry.as_dict() for entry in self._audit]
        rows.reverse()
        return rows

    def audit_text(self) -> str:
        """可导出的纯文本审计。"""
        with self._lock:
            lines = [entry.as_text() for entry in self._audit]
        lines.reverse()
        header = f"# 同桌猫审批审计（共 {len(lines)} 条）"
        return "\n".join([header, *lines])

    def find(self, request_id: str) -> PendingApproval | None:
        with self._lock:
            for record in self._records.values():
                if record.request.request_id == request_id:
                    return record
        return None

    def stats(self) -> dict[str, int]:
        return {
            "pending": self.undecided_count(),
            "allow": self.stats_allow,
            "deny": self.stats_deny,
            "ai": self.stats_ai,
            "expired": self.stats_expired,
            "host_timeout": self.stats_host_timeout,
            "authorizations": self.stats_authorizations,
            "running": 1 if self.running() else 0,
        }


def _user_deny_message() -> str:
    """用户亲手点拒绝时，回给 Claude Code 的那句话。

    **必须是硬指令**（2026-11-21 真机实测后改）：原来只写"用户拒绝了这次调用"，
    Claude 把它理解成"命令失败"，于是换写法重试、换路径绕过，越拦越忙。
    所以这里明确说清三件事：这是用户的明确拒绝、不要换写法、不要绕过、
    等用户下一步指示。

    这句话会进 Claude 的上下文，是给它看的，不是给用户看的——
    写成人话它会当成建议，写成指令它才会停。
    """
    return ("同桌猫：用户在 N.E.K.O 里明确拒绝了这次调用。"
            "不要换写法重试，不要绕过，也不要尝试等价的其他命令。"
            "停下来，等用户的下一步指示。")


class _HookHandler(BaseHTTPRequestHandler):
    """只认 POST /hook/permission。其余一律 404，不猜意图。"""

    gateway: ApprovalGateway

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 基类默认往 stderr 打访问日志；插件的日志走 self.logger，这里静音。
        return

    def do_GET(self) -> None:  # noqa: N802  标准库命名
        """探活。中继（hook_relay.py）在转发之前先打这里，1s 超时。

        回答里带**服务标识与 token**：中继要确认对面是自己这一版插件，
        而不是端口上随便一个 HTTP 服务（见 HEALTH_TOKEN 的说明）。
        只回答"活着"是不够的——那证明不了一件事：对面不会把命令送给别人。

        这个端点**不占 in-flight 额度**：它必须一直应答，否则插件一忙，
        中继就会以为插件死了，从此不再转发（那比忙更糟）。
        """
        if self.path.split("?", 1)[0] != HEALTH_PATH:
            self._respond(404, {"ok": False, "reason": "not_found"})
            return
        self._respond(200, {"ok": True, "service": HEALTH_SERVICE,
                            "token": HEALTH_TOKEN})

    def do_POST(self) -> None:  # noqa: N802  标准库命名
        if self.path.split("?", 1)[0] != HOOK_PATH:
            self._respond(404, {"ok": False, "reason": "not_found"})
            return
        # 挂起数量有上限（见 MAX_INFLIGHT）。拿不到额度就回 503：
        # 中继把 503 当"插件没有决定"，于是交还原生权限流程——
        # 用户照样能批准，只是不走我们的卡片。
        if not self.gateway.acquire_slot():
            self._respond(503, {"ok": False, "reason": "busy"})
            return
        try:
            self._handle_post()
        finally:
            # 必须在 submit 返回之后放：它要阻塞到用户点按钮或到期，
            # 提前放就等于没限流。
            self.gateway.release_slot()

    def _handle_post(self) -> None:
        if self.path.split("?", 1)[0] != HOOK_PATH:
            self._respond(404, {"ok": False, "reason": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(400, {"ok": False, "reason": "bad_length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            self._respond(400, {"ok": False, "reason": "bad_json"})
            return

        try:
            self.gateway.submit(payload, respond=self._deliver)
        except Exception as exc:  # noqa: BLE001  handler 崩了会带走整个服务
            self.gateway._logger.warning(  # noqa: SLF001  同类，允许
                "审批处理异常（{}）：{}", type(exc).__name__, exc,
            )
            self._respond(500, {"ok": False, "reason": "internal_error"})
            return

    def _deliver(self, result: dict | None) -> None:
        """把回填结果写回连接。``None`` = 插件没有决定 → 503 + 无 decision。"""
        if result is None:
            self._respond(503, no_decision_response(
                reason="no_decision",
                timeout_seconds=self.gateway.timeout_seconds,
            ))
            return
        self._respond(200, result)

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
