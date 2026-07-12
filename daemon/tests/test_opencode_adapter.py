"""OpenCode (.db) parsing.

The synthetic db mirrors the real schema slice the adapter reads: one
``message`` row per turn, whose ``data`` column is the JSON object OpenCode
stores (role, modelID, tokens{input/output/reasoning/cache}, cost, time).
"""

import json
import sqlite3
from pathlib import Path

from ai_token_monitor.adapters.opencode import OpenCodeAdapter


def _msg(role="assistant", model="minimax-m3", inp=60, out=70, reasoning=0,
         cache_read=10509, cache_write=0, cost=0.00024418, created_ms=1781826586131):
    return {
        "role": role,
        "modelID": model,
        "providerID": "opencode-go",
        "cost": cost,
        "tokens": {
            "total": inp + out + reasoning + cache_read + cache_write,
            "input": inp, "output": out, "reasoning": reasoning,
            "cache": {"write": cache_write, "read": cache_read},
        },
        "time": {"created": created_ms, "completed": created_ms + 2000},
    }


def make_db(tmp_path, rows):
    path = tmp_path / "opencode.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT, "
        "time_created INTEGER, data TEXT)")
    con.executemany("INSERT INTO message VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return path


def test_parse_extracts_per_turn_usage(tmp_path):
    path = make_db(tmp_path, [
        ("msg_a", "ses_1", 1781826586000, json.dumps(_msg())),
    ])
    records = list(OpenCodeAdapter({}).parse(path, []))
    assert len(records) == 1
    r = records[0]
    assert r.tool == "opencode"
    assert r.model == "minimax-m3"
    assert r.input_tokens == 60
    assert r.output_tokens == 70          # output + reasoning
    assert r.cache_read_tokens == 10509
    assert r.cache_write_tokens == 0
    assert r.session_id == "ses_1"
    assert r.dedup_key == "msg_a"
    assert r.ts == 1781826586131 / 1000.0  # from data.time.created (ms)


def test_reasoning_folded_into_output(tmp_path):
    path = make_db(tmp_path, [
        ("msg_r", "ses_1", 0, json.dumps(_msg(out=70, reasoning=25))),
    ])
    r = next(iter(OpenCodeAdapter({}).parse(path, [])))
    assert r.output_tokens == 95


def test_cost_is_passed_through_verbatim(tmp_path):
    # OpenCode already priced the turn; the adapter must not zero it out for an
    # unknown model. The daemon keeps a non-zero adapter cost.
    path = make_db(tmp_path, [
        ("msg_c", "ses_1", 0, json.dumps(_msg(model="some-exotic-model",
                                              cost=0.0731))),
    ])
    r = next(iter(OpenCodeAdapter({}).parse(path, [])))
    assert r.cost_usd == 0.0731


def test_skips_user_and_empty_turns(tmp_path):
    path = make_db(tmp_path, [
        ("m1", "s", 0, json.dumps(_msg(role="user"))),           # not assistant
        ("m2", "s", 0, json.dumps(_msg(inp=0, out=0, reasoning=0,
                                       cache_read=0, cache_write=0))),  # empty
        ("m3", "s", 0, "not json {{{"),                          # malformed
        ("m4", "s", 0, json.dumps({"role": "assistant"})),       # no tokens
        ("m5", "s", 0, json.dumps(_msg())),                      # the only real one
    ])
    records = list(OpenCodeAdapter({}).parse(path, []))
    assert [r.dedup_key for r in records] == ["m5"]


def test_timestamp_falls_back_to_row_time(tmp_path):
    entry = _msg()
    del entry["time"]
    path = make_db(tmp_path, [("m1", "s", 1781800000000, json.dumps(entry))])
    r = next(iter(OpenCodeAdapter({}).parse(path, [])))
    assert r.ts == 1781800000000 / 1000.0


def test_dedup_keys_stable_and_unique(tmp_path):
    path = make_db(tmp_path, [
        ("msg_a", "s", 0, json.dumps(_msg())),
        ("msg_b", "s", 0, json.dumps(_msg(created_ms=1781826590000))),
    ])
    adapter = OpenCodeAdapter({})
    once = [r.dedup_key for r in adapter.parse(path, [])]
    again = [r.dedup_key for r in adapter.parse(path, [])]
    assert once == again                       # reparsing is idempotent
    assert len(set(once)) == len(once) == 2


def test_matches_only_the_db_file():
    adapter = OpenCodeAdapter({})
    assert adapter.matches(Path("/x/opencode/opencode.db"))
    assert not adapter.matches(Path("/x/opencode/opencode.db-wal"))
    assert not adapter.matches(Path("/x/opencode/auth.json"))


def test_default_root_honours_xdg(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
    assert OpenCodeAdapter({}).roots() == [Path("/custom/data/opencode")]
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert OpenCodeAdapter({}).roots()[0].name == "opencode"
