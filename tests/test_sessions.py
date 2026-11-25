"""会话清单单测（2026-11-25）。

重点盯三件事：
1. **``live`` 只表示"最近有活动"**，且必须由传入的 ``now`` 决定——
   测试里时钟是假的，不许摸真时钟。
2. **转录枚举与网关枚举语义一致**：都能说出"有几个、几个在动"。
3. **坏文件/坏库不拖垮整盘家底**：一个 stat 失败、一个 sqlite 错误，
   都只该损失那一个源。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plugin.plugins.agent_deskmate.adapters.sessions import (
    LIVE_WINDOW,
    AgentSessions,
    SessionGlance,
    describe,
    gateway_app_types,
    glance_gateway_sessions,
    glance_transcript_sessions,
    group_by_agent,
)

NOW = 1_800_000_000.0  # 固定假时钟，测试不碰真时间


def _mk_jsonl(path: Path, *, size: int = 100, age: float) -> None:
    """造一个指定大小的转录文件，mtime 设为 ``NOW - age``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = NOW - age
    import os
    os.utime(path, (stamp, stamp))


# ── 转录侧 ───────────────────────────────────────────────────────────────


def test_empty_dir_returns_nothing(tmp_path: Path) -> None:
    assert glance_transcript_sessions(tmp_path, now=NOW) == []


def test_missing_dir_returns_nothing(tmp_path: Path) -> None:
    assert glance_transcript_sessions(tmp_path / "nope", now=NOW) == []


def test_live_when_mtime_within_window(tmp_path: Path) -> None:
    _mk_jsonl(tmp_path / "proj" / "aaaa1111-2222.jsonl", age=30)
    got = glance_transcript_sessions(tmp_path, now=NOW)
    assert len(got) == 1
    assert got[0].live is True
    assert got[0].session == "aaaa1111-2222"
    assert got[0].where == "proj"


def test_not_live_when_mtime_older_than_window(tmp_path: Path) -> None:
    _mk_jsonl(tmp_path / "proj" / "old-sess.jsonl", age=LIVE_WINDOW + 60)
    got = glance_transcript_sessions(tmp_path, now=NOW)
    assert got[0].live is False


def test_window_boundary_is_inclusive(tmp_path: Path) -> None:
    """正好踩在阈值上算活的——边界不该靠"差一点点"决定。"""
    _mk_jsonl(tmp_path / "proj" / "edge.jsonl", age=LIVE_WINDOW)
    got = glance_transcript_sessions(tmp_path, now=NOW)
    assert got[0].live is True


def test_sorted_by_recency_not_name(tmp_path: Path) -> None:
    _mk_jsonl(tmp_path / "p" / "zzz-new.jsonl", age=5)
    _mk_jsonl(tmp_path / "p" / "aaa-old.jsonl", age=5000)
    got = glance_transcript_sessions(tmp_path, now=NOW)
    assert [g.session for g in got] == ["zzz-new", "aaa-old"]


def test_size_and_where_reported(tmp_path: Path) -> None:
    _mk_jsonl(tmp_path / "D--workspace-x" / "s1.jsonl", size=4096, age=1)
    got = glance_transcript_sessions(tmp_path, now=NOW)
    assert got[0].size_bytes == 4096
    assert got[0].where == "D--workspace-x"


def test_limit_truncates(tmp_path: Path) -> None:
    for i in range(5):
        _mk_jsonl(tmp_path / "p" / f"s{i}.jsonl", age=i + 1)
    got = glance_transcript_sessions(tmp_path, now=NOW, limit=3)
    assert len(got) == 3
    # 限了 3 条就该是最近的 3 条
    assert got[0].session == "s0"


def test_unreadable_file_does_not_kill_the_scan(tmp_path: Path, monkey_path=None) -> None:
    """一个文件 stat 失败，只损失它自己。"""
    good = tmp_path / "p" / "good.jsonl"
    _mk_jsonl(good, age=1)
    bad = tmp_path / "p" / "bad.jsonl"
    _mk_jsonl(bad, age=2)

    real_stat = Path.stat

    def flaky_stat(self: Path, *a, **kw):  # noqa: ANN001
        if self.name == "bad.jsonl":
            raise OSError("文件被锁")
        return real_stat(self, *a, **kw)

    mp = pytest.MonkeyPatch()
    mp.setattr(Path, "stat", flaky_stat)
    try:
        got = glance_transcript_sessions(tmp_path, now=NOW)
    finally:
        mp.undo()
    assert [g.session for g in got] == ["good"]


def test_custom_live_window_honoured(tmp_path: Path) -> None:
    _mk_jsonl(tmp_path / "p" / "s.jsonl", age=120)
    # 默认 300s 下它是活的；把窗口缩到 60s 它就该是死的
    assert glance_transcript_sessions(tmp_path, now=NOW)[0].live is True
    assert glance_transcript_sessions(
        tmp_path, now=NOW, live_window=60)[0].live is False


# ── 网关侧 ───────────────────────────────────────────────────────────────


def _mk_gateway(db: Path, rows: list[tuple[str, float]]) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE proxy_request_logs ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "created_at REAL, app_type TEXT)")
    conn.executemany(
        "INSERT INTO proxy_request_logs (session_id, created_at, app_type) "
        "VALUES (?, ?, 'claude')", rows)
    conn.commit()
    conn.close()


def test_gateway_missing_db_returns_nothing(tmp_path: Path) -> None:
    assert glance_gateway_sessions(tmp_path / "nope.db", now=NOW) == []


def test_gateway_groups_by_session(tmp_path: Path) -> None:
    db = tmp_path / "gw.db"
    _mk_gateway(db, [
        ("sess-a", NOW - 10),
        ("sess-a", NOW - 5),          # 同一会话两次，取更近的
        ("sess-b", NOW - 1000),
    ])
    got = glance_gateway_sessions(db, now=NOW, app_type="claude")
    by_id = {g.session: g for g in got}
    assert set(by_id) == {"sess-a", "sess-b"}
    assert by_id["sess-a"].last_seen == NOW - 5
    assert by_id["sess-a"].live is True
    assert by_id["sess-b"].live is False


def test_gateway_skips_blank_session_ids(tmp_path: Path) -> None:
    db = tmp_path / "gw.db"
    _mk_gateway(db, [("", NOW - 1), (None, NOW - 2), ("real", NOW - 3)])
    got = glance_gateway_sessions(db, now=NOW, app_type="claude")
    assert [g.session for g in got] == ["real"]


def test_gateway_respects_lookback(tmp_path: Path) -> None:
    db = tmp_path / "gw.db"
    _mk_gateway(db, [("recent", NOW - 60), ("ancient", NOW - 10 * 24 * 3600)])
    got = glance_gateway_sessions(db, now=NOW, lookback=24 * 3600,
                                  app_type="claude")
    assert [g.session for g in got] == ["recent"]


def test_gateway_raises_on_broken_db(tmp_path: Path) -> None:
    """表不存在就该抛 ``sqlite3.Error``——调用方要能按"这个源盘不了"处理。"""
    db = tmp_path / "gw.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.Error):
        glance_gateway_sessions(db, now=NOW)


# ── app_type 过滤：真机截图逼出来的那条 ──────────────────────────────────


def test_gateway_filters_by_app_type(tmp_path: Path) -> None:
    """**不过滤就会把 Claude 的会话列到 Codex 名下。**

    实测本机：网关里排第一的会话是 Claude Code 的（706 次请求），
    Codex 只有 3 次。不过滤的话给 Codex 盘家底，盘出来的是 Claude。
    """
    db = tmp_path / "gw.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE proxy_request_logs ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "created_at REAL, app_type TEXT)")
    conn.executemany(
        "INSERT INTO proxy_request_logs (session_id, created_at, app_type) "
        "VALUES (?, ?, ?)",
        [("claude-sess", NOW - 5, "claude"),
         ("claude-sess", NOW - 4, "claude"),
         ("codex-sess", NOW - 6, "codex")])
    conn.commit()
    conn.close()

    codex = glance_gateway_sessions(db, agent="codex", app_type="codex", now=NOW)
    assert [g.session for g in codex] == ["codex-sess"]

    claude = glance_gateway_sessions(db, agent="claude", app_type="claude", now=NOW)
    assert [g.session for g in claude] == ["claude-sess"]
    assert claude[0].where == "2 次请求"


def test_gateway_app_types_counts_each(tmp_path: Path) -> None:
    """本机实况：mcode 37816 / claude 13268 / dsh 7221 / codex 160。
    dsh 早先被误判成 blind，就是因为我们没这个函数。
    """
    db = tmp_path / "gw.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE proxy_request_logs ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "created_at REAL, app_type TEXT)")
    conn.executemany(
        "INSERT INTO proxy_request_logs (session_id, created_at, app_type) "
        "VALUES (?, ?, ?)",
        [("a", NOW - 1, "dsh"), ("b", NOW - 2, "dsh"),
         ("c", NOW - 3, "claude"), ("", NOW - 4, "codex"),
         (None, NOW - 5, "")])
    conn.commit()
    conn.close()

    got = gateway_app_types(db, now=NOW)
    # 注意：这里数的是**请求**，不管 session_id 空不空——
    # 那条 app_type='codex'、session_id 为空的行仍然是一次 codex 请求，
    # 网关确实收到了它。会话级的过滤是 glance_gateway_sessions 的事，
    # 两个函数职责不同，别混。
    assert got == {"dsh": 2, "claude": 1, "codex": 1}
    # app_type 为空/NULL 的不算一个 agent
    assert "" not in got


def test_gateway_app_types_respects_lookback(tmp_path: Path) -> None:
    db = tmp_path / "gw.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE proxy_request_logs ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "created_at REAL, app_type TEXT)")
    conn.executemany(
        "INSERT INTO proxy_request_logs (session_id, created_at, app_type) "
        "VALUES (?, ?, ?)",
        [("a", NOW - 60, "codex"),
         ("b", NOW - 10 * 24 * 3600, "mcode")])
    conn.commit()
    conn.close()
    got = gateway_app_types(db, now=NOW, lookback=24 * 3600)
    assert got == {"codex": 1}


def test_gateway_app_types_missing_db_is_empty(tmp_path: Path) -> None:
    assert gateway_app_types(tmp_path / "nope.db", now=NOW) == {}


# ── subagents 不是会话 ───────────────────────────────────────────────────


def test_subagent_transcripts_are_not_sessions(tmp_path: Path) -> None:
    """``<session>/subagents/agent-aa*.jsonl`` 是子 agent 运行记录。

    实测本机：4 个子 agent 文件 vs 30 个真会话文件。不排除的话
    会话数虚高，而且它们共享 ``agent-aa`` 前缀，短显示下像同一个会话重复出现。
    """
    _mk_jsonl(tmp_path / "proj" / "real-session.jsonl", age=10)
    _mk_jsonl(tmp_path / "proj" / "real-session" / "subagents" / "agent-aa123.jsonl",
              age=5)
    _mk_jsonl(tmp_path / "proj" / "real-session" / "subagents" / "agent-aa456.jsonl",
              age=6)
    got = glance_transcript_sessions(tmp_path, now=NOW)
    assert [g.session for g in got] == ["real-session"]


# ── 汇总 ─────────────────────────────────────────────────────────────────


def test_group_by_agent_counts_live() -> None:
    grouped = group_by_agent([
        SessionGlance(agent="claude", session="a", last_seen=NOW, live=True),
        SessionGlance(agent="claude", session="b", last_seen=NOW - 9999, live=False),
        SessionGlance(agent="codex", session="c", last_seen=NOW, live=True),
    ])
    assert grouped["claude"].total == 2
    assert grouped["claude"].live == 1
    assert grouped["codex"].total == 1
    assert grouped["codex"].live == 1


def test_as_dict_shape() -> None:
    row = AgentSessions(agent="claude", sessions=[
        SessionGlance(agent="claude", session="s", last_seen=NOW, live=True),
    ]).as_dict()
    assert row["agent"] == "claude"
    assert row["total"] == 1
    assert row["live"] == 1
    assert row["sessions"][0]["session"] == "s"
    assert row["sessions"][0]["live"] is True


def test_describe_mentions_counts() -> None:
    text = describe([
        SessionGlance(agent="claude", session="a", last_seen=NOW, live=True),
        SessionGlance(agent="claude", session="b", last_seen=0.0, live=False),
    ])
    assert "2" in text and "1" in text


def test_describe_empty() -> None:
    assert describe([]) == "没有会话"


# ── 隐私：这一层不许碰内容 ────────────────────────────────────────────────


def test_no_content_is_ever_read() -> None:
    """``sessions.py`` 只许 stat 和聚合查询，不许 open 任何会话文件。"""
    import inspect

    from plugin.plugins.agent_deskmate.adapters import sessions
    source = inspect.getsource(sessions)
    for banned in ("open(", "read_text", "read_bytes", ".readlines("):
        assert banned not in source, f"出现了 {banned}——这一层不该读内容"
