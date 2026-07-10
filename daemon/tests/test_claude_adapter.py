import json
from pathlib import Path

from ai_token_monitor.adapters.claude_code import ClaudeCodeAdapter

LOG = Path("/x/.claude/projects/-home-me/session-uuid.jsonl")


def _line(**overrides):
    entry = {
        "type": "assistant",
        "timestamp": "2026-06-03T23:05:03.867Z",
        "sessionId": "sess-1",
        "requestId": "req_1",
        "message": {"id": "msg_1", "model": "claude-opus-4-8"},
        "usage": {"input_tokens": 6, "output_tokens": 299,
                  "cache_read_input_tokens": 17258,
                  "cache_creation_input_tokens": 10702},
    }
    entry.update(overrides)
    return json.dumps(entry)


def test_parses_assistant_usage():
    records = list(ClaudeCodeAdapter({}).parse(LOG, [_line()]))
    assert len(records) == 1
    rec = records[0]
    assert rec.model == "claude-opus-4-8"
    assert rec.input_tokens == 6
    assert rec.output_tokens == 299
    assert rec.cache_read_tokens == 17258
    assert rec.cache_write_tokens == 10702
    assert rec.dedup_key == "msg_1:req_1"


def test_skips_synthetic_and_non_assistant():
    lines = [
        _line(message={"id": "m", "model": "<synthetic>"}),
        json.dumps({"type": "user", "message": {}}),
        "garbage",
    ]
    assert list(ClaudeCodeAdapter({}).parse(LOG, lines)) == []


def test_dedup_falls_back_to_uuid():
    line = _line(requestId=None, uuid="line-uuid")
    records = list(ClaudeCodeAdapter({}).parse(LOG, [line]))
    assert records[0].dedup_key == "line-uuid"
