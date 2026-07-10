"""Antigravity (.db) and legacy Gemini CLI (.jsonl) parsing.

The synthetic .db mirrors the real schema slice the adapter reads: usage
protobufs on steps rows (step_type 15) and model names in gen_metadata
(field 1.21), joined by nearest-preceding idx.
"""

import json
import sqlite3

from ai_token_monitor.adapters.gemini_cli import (
    GeminiCliAdapter,
    normalize_model_name,
)


# -- minimal protobuf writers (inverse of the adapter's decoder) --------------

def _varint(n: int) -> bytes:
    out = b""
    while True:
        low, n = n & 0x7F, n >> 7
        out += bytes([low | (0x80 if n else 0)])
        if not n:
            return out


def _f_varint(num: int, val: int) -> bytes:
    return _varint(num << 3) + _varint(val)


def _f_bytes(num: int, data: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(data)) + data


def usage_payload(ts: int, inp: int, out: int, cache: int) -> bytes:
    inner = _f_bytes(1, _f_varint(1, ts)) + _f_bytes(
        9, _f_varint(2, inp) + _f_varint(3, out) + _f_varint(5, cache))
    return _f_bytes(5, inner)


def gen_metadata_blob(display_name: str) -> bytes:
    return _f_bytes(1, _f_bytes(21, display_name.encode()))


def make_db(tmp_path):
    path = tmp_path / "conversations" / "abc123.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, step_payload BLOB)")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
    rows = [
        # usage before any gen_metadata -> default model
        (0, 15, usage_payload(1700000000, 10, 5, 0)),
        (1, 21, b""),
        (3, 15, usage_payload(1700000100, 100, 50, 1000)),
        (5, 15, usage_payload(1700000200, 200, 60, 2000)),
        (7, 15, usage_payload(1700000300, 300, 70, 3000)),
    ]
    con.executemany("INSERT INTO steps VALUES (?, ?, ?)", rows)
    con.executemany("INSERT INTO gen_metadata VALUES (?, ?)", [
        (2, gen_metadata_blob("Gemini 3.1 Pro (High)")),
        (6, gen_metadata_blob("Claude Sonnet 4.6 (Thinking)")),
    ])
    con.commit()
    con.close()
    return path


def test_db_models_join_nearest_preceding_idx(tmp_path):
    path = make_db(tmp_path)
    records = sorted(GeminiCliAdapter({}).parse(path, []), key=lambda r: r.ts)
    assert [r.model for r in records] == [
        "gemini-3.5-flash",            # idx 0: before any gen_metadata
        "gemini-3.1-pro-high",         # idx 3: gm@2
        "gemini-3.1-pro-high",         # idx 5: still gm@2
        "claude-sonnet-4.6-thinking",  # idx 7: gm@6
    ]
    assert records[1].input_tokens == 100
    assert records[1].cache_read_tokens == 1000
    assert records[1].dedup_key == "abc123:3"


def test_db_default_model_is_configurable(tmp_path):
    path = make_db(tmp_path)
    adapter = GeminiCliAdapter({"default_model": "gemini-x"})
    first = min(adapter.parse(path, []), key=lambda r: r.ts)
    assert first.model == "gemini-x"


def test_normalize_model_name():
    assert normalize_model_name("Gemini 3.1 Pro (High)") == "gemini-3.1-pro-high"
    assert normalize_model_name("Claude Sonnet 4.6 (Thinking)") == \
        "claude-sonnet-4.6-thinking"


def test_legacy_jsonl_parse(tmp_path):
    path = tmp_path / "chats" / "session-2026-05-19.jsonl"
    lines = [json.dumps({
        "type": "gemini", "id": "msg-1", "model": "gemini-3-flash-preview",
        "timestamp": "2026-05-19T23:08:00Z",
        "tokens": {"input": 120, "cached": 20, "tool": 5,
                   "output": 30, "thoughts": 7},
    })]
    records = list(GeminiCliAdapter({}).parse(path, lines))
    assert len(records) == 1
    rec = records[0]
    assert rec.model == "gemini-3-flash-preview"
    assert rec.input_tokens == (120 - 20) + 5
    assert rec.output_tokens == 30 + 7
    assert rec.cache_read_tokens == 20
