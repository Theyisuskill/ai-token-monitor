import json
from pathlib import Path

from ai_token_monitor import config as cfg
from ai_token_monitor.adapters.codex import CodexAdapter, FALLBACK_MODEL
from ai_token_monitor.pricing import CostEngine
from ai_token_monitor.store import Store

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


def _usage_event(total=2168, timestamp="2026-07-09T10:00:05.000Z", percent=25):
    event = json.loads(_lines()[2])
    event["timestamp"] = timestamp
    event["payload"]["info"]["total_token_usage"]["input_tokens"] = total
    event["payload"]["rate_limits"] = {"primary": {"used_percent": percent}}
    return json.dumps(event)


def test_repeated_counter_ignores_timestamp_and_quota_changes():
    first = _usage_event()
    repeat = _usage_event(timestamp="2026-07-09T10:00:06.000Z", percent=26)
    records = list(CodexAdapter({}).parse(ROLLOUT, [first, repeat]))
    assert len(records) == 2  # storage performs idempotent ingestion
    assert records[0].dedup_key == records[1].dedup_key


def test_real_new_usage_with_identical_last_delta_is_not_dropped():
    records = list(CodexAdapter({}).parse(ROLLOUT, [_usage_event(), _usage_event(total=4336)]))
    assert records[0].dedup_key != records[1].dedup_key


def test_repeat_is_deduplicated_across_chunks_and_daemon_restart(tmp_path):
    store = Store(tmp_path / "usage.db")
    first = list(CodexAdapter({}).parse(ROLLOUT, [_usage_event()]))
    repeat = list(CodexAdapter({}).parse(ROLLOUT, [
        _usage_event(timestamp="2026-07-09T11:00:06.000Z", percent=26)]))
    assert store.add(first) == 1
    store.close()
    store = Store(tmp_path / "usage.db")
    assert store.add(repeat) == 0
    assert store.summary(0)["totals"]["total_tokens"] == 2678
    store.close()


def test_same_counters_in_different_sessions_are_not_deduplicated():
    first = next(CodexAdapter({}).parse(ROLLOUT, [_usage_event()]))
    second = next(CodexAdapter({}).parse(Path("/x/rollout-another-session.jsonl"), [_usage_event()]))
    assert first.dedup_key != second.dedup_key


def test_missing_cumulative_usage_retains_legacy_identity():
    event = json.loads(_usage_event())
    del event["payload"]["info"]["total_token_usage"]
    record = next(CodexAdapter({}).parse(ROLLOUT, [json.dumps(event)]))
    assert ":total:" not in record.dedup_key


def test_bad_token_counts_do_not_block_later_records():
    lines = []
    for bad in ("broken", {}, True, -1, 1.25):
        event = json.loads(_usage_event())
        event["payload"]["info"]["last_token_usage"]["output_tokens"] = bad
        lines.append(json.dumps(event))
    lines.append(_usage_event())
    assert len(list(CodexAdapter({}).parse(ROLLOUT, lines))) == 1


def test_migration_rekeys_old_records_and_removes_only_proven_repeats(tmp_path):
    from dataclasses import replace
    import hashlib

    lines = [_usage_event(), _usage_event(timestamp="2026-07-09T11:00:06.000Z")]
    store = Store(tmp_path / "usage.db")
    parsed = list(CodexAdapter({}).parse(ROLLOUT, lines))
    legacy = [replace(record, dedup_key=f"{ROLLOUT.stem}:{hashlib.sha1(line.encode()).hexdigest()[:24]}")
              for record, line in zip(parsed, lines)]
    # Usage from a log that is no longer available must be preserved.
    unknown = replace(parsed[0], dedup_key="unavailable-log", session_id="old-session")
    store.add([*legacy, unknown])
    removed = store.migrate_dedup_keys("codex", CodexAdapter({}).dedup_migration(ROLLOUT, lines), "test")
    assert removed == 1
    assert store.summary(0)["totals"]["requests"] == 2
    assert store.add(parsed) == 0  # a subsequent full rescan cannot double-count
    assert store.migration_done("test")
    assert store.migrate_dedup_keys("codex", iter(()), "test") == 0
    store.close()
