"""SQLite persistence: usage history, dedup, and per-file tail offsets.

Single-threaded by design — everything runs on the GLib main loop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date
from pathlib import Path

from .models import UsageRecord

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id                 INTEGER PRIMARY KEY,
    ts                 REAL    NOT NULL,
    tool               TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    session_id         TEXT    NOT NULL DEFAULT '',
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL    NOT NULL DEFAULT 0,
    dedup_key          TEXT    NOT NULL,
    UNIQUE (tool, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage (ts);
CREATE INDEX IF NOT EXISTS idx_usage_tool_ts ON usage (tool, ts);

CREATE TABLE IF NOT EXISTS file_state (
    path   TEXT PRIMARY KEY,
    inode  INTEGER NOT NULL,
    offset INTEGER NOT NULL
);
"""


def period_start(period: str) -> float:
    """Epoch marking the start of a reporting window.

    '5h' and 'week' are *rolling* windows because they mirror provider rate
    limits: Claude Code and Antigravity both reset the 5-hour session and the
    weekly cap on a rolling basis anchored to first use, NOT on a calendar
    boundary — the weekly limit in particular does not reset on Mondays. A
    trailing window is the honest approximation for a "how much have I burned
    recently" gauge and never under-reports the way a since-Monday window does
    at the start of the week.

    'today' and 'month' stay calendar-aligned — they are cost-tracking
    conveniences, not rate-limit mirrors. They deliberately go through
    time.mktime() on a naive local date rather than datetime.now().astimezone():
    astimezone() freezes *today's* UTC offset, so arithmetic that lands the
    boundary on the other side of a DST transition (e.g. the 1st of the month)
    is off by the DST delta. mktime() re-resolves the correct offset for
    whatever date it's given.
    """
    if period == "5h":
        return time.time() - 5.0 * 3600.0
    if period == "week":  # rolling 7 days (Claude/Antigravity reset, not Monday)
        return time.time() - 7.0 * 24.0 * 3600.0
    today = date.today()
    if period == "today":
        day = today
    elif period == "month":
        day = today.replace(day=1)
    else:
        return 0.0  # "all"
    return time.mktime(day.timetuple())


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- usage ---------------------------------------------------------------

    def add(self, records: list[UsageRecord]) -> int:
        """Insert records, silently skipping duplicates. Returns rows added."""
        if not records:
            return 0
        cur = self._db.executemany(
            """INSERT OR IGNORE INTO usage
               (ts, tool, model, session_id, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, cost_usd, dedup_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (r.ts, r.tool, r.model, r.session_id, r.input_tokens,
                 r.output_tokens, r.cache_read_tokens, r.cache_write_tokens,
                 r.cost_usd, r.dedup_key)
                for r in records
            ],
        )
        self._db.commit()
        return cur.rowcount

    def summary(self, since: float) -> dict:
        """Aggregate usage since an epoch, grouped by tool, plus totals."""
        rows = self._db.execute(
            """SELECT tool,
                      COALESCE(SUM(input_tokens), 0),
                      COALESCE(SUM(output_tokens), 0),
                      COALESCE(SUM(cache_read_tokens), 0),
                      COALESCE(SUM(cache_write_tokens), 0),
                      COALESCE(SUM(cost_usd), 0),
                      COUNT(*)
               FROM usage WHERE ts >= ?
               GROUP BY tool ORDER BY SUM(cost_usd) DESC""",
            (since,),
        ).fetchall()

        tools = []
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                  "cache_write_tokens": 0, "total_tokens": 0,
                  "cost_usd": 0.0, "requests": 0}
        for tool, inp, out, cr, cw, cost, count in rows:
            entry = {
                "tool": tool,
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_tokens": cr,
                "cache_write_tokens": cw,
                "total_tokens": inp + out + cr + cw,
                "cost_usd": round(cost, 4),
                "requests": count,
            }
            tools.append(entry)
            for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                        "cache_write_tokens", "total_tokens", "requests"):
                totals[key] += entry[key]
            totals["cost_usd"] += cost
        totals["cost_usd"] = round(totals["cost_usd"], 4)
        return {"since": since, "tools": tools, "totals": totals}

    def tools_seen(self) -> list[str]:
        """Every tool with at least one usage record, alphabetically.

        Drives the dynamic tool list in the UI: only tools the user actually
        uses get a section, regardless of which adapters are enabled.
        """
        rows = self._db.execute(
            "SELECT DISTINCT tool FROM usage ORDER BY tool").fetchall()
        return [row[0] for row in rows]

    def peak_window(self, tool: str, span: float,
                    lookback_days: int = 60) -> float:
        """Largest summed cost_usd inside any rolling ``span``-second window
        for ``tool`` over the last ``lookback_days``.

        Used by the 'auto' budget mode to self-calibrate a limit to the user's
        own busiest window. O(n) sliding window over that tool's recent rows;
        the lookback bounds the work and keeps the calibration current.
        """
        since = time.time() - lookback_days * 86400.0
        rows = self._db.execute(
            "SELECT ts, cost_usd FROM usage WHERE tool = ? AND ts >= ? ORDER BY ts",
            (tool, since),
        ).fetchall()
        best = 0.0
        run = 0.0
        head = 0
        for ts_i, cost_i in rows:
            run += cost_i
            while ts_i - rows[head][0] > span:
                run -= rows[head][1]
                head += 1
            if run > best:
                best = run
        return best

    def daily_series(self, since: float) -> list[dict]:
        """Per-day totals (local time) — for future charting/CLI queries."""
        rows = self._db.execute(
            """SELECT date(ts, 'unixepoch', 'localtime') AS day,
                      COALESCE(SUM(input_tokens + output_tokens +
                                   cache_read_tokens + cache_write_tokens), 0),
                      COALESCE(SUM(cost_usd), 0)
               FROM usage WHERE ts >= ?
               GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()
        return [
            {"day": day, "total_tokens": tokens, "cost_usd": round(cost, 4)}
            for day, tokens, cost in rows
        ]

    # -- tail offsets ----------------------------------------------------------

    def get_file_state(self, path: str) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT inode, offset FROM file_state WHERE path = ?", (path,)
        ).fetchone()
        return (row[0], row[1]) if row else (-1, 0)

    def set_file_state(self, path: str, inode: int, offset: int) -> None:
        self._db.execute(
            """INSERT INTO file_state (path, inode, offset) VALUES (?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET inode = excluded.inode,
                                               offset = excluded.offset""",
            (path, inode, offset),
        )
        self._db.commit()
