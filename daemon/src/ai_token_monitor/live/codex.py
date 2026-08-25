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

**Not every plan has both windows.** On the cheaper tiers (ChatGPT Go, whose
credential reports ``prolite``) Codex is metered on a weekly allowance only,
and the account's single window arrives as ``primary_window``. Each window is
therefore classified by its OWN ``limit_window_seconds``, never by position:
reading a lone weekly window as the 5-hour one would invent a session limit
the account does not have.

The tier is also recoverable without the network: the ``id_token`` /
``access_token`` in ``auth.json`` are JWTs whose ``https://api.openai.com/auth``
claim carries ``chatgpt_plan_type``. The payload is decoded (never verified —
it is a hint for picking a plan preset, not an authorization decision) so the
right plan, and therefore the right set of windows, is known even while the
poller is disabled or offline.

This poller is READ-ONLY: it never rewrites ``auth.json``. The Codex CLI owns
that file and refreshes the token during normal use; a bad write could sign the
user out, so an expired/invalid token is simply reported as ``unauthorized``
and the fresh token is picked up on the next poll.

Field names and endpoints mirror CodexBar's ``CodexOAuthUsageFetcher`` /
``CodexRateWindowNormalizer``.
"""

from __future__ import annotations

import base64
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
from . import http as _http
from .base import LivePoller

log = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_CODEX_HOME = "~/.codex"

# limit_window_seconds sentinels used to disambiguate which API window is the
# 5-hour session bucket vs the weekly bucket (mirrors CodexRateWindowNormalizer:
# 300 min == 18000 s session, 10080 min == 604800 s weekly). Anything a day or
# longer counts as the long window, so an unusual span still lands sanely.
SESSION_WINDOW_SECONDS = 18_000
WEEKLY_WINDOW_SECONDS = 604_800
LONG_WINDOW_CUTOFF_SECONDS = 86_400

#: JWT claim holding the ChatGPT plan tier ("plus", "pro", "prolite", ...).
AUTH_CLAIM = "https://api.openai.com/auth"


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


def _role(obj: Any) -> str | None:
    """Which window an API entry is, from its own declared span (or None)."""
    if not isinstance(obj, dict):
        return None
    secs = _num(obj.get("limit_window_seconds"))
    if secs is None:
        return None
    return "weekly" if secs >= LONG_WINDOW_CUTOFF_SECONDS else "five_hour"


def normalize_usage(payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Map the /wham/usage JSON to the daemon's real-limit shape.

    Each window is classified by its own ``limit_window_seconds`` — a span of a
    day or more is the weekly bucket, anything shorter the 5-hour session one.
    Position (``primary_window`` / ``secondary_window``) is only the fallback
    for a window that declares no span. This matters beyond tidiness: a plan
    with a weekly limit and no session limit sends that single window as
    ``primary_window``, and reading it positionally would show the account a
    5-hour bar it does not actually have.

    A window the account is not metered on is simply absent from ``windows``;
    the daemon reads that as "this plan does not have it".
    """
    if now is None:
        now = time.time()

    plan_tier = payload.get("plan_type")
    if not isinstance(plan_tier, str):
        plan_tier = None

    rate = payload.get("rate_limit")
    windows: dict[str, dict[str, Any]] = {}
    if isinstance(rate, dict):
        positional = (("five_hour", rate.get("primary_window")),
                      ("weekly", rate.get("secondary_window")))
        mapping: dict[str, Any] = {}
        unlabelled = []
        for position, obj in positional:
            role = _role(obj)
            if role is None:
                if isinstance(obj, dict):
                    unlabelled.append((position, obj))
            else:
                mapping.setdefault(role, obj)
        for position, obj in unlabelled:
            mapping.setdefault(position, obj)
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


def _jwt_claims(token: Any) -> dict[str, Any]:
    """A JWT's payload, WITHOUT verifying its signature.

    This is the user's own credential, already on disk and already trusted by
    the Codex CLI; the claims are read only to pick a plan preset, so there is
    nothing here to authorize and no key to verify against.
    """
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body))
    except ValueError:  # bad base64, bad utf-8 or bad JSON
        return {}
    return claims if isinstance(claims, dict) else {}


def plan_tier_from_tokens(tokens: dict[str, Any] | None) -> str | None:
    """``chatgpt_plan_type`` out of the credential's JWTs (no network).

    Lets the daemon pick the right plan preset — and therefore the right set
    of windows — before, or entirely without, a successful poll.
    """
    for name in ("id_token", "access_token"):
        auth = _jwt_claims((tokens or {}).get(name)).get(AUTH_CLAIM)
        if isinstance(auth, dict):
            tier = auth.get("chatgpt_plan_type")
            if isinstance(tier, str) and tier:
                return tier
    return None


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

    @staticmethod
    def _resolve_cred(settings: dict[str, Any]) -> Path:
        override = settings.get("credentials")
        if override:
            return Path(override).expanduser()
        return _codex_home() / "auth.json"

    @classmethod
    def is_available(cls, settings: dict[str, Any]) -> bool:
        # Not the base implementation: the default path depends on $CODEX_HOME.
        return cls._resolve_cred(settings).exists()

    def _cred_path(self) -> Path:
        return self._resolve_cred(self.settings)

    @classmethod
    def offline_tier(cls, settings: dict[str, Any]) -> str | None:
        return plan_tier_from_tokens(read_tokens(cls._resolve_cred(settings)))

    def poll(self) -> dict[str, Any]:
        tokens = read_tokens(self._cred_path())
        if tokens is None:
            return _empty("credentials_missing")

        token = tokens.get("access_token")
        if not token or not isinstance(token, str):
            return _empty("credentials_missing")

        # The credential names the plan tier itself, so even a failed poll can
        # report it — the plan decides which windows exist, and the UI should
        # not fall back to a preset with a 5h window the account never had.
        tier = plan_tier_from_tokens(tokens)

        def fail(status: str) -> dict[str, Any]:
            return _empty(status, plan_tier=tier)

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
            with _http.urlopen(req, timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return fail("unauthorized")
            if exc.code == 429:
                return fail("rate_limited")
            log.warning("codex live: HTTP %s", exc.code)
            return fail(f"http_{exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.debug("codex live: network error: %s", exc)
            return fail("network")
        except ValueError:
            return fail("bad_response")

        if not isinstance(payload, dict):
            return fail("bad_response")
        result = normalize_usage(payload)
        if not result.get("plan_tier"):
            result["plan_tier"] = tier
        return result
