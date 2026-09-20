"""Per-symbol agent parameters (D-15, §13-§17).

The level and wick thresholds are v1.0.0 placeholders chosen to be plausible on gold and
researched on nothing. This module exists so that stays a stated fact rather than
becoming folklore, and so a second symbol can carry its own values without editing an
agent's defaults — which would silently change gold too.

The property that matters most: adding the hook must not move XAUUSD. A refactor that
quietly retuned the shipped symbol would invalidate every baseline number in the
repository, and under §12 the detections would sit at ids the new parameters would never
produce.
"""

from __future__ import annotations

import pytest

from aureon.config.symbol_tuning import (
    DEFAULT_MIN_PENETRATION_POINTS,
    DEFAULT_MIN_REJECTION_FRACTION,
    OVERRIDES,
    SymbolTuning,
    tuning_for,
)


def test_xauusd_gets_the_shipped_values_unchanged() -> None:
    """The hook must be a no-op for the symbol everything is calibrated against."""
    tuning = tuning_for("XAUUSD")

    assert tuning.is_default is True
    assert tuning.overridden == ()
    assert tuning.min_penetration_points == DEFAULT_MIN_PENETRATION_POINTS
    assert tuning.min_rejection_fraction == DEFAULT_MIN_REJECTION_FRACTION
    assert tuning.point == 0.01


def test_an_unknown_symbol_gets_the_defaults_and_says_so() -> None:
    """Gold's numbers on another instrument are a KNOWN approximation, not a silent one.

    ``is_default`` is what lets a reader of a params snapshot tell "researched for this
    symbol" from "ran on gold's numbers because nobody has looked yet".
    """
    tuning = tuning_for("EURUSD")
    assert tuning.is_default is True
    assert tuning.min_penetration_points == DEFAULT_MIN_PENETRATION_POINTS


def test_a_symbol_can_carry_its_own_values(monkeypatch) -> None:
    monkeypatch.setitem(
        OVERRIDES, "XAGUSD", {"min_penetration_points": 2.5, "point": 0.001}
    )
    tuning = tuning_for("XAGUSD")

    assert tuning.is_default is False
    assert tuning.min_penetration_points == 2.5
    assert tuning.point == 0.001
    assert "min_penetration_points" in tuning.overridden
    # And the untouched values stay at the shipped defaults.
    assert tuning.min_rejection_fraction == DEFAULT_MIN_REJECTION_FRACTION


def test_overriding_one_symbol_does_not_touch_another(monkeypatch) -> None:
    """The failure this whole module exists to prevent."""
    monkeypatch.setitem(OVERRIDES, "XAGUSD", {"min_penetration_points": 2.5})

    assert tuning_for("XAGUSD").min_penetration_points == 2.5
    assert tuning_for("XAUUSD").min_penetration_points == DEFAULT_MIN_PENETRATION_POINTS
    assert tuning_for("XAUUSD").is_default is True


def test_the_lookup_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setitem(OVERRIDES, "XAGUSD", {"min_penetration_points": 2.5})
    assert tuning_for("xagusd").min_penetration_points == 2.5


def test_an_environment_override_works_for_a_one_off_experiment(monkeypatch) -> None:
    monkeypatch.setenv("AUREON_TUNING_XAUUSD_MIN_PENETRATION_POINTS", "7.5")
    tuning = tuning_for("XAUUSD")
    assert tuning.min_penetration_points == 7.5
    assert tuning.is_default is False


def test_an_unparseable_environment_value_is_ignored_not_crashed(monkeypatch) -> None:
    """A typo in an env var must not take the observer down mid-session."""
    monkeypatch.setenv("AUREON_TUNING_XAUUSD_MIN_PENETRATION_POINTS", "five")
    assert tuning_for("XAUUSD").min_penetration_points == DEFAULT_MIN_PENETRATION_POINTS


def test_an_unknown_field_in_the_environment_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("AUREON_TUNING_XAUUSD_NOT_A_FIELD", "1.0")
    assert tuning_for("XAUUSD").is_default is True


def test_a_tuning_cannot_be_mutated_after_an_agent_reads_it() -> None:
    """Two agents disagreeing about one symbol is the same class of bug as two
    repositories disagreeing about a collection prefix."""
    tuning = tuning_for("XAUUSD")
    with pytest.raises(Exception):  # noqa: B017 - dataclass FrozenInstanceError
        tuning.min_penetration_points = 99.0  # type: ignore[misc]


def test_every_placeholder_has_a_named_default() -> None:
    """So a reader sees them as the placeholders they are, not as settled numbers."""
    from aureon.config import symbol_tuning

    for field_name in SymbolTuning.__dataclass_fields__:
        if field_name in {"point", "overridden"}:
            continue
        constant = f"DEFAULT_{field_name.upper()}"
        assert hasattr(symbol_tuning, constant), f"{field_name} has no named default"


# ── The roster actually uses it ───────────────────────────────────────────────


def test_the_observer_roster_is_unchanged_for_xauusd() -> None:
    """Adding the hook must not move the symbol every baseline number came from.

    A quiet retune here would invalidate docs/PHASE2_BASELINE.md, and under §12 the
    resulting detections would sit at ids the new parameters would never reproduce.
    """
    from aureon.config import AureonConfig
    from main_observer import default_agents

    snapshots = {a.agent_name: a.params_snapshot() for a in default_agents(AureonConfig())}

    assert snapshots["liquidity"]["min_penetration_points"] == 5.0
    assert snapshots["liquidity"]["min_rejection_fraction"] == 0.25
    assert snapshots["breakout"]["min_close_beyond_points"] == 10.0
    assert snapshots["wick"]["min_wick_range_ratio"] == 0.55
    assert snapshots["wick"]["min_range_points"] == 20.0
    assert snapshots["session_trend"]["flat_points"] == 50.0
    for name in ("liquidity", "breakout", "wick", "session_trend"):
        assert snapshots[name]["point"] == 0.01


def test_the_roster_carries_a_symbols_overrides(monkeypatch) -> None:
    """The hook is wired, not merely present."""
    from aureon.config import AureonConfig
    from main_observer import default_agents

    monkeypatch.setitem(OVERRIDES, "XAGUSD", {"min_penetration_points": 2.5})
    config = AureonConfig(symbols=("XAGUSD",))
    snapshots = {a.agent_name: a.params_snapshot() for a in default_agents(config)}

    assert snapshots["liquidity"]["min_penetration_points"] == 2.5
