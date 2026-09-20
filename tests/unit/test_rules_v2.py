"""``XAU_OUTCOME_V2`` and the freezing of ``EMA_OUTCOME_V1`` (§21).

A shipped rule's results are stored under ``{detection_id}__{rule_id}``, so editing a
rule does not produce new numbers -- it silently redefines every number already
recorded against it. There is no diff to review and no migration to run, just a
collection whose meaning changed. Hence the definition hash: the freeze is a test, not
a convention.

The other thing tested here is the unit conversion. A threshold in PRICE has to become
points before the tracker can compare it, and the multiplier must come from the
symbol's own ``point`` -- writing 100 into the code would make "$5" mean $5 on gold and
something else on every other instrument, while looking correct in both places.
"""

from __future__ import annotations

import pytest

from aureon.evaluation.rules import (
    EMA_OUTCOME_V1,
    XAU_OUTCOME_V2,
    XAU_V2_AGENTS,
    RuleFrozenError,
    get_rule,
    register,
    registered_rules,
)
from aureon.models.enums import ThresholdUnit
from aureon.models.evaluation import EvaluationRule

#: Pinned by value, deliberately. Recomputing it from the rule under test would make
#: this assertion vacuous -- it would pass whatever the rule said.
EMA_OUTCOME_V1_HASH = "2dda25257fd6f139fcfcb0df14386b862059b385bb65b77626dec5b4efd18f05"


# ── V1 is frozen ──────────────────────────────────────────────────────────────


def test_v1_definition_is_unchanged() -> None:
    """The freeze. If this fails, results already stored under V1 changed meaning.

    The remedy is never to update the constant: it is to revert the edit and publish a
    new ``rule_id``, exactly as V2 did.
    """
    assert EMA_OUTCOME_V1.definition_hash == EMA_OUTCOME_V1_HASH, (
        "EMA_OUTCOME_V1 was edited. Every DetectionEvaluation stored under "
        "'__EMA_OUTCOME_V1' was computed with the old definition and is now "
        "mislabelled. Revert and publish a new rule_id instead."
    )


def test_v1_still_measures_in_points() -> None:
    """The unit is part of the frozen definition, not a new default."""
    assert EMA_OUTCOME_V1.threshold_unit is ThresholdUnit.POINTS
    assert EMA_OUTCOME_V1.thresholds == (3.0, 5.0, 10.0, 15.0, 20.0)
    # A points rule converts to itself, whatever the symbol's tick is.
    assert EMA_OUTCOME_V1.thresholds_in_points(0.01) == EMA_OUTCOME_V1.thresholds
    assert EMA_OUTCOME_V1.thresholds_in_points(0.1) == EMA_OUTCOME_V1.thresholds


def test_a_shipped_rule_cannot_be_retuned_in_place() -> None:
    """Assignment is refused by the model, not merely discouraged."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own frozen-instance error
        EMA_OUTCOME_V1.thresholds = (99.0,)  # type: ignore[misc]


def test_registering_a_different_rule_under_a_taken_id_is_refused() -> None:
    impostor = EvaluationRule(
        rule_id="EMA_OUTCOME_V1",
        reference_price=EMA_OUTCOME_V1.reference_price,
        horizons=EMA_OUTCOME_V1.horizons,
        thresholds=(1.0,),
    )
    with pytest.raises(RuleFrozenError, match="EMA_OUTCOME_V1"):
        register(impostor)


def test_re_registering_the_identical_rule_is_a_no_op() -> None:
    assert register(EMA_OUTCOME_V1) is EMA_OUTCOME_V1


# ── V2 ────────────────────────────────────────────────────────────────────────


def test_v2_is_registered_and_reachable_by_id() -> None:
    assert "XAU_OUTCOME_V2" in registered_rules()
    assert get_rule("XAU_OUTCOME_V2") is XAU_OUTCOME_V2


def test_v2_measures_in_price_not_points() -> None:
    assert XAU_OUTCOME_V2.threshold_unit is ThresholdUnit.PRICE
    assert XAU_OUTCOME_V2.thresholds == (3.0, 5.0, 10.0, 15.0, 20.0)


def test_v2_keeps_v1_horizons_and_reference_price() -> None:
    """Only the scale changed. Changing one thing keeps the two comparable."""
    assert XAU_OUTCOME_V2.horizons == EMA_OUTCOME_V1.horizons
    assert XAU_OUTCOME_V2.reference_price is EMA_OUTCOME_V1.reference_price


def test_v2_and_v1_are_distinct_rules() -> None:
    assert XAU_OUTCOME_V2.rule_id != EMA_OUTCOME_V1.rule_id
    assert XAU_OUTCOME_V2.definition_hash != EMA_OUTCOME_V1.definition_hash


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (0.01, (300.0, 500.0, 1000.0, 1500.0, 2000.0)),
        (0.1, (30.0, 50.0, 100.0, 150.0, 200.0)),
        (0.001, (3000.0, 5000.0, 10000.0, 15000.0, 20000.0)),
    ],
)
def test_price_thresholds_are_scaled_by_the_symbols_own_point(
    point: float, expected: tuple[float, ...]
) -> None:
    """The multiplier is derived, never written down.

    Three ticks rather than one, because a hard-coded 100 would satisfy the
    ``point=0.01`` row and fail the other two -- which is the whole point of the test.
    """
    assert XAU_OUTCOME_V2.thresholds_in_points(point) == expected


def test_a_nonsense_point_is_refused_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValueError, match="point must be positive"):
        XAU_OUTCOME_V2.thresholds_in_points(0.0)


def test_threshold_keys_stay_in_the_unit_the_rule_was_written_in() -> None:
    """A stored result reads "3", meaning $3 -- not "300".

    Keys come from the rule's own values, so a review rendering "reached 3" is
    rendering the number a human specified rather than its internal conversion.
    """
    assert XAU_OUTCOME_V2.threshold_keys == ("3", "5", "10", "15", "20")
    assert XAU_OUTCOME_V2.threshold_keys == EMA_OUTCOME_V1.threshold_keys


def test_v2_applies_to_the_directional_agents_only() -> None:
    """``wick`` emits direction=None, so it has no favourable side to measure.

    Evaluating it would require inventing a direction, and an invented direction makes
    a reached-N figure that describes the invention rather than the market.
    """
    assert XAU_V2_AGENTS == {"ema_cross", "liquidity", "breakout"}
    assert "wick" not in XAU_V2_AGENTS
    assert "rsi" not in XAU_V2_AGENTS
    assert "session_trend" not in XAU_V2_AGENTS
