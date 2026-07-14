"""effective_live: a transient poll failure must not blank provider-real data.

Regression for the popup "flapping" between the provider's real % and the
much lower dollar estimate whenever a single poll failed.
"""

from ai_token_monitor.live.base import effective_live

OK = {
    "status": "ok", "fetched_at": 1000.0, "plan_tier": "max_20x",
    "windows": {
        "five_hour": {"used_percent": 58.0, "resets_at": 5000.0},
        "weekly": {"used_percent": 64.0, "resets_at": 90000.0},
    },
    "scoped": [{"label": "Fable", "used_percent": 68.0, "resets_at": 90000.0}],
}
ERR = {"status": "http 500", "fetched_at": 1100.0}


def test_ok_result_wins():
    data, stale = effective_live(OK, None, now=1000.0)
    assert data is OK
    assert stale is False


def test_ok_result_wins_even_over_history():
    data, stale = effective_live(OK, {"status": "ok", "fetched_at": 1.0},
                                 now=1000.0)
    assert data is OK
    assert stale is False


def test_failure_serves_last_ok():
    data, stale = effective_live(ERR, OK, now=1200.0)
    assert stale is True
    assert data["windows"]["five_hour"]["used_percent"] == 58.0
    assert data["scoped"][0]["label"] == "Fable"
    assert data["plan_tier"] == "max_20x"


def test_failure_without_history_serves_nothing():
    assert effective_live(ERR, None, now=1200.0) == (None, False)


def test_last_ok_ages_out():
    now = 1000.0 + 1801.0
    assert effective_live(ERR, OK, now=now, ttl_s=1800.0) == (None, False)


def test_expired_windows_are_dropped():
    # 5h window reset at t=5000 already passed; weekly is still valid.
    data, stale = effective_live(ERR, OK, now=6000.0, ttl_s=86400.0)
    assert stale is True
    assert "five_hour" not in data["windows"]
    assert data["windows"]["weekly"]["used_percent"] == 64.0


def test_nothing_left_after_all_windows_expire():
    old = {"status": "ok", "fetched_at": 1000.0,
           "windows": {"five_hour": {"used_percent": 10.0, "resets_at": 2000.0}},
           "scoped": []}
    assert effective_live(ERR, old, now=3000.0, ttl_s=86400.0) == (None, False)
