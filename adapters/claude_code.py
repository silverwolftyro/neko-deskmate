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


class ClaudeCodeAdapter:
    """一次会话一个 offset 的 tail 器。offset 存在内存里，重启后从末尾开始。"""

    def __init__(self, projects_dir: Any) -> None:
        self.projects_dir = Path(projects_dir)
        self._offsets: dict[str, int] = {}

    def available(self) -> bool:
        return self.projects_dir.is_dir()

    def poll(self, max_files: int = 20) -> list[UnifiedEvent]:
        """扫一遍所有转录文件，返回新增事件（按文件从新到旧，最多 max_files 个）。"""
        events: list[UnifiedEvent] = []
        for path in iter_transcript_files(self.projects_dir)[:max_files]:
            key = str(path)
            offset = self._offsets.get(key, 0)
            if offset == 0:
                # 第一次见到这个文件：从当前末尾开始，不要把历史全量倒进来
                offset = path.stat().st_size
                self._offsets[key] = offset
                continue
            new_events, new_offset = read_new_transcript_events(path, offset)
            self._offsets[key] = new_offset
            events.extend(new_events)
        return events
