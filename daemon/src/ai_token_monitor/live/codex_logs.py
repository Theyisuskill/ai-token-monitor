"""Provider-confirmed Codex quotas recorded in local rollout logs; no network."""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..models import iso_to_epoch


def normalize_event(entry: object) -> dict | None:
    if not isinstance(entry, dict) or entry.get("type") != "event_msg":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
        return None
    try:
        timestamp = iso_to_epoch(entry["timestamp"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    windows = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        pct, minutes, reset = (window.get(k) for k in
                               ("used_percent", "window_minutes", "resets_at"))
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) for v in (pct, minutes, reset)):
            continue
        if not 0 <= pct <= 100 or minutes <= 0 or reset <= 0:
            continue
        role = "weekly" if minutes >= 1440 else "five_hour"
        windows[role] = {"used_percent": float(pct), "resets_at": float(reset)}
    if not windows:
        return None
    return {"status": "ok", "fetched_at": timestamp, "source": "local_log",
            "plan_tier": limits.get("plan_type"), "windows": windows,
            "scoped": [], "extra_usage": None}


class LocalLimitsReader:
    def __init__(self, root: Path):
        self.root = root
        self._states: dict[Path, tuple[int, int]] = {}
        self._latest: dict | None = None

    def poll(self) -> dict:
        # Only read appended complete lines. Retain the event timestamp;
        # rereading a file must never turn yesterday's quota into fresh data.
        for path in sorted(self.root.rglob("rollout-*.jsonl")):
            try:
                stat = path.stat()
                inode, offset = self._states.get(path, (0, 0))
                if inode != stat.st_ino or stat.st_size < offset:
                    offset = 0
                with path.open("rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break
                        offset += len(line)
                        try:
                            result = normalize_event(json.loads(line))
                        except (ValueError, UnicodeError):
                            continue
                        if result and (self._latest is None or
                                       result["fetched_at"] >= self._latest["fetched_at"]):
                            self._latest = result
                self._states[path] = (stat.st_ino, offset)
            except OSError:
                continue
        return self._latest or {"status": "local_limits_missing", "windows": {},
                                "source": "local_log"}
