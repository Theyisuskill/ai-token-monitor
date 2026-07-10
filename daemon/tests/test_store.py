import time

import pytest

from ai_token_monitor.models import UsageRecord
from ai_token_monitor.store import Store, period_start


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "usage.db")
    yield s
    s.close()


def rec(ts, tool="claude_code", model="claude-opus-4-8", cost=1.0, dedup=None):
    return UsageRecord(tool=tool, model=model, ts=ts, input_tokens=10,
                       output_tokens=20, cache_read_tokens=30,
                       cost_usd=cost, dedup_key=dedup or f"k{ts}:{tool}")


def test_add_dedups_on_tool_and_key(store):
    now = time.time()
    assert store.add([rec(now, dedup="same"), rec(now + 1, dedup="same")]) == 1
    assert store.add([rec(now, tool="codex", dedup="same")]) == 1  # other tool ok


def test_summary_and_tools_seen(store):
    now = time.time()
    store.add([rec(now), rec(now - 10, tool="gemini_cli", cost=2.0)])
    summary = store.summary(0)
    assert summary["totals"]["cost_usd"] == 3.0
    assert summary["totals"]["requests"] == 2
    assert {t["tool"] for t in summary["tools"]} == {"claude_code", "gemini_cli"}
    assert store.tools_seen() == ["claude_code", "gemini_cli"]


def test_models_summary_limits_and_orders_by_cost(store):
    now = time.time()
    store.add([rec(now - i, model=f"m{i}", cost=float(i)) for i in range(5)])
    top = store.models_summary(0, limit_per_tool=3)["claude_code"]
    assert [m["model"] for m in top] == ["m4", "m3", "m2"]


def test_peak_window_finds_the_burst(store):
    base = time.time() - 10 * 86400
    # a $30 burst inside one hour, plus scattered cheap records
    store.add([rec(base + i * 60, cost=10.0, dedup=f"burst{i}") for i in range(3)])
    store.add([rec(base + 5 * 86400 + i * 3600, cost=1.0, dedup=f"bg{i}")
               for i in range(4)])
    assert store.peak_window("claude_code", 3600, lookback_days=30) == 30.0


def test_delete_tool_and_reset_file_state(store):
    now = time.time()
    store.add([rec(now), rec(now, tool="gemini_cli", dedup="g1")])
    store.set_file_state("/roots/gemini/a.db", 1, 100)
    store.set_file_state("/other/claude.jsonl", 2, 200)
    assert store.delete_tool("gemini_cli") == 1
    assert store.tools_seen() == ["claude_code"]
    assert store.reset_file_state_under("/roots/gemini") == 1
    assert store.get_file_state("/roots/gemini/a.db") == (-1, 0)
    assert store.get_file_state("/other/claude.jsonl") == (2, 200)


def test_summary_optional_tool_filter(store):
    now = time.time()
    store.add([rec(now, cost=2.0), rec(now, tool="codex", cost=5.0, dedup="x")])
    only = store.summary(0, tool="codex")
    assert only["totals"]["cost_usd"] == 5.0
    assert [t["tool"] for t in only["tools"]] == ["codex"]


def test_session_anchor_follows_first_use(store):
    now = time.time()
    t0 = now - 8 * 3600  # opened 8h ago, expired at t0+5h
    store.add([
        rec(t0, dedup="a"),
        rec(t0 + 3600, dedup="b"),          # inside the first session
        rec(now - 90 * 60, dedup="c"),      # >5h after t0: NEW session anchor
        rec(now - 60, dedup="d"),
    ])
    anchor = store.session_anchor("claude_code")
    assert anchor is not None
    assert abs(anchor - (now - 90 * 60)) < 1


def test_summary_and_anchor_model_family_filters(store):
    now = time.time()
    store.add([
        rec(now - 60, tool="gemini_cli", model="gemini-3.1-pro-high",
            cost=3.0, dedup="g1"),
        rec(now - 6 * 3600, tool="gemini_cli",
            model="claude-sonnet-4.6-thinking", cost=2.0, dedup="c1"),
    ])
    gem = store.summary(0, tool="gemini_cli", model_like="gemini%")
    assert gem["totals"]["cost_usd"] == 3.0
    other = store.summary(0, tool="gemini_cli", model_not_like="gemini%")
    assert other["totals"]["cost_usd"] == 2.0
    # Independent anchor chains per pool: gemini session active, claude idle.
    assert store.session_anchor("gemini_cli", model_like="gemini%") is not None
    assert store.session_anchor("gemini_cli",
                                model_not_like="gemini%") is None


def test_session_anchor_weekly_replays_full_history(store):
    now = time.time()
    # 20d ago opens a weekly window (expires 13d ago); 10d ago opens the
    # next (expires 3d ago); yesterday opens the CURRENT one.
    store.add([
        rec(now - 20 * 86400, dedup="w1"),
        rec(now - 10 * 86400, dedup="w2"),
        rec(now - 1 * 86400, dedup="w3"),
    ])
    week = 7 * 86400.0
    anchor = store.session_anchor("claude_code", week, lookback_days=None)
    assert anchor is not None
    assert abs(anchor - (now - 86400)) < 1


def test_session_anchor_none_when_idle(store):
    now = time.time()
    store.add([rec(now - 9 * 3600, dedup="old")])  # expired 4h ago
    assert store.session_anchor("claude_code") is None
    assert store.session_anchor("never_used") is None


def test_prune_drops_only_old_rows(store):
    now = time.time()
    store.add([rec(now - 100 * 86400, dedup="ancient"), rec(now, dedup="new")])
    assert store.prune(now - 90 * 86400) == 1
    assert store.summary(0)["totals"]["requests"] == 1


def test_daily_series_breaks_down_by_tool(store):
    now = time.time()
    store.add([
        rec(now, cost=2.0, dedup="c1"),
        rec(now - 30, tool="gemini_cli", cost=1.0, dedup="g1"),
        rec(now - 2 * 86400, cost=5.0, dedup="c2"),
    ])
    series = store.daily_series(0)
    assert len(series) == 2
    today = series[-1]
    assert today["cost_usd"] == 3.0
    assert today["by_tool"] == {"claude_code": 2.0, "gemini_cli": 1.0}
    assert series[0]["by_tool"] == {"claude_code": 5.0}


def test_rolling_periods_are_trailing_windows():
    now = time.time()
    assert abs(period_start("1h") - (now - 3600)) < 5
    assert abs(period_start("5h") - (now - 5 * 3600)) < 5
    assert abs(period_start("24h") - (now - 86400)) < 5
    assert abs(period_start("week") - (now - 7 * 86400)) < 5
    assert period_start("all") == 0.0
    # calendar windows floor to local midnight / first of month
    assert period_start("today") <= now
    assert time.localtime(period_start("today")).tm_hour == 0
    assert time.localtime(period_start("month")).tm_mday == 1
