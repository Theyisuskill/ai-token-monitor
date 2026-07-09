"""Cursor adapter.

Cursor local usage can typically be found in its global storage SQLite
database or in telemetry logs depending on the exact configuration and version.
For this example, we assume we are parsing local JSONL logs if they exist.

TODO: Determine the exact path and structure of Cursor's offline token
usage logs. This is a skeleton based on standard JSONL parsing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..models import UsageRecord, iso_to_epoch
from . import register
from .base import Adapter

log = logging.getLogger(__name__)


@register
class CursorAdapter(Adapter):
    name = "cursor"

    def roots(self) -> list[Path]:
        # Typically ~/.config/Cursor/logs/..., adjust as needed
        return [Path(self.settings.get("root", "~/.config/Cursor/logs")).expanduser()]

    def matches(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def parse(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue

            # This is a hypothetical structure for Cursor logs
            if not isinstance(entry, dict) or entry.get("event") != "completion":
                continue

            usage = entry.get("usage") or {}
            model = entry.get("model") or "unknown"
            if not usage:
                continue

            # Need a reliable deduplication key per request
            req_id = entry.get("requestId")
            if not req_id:
                continue

            try:
                ts = iso_to_epoch(entry.get("timestamp", ""))
            except (KeyError, TypeError, ValueError):
                continue

            yield UsageRecord(
                tool=self.name,
                model=model,
                ts=ts,
                input_tokens=int(usage.get("promptTokens") or 0),
                output_tokens=int(usage.get("completionTokens") or 0),
                session_id=entry.get("sessionId") or path.stem,
                dedup_key=f"cursor:{req_id}",
            )
