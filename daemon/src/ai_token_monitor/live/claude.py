"""Claude (Anthropic) real 5h + weekly limits via the OAuth usage API.

Claude Code writes its OAuth credential to ``~/.claude/.credentials.json`` on
Linux (no Keychain), shape::

    {"claudeAiOauth": {"accessToken": "sk-ant-oat...", "refreshToken": ...,
                       "expiresAt": <ms epoch>, "scopes": [...],
                       "subscriptionType": "max",
                       "rateLimitTier": "default_claude_max_20x"}}

The token must carry the ``user:profile`` scope. With it, a single request
returns the account's authoritative usage:

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <accessToken>
    anthropic-beta: oauth-2025-04-20        (required gate)

The response's ``limits[]`` array (or the legacy ``five_hour``/``seven_day``
objects) gives ``percent``/``utilization`` on a 0–100 scale plus an ISO-8601
``resets_at`` per window — the provider's real numbers, no dollar scaling.

This poller is READ-ONLY: it never rewrites the credential file. Claude Code
owns that file and refreshes the token during normal use; if the access token
is expired the poller simply reports a stale status and picks up the fresh
token on the next poll, rather than racing Claude Code for the login (a bad
write could sign the user out).

It also does NOT refresh the token in memory, unlike the Antigravity poller.
The risk is asymmetric: Claude Code rotates this file by itself whenever you
use it, so `token_expired` here heals on its own within minutes, while an
OAuth refresh grant that rotates its refresh token would leave the on-disk
copy pointing at a spent one — trading a stale bar for a signed-out CLI.
`effective_live` already carries the last good numbers across the gap.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..models import iso_to_epoch
from . import register
from . import http as _http
from .base import LivePoller

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
REQUIRED_SCOPE = "user:profile"
DEFAULT_CRED = "~/.claude/.credentials.json"


def _empty(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "fetched_at": time.time(),
            "plan_tier": None, "windows": {}, "scoped": [],
            "extra_usage": None, **extra}


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return iso_to_epoch(iso)
    except ValueError:
        return None


def normalize_usage(payload: dict[str, Any], plan_tier: str | None) -> dict[str, Any]:
    """Map the /oauth/usage JSON to the daemon's real-limit shape.

    Prefers the modern ``limits[]`` array (per-window ``percent`` + ``resets_at``
    + optional model ``scope``); falls back to the legacy ``five_hour`` /
    ``seven_day`` ``utilization`` objects. Percentages are already 0–100.
    """
    windows: dict[str, dict[str, Any]] = {}
    scoped: list[dict[str, Any]] = []

    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            group = item.get("group")
            pct = item.get("percent")
            if not isinstance(pct, (int, float)):
                continue
            resets = _epoch(item.get("resets_at"))
            if kind == "session" or group == "session":
                windows["five_hour"] = {"used_percent": float(pct), "resets_at": resets}
            elif kind == "weekly_all" or (group == "weekly" and not item.get("scope")):
                windows["weekly"] = {"used_percent": float(pct), "resets_at": resets}
            elif kind == "weekly_scoped" or (group == "weekly" and item.get("scope")):
                scope = item.get("scope") or {}
                model = scope.get("model") or {}
                label = model.get("display_name") or model.get("id") or "scoped"
                scoped.append({"label": label, "used_percent": float(pct),
                               "resets_at": resets, "group": "weekly"})

    # Legacy fallback / gap-fill from the flat objects.
    for key, api_key in (("five_hour", "five_hour"), ("weekly", "seven_day")):
        if key in windows:
            continue
        obj = payload.get(api_key)
        if isinstance(obj, dict) and isinstance(obj.get("utilization"), (int, float)):
            windows[key] = {"used_percent": float(obj["utilization"]),
                            "resets_at": _epoch(obj.get("resets_at"))}

    extra = None
    xu = payload.get("extra_usage")
    if isinstance(xu, dict) and xu.get("is_enabled"):
        extra = {
            "enabled": True,
            "used_percent": xu.get("utilization"),
            "used_credits": xu.get("used_credits"),
            "monthly_limit": xu.get("monthly_limit"),
            "currency": xu.get("currency"),
        }

    return {"status": "ok", "fetched_at": time.time(), "plan_tier": plan_tier,
            "windows": windows, "scoped": scoped, "extra_usage": extra}


def read_oauth(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


@register
class ClaudeLimitsPoller(LivePoller):
    name = "claude_code"
    tool = "claude_code"
    credential_paths = (DEFAULT_CRED,)

    def _cred_path(self) -> Path:
        return Path(self.settings.get("credentials", DEFAULT_CRED)).expanduser()

    def poll(self) -> dict[str, Any]:
        oauth = read_oauth(self._cred_path())
        if oauth is None:
            return _empty("credentials_missing")

        scopes = oauth.get("scopes") or []
        if REQUIRED_SCOPE not in scopes:
            return _empty("scope_missing")

        token = oauth.get("accessToken")
        if not token:
            return _empty("credentials_missing")

        expires_at = oauth.get("expiresAt") or 0
        # Read-only: don't refresh the token ourselves. If it's expired, wait
        # for Claude Code to rotate the file; report stale meanwhile.
        if expires_at and time.time() >= float(expires_at) / 1000.0:
            return _empty("token_expired")

        plan_tier = oauth.get("rateLimitTier") or oauth.get("subscriptionType")
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ai-token-monitor",
        })
        timeout = float(self.settings.get("timeout_s", 15) or 15)
        try:
            with _http.urlopen(req, timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return _empty("unauthorized")
            if exc.code == 403:
                return _empty("scope_missing")
            if exc.code == 429:
                return _empty("rate_limited")
            log.warning("claude live: HTTP %s", exc.code)
            return _empty(f"http_{exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.debug("claude live: network error: %s", exc)
            return _empty("network")
        except ValueError:
            return _empty("bad_response")

        if not isinstance(payload, dict):
            return _empty("bad_response")
        return normalize_usage(payload, plan_tier)
