"""OpenAI Codex / ChatGPT subscription real 5h + weekly limits poller.

The Codex CLI writes its OAuth credential to ``~/.codex/auth.json`` (honouring
``$CODEX_HOME``), shape::

    {"tokens": {"access_token": "...", "refresh_token": "...",
                "id_token": "...", "account_id": "..."},
     "last_refresh": "2026-07-11T12:00:00Z"}

With the access token a single request returns the account's authoritative
usage:

    GET https://chatgpt.com/backend-api/wham/usage
    Authorization: Bearer <access_token>
    Accept: application/json
    ChatGPT-Account-Id: <account_id>

The response's ``rate_limit`` object carries ``primary_window`` (the 5-hour /
session window, ``limit_window_seconds`` ~= 18000) and ``secondary_window``
(the weekly window, ~= 604800). Each window is
``{used_percent, reset_at, limit_window_seconds}`` where ``used_percent`` is
already 0-100 and ``reset_at`` is epoch seconds (some deployments send
``resets_in_seconds`` instead, a delta from now). ``plan_type`` gives the plan
tier, and the optional ``credits`` object surfaces pay-as-you-go balance.

This poller is READ-ONLY: it never rewrites ``auth.json``. The Codex CLI owns
that file and refreshes the token during normal use; a bad write could sign the
user out, so an expired/invalid token is simply reported as ``unauthorized``
and the fresh token is picked up on the next poll.

Field names and endpoints mirror CodexBar's ``CodexOAuthUsageFetcher`` /
``CodexRateWindowNormalizer``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..models import iso_to_epoch
from . import register
from .base import LivePoller

log = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_CODEX_HOME = "~/.codex"

# limit_window_seconds sentinels used to disambiguate which API window is the
# 5-hour session bucket vs the weekly bucket (mirrors CodexRateWindowNormalizer:
# 300 min == 18000 s session, 10080 min == 604800 s weekly).
SESSION_WINDOW_SECONDS = 18_000
WEEKLY_WINDOW_SECONDS = 604_800


def _empty(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "fetched_at": time.time(),
            "plan_tier": None, "windows": {}, "scoped": [],
            "extra_usage": None, **extra}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _reset_epoch(obj: dict[str, Any], now: float) -> float | None:
    """Resolve a window's reset time to epoch seconds.

    Prefers an absolute ``reset_at`` (epoch seconds, or an ISO-8601 string);
    falls back to ``resets_in_seconds`` interpreted as a delta from ``now``.
    """
    reset_at = obj.get("reset_at")
    if isinstance(reset_at, str):
        try:
            return iso_to_epoch(reset_at)
        except ValueError:
            reset_at = None
    absolute = _num(reset_at)
    if absolute is not None:
        return absolute
    delta = _num(obj.get("resets_in_seconds"))
    if delta is not None:
        return now + delta
    return None


def _window(obj: Any, now: float) -> dict[str, Any] | None:
    """Map one API window object to ``{used_percent, resets_at}``."""
    if not isinstance(obj, dict):
        return None
    used = _num(obj.get("used_percent"))
    if used is None:
        return None
    return {"used_percent": used, "resets_at": _reset_epoch(obj, now)}


def normalize_usage(payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Map the /wham/usage JSON to the daemon's real-limit shape.

    ``rate_limit.primary_window`` -> ``five_hour`` and ``secondary_window`` ->
    ``weekly``. When ``limit_window_seconds`` clearly identifies a window's role
    (session vs weekly), that classification wins over positional order so a
    provider that swaps the two still lands correctly.
    """
    if now is None:
        now = time.time()

    plan_tier = payload.get("plan_type")
    if not isinstance(plan_tier, str):
        plan_tier = None

    rate = payload.get("rate_limit")
    windows: dict[str, dict[str, Any]] = {}
    if isinstance(rate, dict):
        primary_obj = rate.get("primary_window")
        secondary_obj = rate.get("secondary_window")
        # Default positional mapping: primary -> five_hour, secondary -> weekly.
        mapping = {"five_hour": primary_obj, "weekly": secondary_obj}
        # If limit_window_seconds tags roles unambiguously, honour it (handles a
        # provider that reports the windows in the opposite order).
        role = {}
        for obj in (primary_obj, secondary_obj):
            if not isinstance(obj, dict):
                continue
            secs = _num(obj.get("limit_window_seconds"))
            if secs == SESSION_WINDOW_SECONDS:
                role.setdefault("five_hour", obj)
            elif secs == WEEKLY_WINDOW_SECONDS:
                role.setdefault("weekly", obj)
        if "five_hour" in role and "weekly" in role:
            mapping = role
        for key, obj in mapping.items():
            win = _window(obj, now)
            if win is not None:
                windows[key] = win

    extra = None
    credits = payload.get("credits")
    if isinstance(credits, dict):
        balance = _num(credits.get("balance"))
        has_credits = bool(credits.get("has_credits"))
        unlimited = bool(credits.get("unlimited"))
        if has_credits or unlimited or balance is not None:
            extra = {
                "enabled": has_credits or unlimited,
                "has_credits": has_credits,
                "unlimited": unlimited,
                "balance": balance,
            }

    return {"status": "ok", "fetched_at": time.time(), "plan_tier": plan_tier,
            "windows": windows, "scoped": [], "extra_usage": extra}


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env and env.strip():
        return Path(env).expanduser()
    return Path(DEFAULT_CODEX_HOME).expanduser()


def read_tokens(path: Path) -> dict[str, Any] | None:
    """Read ``auth.json`` and return its ``tokens`` dict, or None."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else None


@register
class CodexLimitsPoller(LivePoller):
    name = "codex"
    tool = "codex"

    def _cred_path(self) -> Path:
        override = self.settings.get("credentials")
        if override:
            return Path(override).expanduser()
        return _codex_home() / "auth.json"

    def poll(self) -> dict[str, Any]:
        tokens = read_tokens(self._cred_path())
        if tokens is None:
            return _empty("credentials_missing")

        token = tokens.get("access_token")
        if not token or not isinstance(token, str):
            return _empty("credentials_missing")

        account_id = tokens.get("account_id")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ai-token-monitor",
        }
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-Id"] = account_id

        req = urllib.request.Request(USAGE_URL, headers=headers)
        timeout = float(self.settings.get("timeout_s", 15) or 15)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _empty("unauthorized")
            if exc.code == 429:
                return _empty("rate_limited")
            log.warning("codex live: HTTP %s", exc.code)
            return _empty(f"http_{exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.debug("codex live: network error: %s", exc)
            return _empty("network")
        except ValueError:
            return _empty("bad_response")

        if not isinstance(payload, dict):
            return _empty("bad_response")
        return normalize_usage(payload)
