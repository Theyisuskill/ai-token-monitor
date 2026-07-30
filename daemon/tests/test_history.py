"""History read model: calendar months, sessions and the assembled view.

The daily/5h/weekly numbers mirror provider windows; this is the other half —
plain cost tracking, looking backwards over what is already in the database.
"""

import time
from datetime import datetime

import pytest

from ai_token_monitor.models import UsageRecord
from ai_token_monitor.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "usage.db")
    yield s
    s.close()


def rec(ts, tool="claude_code", model="claude-opus-4-8", cost=1.0,
        session="s1", dedup=None):
    return UsageRecord(tool=tool, model=model, ts=ts, input_tokens=10,
                       output_tokens=20, cache_read_tokens=30,
                       session_id=session, cost_usd=cost,
                       dedup_key=dedup or f"k{ts}:{tool}:{session}")


DAY = 86400.0


def test_monthly_series_groups_by_calendar_month(store):
    now = time.time()
    store.add([rec(now, cost=2.0),
               rec(now - 1, tool="gemini_cli", cost=0.5),
               rec(now - 70 * DAY, cost=3.0)])

    months = store.monthly_series(0)
    assert len(months) == 2
    assert months[0]["month"] < months[1]["month"]  # oldest first
    current = months[-1]
    assert current["month"] == datetime.fromtimestamp(now).strftime("%Y-%m")
    assert current["cost_usd"] == 2.5
    assert current["requests"] == 2
    assert current["by_tool"] == {"claude_code": 2.0, "gemini_cli": 0.5}


def test_monthly_series_honours_since(store):
    now = time.time()
    store.add([rec(now), rec(now - 400 * DAY, cost=9.0)])
    assert len(store.monthly_series(now - 40 * DAY)) == 1


def test_sessions_recent_aggregates_a_conversation(store):
    now = time.time()
    store.add([rec(now - 3600, cost=1.0, session="a", dedup="a1"),
               rec(now - 1800, cost=2.0, session="a", dedup="a2"),
               rec(now - 60, cost=0.5, session="b", dedup="b1")])

    sessions = store.sessions_recent()
    assert [s["session_id"] for s in sessions] == ["b", "a"]  # newest first
    first = sessions[1]
    assert first["requests"] == 2
    assert first["cost_usd"] == 3.0
    assert first["started"] == pytest.approx(now - 3600)
    assert first["ended"] == pytest.approx(now - 1800)
    assert first["duration_s"] == pytest.approx(1800, abs=1)


def test_sessions_recent_reports_the_priciest_model(store):
    now = time.time()
    store.add([rec(now - 10, model="haiku", cost=0.1, dedup="1"),
               rec(now - 9, model="opus", cost=5.0, dedup="2"),
               rec(now - 8, model="haiku", cost=0.2, dedup="3")])
    assert store.sessions_recent()[0]["top_model"] == "opus"


def test_sessions_recent_separates_tools_sharing_an_id(store):
    now = time.time()
    store.add([rec(now, session="dup", dedup="x"),
               rec(now, tool="codex", session="dup", dedup="y")])
    assert len(store.sessions_recent()) == 2


def test_sessions_recent_skips_rows_without_a_session_id(store):
    now = time.time()
    store.add([rec(now, session=""), rec(now - 5, session="real", dedup="r")])
    assert [s["session_id"] for s in store.sessions_recent()] == ["real"]


def test_sessions_recent_honours_limit_and_since(store):
    now = time.time()
    store.add([rec(now - i * 60, session=f"s{i}", dedup=f"d{i}")
               for i in range(5)])
    assert len(store.sessions_recent(limit=2)) == 2
    assert len(store.sessions_recent(since=now - 150)) == 3


def test_history_period_scopes_the_daily_series_not_the_months(store):
    now = time.time()
    store.add([rec(now, cost=1.0), rec(now - 20 * DAY, cost=2.0, dedup="old")])

    week = store.history("week")
    assert week["totals"]["cost_usd"] == 1.0      # 20 days ago is out of range
    assert len(week["daily"]) == 1
    # ...but the month roll-up still spans a year, so month-over-month works
    # even when you're looking at a 7-day chart.
    assert sum(m["cost_usd"] for m in week["monthly"]) == 3.0

    month = store.history("month")
    assert month["totals"]["cost_usd"] == 3.0
    assert len(month["daily"]) == 2


def test_history_all_covers_everything(store):
    store.add([rec(time.time() - 400 * DAY, cost=7.0)])
    assert store.history("all")["totals"]["cost_usd"] == 7.0
    assert store.history("all")["since"] == 0.0


def test_history_unknown_period_falls_back_to_a_month(store):
    # The D-Bus layer validates; the store must still not blow up.
    assert store.history("nonsense")["period"] == "nonsense"
    assert store.history("nonsense")["since"] > 0.0


def test_history_shape_is_complete(store):
    store.add([rec(time.time())])
    out = store.history("month")
    assert set(out) == {"period", "since", "daily", "monthly", "sessions",
                        "models", "tools", "totals", "updated"}


def test_history_on_an_empty_database(store):
    out = store.history("month")
    assert out["daily"] == [] and out["monthly"] == [] and out["sessions"] == []
    assert out["totals"]["cost_usd"] == 0.0
