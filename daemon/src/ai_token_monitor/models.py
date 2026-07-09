"""Domain model shared by adapters, store and cost engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One billable API interaction, normalized across tools.

    ``dedup_key`` must be stable across re-reads of the same log line so the
    store can INSERT OR IGNORE when a file is reparsed from offset 0 (e.g.
    after inode rotation or a full rescan).
    """

    tool: str
    model: str
    ts: float  # epoch seconds, UTC
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    session_id: str = ""
    dedup_key: str = ""
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


def iso_to_epoch(value: str) -> float:
    """Parse an ISO-8601 timestamp (with or without trailing 'Z') to epoch.

    Raises ValueError for anything unparsable, including a non-string
    ``value`` (e.g. ``null`` or a bare number in a malformed log line) — this
    lets every caller share one narrow except clause instead of also having
    to catch AttributeError.
    """
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
