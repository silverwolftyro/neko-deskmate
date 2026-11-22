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
_RM_RE = re.compile(r"\brm\s+(?P<flags>(?:-[a-zA-Z]+\s+)*)(?P<rest>.+)$")
_FLAG_RE = re.compile(r"-([a-zA-Z]+)")
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:\\?$")


@dataclass(frozen=True)
class _RmFacts:
    recursive: bool
    target: str
    is_disk_root: bool


def _parse_rm(command: str) -> _RmFacts | None:
    match = _RM_RE.search(command)
    if match is None:
        return None
    letters = "".join(_FLAG_RE.findall(match.group("flags") or "")).lower()
    words = (match.group("rest") or "").split()
    target = words[0].strip("'\"") if words else ""
    bare = target.replace("/", "\\").rstrip("\\")
    is_disk_root = bool(_DRIVE_ROOT_RE.match(bare)) or bare in ("", "\\")
    return _RmFacts(recursive="r" in letters, target=target,
                    is_disk_root=is_disk_root)


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
