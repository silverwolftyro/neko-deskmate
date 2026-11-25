"""观测适配器：把纯解析函数绑到具体的数据源上。

拆分原则：``events.py`` 只放纯函数（好单测），这里只做 I/O 与状态（offset / rowid 水位）。
路径一律来自配置，代码里不出现硬编码的绝对路径（AGENTS.md 硬约束 6）。
"""

from __future__ import annotations

from .claude_code import (
    ClaudeCodeAdapter,
    iter_transcript_files,
    read_new_transcript_events,
)
from .discovery import AgentProbe, discover_agents, summarize
from .gateway import GatewayAdapter, read_new_gateway_events

__all__ = [
    "AgentProbe",
    "ClaudeCodeAdapter",
    "GatewayAdapter",
    "discover_agents",
    "iter_transcript_files",
    "read_new_gateway_events",
    "read_new_transcript_events",
    "summarize",
]
