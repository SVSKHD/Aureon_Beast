"""Configuration and session boundaries (decisions 4, 6, 7, 11)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aureon.config import AureonConfig
from aureon.config.sessions import (
    SESSION_CONFIG_VERSION,
    SESSION_PRECEDENCE,
    day_close,
    session_close,
    session_for,
)
from aureon.models.enums import SessionName

ATHENS = ZoneInfo("Europe/Athens")


def market(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 18, hour, minute, tzinfo=ATHENS)


# ── Defaults (decisions 4, 6) ─────────────────────────────────────────────────


def test_defaults_match_the_decisions() -> None:
    config = AureonConfig.from_env(env={})
    assert config.account_scope == "primary"  # decision 4
    assert config.market_tz == "Europe/Athens"  # decision 6
    # decision 103: V2 is the default; the old name still wins when explicitly set.
    assert config.evaluation_rule_id == "XAU_OUTCOME_V2"


def test_an_empty_allowlist_authorises_nobody() -> None:
    """Fails closed: a missing env var must not become an open trading bot."""
    assert AureonConfig.from_env(env={}).is_authorized("123") is False
    config = AureonConfig.from_env(env={"AUREON_AUTHORIZED_USER_IDS": "111,222"})
    assert config.is_authorized("111") is True
    assert config.is_authorized("999") is False


def test_csv_env_vars_tolerate_whitespace() -> None:
    config = AureonConfig.from_env(env={"AUREON_SYMBOLS": "XAUUSD, EURUSD ,"})
    assert config.symbols == ("XAUUSD", "EURUSD")


@pytest.mark.parametrize(
    "env",
    [
        {"AUREON_MARKET_TZ": "Not/AZone"},
        # Blank (not merely unset) account_scope: see the dedicated tests below.
        {"AUREON_ACCOUNT_SCOPE": "   "},
        {"AUREON_ACCOUNT_SCOPE": "primary "},
        {"AUREON_MAX_LOT": "not-a-number"},
        {"AUREON_TIMEFRAMES": "M7"},
        # A stale threshold at or below the write interval reports the observer
        # STALE while it is writing normally.
        {"AUREON_STATUS_STALE_AFTER_SECONDS": "2"},
    ],
)
def test_invalid_configuration_fails_fast(env: dict[str, str]) -> None:
    with pytest.raises((ValueError, Exception)):
        AureonConfig.from_env(env=env)


def test_an_unset_var_falls_back_to_its_default() -> None:
    """Empty means unset, the ordinary env convention.

    Deliberately distinguished from a blank value: `AUREON_ACCOUNT_SCOPE=` reads
    as "not configured" and takes the default, while a whitespace-only value is a
    mistake worth failing on, because account_scope keys every detection id.
    """
    assert AureonConfig.from_env(env={"AUREON_ACCOUNT_SCOPE": ""}).account_scope == "primary"
    assert AureonConfig.from_env(env={"AUREON_MAX_LOT": ""}).max_lot == 1.0


def test_a_blank_account_scope_is_rejected() -> None:
    with pytest.raises((ValueError, Exception)):
        AureonConfig.from_env(env={"AUREON_ACCOUNT_SCOPE": "   "})


def test_secrets_never_appear_in_a_repr_or_str() -> None:
    """A config printed in a traceback must not leak the MT5 password."""
    config = AureonConfig.from_env(
        env={"AUREON_MT5_PASSWORD": "hunter2xyz", "AUREON_DISCORD_TOKEN": "SECRETVALUE"}
    )
    for rendered in (repr(config), str(config)):
        assert "hunter2xyz" not in rendered
        assert "SECRETVALUE" not in rendered
    # The values are still usable by the code that needs them.
    assert config.mt5_password == "hunter2xyz"
    assert config.discord_token == "SECRETVALUE"


def test_from_env_does_not_leak_into_the_process_environment() -> None:
    """Tests pass an explicit env; it must be restored afterwards."""
    import os

    before = dict(os.environ)
    AureonConfig.from_env(env={"AUREON_ACCOUNT_SCOPE": "throwaway"})
    assert dict(os.environ) == before


# ── Sessions (decision 7) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hour,expected",
    [
        (0, SessionName.OFF),
        (1, SessionName.OFF),
        (2, SessionName.ASIA),
        (9, SessionName.ASIA),
        (10, SessionName.LONDON),
        (14, SessionName.LONDON),
        (15, SessionName.LONDON),  # overlap with New York
        (17, SessionName.LONDON),  # overlap with New York
        (18, SessionName.NEW_YORK),
        (22, SessionName.NEW_YORK),
        (23, SessionName.OFF),
    ],
)
def test_session_windows_and_overlap_precedence(hour: int, expected: SessionName) -> None:
    """Decision 7: London wins the 15-18 overlap, by explicit precedence.

    Relying on dict declaration order instead would make session assignment
    sensitive to an unrelated edit.
    """
    assert session_for(market(hour)) is expected


def test_precedence_is_explicit_and_london_first() -> None:
    assert SESSION_PRECEDENCE[0] is SessionName.LONDON


def test_boundaries_are_half_open_so_a_minute_belongs_to_one_session() -> None:
    """Asia ends and London starts at 10:00; 10:00 must be London alone."""
    assert session_for(market(9, 59)) is SessionName.ASIA
    assert session_for(market(10, 0)) is SessionName.LONDON


def test_session_close_returns_the_windows_end() -> None:
    assert session_close(market(14)) == market(18)
    assert session_close(market(18)) == market(23)


def test_off_session_has_no_close_to_wait_for() -> None:
    assert session_close(market(1)) is None


def test_day_close_is_the_next_market_midnight() -> None:
    assert day_close(market(14)) == datetime(2026, 9, 19, 0, 0, tzinfo=ATHENS)


def test_session_config_version_is_stamped_and_positive() -> None:
    """Detections carry this so a later boundary change cannot reinterpret
    old data (decision 7)."""
    assert SESSION_CONFIG_VERSION >= 1
