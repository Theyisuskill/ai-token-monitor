"""Claude live-limit poller: normalization + credential short-circuits.

No network: normalize_usage() is pure, and the status paths (missing creds,
missing scope, expired token) all return before any HTTP request.
"""

import json
import time
from datetime import datetime, timedelta, timezone

from ai_token_monitor.live.claude import ClaudeLimitsPoller, normalize_usage


def _iso_in(hours, micros="469219"):
    """A real ISO-8601 reset stamp `hours` from now. Anchoring the fixture to
    the clock instead of a literal date keeps "resets in the future" assertions
    true forever — a hardcoded 2026-07-16 quietly turns the test red once it
    passes."""
    when = datetime.now(timezone.utc) + timedelta(hours=hours)
    return when.replace(microsecond=int(micros)).isoformat()


FIVE_HOUR_RESET = _iso_in(2)
WEEKLY_RESET = _iso_in(72, "469249")
SCOPED_RESET = _iso_in(72, "469572")

# Trimmed real response shape from GET /api/oauth/usage (Max 20x account).
REAL_PAYLOAD = {
    "five_hour": {"utilization": 0.0, "resets_at": FIVE_HOUR_RESET},
    "seven_day": {"utilization": 26.0, "resets_at": WEEKLY_RESET},
    "extra_usage": {"is_enabled": False, "utilization": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 0, "is_active": False,
         "resets_at": FIVE_HOUR_RESET, "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 26, "is_active": True,
         "resets_at": WEEKLY_RESET, "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 21, "is_active": False,
         "resets_at": SCOPED_RESET,
         "scope": {"model": {"id": None, "display_name": "Fable"}}},
    ],
}


def test_normalize_prefers_limits_array():
    out = normalize_usage(REAL_PAYLOAD, "default_claude_max_20x")
    assert out["status"] == "ok"
    assert out["plan_tier"] == "default_claude_max_20x"
    assert out["windows"]["five_hour"]["used_percent"] == 0.0
    assert out["windows"]["weekly"]["used_percent"] == 26.0
    # resets_at parsed to a real future epoch
    assert out["windows"]["weekly"]["resets_at"] > time.time()
    # the per-model scoped weekly cap is surfaced
    assert out["scoped"] == [{"label": "Fable", "used_percent": 21.0,
                              "resets_at": out["scoped"][0]["resets_at"],
                              "group": "weekly"}]
    assert out["extra_usage"] is None  # disabled by the org


def test_normalize_legacy_fallback_without_limits():
    payload = {
        "five_hour": {"utilization": 12.5, "resets_at": "2026-07-12T16:30:00+00:00"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-07-16T00:00:00+00:00"},
    }
    out = normalize_usage(payload, None)
    assert out["windows"]["five_hour"]["used_percent"] == 12.5
    assert out["windows"]["weekly"]["used_percent"] == 40.0
    assert out["scoped"] == []


def test_extra_usage_surfaced_when_enabled():
    payload = dict(REAL_PAYLOAD)
    payload["extra_usage"] = {"is_enabled": True, "utilization": 30.0,
                              "used_credits": 1500, "monthly_limit": 5000,
                              "currency": "USD"}
    out = normalize_usage(payload, None)
    assert out["extra_usage"]["enabled"] is True
    assert out["extra_usage"]["used_percent"] == 30.0


def _write_cred(tmp_path, oauth):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": oauth}))
    return p


def _poller(path):
    return ClaudeLimitsPoller({"credentials": str(path)})


def test_credentials_missing(tmp_path):
    out = _poller(tmp_path / "nope.json").poll()
    assert out["status"] == "credentials_missing"
    assert out["windows"] == {}


def test_scope_missing_short_circuits(tmp_path):
    path = _write_cred(tmp_path, {
        "accessToken": "sk-ant-x", "scopes": ["user:inference"],
        "expiresAt": int((time.time() + 3600) * 1000)})
    out = _poller(path).poll()
    assert out["status"] == "scope_missing"


def test_expired_token_is_not_refreshed(tmp_path):
    # Read-only poller: an expired token reports stale, never hits the network.
    path = _write_cred(tmp_path, {
        "accessToken": "sk-ant-x", "scopes": ["user:profile"],
        "expiresAt": int((time.time() - 60) * 1000)})
    out = _poller(path).poll()
    assert out["status"] == "token_expired"
