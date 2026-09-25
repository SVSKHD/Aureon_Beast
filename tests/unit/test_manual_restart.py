"""Durable manual restart request used by Discord /restart."""

from datetime import UTC, datetime, timedelta

from aureon.services import manual_restart


def test_restart_request_waits_for_discord_ack_window(tmp_path, monkeypatch) -> None:
    created = datetime(2026, 9, 25, 13, 0, tzinfo=UTC)
    manual_restart.request_restart(
        tmp_path,
        requested_by="42",
        reason="operator test",
        now=created,
    )

    monkeypatch.setattr(manual_restart, "utc_now", lambda: created + timedelta(seconds=1))
    assert manual_restart.consume_restart_request(tmp_path) is None

    monkeypatch.setattr(manual_restart, "utc_now", lambda: created + timedelta(seconds=3))
    payload = manual_restart.consume_restart_request(tmp_path)
    assert payload is not None
    assert payload["requested_by"] == "42"
    assert payload["reason"] == "operator test"
    assert manual_restart.read_restart_request(tmp_path) is None
