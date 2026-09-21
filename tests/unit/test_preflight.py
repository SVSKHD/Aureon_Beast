"""One test per preflight check, and one per way each can be wrong (P-2).

Preflight exists to be believed at 01:55 on a morning nobody wants to debug, so the
tests are about the distinctions it makes rather than about its output: a SKIP is never
a PASS, a WARN never fails the exit code, an unmeasurable clock is not a good clock.

Every dependency is injected. Nothing here needs MetaTrader5, Firestore credentials or
a market -- which is the point, since the machine that runs this suite has none of them
and the machine that runs a session has all three.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureon.config.config import AureonConfig
from aureon.data.historical_provider import DEFAULT_SYMBOL_INFO
from aureon.models.settings import ExecutionSettings
from aureon.services.checks import CheckReport, Status
from aureon.services.preflight import (
    MARKET_IDLE_SECONDS,
    PREFLIGHT_SERVICE,
    Preflight,
)
from aureon.storage import paths
from tests.conftest import InMemoryFirestore

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


class FakeTerminal:
    """A terminal that behaves, until a test makes it misbehave."""

    def __init__(
        self,
        *,
        login: int = 5150,
        server: str = "BrokerX-Demo",
        symbol_info: Any = None,
        tick_at: datetime | None = NOW,
        connect_error: Exception | None = None,
        trade_mode: object = 0,
    ) -> None:
        self.login = login
        self.server = server
        #: MT5's own integer: 0 demo, 1 contest, 2 real (11A F-3). Defaults to 0 because
        #: this double is named BrokerX-*Demo*; a test wanting the live-account row asks
        #: for 2, and one wanting a terminal that will not say passes ``None``.
        self.trade_mode = trade_mode
        self._symbol_info = symbol_info or DEFAULT_SYMBOL_INFO
        self._tick_at = tick_at
        self._connect_error = connect_error
        self.connected = False

    def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    def terminal_info(self) -> dict[str, Any]:
        return {"build": 4410, "name": "MetaTrader 5", "connected": True}

    def account_info(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "server": self.server,
            "currency": "USD",
            "leverage": 100,
            "trade_allowed": True,
            "trade_mode": self.trade_mode,
        }

    def symbol_info(self, symbol: str) -> Any:
        return self._symbol_info.model_copy(update={"symbol": symbol})

    def last_tick_time(self, symbol: str) -> datetime | None:
        return self._tick_at


def build(
    tmp_path: Path,
    *,
    config: AureonConfig | None = None,
    terminal: FakeTerminal | None = None,
    client: Any | None = None,
    now: datetime = NOW,
    **kwargs: Any,
) -> Preflight:
    """A Preflight whose every side effect lands in ``tmp_path``."""
    terminal = terminal if terminal is not None else FakeTerminal()
    kwargs.setdefault("archive_dir", tmp_path / "archive")
    kwargs.setdefault("outbox_path", tmp_path / "outbox.db")
    return Preflight(
        config or AureonConfig(mt5_login=5150, mt5_server="BrokerX-Demo"),
        provider_factory=(lambda: terminal),
        client_factory=(lambda: client if client is not None else InMemoryFirestore()),
        now=lambda: now,
        **kwargs,
    )


# ── config ───────────────────────────────────────────────────────────────────


def test_config_reports_the_identity_and_the_current_offset(tmp_path) -> None:
    result = build(tmp_path).check_config()
    assert result.status is Status.PASS
    assert "account_scope=primary" in result.detail
    assert "ema=20/50" in result.detail
    # The offset, not just the zone name: Athens is +2 or +3 depending on the date, and
    # +3 is what the operator compares against the terminal in September.
    assert "UTC+3 now" in result.detail


def test_config_fails_when_the_symbol_is_not_one_the_observer_watches(tmp_path) -> None:
    result = build(tmp_path, symbol="XAGUSD").check_config()
    assert result.status is Status.FAIL
    assert "XAGUSD" in result.detail


# ── collection_prefix ────────────────────────────────────────────────────────


def test_the_test_prefix_passes(tmp_path) -> None:
    result = build(tmp_path).check_collection_prefix()
    assert result.status is Status.PASS
    assert paths.PREFIX in result.detail


def test_the_production_prefix_warns_but_does_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PREFIX", paths.DEFAULT_COLLECTION_PREFIX)
    result = build(tmp_path).check_collection_prefix()
    assert result.status is Status.WARN
    assert not result.blocking, "writing to production is a warning, not a refusal"
    assert "PRODUCTION" in result.detail


# ── outbox ───────────────────────────────────────────────────────────────────


def test_an_empty_outbox_passes_and_reports_its_counts(tmp_path) -> None:
    result = build(tmp_path).check_outbox()
    assert result.status is Status.PASS
    assert "0 pending" in result.detail


def test_an_outbox_with_undelivered_rows_warns(tmp_path, candles) -> None:
    """Rows left from a previous run are the finding, not the file's existence."""
    from aureon.engine.analysis_engine import AnalysisEngine
    from aureon.outbox.local_outbox import LocalOutbox
    from tests.conftest import MARKET_TZ, cross_agent

    detections = AnalysisEngine(
        [cross_agent()], account_scope="primary", market_tz=MARKET_TZ
    ).feed(candles[:400])
    assert detections, "the fixture must produce something to leave undelivered"
    with LocalOutbox(tmp_path / "outbox.db") as outbox:
        outbox.enqueue(detections[0])

    result = build(tmp_path).check_outbox()
    assert result.status is Status.WARN
    assert "1 pending" in result.detail
    assert "never reached" in (result.remedy or "")


def test_an_unusable_outbox_path_fails(tmp_path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    result = build(tmp_path, outbox_path=blocker / "outbox.db").check_outbox()
    assert result.status is Status.FAIL


# ── archive_dir ──────────────────────────────────────────────────────────────


def test_the_archive_directory_is_created_and_probed(tmp_path) -> None:
    result = build(tmp_path).check_archive_dir()
    assert result.status is Status.PASS
    assert (tmp_path / "archive").is_dir()
    assert "MB free" in result.detail


def test_an_unwritable_archive_directory_fails(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    result = build(tmp_path, archive_dir=blocker / "archive").check_archive_dir()
    assert result.status is Status.FAIL


# ── firestore ────────────────────────────────────────────────────────────────


def test_the_firestore_check_writes_and_reads_back_a_heartbeat(tmp_path) -> None:
    store = InMemoryFirestore()
    result = build(tmp_path, client=store).check_firestore()
    assert result.status is Status.PASS
    assert paths.heartbeat_path(PREFLIGHT_SERVICE) in store.docs
    assert result.detail.startswith(paths.heartbeat_path(PREFLIGHT_SERVICE))


def test_the_preflight_heartbeat_is_not_a_service_heartbeat() -> None:
    """Writing ``heartbeats/observer`` would make a dead observer look alive."""
    assert PREFLIGHT_SERVICE not in paths.SERVICES


def test_a_firestore_failure_is_reported_with_its_cause(tmp_path) -> None:
    store = InMemoryFirestore()
    store.fail_next = 1
    result = build(tmp_path, client=store).check_firestore()
    assert result.status is Status.FAIL
    assert "ConnectionError" in result.detail


def test_a_write_that_does_not_read_back_fails(tmp_path, monkeypatch) -> None:
    """The one failure a client library will not raise on."""
    from aureon.storage.system_state_repository import HeartbeatRepository

    monkeypatch.setattr(HeartbeatRepository, "read", lambda self, service: None)
    result = build(tmp_path).check_firestore()
    assert result.status is Status.FAIL
    assert "read back nothing" in result.detail


# ── trading_enabled ──────────────────────────────────────────────────────────


def test_trading_enabled_is_printed_and_never_judged(tmp_path) -> None:
    store = InMemoryFirestore()
    store.docs[paths.execution_settings_path()] = ExecutionSettings(
        trading_enabled=True, settings_version=4, updated_by="operator"
    ).model_dump(mode="json")

    preflight = build(tmp_path, client=store)
    preflight.check_firestore()  # sets the client
    result = preflight.check_trading_enabled()

    assert result.status is Status.INFO, "preflight has no opinion about this"
    assert not result.blocking
    assert "true" in result.detail and "v4" in result.detail


def test_a_missing_settings_document_reports_the_fail_closed_default(tmp_path) -> None:
    preflight = build(tmp_path)
    preflight.check_firestore()
    result = preflight.check_trading_enabled()
    assert result.status is Status.INFO
    assert result.detail.startswith("false")


def test_trading_enabled_is_skipped_without_a_client(tmp_path) -> None:
    result = build(tmp_path).check_trading_enabled()
    assert result.status is Status.SKIP


# ── mt5_init ─────────────────────────────────────────────────────────────────


def test_a_connected_terminal_reports_its_build(tmp_path) -> None:
    result = build(tmp_path).check_mt5_init()
    assert result.status is Status.PASS
    assert "build 4410" in result.detail, "the checklist asks for the build number"


def test_a_terminal_that_will_not_start_fails(tmp_path) -> None:
    terminal = FakeTerminal(connect_error=RuntimeError("MT5 initialize failed: (-6,)"))
    result = build(tmp_path, terminal=terminal).check_mt5_init()
    assert result.status is Status.FAIL
    assert "initialize failed" in result.detail


def test_skip_mt5_skips_and_never_passes(tmp_path) -> None:
    report = build(tmp_path, skip_mt5=True).run()
    terminal_checks = [
        "mt5_init",
        "mt5_account",
        "symbol_tradable",
        "symbol_specs_published",
        "clock_drift",
    ]
    for name in terminal_checks:
        assert report.get(name).status is Status.SKIP
    assert report.ok, "a skip does not fail the run"
    assert "5 checks not run" in report.summary(), (
        "and it must not be possible to read the summary as a clean pass"
    )


# ── mt5_account ──────────────────────────────────────────────────────────────


def test_the_configured_account_must_be_the_one_logged_in(tmp_path) -> None:
    preflight = build(tmp_path)
    preflight.check_mt5_init()
    result = preflight.check_mt5_account()
    assert result.status is Status.PASS
    assert "login=5150" in result.detail


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        (FakeTerminal(login=9999), "login is 9999"),
        (FakeTerminal(server="BrokerX-Live"), "server is 'BrokerX-Live'"),
    ],
)
def test_a_terminal_on_another_account_or_server_fails(
    tmp_path, terminal, expected
) -> None:
    """The expensive failure: a plausible session whose candles came from elsewhere."""
    preflight = build(tmp_path, terminal=terminal)
    preflight.check_mt5_init()
    result = preflight.check_mt5_account()
    assert result.status is Status.FAIL
    assert expected in result.detail


def test_a_config_that_names_no_account_has_nothing_to_verify(tmp_path) -> None:
    preflight = build(tmp_path, config=AureonConfig())
    preflight.check_mt5_init()
    result = preflight.check_mt5_account()
    assert result.status is Status.WARN
    assert not result.blocking


# ── symbol_tradable ──────────────────────────────────────────────────────────


def test_a_tradable_symbol_reports_its_specs(tmp_path) -> None:
    preflight = build(tmp_path)
    preflight.check_mt5_init()
    result = preflight.check_symbol_tradable()
    assert result.status is Status.PASS
    for fragment in ("point=0.01", "vol=0.01/0.01/50.0", "fill=fok,ioc"):
        assert fragment in result.detail


@pytest.mark.parametrize(
    ("update", "status"),
    [
        ({"trade_mode": "disabled"}, Status.FAIL),
        ({"filling_modes": ()}, Status.FAIL),
        ({"trade_mode": "longonly"}, Status.WARN),
        ({"trade_mode": "close_only"}, Status.WARN),
    ],
)
def test_a_symbol_the_broker_restricts(tmp_path, update, status) -> None:
    terminal = FakeTerminal(symbol_info=DEFAULT_SYMBOL_INFO.model_copy(update=update))
    preflight = build(tmp_path, terminal=terminal)
    preflight.check_mt5_init()
    assert preflight.check_symbol_tradable().status is status


def test_an_unknown_symbol_fails(tmp_path) -> None:
    class Missing(FakeTerminal):
        def symbol_info(self, symbol: str) -> Any:
            raise RuntimeError(f"symbol_info({symbol}) failed")

    preflight = build(tmp_path, terminal=Missing())
    preflight.check_mt5_init()
    result = preflight.check_symbol_tradable()
    assert result.status is Status.FAIL
    assert "Market Watch" in (result.remedy or "")


# ── symbol_specs ─────────────────────────────────────────────────────────────


def test_symbol_specs_are_published_and_verified(tmp_path) -> None:
    store = InMemoryFirestore()
    preflight = build(tmp_path, client=store)
    preflight.check_mt5_init()
    preflight.check_firestore()
    result = preflight.check_symbol_specs()
    assert result.status is Status.PASS
    assert paths.symbol_spec_path("XAUUSD") in store.docs


def test_specs_that_read_back_different_fail(tmp_path, monkeypatch) -> None:
    """Discord validates lots against this copy and can never call the broker."""
    from aureon.storage.symbol_repository import SymbolRepository

    monkeypatch.setattr(
        SymbolRepository,
        "get",
        lambda self, symbol: DEFAULT_SYMBOL_INFO.model_copy(update={"volume_max": 1.0}),
    )
    preflight = build(tmp_path)
    preflight.check_mt5_init()
    preflight.check_firestore()
    result = preflight.check_symbol_specs()
    assert result.status is Status.FAIL


def test_symbol_specs_without_a_client_is_skipped(tmp_path) -> None:
    preflight = build(tmp_path)
    preflight.check_mt5_init()
    result = preflight.check_symbol_specs()
    assert result.status is Status.SKIP


# ── clock_drift ──────────────────────────────────────────────────────────────


def _drift(tmp_path, seconds: float | None, **kwargs):
    tick_at = None if seconds is None else NOW - timedelta(seconds=seconds)
    preflight = build(tmp_path, terminal=FakeTerminal(tick_at=tick_at), **kwargs)
    preflight.check_mt5_init()
    return preflight.check_clock_drift()


def test_a_tick_within_tolerance_passes(tmp_path) -> None:
    assert _drift(tmp_path, 1.0).status is Status.PASS


def test_a_tick_from_the_future_is_unambiguous_drift(tmp_path) -> None:
    """No tick can have happened after now, so this one cannot be a quiet market."""
    result = _drift(tmp_path, -30.0)
    assert result.status is Status.FAIL
    assert "AHEAD" in result.detail


def test_a_lagging_tick_is_inconclusive_rather_than_a_pass_or_a_failure(
    tmp_path,
) -> None:
    """A quiet market and a fast local clock produce the same number."""
    result = _drift(tmp_path, 30.0)
    assert result.status is Status.WARN
    assert "quiet market" in result.detail


def test_a_long_dead_symbol_is_not_measured_at_all(tmp_path) -> None:
    result = _drift(tmp_path, MARKET_IDLE_SECONDS + 60)
    assert result.status is Status.SKIP
    assert "not measured" in result.detail


def test_no_tick_at_all_is_skipped(tmp_path) -> None:
    assert _drift(tmp_path, None).status is Status.SKIP


def test_the_tolerance_is_configurable(tmp_path) -> None:
    assert _drift(tmp_path, 5.0, drift_tolerance_seconds=10.0).status is Status.PASS


# ── The run and its exit code ────────────────────────────────────────────────


def test_a_clean_run_is_ready_and_exits_zero(tmp_path) -> None:
    report = build(tmp_path).run()
    assert [r.name for r in report.results] == [
        "config",
        "collection_prefix",
        "outbox",
        "archive_dir",
        "credentials",
        "firestore",
        "trading_enabled",
        "mt5_init",
        "mt5_account",
        "symbol_tradable",
        "symbol_specs_published",
        "clock_drift",
    ]
    assert report.ok
    assert report.exit_code == 0
    assert report.summary().startswith("READY")


def test_one_failure_makes_the_whole_run_non_zero(tmp_path) -> None:
    terminal = FakeTerminal(connect_error=RuntimeError("no terminal"))
    report = build(tmp_path, terminal=terminal).run()
    assert report.exit_code == 1
    assert [r.name for r in report.failures] == ["mt5_init"]
    # And everything downstream says it was not run, rather than reporting a verdict.
    assert report.get("mt5_account").status is Status.SKIP


def test_warnings_alone_do_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "PREFIX", paths.DEFAULT_COLLECTION_PREFIX)
    report = build(tmp_path).run()
    assert report.warnings
    assert report.exit_code == 0


def test_the_rendered_table_carries_every_check_and_its_remedy(tmp_path) -> None:
    terminal = FakeTerminal(connect_error=RuntimeError("no terminal"))
    text = build(tmp_path, terminal=terminal).run().render()
    assert "What to do:" in text
    assert "mt5_init" in text
    assert "NOT READY" in text
    for result in build(tmp_path).run().results:
        assert result.name in text or result.status is Status.PASS


def test_an_empty_report_renders_without_raising() -> None:
    assert "READY" in CheckReport().summary()


# ── 11A F-2: credentials ──────────────────────────────────────────────────────


def test_the_emulator_skips_the_credentials_check_rather_than_passing_it(
    tmp_path,
) -> None:
    """SKIP, never PASS. A green row for a check that never ran is how an emulator-only
    run comes to look like evidence about production."""
    config = AureonConfig(
        mt5_login=5150, mt5_server="BrokerX-Demo", firestore_emulator_host="127.0.0.1:8080"
    )
    result = build(tmp_path, config=config).check_credentials()
    assert result.status is Status.SKIP
    assert "no credentials needed" in result.detail


def test_a_missing_key_file_fails_with_the_remedy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "absent.json"))
    result = build(tmp_path).check_credentials()
    assert result.status is Status.FAIL
    assert "no such file" in result.detail
    assert "gcloud auth application-default login" in result.remedy


def test_an_oauth_client_secret_is_named_as_the_wrong_file(tmp_path, monkeypatch) -> None:
    """The single most common wrong file: both are JSON, both come from the same console,
    and only one of them works. Telling somebody "invalid credentials" sends them to
    re-download the same file."""
    key = tmp_path / "client_secret.json"
    key.write_text('{"installed": {"client_id": "x"}, "project_id": "p"}')
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))

    result = build(tmp_path).check_credentials()
    assert result.status is Status.FAIL
    assert "client_email" in result.detail
    assert "OAuth client secret" in result.detail


def test_a_key_that_is_not_json_says_so(tmp_path, monkeypatch) -> None:
    key = tmp_path / "key.json"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnope\n")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    assert "not valid JSON" in build(tmp_path).check_credentials().detail


def test_a_real_looking_key_passes_and_reports_who_it_is(tmp_path, monkeypatch) -> None:
    key = tmp_path / "key.json"
    key.write_text(
        '{"client_email": "aureon@p.iam.gserviceaccount.com", '
        '"private_key": "-----BEGIN PRIVATE KEY-----", "project_id": "aureon-prod"}'
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))

    result = build(tmp_path).check_credentials()
    assert result.status is Status.PASS
    assert "aureon@p.iam.gserviceaccount.com" in result.detail
    assert "aureon-prod" in result.detail


def test_no_variable_at_all_warns_rather_than_failing(tmp_path, monkeypatch) -> None:
    """Application default credentials from `gcloud auth` are a legitimate developer setup,
    and the round trip behind this is what decides. But a box about to run unattended for a
    week should hear which path it is on."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    result = build(tmp_path).check_credentials()
    assert result.status is Status.WARN
    assert "application default credentials" in result.detail


def test_a_credentials_error_from_the_round_trip_prints_the_remedy(tmp_path) -> None:
    """The round trip's own translation: "no credentials found" is a different action from
    a permission denial, and both arrive as one opaque exception."""

    class _NoCredentials(InMemoryFirestore):
        def document(self, path: str):
            class DefaultCredentialsError(Exception):
                pass

            raise DefaultCredentialsError("could not automatically determine credentials")

    result = build(tmp_path, client=_NoCredentials()).check_firestore()
    assert result.status is Status.FAIL
    assert "no usable credentials" in result.detail
    assert "GOOGLE_APPLICATION_CREDENTIALS" in result.remedy


# ── 11A F-3: the account mode is on the table ─────────────────────────────────


def test_a_demo_terminal_passes_and_says_so(tmp_path) -> None:
    preflight = build(tmp_path, terminal=FakeTerminal(trade_mode=0))
    preflight.check_mt5_init()
    result = preflight.check_mt5_account()
    assert result.status is Status.PASS
    assert "mode=DEMO" in result.detail


def test_a_live_terminal_warns_and_never_passes_quietly(tmp_path) -> None:
    """Observation on a live account is legitimate and read-only. It is never a silent
    green row: an operator scanning a passing table would not notice."""
    preflight = build(tmp_path, terminal=FakeTerminal(trade_mode=2))
    preflight.check_mt5_init()
    result = preflight.check_mt5_account()
    assert result.status is Status.WARN
    assert "mode=REAL" in result.detail
    assert "REAL MONEY" in result.detail
    assert "AUREON_ALLOW_LIVE_EXECUTION" in result.remedy


def test_a_terminal_that_will_not_say_is_treated_as_live(tmp_path) -> None:
    preflight = build(tmp_path, terminal=FakeTerminal(trade_mode=None))
    preflight.check_mt5_init()
    result = preflight.check_mt5_account()
    assert result.status is Status.WARN
    assert "mode=UNKNOWN" in result.detail
