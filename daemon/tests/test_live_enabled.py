"""Poller activation (`enabled: auto`) and the quiet-status downgrade.

Both exist so the UI's amber "live limits unavailable" warning keeps meaning
"something broke": a tool you don't have shouldn't switch its poller on, and a
tool you aren't running shouldn't be reported as a failure.
"""

import json
import urllib.error
import urllib.request

import pytest

from ai_token_monitor import live as live_mod
from ai_token_monitor.live import http as live_http
from ai_token_monitor.live.antigravity import (
    CREDENTIAL_STALE_STATUSES,
    AntigravityLimitsPoller,
    normalize_token_response,
)
from ai_token_monitor.live.base import QUIET_STATUSES, LivePoller
from ai_token_monitor.live.codex import CodexLimitsPoller


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body):
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _boom(*args, **kwargs):
    raise urllib.error.URLError("offline")


class FakePoller(LivePoller):
    name = "fake"
    tool = "fake"
    credential_paths = ("~/.definitely-not-here/creds.json",)

    def poll(self):  # pragma: no cover - never called here
        return {"status": "ok"}


def test_enabled_true_and_false_ignore_the_credential():
    assert live_mod.is_enabled(FakePoller, {"enabled": True}) is True
    assert live_mod.is_enabled(FakePoller, {"enabled": False}) is False


def test_missing_key_stays_off():
    # Local-only default: a poller makes network calls, so absence means off.
    assert live_mod.is_enabled(FakePoller, {}) is False


def test_auto_follows_the_credential(tmp_path):
    cred = tmp_path / "auth.json"
    assert live_mod.is_enabled(FakePoller, {"enabled": "auto"}) is False

    conf = {"enabled": "auto", "credentials": str(cred)}
    assert live_mod.is_enabled(FakePoller, conf) is False
    cred.write_text("{}")
    assert live_mod.is_enabled(FakePoller, conf) is True


def test_auto_is_case_and_space_insensitive(tmp_path):
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    conf = {"enabled": " Auto ", "credentials": str(cred)}
    assert live_mod.is_enabled(FakePoller, conf) is True


def test_codex_auto_uses_its_own_path_resolution(tmp_path):
    # Codex resolves $CODEX_HOME, so it overrides is_available.
    cred = tmp_path / "auth.json"
    conf = {"enabled": "auto", "credentials": str(cred)}
    assert live_mod.is_enabled(CodexLimitsPoller, conf) is False
    cred.write_text('{"tokens": {}}')
    assert live_mod.is_enabled(CodexLimitsPoller, conf) is True


def test_antigravity_auto_follows_the_dot_dir(tmp_path):
    conf = {"enabled": "auto", "home": str(tmp_path)}
    assert live_mod.is_enabled(AntigravityLimitsPoller, conf) is False
    (tmp_path / ".gemini").mkdir()
    assert live_mod.is_enabled(AntigravityLimitsPoller, conf) is True


def _poller(monkeypatch, loopback, oauth):
    poller = AntigravityLimitsPoller({})
    monkeypatch.setattr(poller, "_poll_loopback", lambda: loopback)
    monkeypatch.setattr(poller, "_poll_oauth", lambda: oauth)
    return poller


def test_stale_credential_without_loopback_is_quiet(monkeypatch):
    # agy isn't running (no loopback) and its stored login went stale: that is
    # a state, not a fault — running agy fixes both.
    for status in sorted(CREDENTIAL_STALE_STATUSES):
        out = _poller(monkeypatch, None, {"status": status}).poll()
        assert out["status"] == "not_running"
        assert out["status"] in QUIET_STATUSES
        assert out["detail"] == status  # the reason survives for --live


def test_real_fallback_failures_still_surface(monkeypatch):
    out = _poller(monkeypatch, None, {"status": "http_503"}).poll()
    assert out["status"] == "http_503"
    assert out["status"] not in QUIET_STATUSES


def test_loopback_result_wins(monkeypatch):
    live = {"status": "ok", "windows": {"five_hour": {"used_percent": 3.0}}}
    assert _poller(monkeypatch, live, {"status": "token_expired"}).poll() is live


def test_no_credential_at_all_is_not_running(monkeypatch):
    assert _poller(monkeypatch, None, None).poll()["status"] == "not_running"


# -- Google token refresh (in memory, never written back) ------------------- #

def test_normalize_token_response_reads_a_bearer():
    out = normalize_token_response({"access_token": "ya29.new", "expires_in": 3599},
                                   now=1000.0)
    assert out == {"access_token": "ya29.new", "expires_at": 4599.0}


def test_normalize_token_response_defaults_a_missing_lifetime():
    out = normalize_token_response({"access_token": "ya29.new"}, now=0.0)
    assert out["expires_at"] == 3600.0


@pytest.mark.parametrize("payload", [
    None, {}, {"error": "invalid_grant"}, {"access_token": ""},
    {"access_token": 42}, "not-a-dict",
])
def test_normalize_token_response_rejects_junk(payload):
    assert normalize_token_response(payload, now=0.0) is None


def test_refresh_needs_a_configured_client(monkeypatch):
    # No client id/secret -> no request at all, and no token.
    poller = AntigravityLimitsPoller({})
    monkeypatch.setattr(live_http, "urlopen", _boom)
    assert poller._refresh_token("1//refresh", timeout=1) is None


def test_refresh_caches_until_it_expires(monkeypatch):
    poller = AntigravityLimitsPoller(
        {"oauth_client_id": "cid", "oauth_client_secret": "secret"})
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps({"access_token": "ya29.fresh", "expires_in": 3600}))

    monkeypatch.setattr(live_http, "urlopen", fake_urlopen)
    assert poller._refresh_token("1//refresh", timeout=1) == "ya29.fresh"
    assert poller._refresh_token("1//refresh", timeout=1) == "ya29.fresh"
    assert len(calls) == 1  # second call served from the in-memory cache
    body = calls[0].data.decode()
    assert "grant_type=refresh_token" in body and "client_id=cid" in body


def test_refresh_survives_a_network_error(monkeypatch):
    poller = AntigravityLimitsPoller(
        {"oauth_client_id": "cid", "oauth_client_secret": "secret"})
    monkeypatch.setattr(live_http, "urlopen", _boom)
    assert poller._refresh_token("1//refresh", timeout=1) is None


# --- credential_tiers: knowing the plan without polling ---------------------

def test_credential_tiers_reads_every_poller_enabled_or_not(tmp_path):
    """The plan decides which windows a tool even has, so the tier is read
    from the credential whether or not the poller is switched on — it is a
    local file read, not a request."""
    import base64

    claims = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth":
                    {"chatgpt_plan_type": "prolite"}}).encode()
    ).decode().rstrip("=")
    cred = tmp_path / "auth.json"
    cred.write_text(json.dumps({"tokens": {"id_token": f"h.{claims}.s"}}))

    tiers = live_mod.credential_tiers(
        {"codex": {"enabled": False, "credentials": str(cred)}})
    assert tiers["codex"] == "prolite"


def test_credential_tiers_is_quiet_when_nothing_is_readable(monkeypatch, tmp_path):
    # $CODEX_HOME keeps the default-path branch off this machine's real
    # credential, so the test says the same thing everywhere.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert live_mod.credential_tiers(
        {"codex": {"credentials": str(tmp_path / "absent.json")}}) == {}
    assert live_mod.credential_tiers(None) == {}
    assert live_mod.credential_tiers({"codex": "yes"}) == {}  # non-dict conf


def test_credential_tiers_survives_a_broken_poller(monkeypatch, tmp_path):
    """One provider's mangled credential must not blank every other tool's
    plan (or take the snapshot down with it)."""
    def boom(_settings):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(CodexLimitsPoller, "offline_tier",
                        classmethod(lambda cls, s: boom(s)))
    assert live_mod.credential_tiers({"codex": {}}) == {}
