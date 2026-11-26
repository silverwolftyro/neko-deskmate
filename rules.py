"""L0 规则层：零 key 可用的固定公式判定（M0-lite-3a）。

只做两类：**删除类**与**失败类**。这两类不依赖模型、不依赖 key，
所以无 key 时照样能报——这正是零 key 策略里"装得上、看得见规则级红灯"的那部分。

判定方式（2026-11-21 定，含一次设计返工）：
第一版按「命令位置」匹配（开头或 `; && || |` 换行 `(` `$(` 之后），
实测会**漏掉** `sudo rm`、`xargs rm`、`bash -c "rm -rf x"`、`/bin/rm`、`C:\\...\\del`
——这些前面是空格、引号或路径分隔符，都不在分隔符集合里。
改成**整 token 匹配 + 取 basename**：引号内的 `rm` 也会命中，
于是 `echo "rm -rf 是危险操作"` 会误报。这个交换是刻意的：
误报在 summary 里一眼可辨（用户能看到那是 echo），漏判却是隐形的。

严重度阶梯（失败类，我来定）：
  notice    429 / 502 / 503 / 504  瞬态：限流或上游临时不可用，Agent 会自己重试
  critical  status >= 500 且非瞬态  服务端真的出了错（500 / 501 …），全线不可用
  warn      400 <= status < 500     客户端/配置问题，范围有限
  warn      无 status / 坏值 / 转录 error
`info` 不产生规则命中，预留给非失败的一般状态变化。

**2026-11-26 第五条误报**：老口径一句 `status >= 500 → critical`，实测今晚
连报 4 条【严重】全是网关侧 502。502/503/504 说的是"上游临时接不上"，
不是"全线不可用"——用【严重】喊它，等于拿一个我们不掌握的事实去训练用户
看到红字就点掉。详见 ``severity_for_status``。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import Field

from .commands import (
    DELETE_PAIRS,
    DELETE_TOKENS,
    PRINT_ONLY_COMMANDS,
    TIER_SCOPE_UNKNOWN,
    _expand_literals,
    _literal_assignments,
    basename,
    command_length,
    delete_command_bodies,
    delete_segment_texts,
    delete_tier_for_command,
    severity_for_tier,
    simple_commands,
)
from .events import (
    AttributionFields,
    UnifiedEvent,
    delegated_server,
)

SEVERITY_INFO = "info"
SEVERITY_NOTICE = "notice"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"
SEVERITIES = (SEVERITY_INFO, SEVERITY_NOTICE, SEVERITY_WARN, SEVERITY_CRITICAL)

#: **瞬态** HTTP 状态码：上游临时不可用，Agent 自己会重试。
#: 与 429 同一类处理（notice），理由见 ``severity_for_status``。
#: 只收这三个，**500 / 501 不在内**——那两个不是"稍后再来"，
#: 是服务端真的出了错或端点根本不支持。
TRANSIENT_STATUS = frozenset({502, 503, 504})

RULE_DELETE = "delete"
RULE_FAILURE = "failure"
# 下派观测（M0-lite-4 补 / 归因第 4 步）：只记录"本会话把活交给了外部 worker"，
# 不读 args、不猜动作、不判危险——那是归因错的高发区。
RULE_DELEGATION = "delegation"

# 任务书点名的六个模式。大小写不敏感（PowerShell 本身不敏感）。
# **词表本身已搬到 commands.py**（B3：所有命令词表同一个家，
# 否则 B3 的危险度排序要么抄一份、要么反向 import）。这里只转发。
HELP_FLAGS = frozenset({"--help", "-h", "/?", "-?"})

# ── L2 只读白名单（B2）───────────────────────────────────────────────────
# L2 是"用户签了字之后的代办"。代办的范围必须收敛到**看了不会出事**的命令，
# 而不是"大概不会出事"的命令：B2 把删除按目标分档之后，`rm -rf build/`
# 落在可恢复档、阈值之内——没有这道白名单，它就会被自动放行。
# 所以白名单外一律不进自动，哪怕它可恢复、哪怕阈值开到顶。
#
# 收录标准：**不写、不删、不改**任何文件，也不发起网络请求。
# `find` 刻意不收（`find -delete` 能删），`git` 只收明确的只读子命令。
L2_READONLY_COMMANDS = frozenset({
    "ls", "dir", "pwd", "cd", "echo", "printf", "cat", "type", "more",
    "less", "head", "tail", "which", "where", "whoami", "hostname",
    "date", "printenv", "env", "grep", "egrep", "fgrep", "findstr",
    "rg", "select-string", "get-content", "get-location", "get-childitem",
    "jq", "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git config --list", "npm ls", "pip list", "pip show",
    "uv pip list", "uv pip show",
})


def l2_readonly_allowed(command: str) -> bool:
    """这条命令整个都在 L2 只读白名单里吗。纯函数。

    **每一段都必须在**：`ls && rm -rf x` 里有一段不在，整条就不批。
    有一段看不懂（归一化之后没有命令主体）同样不批——白名单是"认得且安全"，
    不是"没认出所以放过"。
    """
    segments = simple_commands(command)
    if not segments:
        return False
    for body in segments:
        if not body:
            return False
        head = body[0]
        pair = f"{body[0]} {body[1]}" if len(body) > 1 else head
        if pair in L2_READONLY_COMMANDS or head in L2_READONLY_COMMANDS:
            continue
        return False
    return True

# 分词：先按 shell 命令分隔符切成「简单命令」，再按空白与括号等切 token。
# 必须分段：否则 `rm -rf x && del --help` 里后面那个 --help 会把前面的真删除一起赦免了。
# **分词与归一化实现在 commands.py**（B1/B2：闸门与词典共用一套，禁止两套判据）。


class RuleHit(AttributionFields):
    """一条规则命中的结果。coalesce_key 相同的命中在窗口内合并。

    归因字段来自 ``AttributionFields``（与 ``UnifiedEvent`` 共用同一份不变式），
    外加三个播报层字段。``attribution`` 由 requester/evidence 推导，不手填。
    """

    rule: str = Field(description="规则名：delete / failure")
    severity: str = Field(description="info / notice / warn / critical")
    summary: str = Field(description="给人看的一行描述")
    matched: str = Field(description="具体命中了什么，如 rm / git clean / 429")
    agent: str = Field(default="", description="来源 Agent")
    session: str = Field(default="", description="会话标识")
    ts: str = Field(default="", description="ISO8601 UTC")
    coalesce_key: str = Field(default="", description="合并键；相同则在窗口内只报一次")
    # B5：完整命令的字数，**唯一来源是 commands.command_length**。
    # 播报落盘、卡片提示、审计必须用同一个数——用户是拿它判断
    # "我有没有看全"的。summary 里的命令已被截断，不能从它反推
    # （老实现正是那么干的，于是同一条命令有两个字数：120 vs 268）。
    command_length: int | None = Field(
        default=None, description="完整命令字数；非命令类规则为 None")
    # B11：完整命令原文。**summary 是按 80 字截过的**（``_clip``），而上面
    # ``command_length`` 的存在本身就是"不能从 summary 反推命令"的记功碑——
    # 现在播报压制要把命令送模型审（B10 第 2 类输入），没有原文就只能送截的，
    # 而截在中间的 `--exclude=…` 恰好是判断"值不值得说"的关键。
    # 默认空串：失败类 / 下派类没有命令。
    command: str = Field(default="", description="完整命令原文；非命令类规则为空")

    # 播报层归因（2026-11-21 决策记录 §5）：
    # chain_id 委托链根，**今天恒等于 session**。等将来 worker 自己的日志也可观测时，
    #   才靠它把「大脑下派」和「双手执行」并成一条播报；在那之前不要提前造关联键。
    # delegated_to 下派目标（mcp__ 的 server 名），None = 没有下派。
    chain_id: str = Field(default="", description="委托链根；今天恒等于 session")
    delegated_to: str | None = Field(default=None, description="下派目标（MCP server 名）")


def _attribution_of(event: UnifiedEvent | None) -> dict:
    """从源事件取归因字段。

    ``event`` 为 None（测试里直调纯函数）时返回空 dict —— 一切按「未知」处理。
    刻意**不传** ``attribution``：它由 requester/evidence 推导，手填就会打架。
    """
    if event is None:
        return {}
    return {
        "requester": event.requester,
        "requester_evidence": event.requester_evidence,
        "executor": event.executor,
        "working_directory": event.working_directory,
        "chain_id": event.session,
        "delegated_to": delegated_server(event.tool),
    }


# ── 分词 ──────────────────────────────────────────────────────────────────

def _tokens(command: str) -> list[str]:
    """整条命令切 token。分词实现在 ``commands``，这里只做转发。"""
    from .commands import tokens

    return tokens(command)


def _simple_commands(command: str) -> list[list[str]]:
    """按 `;` `|` `&` 换行切段，每段再切 token。段内判定互不影响。"""
    return simple_commands(command)


def _basename(token: str) -> str:
    return basename(token)


# ── 删除类 ────────────────────────────────────────────────────────────────

def _is_flag(token: str) -> bool:
    return token.startswith("-") or token in ("--", "/?", "-?")


def _is_help_only(rest: list[str]) -> bool:
    """这一段是不是**纯粹**在问帮助。

    老实现只看"命令名是删除命令 + 后面任意位置有 --help"，于是
    `rm -rf ~ --help`（真删除）被赦免，而 `sudo rm -rf ~ --help` 因为
    `sudo` 占了段首反而不赦免——**同一个动作两种 verdict**。
    2026-11-22 真机记录到的正是这一类：判定对命令写法敏感，
    不是某个前缀能绕过，verdict 随前缀与尾巴一起变。

    改成：命令名之后**每一个** token 都必须是开关，且其中至少一个是帮助开关。
    `rm --help` 赦免，`rm -rf ~ --help` 不赦免。
    """
    if not rest or not any(t in HELP_FLAGS for t in rest):
        return False
    return all(_is_flag(t) for t in rest)


def match_delete(command: str) -> str | None:
    """返回命中的危险 token 名，未命中返回 None。纯函数。

    B1 之后每段先**归一化**（剥掉前导赋值与包装词）再判，所以
    `sudo rm`、`VAR=x rm`、`env VAR=x rm`、`(rm …)`、`! rm …`、`xargs rm`
    与裸 `rm` 是同一个 verdict——不再随写法变。

    帮助豁免改为整段判定（见 ``_is_help_only``），并且**在归一化之后**执行，
    所以加不加 `sudo` 前缀不再影响它。

    兜底匹配仍然保留，但**命令主体明确"不执行参数"的除外**（echo / git /
    curl …）：`echo "rm -rf 是危险操作"` 每张卡都顶红字，用户会被训练成
    无脑点确认，最后闸门真的失效。命令主体不认识时照旧兜——
    漏判是隐形的，误报至少能在 summary 里看见。

    2026-11-26：兜底扫描从"段内所有 token"收窄到"**命令位** token"
    （``commands._command_position_tokens``）。原来那句只读代码
    ``print('allow 里带 rm 的:')`` 里的 rm 让整条命令被判成删除，
    目标再被 ``perm.get('allow',[])`` 里的 ``[]`` 顶成极高危——
    一条【严重】播报，而 critical 永不递减。段首/帮助/打印类那三层
    豁免都还在，收窄的只是"哪些 token 算被执行的命令"。
    """
    for body, position in delete_command_bodies(command):
        if not body or _is_help_only(body[1:]):
            continue

        for left, right in DELETE_PAIRS:
            for i in range(len(body) - 1):
                if body[i] == left and body[i + 1] == right:
                    return f"{left} {right}"

        head = basename(body[0])
        if head in DELETE_TOKENS:
            return head

        if head not in PRINT_ONLY_COMMANDS:
            for token in position:
                name = basename(token)
                if name in DELETE_TOKENS:
                    return name
    return None


def _working_dir_of(source: UnifiedEvent | None) -> str | None:
    """从源事件取工作目录；取不到就 None（= 说不清在不在范围内）。"""
    if source is None:
        return None
    value = getattr(source, "working_directory", None)
    text = str(value or "").strip()
    return text or None


def evaluate_delete_segments(command: str, *, agent: str = "", session: str = "",
                             ts: str = "",
                             source: UnifiedEvent | None = None) -> list[RuleHit]:
    """**逐删除段**判删除类。纯函数，不碰 I/O。

    2026-11-26 新增。``evaluate_delete`` 一条命令只产一条命中，后果不只是
    "少播几条"——审查（B11）也只有一个项，模型对整批点头就把每一段一起压掉，
    包括里面那个 ``config.yaml``。逐段判才能让模型对每段各自表态。

    **档位的语义没变**：``evaluate_delete``（整条）的档位是各段取最坏，
    而这里每段各自的档位。两条路都要留在——闸门与卡片看整条（"这条命令
    整体有多危险"），播报与审查看分段（"每一段分别值不值得说"）。
    调用方取 ``max`` 就回到整条的答案，不会有两套档位。

    段文由 ``commands.delete_segment_texts`` 给——那走的是同一个
    ``_segments`` 切分，不是第二套判据。

    **定档用展开后的段文，展示用原文**（2026-11-26 真机）：shell 的惯用
    形状是"先赋值、再用"，而赋值往往在**另一段**——

        MSG=/tmp/x && git commit -F "$MSG" && rm -f "$MSG"

    逐段判之后，``rm -f "$MSG"`` 那段看不见前一段的 ``MSG=…``，
    于是仍然判"目标被变量藏住了"→ EXTREME → 【严重】。
    所以这里先把**整条命令**的赋值收集起来，展开段文再定档。
    展示仍用原文：用户在卡片上要确认的是自己敲的那一行，
    不是我们替它展开后的那一行。
    """
    working_dir = _working_dir_of(source)
    assignments = _literal_assignments(command)
    out: list[RuleHit] = []
    for index, segment in enumerate(delete_segment_texts(command)):
        matched = match_delete(segment)
        if matched is None:
            continue
        tier = delete_tier(_expand_literals(segment, assignments), working_dir)
        summary = f"删除文件：{_clip(segment)}"
        if tier == TIER_SCOPE_UNKNOWN:
            # B8：范围未知时必须**明说**，不许拿【严重】冒充一个我们并不掌握的
            # 事实。与 evaluate_delete 同一句文案。
            summary += "；这条记录没带工作目录，说不清在不在工作区外"
        out.append(RuleHit(
            rule=RULE_DELETE,
            severity=severity_for_tier(tier),
            summary=summary,
            matched=matched,
            agent=agent,
            session=session,
            ts=ts,
            # 合并键带**段序号**：同一条命令的各段若共用一个键，
            # 合并器会把后几段当重复吃掉——那正是要修的东西。
            coalesce_key=(f"{RULE_DELETE}|{session}|{agent}|{matched}"
                          f"|{index}|{_fingerprint(segment)}"),
            command_length=command_length(command),
            command=command,
            **_attribution_of(source),
        ))
    return out


def merge_multi_delete(hits: list[RuleHit]) -> list[RuleHit]:
    """同一条命令的多个删除，合成一条播报——**``critical`` 除外**。

    2026-11-26 用户要求按"合成一条"走（``rm -rf a; rm -rf b; rm -rf c``
    播一条「删了 3 个路径」，而不是三条不相干的事），同时要求：
    **真的有危险的、或模型判"不遵从 prompt"的，必须独立报告。**

    所以这里只做一半，另一半在调用方：

    - ``critical`` **一律不合成**，原样返回。它是"强制常亮、永不递减"的
      那一档，合成它等于把红字降级成一句话。
    - 合成只发生在 warn/notice 之间。合出来的那条取**最坏的档位**——
      三个里有一个是 warn，合成的就是 warn，不会因为另外两个是 notice 而降。
    - 调用方只在**没有审查**时调本函数。有审查时每条各自送审：模型对哪条
      不点头，哪条自己播；先合成再送审会把那个"不"稀释掉。

    这不是"少说几条"，是把"一条命令删了 N 个路径"说完整。
    """
    if not hits:
        return []
    groups: list[list[RuleHit]] = [[]]
    for hit in hits:
        if hit.rule != RULE_DELETE:
            groups.append([hit])
            continue
        if hit.severity == "critical" or not groups[-1] or \
                groups[-1][-1].command != hit.command:
            groups.append([hit])
            continue
        groups[-1].append(hit)

    out: list[RuleHit] = []
    for group in groups:
        if not group:
            # 起始占位的空组（第一个 hit 是非删除类时留下的）。别让它
            # 走到下面的 group[0]——那是 IndexError。
            continue
        if len(group) == 1:
            out.append(group[0])
            continue
        first = group[0]
        targets = ", ".join(
            _clip(hit.summary.split("：", 1)[-1], 40) for hit in group)
        # 合成的取最坏的档位：三个里有一个是 warn，合成的就是 warn，
        # 不会因为另外两个是 notice 而降。
        # **刻意不引 ``broadcast.SEVERITY_PRIORITY``**：broadcast 已经 import
        # 本模块，反向 import 会成环；而这里只可能在 warn/notice 之间取
        # （critical 在上面就被排除），四档阶梯写明白比引一张表更难漂。
        if any(hit.severity == "warn" for hit in group):
            worst = "warn"
        elif any(hit.severity == "critical" for hit in group):
            worst = "critical"
        else:
            worst = group[0].severity
        out.append(first.model_copy(update={
            "severity": worst,
            "summary": f"删除文件：一条命令里删了 {len(group)} 处（{targets}）",
        }))
    return out


def delete_tier(command: str, working_directory: str | None = None) -> str:
    """一条删除命令的目标档位。闸门与词典共用这一个函数（B2）。

    **删除动作但一个目标都没解析出来时抬到极高危**：`git clean -fdx`
    （`clean` 是子命令不是路径）、目标被变量藏起来的 `rm`……
    B2 表把"无法解析的目标"归进极高危——说不清要删什么的时候，按最坏的猜。
    （`rm -rf a b` 不在此列：`_looks_like_path` 默认算路径，两个都解析得出来。）

    抬档规则在 ``commands.delete_tier_for_command`` 里（B3 搬过去的），
    播报、闸门、卡片排序共用同一处：放在任一侧都会让另一侧漂移。
    """
    return delete_tier_for_command(command, working_directory)


def evaluate_delete(command: str, *, agent: str = "", session: str = "",
                    ts: str = "", source: UnifiedEvent | None = None) -> RuleHit | None:
    """对一个 Bash 命令判删除类。纯函数，不碰 I/O。

    ``source`` 是产生这个命令的统一事件，只用来带归因字段；不传就是「未知」。

    严重度按**目标档位**定（B2），不再一律 critical：删 `node_modules` 与删 `~`
    不是一回事，老口径把两者都顶成【严重】，结果是用户见到红字就点确认。
    播报层跟着同一个档位走——播报说【严重】而卡片不红，那正是 A 批要消灭的
    "看到的与实际做的不一致"。
    """
    matched = match_delete(command)
    if matched is None:
        return None
    tier = delete_tier(command, _working_dir_of(source))
    summary = f"删除文件：{_clip(command)}"
    if tier == TIER_SCOPE_UNKNOWN:
        # B8：范围未知时必须**明说**，不许拿【严重】冒充一个我们并不掌握的
        # 事实（"它在工作区外"）。用户看到的是【警告】+ 这句解释，
        # 而不是一条每次都对的红字——对得多了，红字就不值钱了。
        summary += "；这条记录没带工作目录，说不清在不在工作区外"
    return RuleHit(
        rule=RULE_DELETE,
        severity=severity_for_tier(tier),
        # 文案模版用「删除文件」而不是「删除类操作」——后者是内部术语，
        # 用户在 3 秒内要读懂的是"发生了什么"，不是我们的规则分类。
        summary=summary,
        matched=matched,
        agent=agent,
        session=session,
        ts=ts,
        # 合并粒度按规则区分：删除类每次都是独立的危险动作，只合并"同一条命令重复出现"，
        # 不能按会话合并——那会让用户在一分钟内漏掉好几次不同的删除。
        # 实测依据：真实转录里一分钟内有 4 条不同 rm，按会话合并只会播 1 条。
        # 归因**不进**合并键（决策记录 §3）：合并按动作算，归因是显示层的事。
        coalesce_key=f"{RULE_DELETE}|{session}|{agent}|{matched}|{_fingerprint(command)}",
        command_length=command_length(command),
        command=command,
        **_attribution_of(source),
    )


# ── 失败类 ────────────────────────────────────────────────────────────────

def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def severity_for_status(status_code: Any) -> str | None:
    """把 HTTP 状态码映射成严重度；2xx/3xx 返回 None（不算失败）。

    **502 / 503 / 504 是 notice 不是 critical**（2026-11-26 真机，第五条误报）。
    实测今晚连报 4 条【严重】，全是网关侧 502：

        【严重】请求失败（状态 502）：模型 step-5-preview；状态 50…

    老口径一句 ``status >= 500 → critical``，理由写的是"服务端在崩，全线不可用"。
    但那三个码说的不是这件事：

        502 Bad Gateway          上游/代理临时接不上
        503 Service Unavailable  明确声明"稍后再来"，常带 Retry-After
        504 Gateway Timeout      上游超时

    它们全是**瞬态**，和 429 同一类——Agent 自己会重试，多数下一轮就好。
    而 500（服务端真的出了错）、501（端点不支持）不是瞬态，照旧 critical。

    这不是"把 5xx 看轻"，是**不再用【严重】冒充一个我们不掌握的事实**：
    我们看到的是一次 502，不是"全线不可用"。红字看多了就等于没有，
    而这里被训练成的后果是用户对真正的 500 也顺手点掉。

    信息一点没少：notice 照样播、照样带状态码，只是不再顶红字。
    """
    status = _to_int(status_code)
    if status is None:
        return SEVERITY_WARN  # 无状态码但已知失败
    if status in TRANSIENT_STATUS:
        return SEVERITY_NOTICE
    if status >= 500:
        return SEVERITY_CRITICAL
    if status == 429:
        return SEVERITY_NOTICE
    if status >= 400:
        return SEVERITY_WARN
    return None


def evaluate_failure(*, status_code: Any = None, error_message: str = "",
                     agent: str = "", session: str = "", ts: str = "",
                     source: UnifiedEvent | None = None) -> RuleHit | None:
    """对一个网关请求判失败类。纯函数。

    ``status_code`` 为 None 但给了 ``error_message`` 也算失败（网络层错误没有状态码）。
    ``source`` 只用来带归因字段，不传就是「未知」。
    """
    severity = severity_for_status(status_code)
    if severity is None:
        return None  # 2xx/3xx 不是失败；有 message 但状态正常也不算失败
    message = str(error_message or "").strip()
    status = _to_int(status_code)
    matched = str(status) if status is not None else "no-status"
    summary = "请求失败"
    if status is not None:
        summary += f"（状态 {status}）"
    if message:
        summary += f"：{_clip(message, 60)}"
    return RuleHit(
        rule=RULE_FAILURE,
        severity=severity,
        summary=summary,
        matched=matched,
        agent=agent,
        session=session,
        ts=ts,
        # 失败类按 会话+状态码 合并：网关每个请求都记一行，同一会话连续 503
        # 不合并就会刷屏。这与删除类刻意不同。
        coalesce_key=f"{RULE_FAILURE}|{session}|{agent}|{matched}",
        **_attribution_of(source),
    )


def evaluate_delegation(event: UnifiedEvent) -> RuleHit | None:
    """MCP 下派观测：本会话把活交给了外部 worker。纯函数。

    **只认转录里的工具名**（``mcp__<server>__<tool>``）。不读 args、不猜它要干什么、
    不判危险——猜动作是归因错的高发区（决策记录 §7 第 4 步的取舍）。
    hint headers 在自定义 base URL 下默认关闭，所以网关元数据这条路走不通，
    只有转录工具名可用。

    notice 级：这是"你的 Agent 背后调了谁"的可见性，不是事故。
    """
    server = delegated_server(event.tool)
    if event.event != "tool_use" or not server:
        return None
    return RuleHit(
        rule=RULE_DELEGATION,
        severity=SEVERITY_NOTICE,
        summary=f"通过 {server} 下派了一次任务",
        matched=server,
        agent=event.agent,
        session=event.session,
        ts=event.ts,
        # 合并键按 会话+server：同一会话反复调同一个 worker 在窗口内只报一次，
        # 否则一次批量任务能刷出几十条。换 server 才算新事件。
        coalesce_key=f"{RULE_DELEGATION}|{event.session}|{event.agent}|{server}",
        **_attribution_of(event),
    )


def evaluate_transcript_error(event: UnifiedEvent) -> RuleHit | None:
    """转录里的 error 事件 → 失败类。纯函数。"""
    if event.event != "error":
        return None
    return RuleHit(
        rule=RULE_FAILURE,
        severity=SEVERITY_WARN,
        summary=f"转录报错：{_clip(event.summary, 80)}",
        matched="transcript-error",
        agent=event.agent,
        session=event.session,
        ts=event.ts,
        coalesce_key=f"{RULE_FAILURE}|{event.session}|{event.agent}|transcript-error",
        # 事件本身就是 source：归因字段直接从它带过来
        **_attribution_of(event),
    )


def _fingerprint(text: str) -> str:
    """命令的短指纹，用于"同一条命令重复出现才合并"。"""
    import hashlib

    return hashlib.sha1(" ".join(str(text).split()).encode("utf-8")).hexdigest()[:12]


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── 合并 ──────────────────────────────────────────────────────────────────

class Coalescer:
    """同一 coalesce_key 在窗口内只放行第一条，避免刷屏。"""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = float(window_seconds)
        self._last: dict[str, float] = {}

    def offer(self, hit: RuleHit, now: float | None = None) -> RuleHit | None:
        """窗口内重复返回 None（被合并），否则原样返回并记账。"""
        moment = time.monotonic() if now is None else float(now)
        previous = self._last.get(hit.coalesce_key)
        if previous is not None and moment - previous < self.window_seconds:
            return None
        self._last[hit.coalesce_key] = moment
        return hit

    def evaluate_event(self, event: UnifiedEvent, now: float | None = None) -> list[RuleHit]:
        """对一个统一事件跑当前阶段所有规则，返回未被合并的命中。"""
        hits: list[RuleHit] = []
        if event.event == "error":
            candidate = evaluate_transcript_error(event)
            if candidate is not None:
                kept = self.offer(candidate, now)
                if kept is not None:
                    hits.append(kept)
        return hits
