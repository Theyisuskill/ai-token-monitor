"""Antigravity ("agy") live-limit poller: pure normalization + credential guards.

No network: the normalizers are pure, and every status path exercised here
(auth gate, missing creds, expired token, no server) short-circuits before any
loopback or HTTP request. ``ports: []`` disables /proc port discovery so
``poll()`` never touches the network.
"""

import json
import time
from datetime import datetime, timedelta, timezone

from pytest import approx

from ai_token_monitor.live.antigravity import (
    AntigravityLimitsPoller,
    normalize_oauth_quota,
    normalize_quota_summary,
    parse_plan_tier,
)


def _iso_in(**delta):
    """A UTC ISO-8601 reset time relative to now, so fixtures never go stale."""
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


_SESSION_RESET = _iso_in(hours=3)
_WEEKLY_RESET = _iso_in(days=4)
_OAUTH_RESET = _iso_in(days=1)

# Trimmed realistic RetrieveUserQuotaSummary response (the two real pools:
# Gemini vs. Claude & GPT), each with a 5-hour (session) and a weekly bucket.
QUOTA_SUMMARY = {
    "response": {
        "description": "Usage",
        "groups": [
            {"displayName": "Gemini Models", "buckets": [
                {"bucketId": "gemini-5h", "displayName": "5-hour usage",
                 "remainingFraction": 0.80,
                 "resetTime": _SESSION_RESET, "disabled": False},
                {"bucketId": "gemini-weekly", "displayName": "Weekly usage",
                 "remainingFraction": 0.60,
                 "resetTime": _WEEKLY_RESET, "disabled": False},
            ]},
            {"displayName": "Claude & GPT models", "buckets": [
                {"bucketId": "claude-gpt-5h", "displayName": "5-hour usage",
                 "remaining": {"remainingFraction": 0.50},
                 "resetTime": _SESSION_RESET, "disabled": False},
                {"bucketId": "claude-gpt-weekly", "displayName": "Weekly usage",
                 "remainingFraction": 0.90,
                 "resetTime": _WEEKLY_RESET, "disabled": False},
                {"bucketId": "autocomplete", "displayName": "Autocomplete",
                 "remainingFraction": None, "disabled": True},
            ]},
        ],
    }
}


def test_quota_summary_maps_two_pools_and_windows():
    out = normalize_quota_summary(QUOTA_SUMMARY, "Google AI Pro")
    assert out["status"] == "ok"
    assert out["plan_tier"] == "Google AI Pro"

    # five_hour window = most-constrained session bucket: Claude&GPT 0.50 -> 50%.
    assert out["windows"]["five_hour"]["used_percent"] == approx(50.0)
    assert out["windows"]["five_hour"]["resets_at"] > time.time()
    # weekly window = most-constrained weekly bucket: Gemini 0.60 -> 40%.
    assert out["windows"]["weekly"]["used_percent"] == approx(40.0)

    # The disabled autocomplete bucket is dropped; the other four are scoped.
    assert len(out["scoped"]) == 4
    labels = {row["label"]: row for row in out["scoped"]}
    assert labels["Gemini 5h"]["used_percent"] == approx(20.0)
    assert labels["Gemini 5h"]["group"] == "session"
    assert labels["Claude & GPT weekly"]["used_percent"] == approx(10.0)
    assert labels["Claude & GPT weekly"]["group"] == "weekly"
    # Pool keys match the daemon's QUOTA_GROUPS so the per-pool sub-bars in
    # the snapshot can be driven by these scoped entries.
    assert labels["Gemini 5h"]["pool"] == "gemini"
    assert labels["Claude & GPT weekly"]["pool"] == "claude_gpt"
    assert out["extra_usage"] is None


def test_quota_summary_accepts_top_level_groups_envelope():
    payload = {"groups": QUOTA_SUMMARY["response"]["groups"]}
    out = normalize_quota_summary(payload, None)
    assert out["windows"]["five_hour"]["used_percent"] == approx(50.0)


def test_quota_summary_empty_when_no_groups():
    out = normalize_quota_summary({"response": {"groups": []}}, None)
    assert out["status"] == "ok"
    assert out["windows"] == {}
    assert out["scoped"] == []


def test_parse_plan_tier_prefers_user_tier():
    assert parse_plan_tier({"userStatus": {"userTier": {"name": "Ultra"}}}) == "Ultra"
    assert parse_plan_tier(
        {"userStatus": {"planStatus": {"planInfo": {"planDisplayName": "Pro"}}}}) == "Pro"
    assert parse_plan_tier({"userStatus": {}}) is None


# --- Google OAuth fallback normalizer -------------------------------------- #

OAUTH_QUOTA = {
    "buckets": [
        {"modelId": "gemini-3-pro", "remainingFraction": 0.25,
         "resetTime": _OAUTH_RESET, "tokenType": "input"},
        {"modelId": "gemini-3-pro", "remainingFraction": 0.40,
         "resetTime": _OAUTH_RESET, "tokenType": "output"},
        {"modelId": "gemini-3-flash", "remainingFraction": 0.90,
         "resetTime": _OAUTH_RESET, "tokenType": "input"},
    ]
}


def test_oauth_quota_keeps_lowest_per_model_and_worst_window():
    out = normalize_oauth_quota(OAUTH_QUOTA, "Free")
    assert out["status"] == "ok"
    assert out["plan_tier"] == "Free"
    per_model = {row["label"]: row["used_percent"] for row in out["scoped"]}
    # gemini-3-pro keeps the lower fraction (0.25 -> 75% used).
    assert per_model["gemini-3-pro"] == approx(75.0)
    assert per_model["gemini-3-flash"] == approx(10.0)
    # weekly window backed by the most-constrained model.
    assert out["windows"]["weekly"]["used_percent"] == approx(75.0)
    assert out["windows"]["weekly"]["resets_at"] > time.time()


# --- poll() short-circuits (no network) ------------------------------------ #

def _write_gemini(tmp_path, creds=None, selected="oauth-personal"):
    gdir = tmp_path / ".gemini"
    gdir.mkdir()
    if selected is not None:
        (gdir / "settings.json").write_text(
            json.dumps({"security": {"auth": {"selectedType": selected}}}))
    if creds is not None:
        (gdir / "oauth_creds.json").write_text(json.dumps(creds))
    return tmp_path


def _poller(tmp_path, **extra):
    return AntigravityLimitsPoller({"home": str(tmp_path), "ports": [], **extra})


def test_not_running_when_no_server_and_no_creds(tmp_path):
    _write_gemini(tmp_path)  # settings only, no oauth_creds.json
    out = _poller(tmp_path).poll()
    assert out["status"] == "not_running"
    assert out["windows"] == {}


def test_expired_token_reports_stale_without_refresh(tmp_path):
    creds = {"access_token": "ya29.stale", "refresh_token": "1//r",
             "expiry_date": int((time.time() - 60) * 1000)}
    _write_gemini(tmp_path, creds=creds)
    before = (tmp_path / ".gemini" / "oauth_creds.json").read_text()

    out = _poller(tmp_path).poll()

    # No server answered, so Antigravity isn't running: a stale stored login is
    # then the expected state, reported quietly with the reason kept for --live.
    assert out["status"] == "not_running"
    assert out["detail"] == "token_expired"
    # READ-ONLY: the CLI owns this file; the poller must never rewrite it.
    assert (tmp_path / ".gemini" / "oauth_creds.json").read_text() == before


def test_api_key_auth_type_is_skipped(tmp_path):
    # Valid, unexpired creds but api-key auth: gate skips OAuth -> not_running.
    _write_gemini(tmp_path, selected="api-key", creds={
        "access_token": "ya29.live",
        "expiry_date": int((time.time() + 3600) * 1000)})
    out = _poller(tmp_path).poll()
    assert out["status"] == "not_running"
