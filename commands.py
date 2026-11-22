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
# 只认**单短线**开关：`-rf`、`-r`。`--force` 不算——老实现靠 `_RM_RE` 的
# `-[a-zA-Z]+` 隐式排掉了它，改成按 token 判之后必须显式排，
# 否则 `--force` 会被拆成字母 `force`，而 `force` 里含着 `r`，
# 于是"非递归"被误判成"递归"。（整条长选项剥离是 B3 的活，这里只保证不退化。）
_SHORT_FLAG_RE = re.compile(r"^-[A-Za-z]+$")
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:\\?$")


@dataclass(frozen=True)
class _RmFacts:
    recursive: bool
    target: str
    is_disk_root: bool


def _parse_rm(command: str) -> _RmFacts | None:
    """找 `rm` **作为命令**的那一段。

    必须在命令位置上：`git commit -m "rm -rf x"` 里的 rm 是字符串，
    不是要执行的东西。老实现用 `\\brm\\s+` 全文搜，于是那句话被判成
    "删除目录及其全部内容"——闸门犯过同一类错（帮助豁免只看段首），
    两套判据各自错得一样，正是 B2 要消灭的"两套判据"。
    现在和闸门一样：先归一化，再看命令名。

    开关字母只从单短线 token 取（见 ``_SHORT_FLAG_RE``）。
    """
    for segment in _segments(command):
        body = _normalise_pairs(segment)
        if not body or basename(body[0][1]) != "rm":
            continue
        shorts = [norm for _, norm in body[1:] if _SHORT_FLAG_RE.match(norm)]
        letters = "".join(_FLAG_RE.findall(" ".join(shorts))).lower()
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
    """把一条命令翻译成事实。认不出来就明说，不猜。"""
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


_SYSTEM_DIRS = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib",
)


def _is_system_path(target: str) -> bool:
    lowered = target.lower().replace("/", "\\")
    return any(lowered.startswith(prefix.replace("/", "\\"))
               for prefix in _SYSTEM_DIRS)


def _under(path: str, root: str) -> bool:
    if not root:
        return False
    normal_root = root.replace("/", "\\").rstrip("\\").lower()
    normal_path = path.replace("/", "\\").lower()
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
_UNPARSEABLE_RE = re.compile(r"[$`*?\[\]%]")


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
    """每段是 ``[(原文, 归一化), …]``。原文给展示层，归一化给判定层。"""
    out: list[list[tuple[str, str]]] = []
    for part in _SIMPLE_SPLIT.split(str(command or "")):
        pairs = [(raw, normalise_token(raw)) for raw in _raw_tokens(part)]
        if pairs:
            out.append(pairs)
    return out


def simple_commands(command: str) -> list[list[str]]:
    """按 `;` `|` `&` 换行切段，每段再切 token **并剥掉段首噪音**。

    段内判定互不影响。返回的就是"命令主体"token 列表——
    ``VAR="a b" sudo rm -rf x`` 出来是 ``['rm', '-rf', 'x']``。

    **判定一律走这个入口，不要自己 ``normalise_tokens(simple_commands(…))``**：
    那样拿到的是已经丢过引号的 token，引号配对就断了，
    ``VAR="a b" rm -rf x`` 会被吃成空段（实测踩过）。
    """
    return [[norm for _, norm in _normalise_pairs(segment)]
            for segment in _segments(command)]


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
    quote = ""
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


def delete_targets(command: str) -> tuple[str, ...]:
    """这条命令**要动哪些路径**。抽不出返回空元组。

    只取命令主体之后、非开关、非赋值的 token；``cd`` / ``export`` 这类
    只改 shell 自身状态的段整段跳过（见 ``_NAVIGATION_COMMANDS``）——
    `rm -rf a b` 要能取到 `a` 和 `b` 两个，漏一个就可能把档位判低。

    **刻意不用"停用词"过滤**：曾经用 `a` / `the` / `in` 之类筛掉"不像路径的
    token"，结果 `rm -rf a b` 只取到 `b`——单字母目录名被当成英文冠词丢掉了。
    丢目标只会让档位变低，那是最危险的方向；多取一个再按 ``_looks_like_path``
    筛，方向才对。

    返回的是**原文**（不是归一化后的）：路径要显示给用户看，
    ``D:/other/x.txt`` 不能变成 ``d:/other/x.txt``。
    """
    found: list[str] = []
    for segment in _segments(command):
        found.extend(_segment_targets(segment))
    return tuple(found)


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


def target_unparseable(command: str) -> bool:
    """目标集合算不算得出来。算不出来 → 调用方必须按最坏的档处理。"""
    return bool(_UNPARSEABLE_RE.search(str(command or "")))


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
    if _is_home_or_root(target) or _is_system_path(target):
        return TIER_EXTREME
    if not working_directory:
        # B8：**拿不到工作目录 ≠ 最坏**。老实现这里返回极高危，于是转录里
        # 每条缺 cwd 的删除都顶【严重】，而审批卡片（hook payload 带 cwd）
        # 对同一条命令说 notice——两边说的不是一件事，用户只能不信其中一边。
        # 改成「范围未知」档：知道要删什么，不知道它在不在工作区外。
        return TIER_SCOPE_UNKNOWN
    if not _is_absolute(target):
        return TIER_NORMAL      # 相对路径：相对于会话工作目录，算在内
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



def scope_line(facts: CommandFacts, working_directory: str | None) -> str:
    """第三行：影响范围。只讲事实——在工作目录内还是外、是不是系统目录。

    **有明确目标时，按目标推**：规格 P3 要求这一行说清"是否在工作目录外"，
    而词条自带的 scope（"整个磁盘" / "这张表"）答不了这个问题。
    只有拿不到目标时才退回词条自带的说法。
    """
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


def card_lines(*, command: str, working_directory: str | None) -> dict[str, Any]:
    """主卡正文的前三行（规格 P3）。第四行结论由调用方填——词典不给结论。"""
    facts = translate(command)
    action = facts.action
    if facts.targets:
        target = facts.targets[0]
        action = f"{facts.action} {target}" if target not in facts.action else facts.action
    return {
        "action": f"⚠️ 它要{action}" if facts.known else f"⚠️ {action}",
        "consequence": f"后果：{facts.consequence}",
        "scope": f"范围：{scope_line(facts, working_directory)}",
        "known": facts.known,
        "irreversible": facts.irreversible,
    }


def command_display_note(command: str) -> str:
    """命令超过卡片展示上限时的那一行提示。没超就返回空串。

    **这一行是安全件，不是装饰**：卡片只显示前 ``COMMAND_DISPLAY_MAX``
    字，而风险判定读的是全文。不告诉用户"你没看全"，就等于让他在
    信息不全的情况下点"允许"。所以超长时必须明说，并把全文指向
    「这是什么」——那里有等宽的完整原文。
    """
    total = len(" ".join(str(command or "").split()))
    if total <= COMMAND_DISPLAY_MAX:
        return ""
    return (f"命令共 {total} 字，只显示前 {COMMAND_DISPLAY_MAX} 字，"
            f"尾部未显示；完整命令在「这是什么」里")


def first_banned(text: str) -> str | None:
    """返回第一个撞到的禁用词；没撞到返回 None。渲染与测试都用它自检。"""
    for phrase in (*BANNED_PHRASES, *BANNED_INTERNAL):
        if phrase in text:
            return phrase
    return None
