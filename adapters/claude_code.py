"""Claude Code 转录 tail 适配器。

数据源：``~/.claude/projects/**/*.jsonl``（一行一条 JSON 记录）。
实测结构（2026-11-21）：17 种记录类型，我们只取三种信号，见 ``events.py`` 模块文档。

两个实现要点：
- **按字节 offset 增量读**，不是每次全量重读。转录文件会涨到数 MB。
- **只消费完整的行**：结尾残行留给下一次，否则半行 JSON 会被当成坏数据丢掉，
  而下一次从残行中间开始读，整行就永远对不上了。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..events import UnifiedEvent, parse_transcript_line


def iter_transcript_files(projects_dir: Any) -> list[Path]:
    """列出转录目录下所有 ``*.jsonl``，按修改时间从新到旧。"""
    root = Path(projects_dir)
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def read_new_transcript_events(path: Any, offset: int = 0) -> tuple[list[UnifiedEvent], int]:
    """从 ``offset`` 字节处读一个新片段，返回 ``(事件, 新 offset)``。

    文件被截断或轮转（``size < offset``）时从头读，避免卡死在旧位置。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return [], 0

    size = file_path.stat().st_size
    if offset > size:
        offset = 0

    # 必须按字节读：文本模式在 Windows 上会把 \r\n 归一成 \n，
    # 于是「读到的字符数」和「文件字节数」对不上，offset 每次都漂一截，
    # 几轮之后就从半行中间开始读，整行永远对不上。
    with file_path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()

    newline = chunk.rfind(b"\n")
    if newline == -1:
        return [], offset

    consumed = offset + newline + 1
    events: list[UnifiedEvent] = []
    for raw in chunk[:newline].split(b"\n"):
        events.extend(parse_transcript_line(raw.decode("utf-8", errors="replace")))
    return events, consumed


class CwdCache:
    """会话级 cwd 缓存（B8）。

    **先说实测**：当前版本 Claude Code 的 ``assistant`` 记录**100% 带 cwd**
    （本机 28 个转录文件、5053 条 assistant 记录，0 条缺失）。
    任务书 B8 记的"该行 has_cwd = False"在现版本上复现不出来——
    那个 4612/9008 是**所有记录类型**的比例（``attachment``/``user``/
    ``ai-title`` 也占一半），而产生事件的只有 ``assistant`` 与 ``system``。

    所以这道缓存**不是当前的主路径，是兜底**：它保证"真出现缺 cwd 的记录时"
    有一个说得过去的答案，而不是每次都对的红字。留着它的理由很便宜——
    三十行纯内存状态，换掉的是"缺一条线索就升级成【严重】"这个行为。

    规则：同一会话里**首个带 cwd 的记录**记下来，后续缺 cwd 的记录沿用。
    跨会话不借（会话之间没有理由共享目录）；只在内存里，不落盘。

    这是**推断**，不是事实：会话中途 ``cd`` 过，沿用的就是旧目录。
    所以它只用来把"完全没线索"提升到"有一条线索"，
    真沿不到（整条会话都没出现过 cwd）时档位落 ``scope_unknown``
    并由播报文案明说，而不是继续冒充【严重】。

    已知边界：适配器首次见到文件时只记水位、不解析历史（否则会把几个月的
    转录全量倒进来播一遍），所以缓存从**第一次读到的记录**开始热。
    """

    def __init__(self) -> None:
        self._by_session: dict[str, str] = {}

    def remember(self, session: str, cwd: str | None) -> None:
        """记下一条记录给出的 cwd。空值不记账（不许用 None 覆盖已知值）。"""
        text = str(cwd or "").strip()
        if session and text:
            self._by_session[session] = text

    def fill(self, events: list[UnifiedEvent]) -> list[UnifiedEvent]:
        """按顺序给这批事件补 cwd，返回新列表（不改原对象）。

        顺序很重要：必须先见到带 cwd 的那条，后面的才有的沿用。
        调用方按文件顺序喂进来即可（适配器本来就是顺序读的）。
        """
        filled: list[UnifiedEvent] = []
        for event in events:
            self.remember(event.session, event.working_directory)
            if event.working_directory or not event.session:
                filled.append(event)
                continue
            inherited = self._by_session.get(event.session)
            if not inherited:
                filled.append(event)
                continue
            filled.append(event.model_copy(update={
                "working_directory": inherited,
            }))
        return filled


class ClaudeCodeAdapter:
    """一次会话一个 offset 的 tail 器。offset 存在内存里，重启后从末尾开始。

    B4：**启动时已存在**的文件从末尾开始（不把几个月历史倒进来播），
    **运行期新出现**的文件从 0 开始读——否则新会话的头几条事件
    （含第一次删除）会被"首次见到就当历史"整段吃掉。
    """

    def __init__(self, projects_dir: Any) -> None:
        self.projects_dir = Path(projects_dir)
        self._offsets: dict[str, int] = {}
        # B8：跨轮次记住每个会话的 cwd。放在适配器里而不是全局——
        # 它是"这一批观测源"的状态，跟 offset 一样重启即失效。
        self._cwd = CwdCache()
        # B4：构造时目录里已经有的文件。用于区分"历史"与"刚刚产生"。
        self._seen_at_startup: frozenset[str] = frozenset(
            str(path) for path in iter_transcript_files(self.projects_dir))

    def available(self) -> bool:
        return self.projects_dir.is_dir()

    def poll(self, max_files: int = 20) -> list[UnifiedEvent]:
        """扫一遍所有转录文件，返回新增事件（按文件从新到旧，最多 max_files 个）。"""
        events: list[UnifiedEvent] = []
        for path in iter_transcript_files(self.projects_dir)[:max_files]:
            key = str(path)
            offset = self._offsets.get(key, 0)
            if offset == 0:
                # 第一次见到这个文件。分两种：
                # ① 构造时就在 → 历史，从当前末尾开始，不播；
                # ② 运行期新出现 → 这个会话是插件加载后才开的，
                #    从 0 开始读，否则它开头的事件全丢
                #    （老实现不区分，一律跳到末尾——新会话的首次删除就这么没了）。
                if key in self._seen_at_startup:
                    self._offsets[key] = path.stat().st_size
                    continue
                # 运行期新文件：offset 保持 0，落到下面的读取里
            new_events, new_offset = read_new_transcript_events(path, offset)
            self._offsets[key] = new_offset
            events.extend(new_events)
        # 补 cwd 放在**返回之前**：调用方（规则层、播报层）拿到的事件
        # 必须已经带上这条线索，否则每个调用方都要自己记得补一遍。
        return self._cwd.fill(events)
