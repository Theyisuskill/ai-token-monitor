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

Dedup key: the rollout filename plus a hash of its cumulative token counters.
Codex can re-emit a token_count with a new timestamp or rate_limits but the
same cumulative usage; those events must not charge last_token_usage twice.
Old events without cumulative usage retain the raw-line hash fallback.
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


def _counts(usage: dict) -> tuple[int, int, int] | None:
    result = []
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        raw = usage.get(key, 0)
        if raw is None:
            raw = 0
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        if value < 0:
            return None
        result.append(value)
    if result[1] > result[0]:
        return None
    return tuple(result)


def usage_keys(path: Path, line: str, info: dict) -> tuple[str, str]:
    """Legacy and current keys; independent of timestamps, quota updates or model."""
    digest = hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:24]
    legacy = f"{path.stem}:{digest}"
    total = info.get("total_token_usage")
    counters = _counts(total) if isinstance(total, dict) else None
    if counters is None or counters[0] + counters[2] <= 0:
        return legacy, legacy
    digest = hashlib.sha256(json.dumps(counters).encode()).hexdigest()[:32]
    return legacy, f"{path.stem}:total:{digest}"


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

    def dedup_migration(self, path: Path, lines: Iterable[str]) -> Iterator[tuple[str, str]]:
        # Only records accepted by the parser can correspond to stored usage.
        # Parsing a full file also restores model context before tailing resumes.
        for line in lines:
            line = line.rstrip("\r\n")
            if not list(self.parse(path, (line,))):
                continue
            info = json.loads(line)["payload"]["info"]
            old, new = usage_keys(path, line, info)
            if old != new:
                yield old, new

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

            counters = _counts(usage)
            if counters is None:
                continue
            raw_input, cached, output = counters
            if raw_input + output <= 0:
                continue

            _legacy, dedup_key = usage_keys(path, line, info)
            yield UsageRecord(
                tool=self.name,
                model=self._models.get(key, FALLBACK_MODEL),
                ts=ts,
                input_tokens=max(raw_input - cached, 0),
                output_tokens=output,
                cache_read_tokens=cached,
                cache_write_tokens=0,
                session_id=path.stem,
                dedup_key=dedup_key,
            )
