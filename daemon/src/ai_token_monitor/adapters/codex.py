"""OpenAI Codex CLI adapter.

Codex CLI records each session as a rollout file under
``~/.codex/sessions/YYYY/MM/DD/rollout-<date>-<uuid>.jsonl``. Every line is an
envelope ``{"timestamp": ..., "type": ..., "payload": {...}}``. Token usage
arrives on ``type == "event_msg"`` entries whose payload is a token count:

    {
      "timestamp": "2026-07-01T12:00:05.000Z",
      "type": "event_msg",
      "payload": {
        "type": "token_count",
        "info": {
          "total_token_usage": {...cumulative...},
          "last_token_usage": {
            "input_tokens": 2168,
            "cached_input_tokens": 2048,
            "output_tokens": 510,
            "reasoning_output_tokens": 320,
            "total_tokens": 2678
          }
        }
      }
    }

``last_token_usage`` is the per-turn delta (``total_token_usage`` is
cumulative, so summing it would double-count). ``cached_input_tokens`` is a
subset of ``input_tokens``; ``reasoning_output_tokens`` a subset of
``output_tokens``. The model comes from the surrounding ``turn_context``
entries, remembered per file since a tail chunk may not include one.

Dedup key: hash of the raw line (the cumulative counter inside makes lines
unique within a session) plus the rollout filename.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..models import UsageRecord, iso_to_epoch
from . import register
from .base import Adapter

log = logging.getLogger(__name__)

FALLBACK_MODEL = "gpt-5-codex"


@register
class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, settings):
        super().__init__(settings)
        # Last model seen in each file's turn_context; survives across tail
        # chunks for the daemon's lifetime (a restart just falls back until
        # the next turn_context line).
        self._models: dict[str, str] = {}

    def roots(self) -> list[Path]:
        return [Path(self.settings.get("root", "~/.codex/sessions")).expanduser()]

    def matches(self, path: Path) -> bool:
        return path.suffix == ".jsonl" and path.name.startswith("rollout-")

    def parse(self, path: Path, lines: Iterable[str]) -> Iterator[UsageRecord]:
        key = str(path)
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue

            kind = entry.get("type")
            if kind == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model:
                    self._models[key] = model
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
                continue

            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue

            try:
                ts = iso_to_epoch(entry["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue

            raw_input = int(usage.get("input_tokens") or 0)
            cached = int(usage.get("cached_input_tokens") or 0)
            output = int(usage.get("output_tokens") or 0)
            if raw_input + cached + output <= 0:
                continue

            digest = hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()
            yield UsageRecord(
                tool=self.name,
                model=self._models.get(key, FALLBACK_MODEL),
                ts=ts,
                input_tokens=max(raw_input - cached, 0),
                output_tokens=output,
                cache_read_tokens=cached,
                cache_write_tokens=0,
                session_id=path.stem,
                dedup_key=f"{path.stem}:{digest[:24]}",
            )
