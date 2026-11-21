"""高风险警示的命中计数（2026-11-22 真机反馈：常驻红字会被看腻）。

现象：每条审批请求都顶一整行红字「高风险动作：任何配置都不会让它自动放行…」。
用户原话——前几次有用，之后价值像反比函数一样暴跌。**看多了就等于没有**，
而真到该被叫住的那一刻，它也跟着被一起跳过。

所以红字按"同一类别第几次命中"递减，前几次显示整行红字，之后缩成一个徽标。
计数必须落在插件数据目录、**重启不丢**：否则每次重启都把用户打回"前三次"，
递减就白做了。

三条硬规则：

1. **计数失败不能影响卡片。** 读写出错一律当"第一次"（显示红字）——
   宁可多显示一次红字，不能因为一个 JSON 坏了让审批卡出不来。
2. **只计数，不判断。** 它回答"这一类第几次见了"，不回答"危不危险"。
3. **归类键由调用方给**（动作类别或风险档）。这里不做任何归类——
   它不知道什么叫"同一类"。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

COUNTS_FILENAME = "warning_counts.json"


def counts_path(data_dir: Any) -> Path:
    """计数文件的位置：跟着插件数据目录走，不写进源码目录。"""
    return Path(data_dir) / COUNTS_FILENAME


class WarningCounter:
    """按类别记命中次数。

    线程安全：审批服务是 ``ThreadingHTTPServer``，同一时刻可能有几条
    不同会话的请求在计数。
    """

    def __init__(self, path: Any = None) -> None:
        self._path: Path | None = Path(path) if path else None
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._loaded = False

    def _load(self) -> dict[str, int]:
        if self._loaded:
            return self._counts
        self._loaded = True
        if self._path is None:
            return self._counts
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 没有文件 / 文件坏了 / 不是 JSON：都当"从没计数过"。
            return self._counts
        if not isinstance(raw, dict):
            return self._counts
        counts: dict[str, int] = {}
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if int(value) > 0:
                counts[str(key)] = int(value)
        self._counts = counts
        return self._counts

    def seen(self, key: str) -> int:
        """这一类已经见过几次（**不含**这次）。读不到按 0。"""
        with self._lock:
            return int(self._load().get(str(key), 0))

    def bump(self, key: str) -> int:
        """记一次命中，返回**含这次**的次数。落盘失败也照样返回计数。"""
        with self._lock:
            counts = self._load()
            name = str(key)
            counts[name] = int(counts.get(name, 0)) + 1
            self._save(counts)
            return int(counts[name])

    def _save(self, counts: dict[str, int]) -> None:
        if self._path is None:
            return
        temp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=".warning-counts-",
                suffix=".tmp",
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(counts, stream, ensure_ascii=False)
            # 先写临时文件再替换：直接写原文件的话，写到一半崩掉会把
            # 历史计数一起毁掉，那会让递减从零开始。
            os.replace(temp_name, self._path)
        except OSError:
            # 落不了盘只意味着"重启后重新递减"，不影响这一次显示什么。
            pass
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
