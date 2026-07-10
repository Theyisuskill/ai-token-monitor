import json
from pathlib import Path

from ai_token_monitor import config as cfg
from ai_token_monitor.adapters.codex import CodexAdapter, FALLBACK_MODEL
from ai_token_monitor.pricing import CostEngine

ROLLOUT = Path("/x/.codex/sessions/2026/07/09/rollout-2026-07-09T10-00-00-abcd.jsonl")


def _lines():
    return [
        json.dumps({"timestamp": "2026-07-09T10:00:00.000Z", "type": "session_meta",
                    "payload": {"id": "abcd", "cli_version": "0.29.0"}}),
        json.dumps({"timestamp": "2026-07-09T10:00:01.000Z", "type": "turn_context",
                    "payload": {"model": "gpt-5.1-codex", "effort": "medium"}}),
        json.dumps({"timestamp": "2026-07-09T10:00:05.000Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 2168, "output_tokens": 510},
                        "last_token_usage": {"input_tokens": 2168,
                                             "cached_input_tokens": 2048,
                                             "output_tokens": 510}}}}),
        json.dumps({"timestamp": "2026-07-09T10:01:00.000Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 3832,
                                             "cached_input_tokens": 2952,
                                             "output_tokens": 390}}}}),
        # ignored: non-usage event, zero usage, malformed json, null timestamp
        json.dumps({"timestamp": "2026-07-09T10:01:02.000Z", "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "hi"}}),
        json.dumps({"timestamp": "2026-07-09T10:01:03.000Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 0, "output_tokens": 0}}}}),
        "not json {{{",
        json.dumps({"timestamp": None, "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 5, "output_tokens": 5}}}}),
    ]


def test_parse_extracts_per_turn_usage():
    records = list(CodexAdapter({}).parse(ROLLOUT, _lines()))
    assert len(records) == 2
    first = records[0]
    assert first.tool == "codex"
    assert first.model == "gpt-5.1-codex"
    assert first.input_tokens == 2168 - 2048  # cached is a subset of input
    assert first.cache_read_tokens == 2048
    assert first.output_tokens == 510
    assert first.session_id == ROLLOUT.stem


def test_dedup_keys_stable_and_unique():
    adapter = CodexAdapter({})
    once = [r.dedup_key for r in adapter.parse(ROLLOUT, _lines())]
    again = [r.dedup_key for r in adapter.parse(ROLLOUT, _lines())]
    assert once == again  # reparsing a file is idempotent
    assert len(set(once)) == len(once)


def test_model_falls_back_without_turn_context():
    lines = _lines()[2:3]  # a token_count with no preceding turn_context
    records = list(CodexAdapter({}).parse(ROLLOUT, lines))
    assert records[0].model == FALLBACK_MODEL


def test_gpt_pricing_rule_applies():
    record = next(iter(CodexAdapter({}).parse(ROLLOUT, _lines())))
    cost = CostEngine(cfg.DEFAULT_PRICING).cost(record)
    expected = (120 * 1.25 + 510 * 10.0 + 2048 * 0.125) / 1e6
    assert abs(cost - expected) < 1e-9


def test_matches_only_rollout_jsonl():
    adapter = CodexAdapter({})
    assert adapter.matches(Path("a/rollout-2026-07-09T10-00-00-x.jsonl"))
    assert not adapter.matches(Path("a/other.jsonl"))
    assert not adapter.matches(Path("a/rollout-x.log"))
