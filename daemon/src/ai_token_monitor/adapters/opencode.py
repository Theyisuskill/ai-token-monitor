"""OpenCode adapter.

OpenCode (the terminal AI coding agent) stores everything in one SQLite
database at ``~/.local/share/opencode/opencode.db``. Per-turn usage lives on
the ``message`` table: each assistant turn is one row whose ``data`` column is
a JSON object carrying the model, the token counts and — usefully —
OpenCode's own computed cost:

    {
      "role": "assistant",
      "modelID": "minimax-m3",
      "providerID": "opencode-go",
      "cost": 0.00024418,
      "tokens": {
        "total": 10639, "input": 60, "output": 70, "reasoning": 0,
        "cache": {"write": 0, "read": 10509}
      },
      "time": {"created": 1781826586131, "completed": 1781826588208}
    }

``tokens.total`` == input + output + reasoning + cache.read + cache.write, so
reasoning is a *separate* bucket (not a subset of output) — it is folded into
output_tokens here, matching how the Gemini adapter treats "thoughts".

OpenCode is bring-your-own-key / pay-as-you-go across arbitrary providers
(``opencode-go``, minimax, openrouter, ...), so it has no subscription 5h/
weekly rate-limit window — the UI shows real spend rather than a % of an
invented budget. Because OpenCode already prices each turn against the live
provider rates, this adapter emits that ``cost`` verbatim; the daemon keeps an
adapter-supplied cost instead of re-pricing an unknown model to $0.

Like the Antigravity ``.db`` source, this adapter ignores the tailed ``lines``
and queries the database directly; ``dedup_key`` (the message id) makes a full
re-read idempotent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..models import UsageRecord
from . import register
from .base import Adapter

log = logging.getLogger(__name__)

DB_NAME = "opencode.db"


def _default_root() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return base / "opencode"


@register
class OpenCodeAdapter(Adapter):
    name = "opencode"

    def roots(self) -> list[Path]:
        configured = self.settings.get("root")
        root = Path(configured).expanduser() if configured else _default_root()
        return [root]

    def matches(self, path: Path) -> bool:
        # The WAL/SHM siblings (opencode.db-wal, -shm) carry a different suffix
        # and are skipped; the periodic rescan re-reads after a checkpoint.
        return path.name == DB_NAME

    def parse(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            log.warning("opencode: cannot open %s: %s", path, exc)
            return
        try:
            try:
                rows = conn.execute(
                    "SELECT id, session_id, time_created, data FROM message"
                ).fetchall()
            except sqlite3.Error as exc:
                log.warning("opencode: query failed on %s: %s", path, exc)
                return
        finally:
            conn.close()

        for msg_id, session_id, time_created, data in rows:
            rec = self._record(msg_id, session_id, time_created, data)
            if rec is not None:
                yield rec

    def _record(self, msg_id, session_id, time_created, data) -> UsageRecord | None:
        if not msg_id or not isinstance(data, str):
            return None
        try:
            entry = json.loads(data)
        except ValueError:
            return None
        if not isinstance(entry, dict) or entry.get("role") != "assistant":
            return None

        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

        input_tokens = int(tokens.get("input") or 0)
        output_tokens = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        cache_read = int(cache.get("read") or 0)
        cache_write = int(cache.get("write") or 0)
        if input_tokens + output_tokens + cache_read + cache_write <= 0:
            return None  # streaming placeholder or empty turn

        model = entry.get("modelID") or "opencode-unknown"

        # Timestamp: prefer the turn's own created time (ms), fall back to the
        # row's time_created (ms). Both are epoch milliseconds.
        ts_ms = None
        when = entry.get("time")
        if isinstance(when, dict):
            ts_ms = when.get("created") or when.get("completed")
        if ts_ms is None:
            ts_ms = time_created
        try:
            ts = float(ts_ms) / 1000.0
        except (TypeError, ValueError):
            return None

        # OpenCode already priced this turn against the live provider rates;
        # pass it through so the daemon keeps it instead of re-pricing an
        # unknown model to $0.
        try:
            cost = float(entry.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0

        return UsageRecord(
            tool=self.name,
            model=model,
            ts=ts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            session_id=str(session_id or ""),
            dedup_key=str(msg_id),
            cost_usd=cost,
        )
