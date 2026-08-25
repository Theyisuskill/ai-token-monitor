import json
import time

import pytest

from ai_token_monitor.cli import waybar_status
from ai_token_monitor.config import DEFAULTS, Config
from ai_token_monitor.models import UsageRecord
from ai_token_monitor.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "usage.db")
    yield s
    s.close()


def cfg(**overrides):
    data = dict(DEFAULTS)
    data.update(overrides)
    return Config(data)


def rec(ts, tool="claude_code", cost=1.0, dedup=None):
    return UsageRecord(tool=tool, model="m", ts=ts, input_tokens=1,
                       cost_usd=cost, dedup_key=dedup or f"{tool}:{ts}")


def test_empty_store_is_calm(store):
    out = waybar_status(store, cfg())
    assert out["text"] == "0%"
    assert out["class"] == "ok"
    assert out["tooltip"] == "no usage recorded"
    json.dumps(out)  # serializable


def test_pressure_class_and_tooltip(store):
    now = time.time()
    # default plans: claude pro -> 5h budget $15. $14 in-session = 93% -> danger
    store.add([rec(now - 60, cost=14.0)])
    out = waybar_status(store, cfg())
    assert out["class"] == "danger"
    assert out["percentage"] >= 90
    assert "Claude Code" in out["tooltip"]
    assert "resets in" in out["tooltip"]  # both windows are active


def test_idle_session_counts_zero_for_5h(store):
    now = time.time()
    store.add([rec(now - 9 * 3600, cost=14.0, dedup="old")])  # expired session
    out = waybar_status(store, cfg())
    # 5h pressure gone; weekly ($14 of $75 = 19%) is what remains
    assert out["class"] == "ok"
    assert "5h $0.00" in out["tooltip"] or "5h 0%" in out["tooltip"]
    # the 5h part carries no reset (idle), the weekly window does
    part_5h = out["tooltip"].split("wk")[0]
    assert "resets in" not in part_5h
    assert "resets in" in out["tooltip"]


def test_multiple_tools_one_line_each(store):
    now = time.time()
    store.add([rec(now, cost=1.0), rec(now, tool="gemini_cli", cost=0.5,
                                       dedup="g")])
    tooltip = waybar_status(store, cfg())["tooltip"]
    assert len(tooltip.splitlines()) == 2
    assert "agy" in tooltip


def test_weekly_only_plan_shows_no_5h_part(store, tmp_path):
    """Codex on ChatGPT Go has a weekly allowance and no session window: the
    tooltip must not offer a 5h figure that stands for nothing."""
    now = time.time()
    store.add([rec(now - 60, tool="codex", cost=2.0)])
    # Isolate from this machine's real ~/.codex/auth.json.
    isolated = {"codex": {"credentials": str(tmp_path / "absent.json")}}
    out = waybar_status(store, cfg(plans={"codex": "go"}, live_limits=isolated))
    assert "Codex ·" in out["tooltip"]
    assert "5h" not in out["tooltip"]
    assert "wk" in out["tooltip"]


def test_plus_plan_keeps_both_windows(store, tmp_path):
    now = time.time()
    store.add([rec(now - 60, tool="codex", cost=2.0)])
    isolated = {"codex": {"credentials": str(tmp_path / "absent.json")}}
    out = waybar_status(store, cfg(plans={"codex": "plus"}, live_limits=isolated))
    assert "5h" in out["tooltip"] and "wk" in out["tooltip"]
