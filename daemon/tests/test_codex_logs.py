import json
from datetime import datetime, timezone

import pytest

from ai_token_monitor.live.codex import CodexLimitsPoller
from ai_token_monitor.live.codex_logs import LocalLimitsReader, normalize_event


def event(ts=1000, percent=25, minutes=10080, reset=1000000, limit_id="codex"):
    return {"timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "limit_id": limit_id, "plan_type": "prolite", "primary": {
                    "used_percent": percent, "window_minutes": minutes, "resets_at": reset},
                "secondary": None}}}


def write(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry) + "\n")


def test_weekly_only_log_preserves_provider_time_and_source():
    result = normalize_event(event())
    assert result["fetched_at"] == 1000
    assert result["source"] == "local_log"
    assert result["windows"] == {"weekly": {"used_percent": 25, "resets_at": 1000000}}


def test_two_windows_are_classified_by_duration():
    entry = event()
    entry["payload"]["rate_limits"]["secondary"] = {
        "used_percent": 12, "window_minutes": 300, "resets_at": 2000}
    assert normalize_event(entry)["windows"]["five_hour"]["used_percent"] == 12


@pytest.mark.parametrize("entry", [None, [], {}, event(percent="25"),
    event(percent=True), event(percent=float("nan")), event(percent=101),
    event(minutes=0), event(reset=None), event(limit_id="other-model")])
def test_invalid_or_other_quota_is_ignored(entry):
    assert normalize_event(entry) is None


def test_newest_event_wins_across_files_and_polling_never_refreshes_its_age(tmp_path):
    write(tmp_path / "rollout-a.jsonl", event(2000, 30))
    write(tmp_path / "rollout-z.jsonl", event(1000, 25))
    reader = LocalLimitsReader(tmp_path)
    assert reader.poll()["fetched_at"] == 2000
    assert reader.poll()["fetched_at"] == 2000
    with (tmp_path / "rollout-z.jsonl").open("a") as stream:
        stream.write(json.dumps(event(3000, 40)) + "\n")
    assert reader.poll()["windows"]["weekly"]["used_percent"] == 40


def test_partial_line_is_read_only_when_complete_and_junk_is_skipped(tmp_path):
    path = tmp_path / "rollout-a.jsonl"
    line = json.dumps(event())
    path.write_text('broken\n' + line[:30])
    reader = LocalLimitsReader(tmp_path)
    assert reader.poll()["status"] == "local_limits_missing"
    with path.open("a") as stream:
        stream.write(line[30:] + "\n")
    assert reader.poll()["fetched_at"] == 1000


def test_replaced_or_truncated_log_is_reread(tmp_path):
    path = tmp_path / "rollout-a.jsonl"
    write(path, event())
    reader = LocalLimitsReader(tmp_path)
    reader.poll()
    replacement = tmp_path / "replacement"
    write(replacement, event(2000, 50))
    replacement.replace(path)
    assert reader.poll()["fetched_at"] == 2000
    path.write_text("")
    reader.poll()
    write(path, event(3000, 60))
    assert reader.poll()["fetched_at"] == 3000


def test_default_poller_needs_no_credentials_and_never_uses_http(tmp_path, monkeypatch):
    from ai_token_monitor.live import http

    def forbidden(*args, **kwargs):
        pytest.fail("Local quota lookup attempted an HTTP request")

    monkeypatch.setattr(http, "urlopen", forbidden)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write(tmp_path / "sessions/2026/10/17/rollout-a.jsonl", event())
    assert CodexLimitsPoller.is_available({})
    result = CodexLimitsPoller({}).poll()
    assert result["source"] == "local_log"
    assert result["fetched_at"] == 1000


def test_missing_logs_do_not_fall_back_to_http(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert CodexLimitsPoller({}).poll()["status"] == "local_limits_missing"
