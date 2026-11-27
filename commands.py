"""命令翻译词典：把一条命令说成人话。**本地、零 key、不调模型。**

存在的理由（2026-11-22）：主卡正文不能贴命令原文。用户看到
``rm -rf D:\\workspace\\build`` 要自己解析"这是删除、不可恢复、影响整个目录"，
而快速扫读时很容易漏掉 ``-rf`` 这两个字符——那正是事故的来源。

三条硬规则：

1. **不许瞎猜。** 未命中的命令原样显示，并明说"未识别的命令"。
   编一个听起来合理的解释，比不解释危险得多。
2. **词典只描述事实，不做判断。** 每个条目给三样东西：它要做什么、
   最坏结果、影响范围。要不要放行是用户的事，词典不给结论。
3. **人话动词，不是命令 token。** 除路径外不许出现英文命令片段
   （``-rf``、``--force`` 这种），那是给机器看的。

词条按"目标 + 效果"组织，不按动词罗列：危险面横跨 cmd / PowerShell /
SQL / git 好几套语法，按动词列必定漏（规格 §16.1「覆盖 §5.2」的同一结论）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# 结论只准这三种写法（规格 P2）。**没有任何第四种**——
# "倾向"、"别放行"、"不建议"这类词一律禁止：快速扫读时会先看到"放行"，
# 把"倾向别放行"读成"放行"，那是会出事故的误读。
CONCLUSION_ALLOW = "建议允许"
CONCLUSION_DENY = "建议拒绝"
CONCLUSION_UNSURE = "无法判断，需要你决定"
CONCLUSIONS = (CONCLUSION_ALLOW, CONCLUSION_DENY, CONCLUSION_UNSURE)

# 模型的建议取值 → 结论行的三种写法。**只有这一处做这个翻译**：
# 模型返回的是 allow / deny / unsure 这种 token，直接印在卡片上就是
# "建议：allow"（2026-11-22 同族第二例，已修）。认不出的取值落最保守的
# 第三态——宁可说"需要你决定"，也不把一个来路不明的 token 印给用户。
_SUGGESTION_TO_CONCLUSION = {
    "allow": CONCLUSION_ALLOW,
    "deny": CONCLUSION_DENY,
    "unsure": CONCLUSION_UNSURE,
}


def conclusion_for(suggestion: str) -> str:
    return _SUGGESTION_TO_CONCLUSION.get(suggestion, CONCLUSION_UNSURE)

# 主卡禁用词（规格 P2）。出现在给用户看的文字里就算违规——
# 测试对整张卡的 HTML 逐个 grep。
BANNED_PHRASES = (
    "倾向", "别放行", "不建议", "最好不要", "可以但", "拒绝或允许都可以",
    "放行", "同意", "批准",
)
# 内部词：泄露到界面用户不知道那是什么（C5 的同类）。
BANNED_INTERNAL = (
    "critical", "delete", "L0", "L1", "L2", "hook", "PermissionRequest",
    "ai_judgement", "auto_decide", "api_key", "base_url", "irreversible",
    "recoverable", "notice", "warn",
)

# 后果行必须出现这几个词之一（规格 P3）：说不出最坏结果就等于没说。
RECOVERY_WORDS = ("不可恢复", "找不回来", "永久")

# 主卡上一行最多显示多少字命令内容。**超过就必须明说**（2026-11-22 A 批）：
# 风险判定读的是完整命令，卡片只显示这么长——一条 150 字的命令在末尾
# 挂 `&& rm -rf ~`，判定看得见、人看不见，那就是在请用户盲签。
# 所以超过这个上限时卡片上要多一行「尾部未显示」，全文进「这是什么」。
COMMAND_DISPLAY_MAX = 80

UNKNOWN_ACTION = "未识别的命令"
UNKNOWN_CONSEQUENCE = "看不出这一步最坏会怎样"

# ── 目标档位（B2：按删除目标分档）───────────────────────────────────────
# 老口径只有一条："命令里出现删除 token"就判 critical，其余一律 notice。
# 后果是删 `node_modules` 和删 `~` 顶同一根红条——用户见到红条就点确认，
# 闸门就是这么贬值的。改成**先解析目标路径集合，再按目标所在位置分档**。
#
# 目标档位（B2 表 + B8 的「范围未知」档）：
TIER_RECOVERABLE = "recoverable"   # 删了重建得回来：依赖 / 构建产物 / 缓存
TIER_NORMAL = "normal"             # 工作区内的其他路径
TIER_EXTREME = "extreme"           # 工作区外 / 家目录 / 盘根 / 系统目录 / 目标不可解析
TIER_UNKNOWN = "unknown"           # 解析不出目标、效果说不清（删除类会再抬到极高危）
# B8 新增：**目标解析得出来，但拿不到工作目录**——知道要删什么，
# 不知道它在不在工作区外。这与"解析不出目标"是两件事，不能混成一档：
# 后者是真不知道要删什么（按最坏的猜 → 极高危），前者是知道删什么、
# 只是说不清位置（→ warn，文案明说）。老实现把两者都按最坏的猜，
# 于是转录里每条缺 cwd 的删除都顶【严重】。
TIER_SCOPE_UNKNOWN = "scope_unknown"
TIERS = (TIER_RECOVERABLE, TIER_NORMAL, TIER_EXTREME, TIER_UNKNOWN,
         TIER_SCOPE_UNKNOWN)
# 从坏到好：多目标取最坏的一档时按这个顺序找。
_WORST_FIRST = (TIER_EXTREME, TIER_SCOPE_UNKNOWN, TIER_NORMAL, TIER_RECOVERABLE)



@dataclass(frozen=True)
class CommandFacts:
    """一条命令的人话描述。"""

    action: str                     # 它要做什么
    consequence: str                # 最坏结果
    targets: tuple[str, ...] = ()   # 从命令里抽出来的路径/目标
    known: bool = False             # 词典认不认这条命令
    irreversible: bool = False      # 词典认定它不可恢复（事实，不是结论）
    scope: str = ""                 # 影响面；空 = 由 scope_line 从目标推


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    action: str
    consequence: str
    irreversible: bool = True
    # 这一条的"范围"行。词典只描述事实，所以这里写影响面，不写结论。
    scope: str = "影响范围不止一个文件"


# 词条。顺序即优先级：先匹配到的赢。
_RULES: tuple[_Rule, ...] = (
    _Rule(
        name="git_reset_hard",
        pattern=re.compile(r"\bgit\s+reset\s+--hard\b"),
        action="丢弃所有未提交的改动",
        consequence="没提交的工作直接没了，找不回来",
        scope="整个工作区里还没提交的改动",
    ),
    _Rule(
        name="git_clean",
        pattern=re.compile(
            r"\bgit\s+clean\s+-[a-zA-Z]*d[a-zA-Z]*x?\b"
            r"|\bgit\s+clean\s+-[a-zA-Z]*x[a-zA-Z]*d?\b"),
        action="删除所有没被 Git 记录的文件",
        consequence="包含通常被忽略的那些，不可恢复",
        scope="这个仓库里所有没被记录的文件",
    ),
    _Rule(
        name="git_push_force",
        pattern=re.compile(
            r"\bgit\s+push\s+(?:--[a-z-]+\s+)*--force\b"
            r"|\bgit\s+push\s+(?:--[a-z-]+\s+)*-f\b"),
        action="用本地历史覆盖远端历史",
        consequence="别人推上去的提交可能被抹掉",
        scope="远端仓库的历史",
        irreversible=False,
    ),
    _Rule(
        name="curl_pipe_shell",
        pattern=re.compile(
            r"\b(?:curl|wget|iwr|invoke-webrequest)\b.*\|\s*(?:sudo\s+)?(?:ba|z|fi)?sh\b",
            re.IGNORECASE),
        action="从网上下载一段脚本并直接执行",
        consequence="下载的内容会原样跑起来，出了事找不回来",
        scope="下载到的脚本能碰到的所有东西",
    ),
    _Rule(
        name="scheduled_task",
        pattern=re.compile(r"\b(?:schtasks|sc\s+create|new-service)\b", re.IGNORECASE),
        action="装一个开机自动运行的任务或服务",
        consequence="以后每次开机它都会自己跑，你未必想得起来",
        scope="以后每次开机都会自己跑",
        irreversible=False,
    ),
    _Rule(
        name="drop_table",
        pattern=re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
        action="删掉整张表",
        consequence="表结构和里面的数据一起没了，找不回来",
        scope="整张表的结构和里面的数据",
    ),
    _Rule(
        name="permissions",
        pattern=re.compile(r"\b(?:chmod|icacls|set-acl|takeown)\b", re.IGNORECASE),
        action="修改文件或目录的权限",
        consequence="改错了可能连你自己都打不开",
        scope="被指定文件或目录的访问权限",
        irreversible=False,
    ),
)

# rm 单独处理：它的三个变体（删文件 / 删目录递归 / 清空磁盘）后果差别极大，
# 用一条松散正则区分不了——`-f` 里也有 f，会被误判成递归（实测踩过），
# 所以这里显式解析开关字母。
_FLAG_RE = re.compile(r"-([a-zA-Z]+)")
# 只认**单短线**开关：`-rf`、`-r`。`--force` 不算单短线 token，
# 但它**是** rm 的开关（= `-f`）——老实现把长选项整条丢掉，
# 于是 `rm --recursive x` 被描述成"删除文件"而不是"删除目录及其全部内容"，
# 用户以为只删一个文件，实际连子目录一起没。
# 所以 B3 的做法是：长选项先**查表映射**成短字母，查不到的一律不贡献字母
# （绝不能把 `--force` 拆成 f/o/r/c/e——那会把"强制"误读成"递归"）。
_SHORT_FLAG_RE = re.compile(r"^-[A-Za-z]+$")
_LONG_FLAG_RE = re.compile(r"^--([a-z][a-z0-9-]*)$")
# 已知的 rm 长选项 → 等价短字母。只收 GNU rm 真实存在的，
# 且**只收改变行为的那几个**：`--verbose` 之类的显示开关不影响后果。
_RM_LONG_FLAGS = {
    "recursive": "r",
    "force": "f",
    "interactive": "i",
}
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:\\?$")


@dataclass(frozen=True)
class _RmFacts:
    recursive: bool
    target: str
    is_disk_root: bool


def _rm_flag_letters(body: list[tuple[str, str]]) -> str:
    """从一段 rm 命令里取开关字母。

    单短线 token 按字母展开；长选项查 ``_RM_LONG_FLAGS`` 表，
    查不到的（`--no-preserve-root`、`--one-file-system`…）不贡献字母——
    它们要么不影响后果，要么由盘根/系统目录那条判据单独兜住。
    """
    letters = ""
    for _, norm in body[1:]:
        short = _SHORT_FLAG_RE.match(norm)
        if short:
            letters += _FLAG_RE.findall(norm)[0].lower()
            continue
        long = _LONG_FLAG_RE.match(norm)
        if long:
            letters += _RM_LONG_FLAGS.get(long.group(1), "")
    return letters


def _parse_rm(command: str) -> _RmFacts | None:
    """找 `rm` **作为命令**的那一段。

    必须在命令位置上：`git commit -m "rm -rf x"` 里的 rm 是字符串，
    不是要执行的东西。老实现用 `\\brm\\s+` 全文搜，于是那句话被判成
    "删除目录及其全部内容"——闸门犯过同一类错（帮助豁免只看段首），
    两套判据各自错得一样，正是 B2 要消灭的"两套判据"。
    现在和闸门一样：先归一化，再看命令名。

    开关字母只从单短线 token 与已知长选项取（见 ``_rm_flag_letters``）。
    """
    for segment in _segments(command):
        body = _normalise_pairs(segment)
        if not body or basename(body[0][1]) != "rm":
            continue
        letters = _rm_flag_letters(body)
        targets = _segment_targets(body)
        target = targets[0] if targets else ""
        bare = target.replace("/", "\\").rstrip("\\")
        is_disk_root = bool(_DRIVE_ROOT_RE.match(bare)) or bare in ("", "\\")
        return _RmFacts(recursive="r" in letters, target=target,
                        is_disk_root=is_disk_root)
    return None


def _rm_facts(command: str) -> CommandFacts | None:
    parsed = _parse_rm(command)
    if parsed is None:
        return None
    target = _clean_target(parsed.target)
    targets = (target,) if target else ()
    if parsed.is_disk_root:
        return CommandFacts(action="清空整个磁盘",
                            consequence="盘上所有文件一起没了，不可恢复",
                            targets=(), known=True, irreversible=True,
                            scope="整个磁盘上的所有文件")
    if parsed.recursive:
        return CommandFacts(
            action="删除目录及其全部内容",
            consequence="连子目录和里面的文件一起删，不可恢复",
            targets=targets, known=True, irreversible=True,
            scope="这个目录里的所有内容，含子目录")
    return CommandFacts(
        action="删除文件",
        consequence="不进回收站，删了就找不回来",
        targets=targets, known=True, irreversible=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_target(raw: str) -> str:
    """把命令里抠出来的目标收拾成能看的一小段。"""
    text = " ".join(str(raw or "").split()).strip().strip("'\"").strip()
    if not text:
        return ""
    head = text.split()[0]
    # 上限与整条命令的展示上限取同一个数：卡片上从命令里搬出来的字
    # 不许比"直接显示命令"还多，否则"只显示前 N 字"就是句假话。
    return _clip(head, COMMAND_DISPLAY_MAX)


# 只是打印/回显，不是真执行。规格 §5.2 明确把 `echo "rm -rf C:\"` 列为
# **不**进闸门：按动作词匹配会把一句字符串当成真删除，那是误报，
# 而误报会训练用户无脑点确认，最后闸门失效。
_PRINT_ONLY_RE = re.compile(r"^\s*(?:echo|printf|write-output|write-host)\b",
                            re.IGNORECASE)


def translate(command: str) -> CommandFacts:
    """把一条命令翻译成事实。认不出来就明说，不猜。

    **单条命令用这个**。一条命令里有多个动作时（`rm a && git reset --hard`）
    用 ``translate_segments``——那个才会把每一段都翻译出来。
    """
    text = " ".join(str(command or "").split())
    if not text:
        return CommandFacts(action=UNKNOWN_ACTION,
                            consequence=UNKNOWN_CONSEQUENCE, known=False)
    if _PRINT_ONLY_RE.match(text):
        return CommandFacts(
            action=f"{UNKNOWN_ACTION}：{_clip(text, COMMAND_DISPLAY_MAX)}",
            consequence="这一步只是打印一句话，不会真的动文件",
            known=False)
    rm = _rm_facts(text)
    if rm is not None:
        return rm
    for rule in _RULES:
        if rule.pattern.search(text):
            return CommandFacts(action=rule.action,
                                consequence=rule.consequence,
                                targets=(), known=True,
                                irreversible=rule.irreversible,
                                scope=rule.scope)
    return CommandFacts(action=f"{UNKNOWN_ACTION}：{_clip(text, COMMAND_DISPLAY_MAX)}",
                        consequence=UNKNOWN_CONSEQUENCE, known=False)


def _split_actions(text: str) -> list[str]:
    """按"一个动作"切原文。引号内的分隔符不算分隔。

    **与闸门的 ``_SIMPLE_SPLIT`` 刻意不同**：那边按 `;` `|` `&` 换行全切，
    因为 `rm a | xargs rm` 两段都要盯，多切只会多几分谨慎。
    翻译层不能这么切——`curl -s x.sh | sh` 是**一个**动作
    （下载并执行），按 `|` 切开会让词典里那条最危险的规则两边都匹配不上，
    于是它反而描述不出来。所以这里 `|` 保留，只切 `;` `&&` `||` 换行。

    切的是**原文字符串**，不是分词后重新拼的：分词会把 `<` `>` `(` `)`
    当分隔符吃掉，拼回来就把 `</p><script>` 变成了 `/p scr`——
    那不是转义，那是把证据毁了（实测踩过：转义测试因此永远验不到东西）。
    """
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            index += 1
            continue
        if char in ";\n":
            parts.append("".join(current))
            current = []
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            parts.append("".join(current))
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def action_units(command: str) -> list[str]:
    """一条命令里的**动作单元**，按出现顺序。空串命令返回空表。"""
    text = str(command or "")
    return _split_actions(text) if text.strip() else []


# 主卡上最多列几条动作（B3）。取 2 而不是 3：B7 定下的主卡上限是
# "动作 + 至多两行辅助 + 三个按钮"，动作占两行还在预算内，三行就超了。
# **超出的不丢**——完整清单进「这是什么」，见 ``all_action_phrases``。
MAX_CARD_ACTIONS = 2

# 分档排序用：越危险越靠前。档位名不出现在任何界面上，只用于排序。
_TIER_RANK = {
    TIER_EXTREME: 0,
    TIER_SCOPE_UNKNOWN: 1,
    TIER_NORMAL: 2,
    TIER_UNKNOWN: 3,
    TIER_RECOVERABLE: 4,
}


def _unit_has_command(text: str) -> bool:
    """这个动作单元里，除了赋值与包装词，还剩得下一个命令主体吗。

    **与 ``command_body_unknown`` 回答的不是同一个问题**，所以不能复用它：
    那个是闸门的 fail-safe——"这条命令我们看得懂吗"，按闸门的切法
    （`|` 也切）判，于是 `curl -s x.sh | sh` 会被判成"看不懂"→ 抬到 warn，
    这是对的（谨慎）。
    这个只回答"有没有东西可描述"——`VAR=x` 单独一段什么也不做，
    渲染成一行"未识别的命令：VAR=x"纯属噪音，还会把真正危险的那条
    挤出主卡（主卡只列两条）。同样按 `|` 切就会把 `curl … | sh`
    整条丢掉，那可就丢错了。
    """
    pairs = [(raw, normalise_token(raw)) for raw in _raw_tokens(text)]
    return bool(_normalise_pairs(pairs))


def translate_segments(command: str, *,
                       working_directory: str | None = None) -> list[CommandFacts]:
    """把**每一段**分别翻译，最危险的排在前面（B3）。

    老实现只翻译第一个命中：`rm a && git reset --hard` 的卡片上只有 `rm`，
    第二个动作用户根本看不见，却和他点的"允许"绑在一起。
    这不是排版问题——**没被描述的动作等于被默认批准**。

    排序判据 `(不可恢复, 目标档位, 原顺序)`：先看后果找不找得回来，
    再看目标危不危险，最后按命令里的先后。档位取该段自己的目标算，
    与闸门、播报共用 ``tier_for_command``——禁止第二套判据。

    返回的 facts 不带段文本；调用方只要"最危险的那几条"时用 ``card_actions``。
    """
    pairs: list[tuple[int, str, CommandFacts]] = []
    for index, text in enumerate(action_units(command)):
        if not _unit_has_command(text):
            # 空段（`VAR=x && ` 末尾）与**纯赋值段**（`VAR=x &&`）都不占位：
            # 后者什么也不做，写一张"未识别的命令：VAR=x"上去只是噪音。
            continue
        pairs.append((index, text, translate(text)))

    def sort_key(item: tuple[int, str, CommandFacts]) -> tuple[int, int, int]:
        index, text, fact = item
        tier = delete_tier_for_command(text, working_directory)
        return (0 if fact.irreversible else 1,
                _TIER_RANK.get(tier, _TIER_RANK[TIER_UNKNOWN]),
                index)

    pairs.sort(key=sort_key)
    return [fact for _, _, fact in pairs]


def action_phrase(fact: CommandFacts) -> str:
    """一条事实的动作短语（含目标，如「删除目录及其全部内容 a」）。"""
    action = fact.action
    if fact.targets:
        target = fact.targets[0]
        action = f"{action} {target}" if target not in action else action
    return f"⚠️ 它要{action}" if fact.known else f"⚠️ {action}"


def card_actions(command: str, *, working_directory: str | None = None,
                 limit: int = MAX_CARD_ACTIONS) -> list[str]:
    """主卡上要列的动作短语：最危险的最多 ``limit`` 条（B3）。"""
    return [action_phrase(fact)
            for fact in translate_segments(command,
                                           working_directory=working_directory)[:limit]]


def all_action_phrases(command: str,
                       working_directory: str | None = None) -> list[str]:
    """**全部**动作短语，一条不省。「这是什么」用——主卡装不下的得有地方看。"""
    return [action_phrase(fact)
            for fact in translate_segments(command,
                                           working_directory=working_directory)]


# ── 删除类判定（B3 从 rules.py 搬过来）────────────────────────────────────
# 搬家的理由：所有"命令词表"本来就在这个模块（打印类 / 导航类 / 包装类），
# 只有删除词表留在 rules.py。结果是"什么算删除"这件事有两个家，
# 而 B3 的排序需要它——排序用不到就得自己抄一份，那就是 B2 明令禁止的
# "两套判据"。所以搬到 leaf 模块，rules.py 改为 import。
DELETE_TOKENS = frozenset({"rm", "rmdir", "del", "remove-item"})
DELETE_PAIRS = (("git", "clean"), ("drop", "table"))


def is_delete_command(command: str) -> bool:
    """这条命令里有删除类动作吗（按段看，任一段命中即算）。

    与 ``rules.match_delete`` 的区别：那个还要管帮助豁免、打印类排除，
    返回命中的词；这个只回答"是不是删除"，给档位抬档与排序用。

    **与 ``_segment_has_delete`` 就是同一个函数**（``delete_targets``
    调的那个）。2026-11-26 之前这里按 ``simple_commands``（已剥噪音）看、
    那边按 ``_segments``（未剥）看，同一句"什么叫删除"开了两个入口。
    """
    return any(_segment_has_delete(segment) for segment in _segments(command))


def _hides_by_substitution(target: str) -> bool:
    """目标被 ``$VAR`` / 反引号 / ``%VAR%`` 藏住——**我们不知道路径**。

    与"被无界通配符藏住"（``*`` / ``../*``）是两件事，必须分开：

    - 替换：路径要从别处取，我们不知道它是什么。这是**不知道**。
    - 无界通配符：我们明确知道它匹配一片没有边界的内容。
      这是**知道它无界**——``rm -rf *`` 会抹掉整个工作目录。

    只有前者够格让模型表态。把后者也放行，等于让模型给
    "抹掉整个目录"开一张免罪牌。
    """
    text = str(target or "").strip().strip("'\"")
    return bool(_SUBSTITUTION_RE.search(text))


def opacity_is_the_only_reason(command: str,
                               working_directory: str | None) -> bool:
    """这条命令的"严重"**只**来自"目标说不清"吗。

    用来决定一件事：B11 能不能送它。``critical`` 按成文规矩永不送审
    （「极高危档强制常亮，永不递减」），而那条规矩的理由原话是
    「连点 3 次删 node_modules 之后，第 4 次的 ``rm -rf ~`` 仍要整行红字」
    ——它防的是**真的灾难**被模型放掉。

    但 ``rm -rf "$MSG"`` 不是灾难，是**我们不知道**。B8 原话：
    「不许拿【严重】冒充一个我们并不掌握的事实」。把这两类一起锁死的
    后果：模型再认同前者是常规操作也压不掉——2026-11-26 真机一晚
    57 条 critical，约 52 条是这一类。用户原话：「这个根本不严重，
    它就只是正常的任务执行，这里却报了严重这个极其错误且荒谬的结论」，
    以及「发一个两个对他来说是警示，发几十个，那对他来说就是麻木…
    真正的严重警告需要有真实性，它不会被过多的严重警告所淹没」。

    **critical 的价值来自稀缺。** 一晚 57 条的"严重"等于没有严重，
    真出事时用户已经被训练得点掉了。

    两道判据都要过：
    1. 每个目标都必须是**被替换**藏住的。字面路径或无界通配符
       （``rm -rf *``）自己就可能是危险的，不许借这条放行；
    2. 拿掉"说不清"之后档位仍不是 extreme（双保险）。

    **刻意不改档位本身**：档位同时喂审批闸门（``approval.py`` 三处按
    EXTREME 特殊处理：不可恢复类、永不递减、强制警示）。改档位会让
    ``rm -rf "$(curl evil)"`` 连审批卡都不弹——那比要解决的问题多得多。
    这里只放开采播报的压制，闸门一格不让。
    """
    targets = delete_targets(command)
    if not targets:
        return False
    for target in targets:
        if _is_temp_path(target):
            continue
        if not _hides_by_substitution(target):
            return False
    try:
        tier = target_tier(targets, working_directory=working_directory,
                           unparseable=False)
    except Exception:                  # noqa: BLE001 —— 判不出一律按"不只是说不清"
        return False
    return tier != TIER_EXTREME


#: 删这些目录毁掉的是**不可恢复的状态**：版本控制目录里同时装着提交历史
#: 和**全部未提交的工作**。后者没有任何备份——远程只有你 push 上去的那些。
#:
#: 为什么值得单独立一套（``_RECOVERABLE_MARKERS`` 管的是反方向）：
#: 2026-11-27 实测，``rm -rf .git`` 的档位是 ``normal``、严重度 ``warn``、
#: 动作类别 ``recoverable``——于是它**过得了 L2 闸门**（``irreversible``
#: 才是那道墙），而 ``warn`` 又是**可送审**的，B11 的模型一句"常规"
#: 就能把通知也压掉。同一条命令写成 ``rm -rf ../.git`` 却是
#: ``extreme`` / ``critical`` / ``irreversible``，两边都拦。
#: **同一个动作两种待遇，而更常见的那种（在当前目录）恰好是哪边都拦不住的。**
#:
#: 只认**目录名恰好等于**，不搞子串匹配——``_RECOVERABLE_MARKERS`` 的注释
#: 写明了子串匹配会把 ``D:\workspace\templates\`` 一起误判，同一个错误
#: 换到安全方向上更贵。``.gitignore`` / ``.github`` / ``git-backup`` 都不算：
#: 删它们不毁历史。
_IRREVERSIBLE_STATE_DIRS = frozenset({".git", ".hg", ".svn"})


def destroys_irreversible_state(command: str) -> bool:
    """这条命令要删版本控制目录吗。纯函数。

    **按路径分量判，不按整串**：``.git``、``./.git``、``../.git``、
    ``foo/.git``、``.git/objects`` 都算——最后一个尤其算，删对象库一样
    毁掉整个仓库。而 ``.gitignore``、``.github``、``git`` 都不算。

    两个消费方，一份事实（避免两侧漂移）：

    1. ``approval._classify`` —— 把动作类别顶成 ``irreversible``，
       于是 ``auto_decide_gate`` 的第一道墙拦住它，**不会再被自动放行**；
    2. ``__init__._reviewable_broadcast`` —— 不许送 B11 审查，
       **不会再被模型一句话静音**。

    **刻意不动档位、不动严重度**：档位同时喂审批闸门与 B3 排序，
    改它会让 ``rm -rf .git`` 从 warn 变 critical——而这件事是两难的
    （刚 clone 完删它是常规，有三天未提交工作删它是灾难，规则层分不出），
    拿【严重】去描述一件我们分不出轻重的事，正是 B8 禁止的那件事。
    这里只关掉"没人看就让它过去"的那两条路，闸门与红字一格不让。
    """
    for target in delete_targets(command):
        parts = [part for part in _norm(target).replace("/", "\\").split("\\")
                 if part not in ("", ".")]
        if any(part in _IRREVERSIBLE_STATE_DIRS for part in parts):
            return True
    return False


def delete_tier_for_command(command: str, working_directory: str | None) -> str:
    """删除类的目标档位，**删不出目标时抬到极高危**。

    ``tier_for_command`` 对"一个目标都没解析出来"返回待定档——那对
    `git status` 这种非删除命令是对的（它本来就没有目标），
    但对删除命令是错的：`git clean -fdx`、目标被变量藏起来的 `rm`，
    说不清要删什么的时候必须按最坏的猜（B2 表）。

    抬档规则**只有这一处**。播报（``rules.evaluate_delete``）、
    闸门（``approval._classify``）、B3 的危险度排序都走它——
    放在任一侧都会让另一侧漂移。
    """
    tier = tier_for_command(command, working_directory)
    if tier == TIER_UNKNOWN and is_delete_command(command):
        return TIER_EXTREME
    return tier


_SYSTEM_DIRS = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib",
)


def _is_system_path(target: str) -> bool:
    # **必须用 ``_norm`` 归一化后再比**（2026-11-27 修）。
    #
    # 老实现直接 `target.lower().replace("/", "\\")`，于是
    # `/tmp/../../etc` 比不出来——它折起来是 `\etc`（系统目录），
    # 而原始串是 `\tmp\..\..\etc`，不以任何系统前缀开头。
    #
    # 这个洞一直都在，只是够不到：那种形状以前会被变量藏着
    # （`MSG=/tmp && rm -rf "$MSG/../../etc"` → 判"目标藏住了" → EXTREME），
    # 所以看不出问题。同命令赋值展开之后它就露出来了——**修一个洞
    # 会露出下一个洞，所以每个"更聪明"的改动都要往危险方向回归一遍**。
    lowered = _norm(target)
    return any(lowered.startswith(prefix.replace("/", "\\"))
               for prefix in _SYSTEM_DIRS)


#: **系统临时目录**：删了不可惜的地方。
#:
#: 2026-11-26 第七条误报。实测我自己删临时文件被顶【严重】：
#:
#:     【严重】删除文件：rm -f "C:/Users/<user>/AppData/Local/Temp/…"
#:
#: 档位判定本身没错——`AppData\Local\Temp` 确实在工作区外，于是
#: `_tier_of_one` 给它 EXTREME。但 **extreme 的存在理由是"破坏面没有边界"**
#: （家目录 / 盘根 / 系统目录），临时目录的破坏面明显有边界：就是临时文件。
#: 删一个临时文件和删 `D:/other/x` 不是一回事。
#:
#: ## 为什么不能用 `_RECOVERABLE_MARKERS`
#:
#: 那是**子串匹配**。把 `"temp"` 加进去，`D:\workspace\templates\`、
#: `D:\pkg\temporary\`、`src\TempStorage\` 全部会被误判成可恢复——
#: 那是降档，是项目明令禁止的最危险方向（见 `_RECOVERABLE_MARKERS` 的注释）。
#: 所以临时目录必须**单独一套精确前缀判断**，两个集合不许合并。
#:
#: ## 收录标准
#:
#: 只收**操作系统自己定义**的临时目录，不收"名字像临时"的目录：
#:   - ``%TEMP%`` / ``%TMP%``（环境变量写法， unambiguously 是临时目录）
#:   - ``C:\Users\<任意用户名>\AppData\Local\Temp``（每用户临时目录）
#:   - ``/tmp`` / ``/var/tmp`` / ``/private/tmp``（POSIX）
#:
#: ## 刻意不收 `C:\Windows\Temp`
#:
#: 它在 `_SYSTEM_DIRS` 的 `c:\windows` 前缀内，会先被 `_is_system_path`
#: 判成 EXTREME。那是刻意的保守：它确实在 Windows 目录底下，
#: 而"系统目录里的东西"按最坏的猜是这个项目的一贯取向。
#: 想清它得单独决定，不该搭这趟车。
#: Windows 形态的临时目录，匹配**归一化之后**的路径（反斜杠、小写）。
_TEMP_WIN_RE = re.compile(
    r"^(?:"
    r"%temp%(?:\\|$)"
    r"|%tmp%(?:\\|$)"
    r"|[a-z]:\\users\\[^\\]+\\appdata\\local\\temp(?:\\|$)"
    r")",
    re.IGNORECASE,
)
#: POSIX 形态。**同样匹配归一化之后的形式**（``/tmp/x`` → ``\tmp\x``）。
#: 不在原文上匹配：Windows 的 ``\tmp\x`` 是盘根相对路径，不是 POSIX 的 /tmp，
#: 两者混在一起就会随斜杠写法给出不同答案。
_TEMP_POSIX_RE = re.compile(r"^\\(?:tmp|var\\tmp|private\\tmp)(?:\\|$)", re.IGNORECASE)


def _is_temp_path(target: str) -> bool:
    """这是操作系统定义的临时目录吗。精确前缀匹配，不是子串。

    **先归一化再比**（``_norm``：统一反斜杠、小写、折叠 ``.`` 与 ``..``）：

    1. 不统一分隔符，``C:/Users/…/Temp/x`` 与 ``C:\\Users\\…\\Temp\\x``
       会得到不同答案——同一条命令两种 verdict，正是这个项目一直在消灭的病；
    2. **不折叠 ``..`` 会开一个新洞**：``/tmp/../etc`` 的字面前缀是 ``/tmp``，
       按前缀匹配会判成临时目录（notice），而它实际指向 ``/etc``。
       这和 0.6.4 修掉的 `../` 逃逸是同一个错误换了个位置。

    **带 ``..`` 的一律不认临时目录**：``%TEMP%/../notes`` 归一化后是 ``notes``，
    前缀那点信息全是假的。这种目标交给 ``_target_hides_scope``——它含 ``%VAR%``，
    会被判"藏住了作用域"，按最坏的猜。方向相反才是危险的。
    """
    raw = str(target or "").strip()
    if not raw:
        return False
    if any(part == ".." for part in re.split(r"[\\/]+", raw)):
        return False
    normalised = _norm(raw)
    if not normalised:
        return False
    return bool(_TEMP_WIN_RE.match(normalised) or _TEMP_POSIX_RE.match(normalised))


def _norm(path: str) -> str:
    """归一化成可比较的形式：统一分隔符、小写、折叠 ``.`` 与 ``..``。

    **折叠 ``..`` 是 2026-11-26 补的**：老实现只做小写 + 替换分隔符，
    于是 ``_under("../x", "D:\\\\proj")`` 拿 ``"../x"`` 去比前缀，永远不匹配。
    更糟的是 ``_tier_of_one`` 在调用它之前就用
    ``if not _is_absolute(target): return TIER_NORMAL`` 把相对路径整个放过了，
    所以 ``rm -rf ../../..`` 一路被判成 normal/warn——
    **删到工作区外三级，比删 ``D:/other/x``（critical）判得还轻**。
    """
    return os.path.normpath(str(path or "").replace("/", "\\")).replace("/", "\\").lower()


def _under(path: str, root: str) -> bool:
    """路径是否在 root 之内。**相对路径按 root 解析**（不是按进程 cwd）。

    审批卡片那一侧（``approval._is_under``）也用这一个函数：
    它原来自己 ``Path.resolve()``，而 ``resolve()`` 对相对路径是按**进程
    当前目录**解的——N.E.K.O 的进程目录和会话工作目录没有关系。于是同一
    条 ``rm -rf src``，卡片说"在工作目录外"，闸门说 normal，两边都对不上。
    """
    if not root:
        return False
    normal_root = _norm(root)
    if not normal_root:
        return False
    candidate = path if _is_absolute(path) else os.path.join(str(root), str(path))
    normal_path = _norm(candidate)
    return normal_path == normal_root or normal_path.startswith(normal_root + "\\")


def _is_absolute(target: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", target)) or target.startswith(("/", "\\"))


# ── 命令解析原语（B1/B2 共用；**禁止两套判据**）─────────────────────────
# 这些是纯字符串函数：不碰 I/O、不碰配置、不 import 本包任何模块。
# 放在这个叶子模块里，是为了让 ``rules.py``（闸门）与 ``approval.py``（审批）
# 和本文件（词典）三方共用同一套分词 / 归一化 / 目标解析。
# B2 的硬要求是"闸门与词典共用同一套目标解析结果"——两套判据等于没有判据，
# 而同一条 ``git reset --hard`` 词典认、闸门不认，就是这么来的。

# 前导赋值：`VAR=x rm …`、`VAR="a b" rm …`（分词后引号已经去了）
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 段首装饰：剥掉之后剩下的才是命令主体
_GROUP_OPENERS = frozenset({"(", "{", "!", "[["})
# 前导包装词：后面跟的才是真正要跑的东西
_LEADING_WRAPPERS = frozenset({
    "env", "sudo", "command", "nohup", "time", "exec", "builtin",
    "nice", "stdbuf", "setsid", "timeout", "watch", "xargs", "eval",
    "runuser", "su", "doas",
})
# 这些词**执行**参数字符串：`bash -c "rm -rf x"` 里的 rm 是真删除
_EXEC_WRAPPERS = frozenset({
    "bash", "sh", "dash", "zsh", "fish", "ksh", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "python", "python3", "py", "node", "deno", "perl", "ruby", "php",
    "busybox", "at", "parallel",
})
# 这些词**不执行**参数：参数是给人看的字符串。
# 列出来是为了让兜底匹配不误伤——`echo "rm -rf 是危险操作"` 每张卡都顶红字，
# 用户会被训练成无脑点确认，最后闸门真的失效。
#
# **刻意不收 `git`**：`git rm --cached x` 是真删除，收了就会漏判它；
# 而 `git commit -m "rm -rf x"` 那种误报在 summary 里一眼可辨（用户看得到那是
# commit）。这个交换和 2026-11-21 定下的"宁可误报、不可漏判"是同一条。
PRINT_ONLY_COMMANDS = frozenset({
    "echo", "printf", "write-output", "write-host", "write-error",
    "cat", "type", "more", "less", "man", "help", "head", "tail",
    "grep", "egrep", "fgrep", "findstr", "rg", "ag", "select-string",
    "get-content", "curl", "wget", "iwr", "invoke-webrequest",
    "sqlcmd", "psql", "mysql", "sqlite3", "jq", "yq",
})

_SIMPLE_SPLIT = re.compile(r"[;|&\n]+")
_SEPARATORS = re.compile(r"[\s;|&()<>]+")
# 这些命令的参数**肯定不是删除目标**：它们只改 shell 自己的状态。
# 不列进来的话，`cd D:/w && rm -rf x` 会把 `D:/w` 也当成要删的路径，
# 于是整条命令被判"删了工作区外的目录"——比真实情况危言耸听得多。
_NAVIGATION_COMMANDS = frozenset({
    "cd", "chdir", "pushd", "popd", "export", "set", "setlocal", "source",
    ".", "alias", "unalias", "unset", "let", "read", "declare", "typeset",
    "local", "exit", "return", "break", "continue", "shift", "trap", "umask",
})
# 这些命令的第一个参数是**子命令**，不是路径：`git clean` 的 `clean`、
# `git reset --hard` 的 `reset`。不跳掉它，它会被当成一个相对路径目标，
# 于是"整个仓库的未追踪文件"被误判成"工作区内的一个目录"——降档。
_SUBCOMMAND_OWNERS = frozenset({
    "git", "npm", "pnpm", "yarn", "pip", "uv", "docker", "kubectl", "helm",
    "cargo", "go", "dotnet", "mvn", "gradle", "make", "cmake", "terraform",
    "aws", "gcloud", "az", "poetry", "conda", "brew", "apt", "apt-get",
})
# 纯粹的语言词，不可能同时是路径。
_SHELL_KEYWORDS = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do",
    "done", "case", "esac", "in", "function", "select", "time",
})

# 删了重建得回来的目录：装出来的依赖 / 构建产物 / 字节码缓存。
# 判据是"路径里出现这些名字"，不是"等于"——`D:\pkg\node_modules\a` 也算。
# 名字取得**保守**：写宽了会把真实路径误判成可恢复，那是降档，最危险的方向。
_RECOVERABLE_MARKERS = (
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".cache", ".npm", ".nuget",
    ".gradle", ".m2", ".parcel-cache", ".next", ".nuxt", "dist", "build",
    "target",
)
# 目标集合算不出来的形态：命令替换、环境变量、通配符。
# `rm -rf $(cat list)` / `rm -rf *` / `rm -rf $HOME` 的破坏面都大于
# 任何单一路径，所以一律按最坏的档处理（B2 表：含通配符或无法解析的目标）。
#: 通配符：``*`` ``?`` 与字符类 ``[ ]``。
_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")
#: 变量 / 命令替换。替换出来的值完全不可知，所以它藏得住目标。
#:
#: ``%NAME%`` 是 Windows 的环境变量写法（``%TEMP%`` / ``%USERPROFILE%``）。
#: **必须是成对的两个 %**：单个 % 是格式符（``date +%H:%M:%S`` / ``printf '%s'``），
#: 那个不藏路径——2026-11-26 把裸 ``%`` 从不可解析正则里删掉正是为这个，
#: 别把它加回来。已知边界：``50%_done%.txt`` 这种文件名会被误判成变量，
#: 方向上是多报不是漏报，可以接受。
#:
#: 为什么需要它：``%TEMP%/../notes`` 归一化之后是 ``notes``，看着像工作区内
#: 的相对路径，于是被判 normal/warn——而它实际落在用户主目录。
#: 根因是 ``_is_absolute`` 对 ``%VAR%…`` 返回 False，调用方就按工作目录
#: 去拼它，可变量展开的结果跟工作目录根本没有关系。
_SUBSTITUTION_RE = re.compile(r"[$`]|%[A-Za-z_][A-Za-z0-9_]*%")


def _target_hides_scope(target: str) -> bool:
    """这个**目标 token** 藏住了作用域吗。

    2026-11-26 重写。老实现是一条正则搜**整条命令串**：

        _UNPARSEABLE_RE = re.compile(r"[$`*?\\[\\]]")
        return bool(_UNPARSEABLE_RE.search(strip_heredocs(command)))

    实测（35 个转录 / 3245 次 Bash / 119 条删除命中）它把 **47 条**抬到
    极高危，其中 **24 条只由通配符触发**，20 条真的因此从【严重】掉下来——
    也就是全部【严重】的 28%。三类误报，全是"字符在一条命令里出现过"：

        rm -rf build --exclude="*.log"   目标只有 build，* 在开关值里
        rm -rf dist && echo "*"           * 在另一条 && 段里
        rm -rf build/*                    * 在有字面前缀的目标里

    删掉 ``%``（同一天，第四条误报）贴的正是这道伤口的第二张创可贴：
    ``date +%H:%M:%S && rm -f x`` 与 ``--exclude='*.log'`` 是同一类病。

    ## 新判据：只看目标，且区分「有目录前缀的通配」与「光板通配」

    1. 含 ``$``` ` `` → 藏（替换值不可知）；
    2. 一个通配符都没有 → 不藏（字面路径）；
    3. 通配符**之前**有字面目录（``build/``、``/tmp/``）→ 不藏，
       作用域被那个目录圈住了，按它判档即可；
    4. 通配符之前没有目录，或只有 ``.``/``..``/分隔符 → 藏。
       ``*.log`` / ``a?b`` / ``f[0-9].txt`` / ``*`` / ``./*`` / ``../*``
       的作用域说不清，按最坏的猜。

    第 4 条是刻意保留的收敛：``rm -rf ./*`` 与 ``rm -rf *`` 语义等价，
    放过它就等于放过后者。而 ``build/*`` 与 ``*`` 不是一回事——
    前者圈在 ``build`` 里，而 ``build`` 本来就在可恢复词表上。
    """
    text = str(target or "").strip().strip("'\"")
    if not text:
        return True
    if _SUBSTITUTION_RE.search(text):
        return True
    match = _GLOB_CHARS_RE.search(text)
    if match is None:
        return False
    prefix = text[: match.start()]
    if not any(sep in prefix for sep in ("/", "\\")):
        return True
    return prefix.strip("/\\. ") in ("", ".", "..")


def target_unparseable(command: str) -> bool:
    """目标集合算不算得出来。算不出来 → 调用方必须按最坏的档处理。

    **同样要先剥 heredoc**：这个函数看的是原始命令串，而
    ``_segments`` 已经剥过了。两边看不用的文本，就会出现
    "``is_delete_command`` 说不是删除、``target_unparseable`` 却说目标藏起来了"
    这种自相矛盾——正是"两套判据"要避免的那件事。

    **逐目标判，不再整条搜**（2026-11-26，见 ``_target_hides_scope``）：
    只要有一个目标藏住作用域，整条就算不可解析——一条命令删两个路径，
    说不清的那个说了算（与 ``target_tier`` 的取最坏同向）。

    **临时目录跳过这一判**：``%TEMP%`` 含 ``%NAME%``，按替换规则算"藏住了"，
    可我们**认识**它——它就是临时目录，不需要解析出什么。
    不跳的后果是刚做的临时目录降档被自己顶回 extreme，白做。
    """
    targets = delete_targets(command)
    if not targets:
        # 没有目标不是"藏起来了"，是"没有"——那由 tier_for_command 的
        # 待定分支处理，删除类调用方会再抬到极高危。两边不许抢同一件事。
        return False
    for target in targets:
        if _is_temp_path(target):
            continue
        if _target_hides_scope(target):
            return True
    return False


def is_assignment(token: str) -> bool:
    """`VAR=…` 形态的前导赋值。"""
    return bool(_ASSIGNMENT_RE.match(str(token or "")))


def normalise_token(token: str) -> str:
    """去引号、转小写。引号留着会让 `"rm` 匹配不上。"""
    return str(token or "").strip("\"'`").lower()


def _raw_tokens(text: str) -> list[str]:
    """按与归一化**同一套规则**切 token，但保留原文（大小写、引号）。

    为什么需要原文：目标路径要显示给用户看。``D:/other/x.txt`` 被转成
    ``d:/other/x.txt`` 之后，用户在卡片上认不出自己刚敲的那个路径——
    而"让他确认一个他不认识的路径"等于没让他确认。
    """
    return [t for t in _SEPARATORS.split(str(text or "")) if t]


def _segments(command: str) -> list[list[tuple[str, str]]]:
    """每段是 ``[(原文, 归一化), …]``。原文给展示层，归一化给判定层。

    **heredoc 正文在这里整段丢掉**（见 ``strip_heredocs``）——这是所有判定的
    唯一共同上游（``simple_commands`` 与 ``delete_targets`` 都走这里），
    所以放这一处就够，不会出现"词典剥了、闸门没剥"的两套判据。

    **重定向也在这里整段丢掉**（见 ``_REDIRECT_RE``）——同一个理由：
    ``rm -f x > /dev/null`` 里 ``/dev/null`` 不是 ``rm`` 的参数，
    不剥的话它会被当成删除目标，而它是绝对路径且在工作区外，
    于是**任何带 ``> /dev/null`` 的 rm 都顶【严重】**。
    """
    text = strip_heredocs(str(command or ""))
    text = _REDIRECT_RE.sub(" ", text)
    out: list[list[tuple[str, str]]] = []
    for part in _SIMPLE_SPLIT.split(text):
        pairs = [(raw, normalise_token(raw)) for raw in _raw_tokens(part)]
        if pairs:
            out.append(pairs)
    return out


#: heredoc 开头：``<<EOF`` / ``<<-EOF`` / ``<<'EOF'`` / ``<<"EOF"``。
#: 结束标记可以是引号包着的，也可以不包；``<<-`` 只是允许正文行首有 Tab。
_HEREDOC_OPEN = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

#: shell 重定向：**操作符 + 它指向的那个文件名**，一起丢掉。
#:
#: 覆盖的形态：``> f`` / ``>> f`` / ``< f`` / ``<< f``（herestring）、
#: ``2> f`` / ``2>> f``（带 fd）、``&> f`` / ``&>> f``、以及
#: ``2>&1`` / ``>&2`` / ``<&0`` 这种复制描述符的。
#:
#: **为什么必须剥（2026-11-26 第六条误报，实测最严重的一条）**：
#: 重定向的目标**不是命令的参数**。``rm -f x > /dev/null`` 里 ``rm``
#: 的参数只有 ``-f x``，``/dev/null`` 归 shell 管。而 ``_SEPARATORS``
#: 把 ``<>`` 当分隔符，于是操作符自己消失了、**文件名留在 token 流里**，
#: 被 ``delete_targets`` 当成删除目标。``/dev/null`` 是绝对路径且在工作区外
#: → 直接判 extreme → 【严重】。实测本机 6 次。
#:
#: 同理 ``2>/dev/null`` 还会多带一个 ``2``（fd 号）进目标集合。
#:
#: 已知边界：引号**里面**的 ``>`` 也会被剥掉（``echo "a > b"`` → ``echo "a``）。
#: 这是故意的——那种段落的命令主体是 echo/printf 这类打印命令，
#: ``match_delete`` 本来就不看它的参数；而真要写一套引号感知的扫描器，
#: 复杂度比它防住的问题大得多。方向上是"多剥"而不是"漏剥"。
_REDIRECT_RE = re.compile(
    r"""(?:\d+|&)?>{1,2}\s*(?:&\d+|"[^"]*"|'[^']*'|[^\s|;&<>]+)"""
    r"""|<{1,2}\s*(?:&\d+|"[^"]*"|'[^']*'|[^\s|;&<>]+)"""
)


def strip_heredocs(command: str) -> str:
    """把 heredoc 的**正文行**丢掉，只留开头行与结束行。幂等。

    ## 为什么需要它（2026-11-26 真机，连误报三次）

    实测一条完全正常的命令：

        cd "…/agent_deskmate" && python - <<'PYEOF'
        cases = ['rm -f /tmp/a.tgz', 'rm -rf ~']
        PYEOF

    被判成【严重】删除文件，severity=critical。heredoc 里那两个 ``rm`` 是
    **Python 的字符串字面量**，是测试用的数据，shell 一个都不会执行。
    但经 ``_segments`` 切段后照样命中删除词。

    于是这是一个**大类误报**：任何 agent 写测试、写脚本、写文档、
    甚至在对话里引用一条危险命令，用户就收到一条红字。
    误报反复几次，用户就被训练成无脑点确认——**闸门真的失效**，
    而且是在最不该失效的时候。

    语义上这与既有的"打印类不算命令"是同一条：
    ``echo "rm -rf 是危险操作"`` 不该顶红字，
    ``python - <<'EOF' … rm -rf … EOF`` 也不该。

    ## 两条边界

    1. **开头行保留**。``cd … && python - <<'PYEOF'`` 这一行本身有真命令
       （``cd``、``python``），同一行里 ``<<`` 之前的删除词也照旧算——
       那确实在命令行上，确实会执行。
    2. **没闭合就整体不剥**。转录被截断时 heredoc 可能没有结束行；
       这时把后半全丢掉会让我们对一个可能真含删除命令的命令视而不见。
       宁可漏剥（回到误报）也不盲掉——**方向要对**。
    """
    text = str(command or "")
    if "<<" not in text:
        return text

    lines = text.split("\n")
    keep: list[str] = []
    delim: str | None = None
    strip_tabs = False
    closed = True

    for line in lines:
        if delim is not None:
            probe = line.lstrip("\t") if strip_tabs else line.strip()
            if probe == delim:
                delim = None          # 结束行本身是 shell 语法，保留
                keep.append(line)
            continue                 # 正文行：数据，丢掉
        match = _HEREDOC_OPEN.search(line)
        if match:
            delim = match.group(3)
            strip_tabs = bool(match.group(1))
        keep.append(line)

    if delim is not None:
        # 走到结尾还没闭合：不剥，原样返回。
        closed = False
    if not closed:
        return text
    return "\n".join(keep)


def _command_bodies(command: str) -> list[list[tuple[str, str]]]:
    """按段切、剥段首噪音，**原文跟着一起留**。

    ``simple_commands`` 的带原文版。为什么需要它：命令位判定要按原文追引号
    开合，而归一化会把收尾引号 strip 掉——按归一化后的文本永远等不到闭合，
    整条命令会被吃光（``_normalise_pairs`` 里那段注释说的就是这件事）。
    """
    return [_normalise_pairs(segment) for segment in _segments(command)]


def simple_commands(command: str) -> list[list[str]]:
    """按 `;` `|` `&` 换行切段，每段再切 token **并剥掉段首噪音**。

    段内判定互不影响。返回的就是"命令主体"token 列表——
    ``VAR="a b" sudo rm -rf x`` 出来是 ``['rm', '-rf', 'x']``。

    **判定一律走这个入口，不要自己 ``normalise_tokens(simple_commands(…))``**：
    那样拿到的是已经丢过引号的 token，引号配对就断了，
    ``VAR="a b" rm -rf x`` 会被吃成空段（实测踩过）。
    """
    return [[norm for _, norm in body] for body in _command_bodies(command)]


def _normalise_pairs(segment: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``simple_commands`` 的带原文版本：剥段首噪音，原文跟着一起留。"""
    out = list(segment or [])
    while out:
        head = out[0][1]
        if is_assignment(head):
            # 带引号的值会被切成多个 token：`VAR="a b"` → `VAR="a` + `b"`。
            # 只丢第一个的话，`b` 会变成"命令主体"——于是 `VAR="a b" rm -rf x`
            # 判不出删除。一路吃到引号闭合为止。
            # **引号状态必须按原文算**：归一化会 strip 掉收尾引号，
            # 按归一化后的文本永远等不到闭合，会把整条命令吃光。
            consumed, out = out[0][0], out[1:]
            while out and _quote_state(consumed):
                consumed += " " + out[0][0]
                out = out[1:]
            continue
        if head in _LEADING_WRAPPERS or head in _EXEC_WRAPPERS:
            out = out[1:]
            # 包装词自己的开关：`sudo -u root`、`env -i`、`bash --norc`。
            # 引号同理：`sudo -u "a b" rm` 不能把 `b` 留成命令主体。
            while out and (out[0][1].startswith("-") or is_assignment(out[0][1])):
                consumed, out = out[0][0], out[1:]
                while out and _quote_state(consumed):
                    consumed += " " + out[0][0]
                    out = out[1:]
            continue
        if head in _GROUP_OPENERS:
            out = out[1:]
            continue
        break
    return out


# 段里出现过这些词，说明后面的引号串是**要执行的代码**，里面的每个词都在命令位：
#   bash -c "sudo rm -rf /"       → rm 是真删除
#   ssh host "sudo rm -rf /x"     → 尾参数是远端命令
#   docker exec c sh -c 'rm -rf /'
# 不在这里的（python / node / echo / git / grep / print…），引号串是**数据**。
#
# **不能直接复用 ``_EXEC_WRAPPERS``**：python 也在那一组里，而
# ``python -c "print('allow 里带 rm 的:')"`` 的 rm 只是要打印的字符。
# 2026-11-26 真机实测：那句只读代码让插件播了一条【严重】删除文件——
# 而 critical 是"强制常亮、永不递减"的那一档。区别在于 bash 跑的是 shell、
# python 跑的是 Python，只有前者的字符串里每个词都是命令。
_CODE_STRING_OWNERS = frozenset({
    "bash", "sh", "dash", "zsh", "fish", "ksh", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "busybox", "at",
    # 在别处跑 shell 的：尾参数或 -c 后面那一整串都是命令
    "ssh", "docker", "podman", "kubectl", "vagrant", "winrs", "psexec", "plink",
})


def _is_quoted_word(raw: str) -> bool:
    """整个 token 就是一整段带引号的词：``'rm'`` / ``"rm"`` / ``'a b'``。

    与"引号开了但没闭合"（``'rm``、``"sudo``）是两回事——后者是一长串
    引号内容的开头，前者本身已经是一个完整的值。
    """
    text = str(raw or "")
    return (len(text) >= 2 and text[0] in "\"'`"
            and text[-1] == text[0] and text[0] not in text[1:-1])


def _command_position_tokens(body: list[tuple[str, str]]) -> list[str]:
    """段里**shell 真的会当命令执行**的 token（归一化形式）。

    2026-11-26 之前，"这段里有删除动作吗"的答案是"任一个 token 的
    basename 等于删除词"。后果不只是多一条播报，是档位被顶到极高危：

    .. code-block::

        print('allow 里带 rm 的:')          # 一句只读代码
          → 判成删除段
          → delete_targets 把 perm.get('allow',[]) 里的 [] 也当目标
          → _target_hides_scope 说"作用域被 glob 藏住了"
          → target_tier(unparseable=True) 直接给 EXTREME
          → 【严重】播报，而 critical 永不递减

    判据换成"这个 token 在不在命令位"：

    - **引号内部往后都是数据**：``echo "please rm this"``、
      ``print('allow 里带 rm 的:')`` 里的 rm 是要打印/匹配的字符。
    - **非命令位的整词引号是值**：``grep 'rm' file``、``if 'rm' in x``、
      ``['rm']`` 里的 rm 是被比较的字符串。
    - **命令位例外**：``"rm" -rf x``、``xargs "rm" -rf`` 整词被引号包住，
      shell 照样执行。判据是"它是不是剥掉包装词之后的段首"
      （``_normalise_pairs`` 已把 sudo / xargs / 赋值 吃掉，
        真正的命令词会落到 stripped[0]）——不是"它在第几位"。
    - **代码串主人不豁免**：见 ``_CODE_STRING_OWNERS``。

    方向仍然是"宁可多报"：``git commit -m "rm the file"`` 的引号串以删除词
    开头，开头那个 token 在命令位，照旧算删除。

    ``body`` 要传**未剥段首噪音**的段——``bash -c "…"`` 里的 bash
    会被 ``_normalise_pairs`` 连 ``-c`` 一起剥掉，事后再看就分不出
    那是 bash 的代码串还是别的。
    """
    stripped = _normalise_pairs(body)
    command_raw = stripped[0][0] if stripped else ""
    quote = ""
    code_string = False
    out: list[str] = []
    for raw, norm in body or []:
        if not quote and basename(norm) in _CODE_STRING_OWNERS:
            code_string = True
        if code_string:
            out.append(norm)
        elif quote:
            pass                    # 引号内部：数据，不是命令
        elif _is_quoted_word(raw) and raw != command_raw:
            pass                    # 非命令位的整词引号：值，不是命令名
        else:
            out.append(norm)
        quote = _advance_quote(quote, raw)
    return out


def delete_segment_texts(command: str) -> list[str]:
    """把命令拆成**含删除动作的段**的原文。

    2026-11-26：一条 Bash 里 ``rm -rf a; rm -rf b; rm -rf c`` 只产一条命中，
    于是审查也只有一个项——模型对整批点头，三条一起被压掉，包括里面那个
    ``config.yaml``。逐段判才能让模型对每段各自表态。

    走 ``_segments`` 而不是自己按 ``;|&`` 切：那份切法已经处理了 heredoc 与
    重定向，自己再写一套就会和它对不上——"``is_delete_command`` 说不是删除、
    这里说有一段是删除"正是要消灭的那类事。

    段文由**原文 token** 用空格拼回：归一化会 strip 引号并转小写，
    拼回去用户就认不出自己敲的那个路径了（``D:/other/x.txt`` 变
    ``d:/other/x.txt`` 正是 ``_raw_tokens`` 的 docstring 要避免的那件事）。
    """
    out: list[str] = []
    for segment in _segments(command):
        if not _segment_has_delete(segment):
            continue
        text = " ".join(raw for raw, _ in segment if raw).strip()
        if text:
            out.append(text)
    return out


def delete_command_bodies(command: str) -> list[tuple[list[str], list[str]]]:
    """逐段给出 **（剥噪音后的命令主体, 命令位 token）**。

    ``rules.match_delete`` 与 ``commands.is_delete_command`` 共用这一份，
    对"哪个 token 在命令位"给出同一个答案——2026-11-26 之前那两处
    各写一遍，而且都写错了（见 ``_command_position_tokens``）。

    一次 ``_segments`` 出两个结果：``match_delete`` 两个都要用
    （主体管段首/帮助豁免，命令位管兜底扫描），分两次算会让 watcher
    每 3 秒把同一条命令的切分工作做两遍。
    """
    out: list[tuple[list[str], list[str]]] = []
    for segment in _segments(command):
        out.append(([norm for _, norm in _normalise_pairs(segment)],
                    _command_position_tokens(segment)))
    return out


def basename(token: str) -> str:
    """`/bin/rm` → `rm`，`C:\\Windows\\System32\\del` → `del`。

    Windows 上要额外剥掉可执行后缀：`C:\\Windows\\System32\\rm.exe`
     basename 之后是 `rm.exe`，而危险词表里是 `rm`——不剥就漏判。
    """
    text = str(token or "")
    for sep in ("/", "\\"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    lowered = text.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if lowered.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _quote_state(text: str) -> str:
    """扫一遍，返回**还没闭合**的引号字符；全部闭合则返回空串。"""
    return _advance_quote("", text)


def _advance_quote(quote: str, text: str) -> str:
    """带着已开的引号状态扫一段文本，返回扫完后**还没闭合**的引号。

    ``_quote_state`` 是它从闭合状态起扫的特例。两个都要有：
    ``_normalise_pairs`` 只知道"这段之后闭没闭合"，
    而命令位判定需要知道"这个 token **进来之前**闭没闭合"。
    """
    for char in str(text or ""):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
    return quote


def tokens(command: str) -> list[str]:
    """整条命令切 token。"""
    return [t for t in _SEPARATORS.split(str(command or "")) if t]


def command_body_unknown(command: str) -> bool:
    """归一化之后仍拿不到命令主体 → True。

    这不是"没命中规则"，是"我们看不懂这条命令"。B1 要求这种情况
    **不得低于 notice**，B2 进一步要求至少 warn：看不懂的命令按最坏的猜，
    比按最好的猜安全。
    """
    segments = simple_commands(command)
    if not segments:
        return bool(str(command or "").strip())
    return any(not segment for segment in segments)


def _segment_targets(segment: list[tuple[str, str]]) -> tuple[str, ...]:
    """一段（**已配对**）里的路径目标，保留原文大小写。

    `git` / `npm` 这类命令的第一个参数是子命令，跳过（见
    ``_SUBCOMMAND_OWNERS``）；开关与赋值也跳过。
    """
    body = _normalise_pairs(segment)
    if not body or body[0][1] in _NAVIGATION_COMMANDS:
        return ()
    rest = body[1:]
    if body[0][1] in _SUBCOMMAND_OWNERS:
        # 先吃掉子命令本身，再往下找路径
        index = 0
        while index < len(rest) and rest[index][1].startswith("-"):
            index += 1
        rest = rest[index + 1:] if index < len(rest) else []
    return tuple(raw.strip("'\"") for raw, norm in rest
                 if not norm.startswith("-") and not is_assignment(norm))


def _segment_has_delete(body: list[tuple[str, str]]) -> bool:
    """这一段里有没有删除类动作。**全项目唯一的删除判据**——

    ``rules.match_delete`` 的兜底、``is_delete_command``、这里三处
    对"什么叫删除"给出不同答案，就会出现"播报说这条是删除、
    档位说它不是"。

    ``body`` 要传**未剥段首噪音**的段（``_segments`` 的直接产物）：
    命令位判定要看到剥掉之前的 ``bash -c``，理由见 ``_CODE_STRING_OWNERS``。

    成对动作（``git clean`` / ``drop table``）按**剥噪音后相邻**判，
    与 ``rules.match_delete`` 逐字相同：那边按 stripped body 判相邻，
    这边若按命令位 token 判"都在"，``mysql -e "drop table users"``
    就会一个说删、一个说不删（``table`` 在引号里，命令位没有它）。
    """
    if not body:
        return False
    stripped = [norm for _, norm in _normalise_pairs(body)]
    for left, right in DELETE_PAIRS:
        for i in range(len(stripped) - 1):
            if stripped[i] == left and stripped[i + 1] == right:
                return True
    return any(basename(token) in DELETE_TOKENS
               for token in _command_position_tokens(body))


def delete_targets(command: str) -> tuple[str, ...]:
    """这条命令**要动哪些路径**。抽不出返回空元组。

    只取**含删除动作那段**里的路径目标；``cd`` / ``export`` 这类
    只改 shell 自身状态的段整段跳过（见 ``_NAVIGATION_COMMANDS``）——
    `rm -rf a b` 要能取到 `a` 和 `b` 两个，漏一个就可能把档位判低。

    **2026-11-26：非删除段整段丢掉。** 老实现对每一段都取目标，于是
    ``rm -rf dist && echo "*"`` 的目标是 ``('dist', '*')``——那个 ``*``
    是 ``echo`` 的参数，shell 一个文件都不会删，却把整条顶成【严重】。
    "这条命令要动哪些路径"的答案只能来自会动路径的那几段。

    **刻意不用"停用词"过滤**：曾经用 `a` / `the` / `in` 之类筛掉"不像路径的
    token"，结果 `rm -rf a b` 只取到 `b`——单字母目录名被当成英文冠词丢掉了。
    丢目标只会让档位变低，那是最危险的方向；多取一个再按 ``_looks_like_path``
    筛，方向才对。

    返回的是**原文**（不是归一化后的）：路径要显示给用户看，
    ``D:/other/x.txt`` 不能变成 ``d:/other/x.txt``。
    """
    found: list[str] = []
    assignments = _literal_assignments(command)
    for segment in _segments(command):
        if not _segment_has_delete(segment):
            continue
        for target in _segment_targets(segment):
            found.append(_expand_literals(target, assignments))
    return tuple(found)


# ``$VAR`` / ``${VAR}`` / ``%VAR%`` 三种引用形态。
# 只认"合法的 shell 标识符"——``$1`` / ``$@`` 这类位置参数不认，
# 它们的值我们永远不知道。
_VAR_REF_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
    r"|%(?P<pct>[A-Za-z_][A-Za-z0-9_]*)%")


def _literal_assignments(command: str) -> dict[str, str]:
    """同一条命令里的 ``VAR=字面值`` 赋值。**只收不含替换的值**。

    shell 脚本的惯用形状是"先赋值、再用"::

        MSG=/tmp/x && git commit -F "$MSG" && rm -f "$MSG"

    老实现看到 ``rm -f "$MSG"`` 判定"目标被变量藏住了"→
    ``target_tier(unparseable=True)`` 直接给 EXTREME → 【严重】播报。
    2026-11-26 真机：这一类占 critical 播报的绝大多数，而它们几乎全是
    脚本清理自己的临时文件。**"永不递减"的那一档被耗光了**——
    用户原话：「其中很大部分还是误报，根本就没必要报告的事情」。

    赋值就在同一条命令里写着，读它不是猜。

    **只收字面值**：值里含 ``$`` / 反引号 / ``%NAME%`` 的一律不收——
    那说明值本身要从别处取，我们不知道它是什么，按最坏的猜
    （与 ``_target_hides_scope`` 同一条理由）。``X=$(rm -rf /)`` 这类
    因此仍然落 EXTREME。
    """
    out: dict[str, str] = {}
    for segment in _segments(command):
        for raw, _norm in segment:
            if not is_assignment(raw):
                continue
            name, _, value = raw.partition("=")
            name = name.strip()
            value = value.strip().strip("\"'")
            if not name or not value:
                continue
            if _SUBSTITUTION_RE.search(value):
                continue          # 值自身要替换 → 我们不知道它是什么
            out[name] = value
    return out


def _expand_literals(target: str, assignments: dict[str, str]) -> str:
    """把目标里的 ``$VAR`` / ``${VAR}`` / ``%VAR%`` 换成已知字面值。

    换不出来就**原样返回**——调用方随后会按"藏住了"判，方向是安全的
    （漏判才危险，多判只是吵）。

    **大小写敏感**：shell 的 ``$msg`` 与 ``$MSG`` 是两个变量。
    宽松匹配会把一条本来无害的命令（``$msg`` 展开成空）误判成已知路径，
    也可能把危险的误判成安全的——严格才是对的。
    """
    if not assignments:
        return target

    def _sub(match: "re.Match[str]") -> str:
        name = match.group("braced") or match.group("bare") or match.group("pct")
        return assignments.get(name, match.group(0))

    return _VAR_REF_RE.sub(_sub, str(target or ""))


def _looks_like_path(token: str) -> bool:
    """这个 token 可能是路径吗。

    **默认是**，只有纯语言词才判否。曾经反过来写（要求"看起来像路径"：
    有分隔符 / 有扩展名 / 是已知目录名），结果 `rm -rf src` 因为 `src`
    两个字不像路径而被当成"没有目标"——而"没有目标"在删除类里会被抬到
    极高危，于是删自己源码目录比删 `src/`（带斜杠）判得还重，全看有没有
    打那个斜杠。默认是、只排除语言词，方向才不会随写法抖。
    """
    text = str(token or "").strip()
    if not text:
        return False
    return text.lower() not in _SHELL_KEYWORDS


def is_recoverable_target(target: str) -> bool:
    """删了重建得回来的目录（依赖 / 构建产物 / 缓存）。"""
    lowered = str(target or "").lower().replace("/", "\\")
    return any(marker in lowered for marker in _RECOVERABLE_MARKERS)


def _is_home_or_root(target: str) -> bool:
    """家目录 / 盘根。这些地方的破坏面没有边界。"""
    text = str(target or "").strip().replace("/", "\\").rstrip("\\")
    if not text:
        return True
    lowered = text.lower()
    if lowered in ("~", "%userprofile%", "%home%", "%homepath%", "$home"):
        return True
    if lowered.startswith(("~\\", "%userprofile%\\", "%home%\\", "$home\\")):
        return True
    return bool(_DRIVE_ROOT_RE.match(text)) or text in ("\\", "/")


def _tier_of_one(target: str, working_directory: str | None) -> str:
    """单个目标的档位。"""
    if is_recoverable_target(target):
        return TIER_RECOVERABLE
    # 临时目录排在 home/root 与 system **之前**：它要先于那两道拦下来，
    # 否则 `%TEMP%` 会掉到 `_under` 里被判成"工作区内"（warn），
    # 而它其实压根不在工作区——那是个错的宽松，不是对的宽松。
    # `C:\Windows\Temp` 不受影响：它不在 `_TEMP_PATH_RE` 里，仍走 system 档。
    if _is_temp_path(target):
        return TIER_RECOVERABLE
    if _is_home_or_root(target) or _is_system_path(target):
        return TIER_EXTREME
    if not working_directory:
        # B8：**拿不到工作目录 ≠ 最坏**。老实现这里返回极高危，于是转录里
        # 每条缺 cwd 的删除都顶【严重】，而审批卡片（hook payload 带 cwd）
        # 对同一条命令说 notice——两边说的不是一回事，用户只能不信其中一边。
        # 改成「范围未知」档：知道要删什么，不知道它在不在工作区外。
        return TIER_SCOPE_UNKNOWN
    # **相对路径也走 containment**（2026-11-26）。老实现这里有一句
    # `if not _is_absolute(target): return TIER_NORMAL`，本意是"相对路径
    # 相对于会话工作目录，算在内"——但 `../..` 同样是相对路径，
    # 它相对于工作目录的解算结果在**工作区外**。一句话同时放过
    # `src`（该放过）和 `../../..`（不该放过），因为没真去解算。
    return TIER_NORMAL if _under(target, working_directory) else TIER_EXTREME


def target_tier(targets: Any, *, working_directory: str | None,
                unparseable: bool = False) -> str:
    """按目标分档（B2 表）。多个目标时**取最坏的一档**——
    一条命令删两个路径，危险的那个说了算。"""
    if unparseable:
        return TIER_EXTREME
    usable = [str(t) for t in (targets or ()) if _looks_like_path(t)]
    if not usable:
        # 一个像路径的目标都没取到。这不是"没有目标"，是"说不清要删什么"，
        # 所以落待定档；删除类调用方会再把它抬到极高危（见 ``_classify``）。
        return TIER_UNKNOWN
    tiers = [_tier_of_one(t, working_directory) for t in usable]
    for tier in _WORST_FIRST:
        if tier in tiers:
            return tier
    return TIER_UNKNOWN


def tier_for_command(command: str, working_directory: str | None) -> str:
    """一条命令的目标档位。闸门与词典共用这一个函数。"""
    return target_tier(delete_targets(command),
                       working_directory=working_directory,
                       unparseable=target_unparseable(command))


def severity_for_tier(tier: str) -> str:
    """目标档位 → 严重度（B2）。

    unknown 落 warn 而不是 notice：看不懂的命令按最坏的猜。
    ``scope_unknown`` 同样落 warn：拿不到工作目录时我们**知道**要删什么，
    只是说不清位置—— warn 表示"要留意、要人点"，又不用【严重】冒充一个
    我们并不掌握的事实（B8）。
    这是 B1「归一化失败不得低于 notice」与 B2「没见过的默认待定」的交汇点。
    """
    return {
        TIER_EXTREME: "critical",
        TIER_NORMAL: "warn",
        TIER_RECOVERABLE: "notice",
        TIER_UNKNOWN: "warn",
        TIER_SCOPE_UNKNOWN: "warn",
    }.get(tier, "warn")



# B7：这两句是"说不出具体内容"时的占位。它们不许出现在卡片上——
# 用户读到的是没有信息量的一句话，而它占的位置本可以放有用的东西。
# 真正影响判定的那句（"这个文件在它的工作目录之外"）**不算**占位，照常显示。
NON_SPECIFIC_SCOPES = frozenset({
    "看不出它会影响哪些文件",
    "影响范围不止一个文件",
})


def path_is_under(path: str, root: str) -> bool:
    """路径是否在 root 之内。**闸门与审批卡片共用这一个函数**。

    2026-11-26 抽出来是为了消灭一处"两边说不一样的话"：审批卡片原来
    自己写了一份 ``Path.resolve()`` 版的 containment，而 ``resolve()``
    对相对路径是按**进程当前目录**解的。N.E.K.O 的进程目录和会话工作目录
    没有关系，于是每条相对路径的删除，卡片都说"在工作目录外"，
    闸门（当时还说 normal）说"在内"。同一份事实两个答案，
    用户只能不信其中一边——那正是 B8 注释里要消灭的失效模式，
    只是它换了个位置又长了一次。

    现在只有这一份判据。要改 containment，改这里，两边一起变。
    """
    return _under(path, root)


def scope_is_specific(scope: str) -> bool:
    """这一行范围说得清具体内容吗。空话不算。"""
    text = str(scope or "").strip()
    return bool(text) and text not in NON_SPECIFIC_SCOPES


def consequence_is_specific(consequence: str) -> bool:
    """这一行后果说得清最坏结果吗。

    门槛是"比占位句长"：``UNKNOWN_CONSEQUENCE`` 与它的任何缩写都不算。
    """
    text = str(consequence or "").strip()
    if not text or text == UNKNOWN_CONSEQUENCE:
        return False
    return len(text) > len("后果：") + 4


def scope_line(facts: CommandFacts, working_directory: str | None) -> str:
    if not facts.known:
        return "看不出它会影响哪些文件"
    target = facts.targets[0] if facts.targets else ""
    if not target:
        return facts.scope or "影响范围不止一个文件"
    if _is_system_path(target):
        return "这是系统目录，改坏了系统起不来"
    if not working_directory:
        return "不知道它的工作目录，说不清在不在范围内"
    if not _is_absolute(target):
        # 相对路径：相对于会话的工作目录，所以算在内
        return "在它的工作目录之内，含它下面的所有内容" \
            if "目录" in facts.action else "在它的工作目录之内"
    inside = _under(target, working_directory)
    where = "在它的工作目录之内" if inside else "这个文件在它的工作目录之外"
    # 递归删除时补一句"含子目录"——影响面和单文件不一样
    return (f"{where}，含它下面的所有内容"
            if "目录" in facts.action else where)


# ── 事实摘要行（2026-11-24 真机验收 §5.1）────────────────────────────────
#
# 用户原话：第一行必须是能扫读的事实摘要，不是「建议拒绝 / 属于高风险」。
# 理由：用户要决策的是「这个文件删了要不要紧」，不是「这条被标了什么档」。
#
# 形态：``删除文件：<目标>（本会话工作目录内 · 不递归 · 13 B · 未被 git 跟踪）``
# 括号里是**这条请求独有**的判据；每张卡都重复的那句话（"高风险"）不放这里。
#
# **只描述能从命令本身推出来的事实，不碰文件系统**：本模块是纯函数层，
# 测试跑几百次不该去 stat 真实路径。文件大小/在不在由调用方（审批卡片）
# 用 ``file_facts`` 传进来，它那边有 try/except 兜着。

def fact_line(*, command: str, working_directory: str | None,
              file_facts: str = "") -> str:
    """主卡第一行：能扫读的事实摘要。

    ``file_facts`` 由调用方从文件系统读来（大小 / 在不在），
    **本函数不碰磁盘**——纯函数层的理由见上面的注释。
    """
    facts = translate_segments(command, working_directory=working_directory)
    top = facts[0] if facts else translate(command)
    action = top.action
    target = top.targets[0] if top.targets else ""
    head = f"{action}：{target}" if target and target not in action else action

    qualifiers: list[str] = []
    scope = scope_line(top, working_directory)
    qualifiers.extend(_compact_qualifiers(top, scope, working_directory))
    if file_facts:
        qualifiers.append(file_facts)
    body = " · ".join(part for part in qualifiers if part)
    return f"{head}（{body}）" if body else head


def _compact_qualifiers(facts: CommandFacts, scope: str,
                        working_directory: str | None) -> list[str]:
    """把 ``scope_line`` 的长句压成能并排读的短语。

    **不新造判据**：每个短语都从既有事实推（在不在工作目录内、递不递归、
    认不认得这条命令）。推不出来就不填——空一项比填错一项好。
    """
    out: list[str] = []
    if not facts.known:
        out.append("看不出它会影响什么")
        return out
    if "系统目录" in scope:
        out.append("系统目录")
    elif working_directory:
        out.append("本会话工作目录内" if "之内" in scope else "工作目录之外")
    # 递不递归只对删除类有意义（用户示例：`删 1 个文件 · 不递归`——
    # 那是在说"只动这一个，不殃及里面"）。非删除动作提它是句废话。
    if "删除" in facts.action:
        out.append("递归" if "含它下面的所有内容" in scope else "不递归")
    if facts.irreversible:
        out.append("不可恢复")
    return out


def card_lines(*, command: str, working_directory: str | None) -> dict[str, Any]:
    """主卡正文的前三行（规格 P3）。第四行结论由调用方填——词典不给结论。

    B3：``action`` 是**最危险那一段**的短语（与 ``actions[0]`` 相同），
    ``actions`` 是主卡上要列的全部动作短语（最危险的最多
    ``MAX_CARD_ACTIONS`` 条）。保留单数键是为了不打断既有调用方，
    真正决定"列几条"的是 ``actions``。

    B7：``consequence_specific`` / ``scope_specific`` 说这两行**有没有实质内容**。
    说不清的时候就别上卡片——一句「未知」占着最显眼的位置，
    比什么都不说更糟（用户会把它当成又一条废话划过去）。
    """
    facts = translate_segments(command, working_directory=working_directory)
    top = facts[0] if facts else translate(command)
    actions = card_actions(command, working_directory=working_directory)
    scope = scope_line(top, working_directory)
    return {
        "action": actions[0] if actions else action_phrase(top),
        "actions": actions,
        "consequence": f"后果：{top.consequence}",
        "scope": f"范围：{scope}",
        "consequence_specific": consequence_is_specific(top.consequence),
        "scope_specific": scope_is_specific(scope),
        "known": top.known,
        "irreversible": top.irreversible,
    }


def command_display_note(command: str) -> str:
    """命令超过卡片展示上限时的那一行提示。没超就返回空串。

    **这一行是安全件，不是装饰**：卡片只显示前 ``COMMAND_DISPLAY_MAX``
    字，而风险判定读的是全文。不告诉用户"你没看全"，就等于让他在
    信息不全的情况下点"允许"。所以超长时必须明说，并把全文指向
    「这是什么」——那里有等宽的完整原文。
    """
    total = command_length(command)
    if total <= COMMAND_DISPLAY_MAX:
        return ""
    return (f"命令共 {total} 字，只显示前 {COMMAND_DISPLAY_MAX} 字，"
            f"尾部未显示；完整命令在「这是什么」里")


def command_length(command: str) -> int:
    """命令的字数。**唯一来源**——播报、卡片、审计都用这一个数。

    老实现两处各算一遍，算法还不一样：播报那边数的是**渲染后的一行**
    （含【严重】前缀、定位尾巴，而命令本身早已被截到 80 字），
    审批那边数的是完整命令。同一条命令于是有两个字数
    （真机实测：播报 120 字 vs 审批 268 字）。
    用户是拿这个数字判断"我有没有看全"的——两个数字等于没有数字。
    """
    return len(" ".join(str(command or "").split()))


def first_banned(text: str) -> str | None:
    """返回第一个撞到的禁用词；没撞到返回 None。渲染与测试都用它自检。"""
    for phrase in (*BANNED_PHRASES, *BANNED_INTERNAL):
        if phrase in text:
            return phrase
    return None
