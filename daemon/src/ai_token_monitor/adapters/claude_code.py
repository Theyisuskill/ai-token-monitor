"""Claude Code adapter.

Claude Code appends one JSON object per line to
``~/.claude/projects/<project-slug>/<session-uuid>.jsonl``. Token usage lives
on ``type == "assistant"`` entries:

    {
      "type": "assistant",
      "timestamp": "2026-06-03T23:05:03.867Z",
      "sessionId": "...",
      "requestId": "req_...",
      "message": {
        "id": "msg_...",
        "model": "claude-opus-4-7"
      },
      "usage": {
        "input_tokens": 6,
        "output_tokens": 299,
        "cache_read_input_tokens": 17258,
        "cache_creation_input_tokens": 10702
      }
    }

Dedup key: ``message.id + requestId`` (a streamed response can be logged more
than once with the same pair), falling back to the entry ``uuid``.
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
class ClaudeCodeAdapter(Adapter):
    name = "claude_code"

    def roots(self) -> list[Path]:
        return [Path(self.settings.get("root", "~/.claude/projects")).expanduser()]

    def matches(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def parse(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue

            message = entry.get("message") or {}
            
            # Support both root-level usage (modern) and nested message-level usage (legacy)
            usage = entry.get("usage") or message.get("usage") or {}
            
            # Support both root-level model and nested message-level model
            model = entry.get("model") or message.get("model") or ""
            
            if not usage or not model or model == "<synthetic>":
                continue

            msg_id = message.get("id")
            req_id = entry.get("requestId")
            dedup = f"{msg_id}:{req_id}" if msg_id and req_id else entry.get("uuid")
            if not dedup:
                continue

            try:
                ts = iso_to_epoch(entry["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue

            yield UsageRecord(
                tool=self.name,
                model=model,
                ts=ts,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                session_id=entry.get("sessionId") or path.stem,
                dedup_key=dedup,
            )
