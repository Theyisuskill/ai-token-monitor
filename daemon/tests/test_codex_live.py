"""Codex live-limit poller: normalization + credential short-circuits.

No network: normalize_usage() is pure, and the missing-credential path returns
before any HTTP request is attempted.
"""

import json
import time

from ai_token_monitor.live.codex import (
    CodexLimitsPoller,
    normalize_usage,
    read_tokens,
)

# Trimmed real response shape from GET /backend-api/wham/usage (Plus account).
# primary_window == 5h/session, secondary_window == weekly. used_percent is
# already 0-100 and reset_at is absolute epoch seconds.
NOW = 1_752_000_000.0
REAL_PAYLOAD = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {
            "used_percent": 42,
            "reset_at": int(NOW + 3_600),
            "limit_window_seconds": 18_000,
        },
        "secondary_window": {
            "used_percent": 17,
            "reset_at": int(NOW + 200_000),
            "limit_window_seconds": 604_800,
        },
    },
    "credits": {"has_credits": True, "unlimited": False, "balance": 12.5},
}


def test_normalize_maps_primary_and_secondary():
    out = normalize_usage(REAL_PAYLOAD, now=NOW)
    assert out["status"] == "ok"
    assert out["plan_tier"] == "plus"
    assert out["windows"]["five_hour"]["used_percent"] == 42.0
    assert out["windows"]["five_hour"]["resets_at"] == NOW + 3_600
    assert out["windows"]["weekly"]["used_percent"] == 17.0
    assert out["windows"]["weekly"]["resets_at"] == NOW + 200_000
    assert out["scoped"] == []


def test_credits_surfaced_as_extra_usage():
    out = normalize_usage(REAL_PAYLOAD, now=NOW)
    assert out["extra_usage"]["enabled"] is True
    assert out["extra_usage"]["has_credits"] is True
    assert out["extra_usage"]["balance"] == 12.5


def test_resets_in_seconds_delta_fallback():
    payload = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 5, "resets_in_seconds": 1_000,
                               "limit_window_seconds": 18_000},
            "secondary_window": {"used_percent": 60, "resets_in_seconds": 500_000,
                                 "limit_window_seconds": 604_800},
        },
    }
    out = normalize_usage(payload, now=NOW)
    assert out["windows"]["five_hour"]["resets_at"] == NOW + 1_000
    assert out["windows"]["weekly"]["resets_at"] == NOW + 500_000
    assert out["extra_usage"] is None


def test_iso_reset_at_string_is_parsed():
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 10,
                               "reset_at": "2026-07-12T16:30:00+00:00",
                               "limit_window_seconds": 18_000},
        },
    }
    out = normalize_usage(payload, now=NOW)
    assert out["windows"]["five_hour"]["used_percent"] == 10.0
    assert out["windows"]["five_hour"]["resets_at"] > NOW


def test_window_role_swap_by_limit_window_seconds():
    # Provider reports the windows in the opposite positional order; the
    # limit_window_seconds tags still classify them correctly.
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 80, "reset_at": int(NOW + 100),
                               "limit_window_seconds": 604_800},
            "secondary_window": {"used_percent": 3, "reset_at": int(NOW + 50),
                                 "limit_window_seconds": 18_000},
        },
    }
    out = normalize_usage(payload, now=NOW)
    assert out["windows"]["weekly"]["used_percent"] == 80.0
    assert out["windows"]["five_hour"]["used_percent"] == 3.0


def test_missing_rate_limit_yields_empty_windows():
    out = normalize_usage({"plan_type": "free"}, now=NOW)
    assert out["status"] == "ok"
    assert out["windows"] == {}
    assert out["plan_tier"] == "free"


def test_read_tokens_and_credentials_missing(tmp_path):
    # No file at all -> credentials_missing, no network.
    poller = CodexLimitsPoller({"credentials": str(tmp_path / "nope.json")})
    out = poller.poll()
    assert out["status"] == "credentials_missing"
    assert out["windows"] == {}


def test_auth_json_without_access_token(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"refresh_token": "r"},
                                "last_refresh": "2026-07-11T12:00:00Z"}))
    assert read_tokens(path) == {"refresh_token": "r"}
    out = CodexLimitsPoller({"credentials": str(path)}).poll()
    assert out["status"] == "credentials_missing"


def test_read_tokens_realistic_auth_json(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "tokens": {"access_token": "a", "refresh_token": "r",
                   "id_token": "i", "account_id": "acct-123"},
        "last_refresh": "2026-07-11T12:00:00Z",
    }))
    tokens = read_tokens(path)
    assert tokens["access_token"] == "a"
    assert tokens["account_id"] == "acct-123"
