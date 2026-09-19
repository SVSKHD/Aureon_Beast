"""Evaluation rules and the registry that keeps them frozen (§21).

A rule says what "did it work?" means: which price to measure from, how far ahead to
look, and what distances count as having got there.

## Frozen means frozen

Once a rule has been used to store results, its definition can never change. A
retuned threshold is a **new ``rule_id``**, never an edit. The reason is that results
are stored keyed by ``{detection_id}__{rule_id}``: editing ``EMA_OUTCOME_V1`` in place
would silently redefine what every already-stored document meant, and nothing in the
data would reveal that the old and new numbers are no longer comparable. The model
itself is immutable, and ``register`` refuses to replace a rule that already exists,
so the freeze is enforced rather than remembered.

## A note on the threshold scale

Thresholds are in **points** (a point being the symbol's smallest price increment),
consistent with every other distance in the system. For XAUUSD at ``point = 0.01`` the
specified 3/5/10/15/20 are $0.03 to $0.20, which is small relative to a typical M5
range -- so most of them saturate. That is reported as a finding rather than quietly
retuned (decision 47): the rule is frozen by design, and the mechanism for a better
scale is a new rule_id, not an edit to this one.
"""

from __future__ import annotations

from aureon.models.enums import HorizonKind, ReferencePrice
from aureon.models.evaluation import EvaluationRule, Horizon

# Horizon ids. These become map keys inside stored documents, so renaming one
# orphans the results already written under the old name.
HORIZON_5_CANDLES = "c5"
HORIZON_10_CANDLES = "c10"
HORIZON_20_CANDLES = "c20"
HORIZON_60_MINUTES = "m60"
HORIZON_SESSION_CLOSE = "session_close"
HORIZON_DAY_CLOSE = "day_close"
HORIZON_OPPOSITE_CROSS = "opposite_cross"

EMA_OUTCOME_V1 = EvaluationRule(
    rule_id="EMA_OUTCOME_V1",
    # next_open, not close: a human acting on a detection cannot transact at the close
    # that produced it -- that price is already gone by the time the candle is known to
    # have closed. Measuring from the next open is the first price actually reachable,
    # so the numbers describe an outcome someone could have had.
    reference_price=ReferencePrice.NEXT_OPEN,
    horizons=(
        Horizon(id=HORIZON_5_CANDLES, kind=HorizonKind.CANDLES, value=5),
        Horizon(id=HORIZON_10_CANDLES, kind=HorizonKind.CANDLES, value=10),
        Horizon(id=HORIZON_20_CANDLES, kind=HorizonKind.CANDLES, value=20),
        Horizon(id=HORIZON_60_MINUTES, kind=HorizonKind.MINUTES, value=60),
        Horizon(id=HORIZON_SESSION_CLOSE, kind=HorizonKind.SESSION_CLOSE),
        Horizon(id=HORIZON_DAY_CLOSE, kind=HorizonKind.DAY_CLOSE),
        Horizon(id=HORIZON_OPPOSITE_CROSS, kind=HorizonKind.OPPOSITE_CROSS),
    ),
    thresholds=(3.0, 5.0, 10.0, 15.0, 20.0),
)


class RuleFrozenError(RuntimeError):
    """An attempt to redefine a rule that has already shipped."""


_REGISTRY: dict[str, EvaluationRule] = {}


def register(rule: EvaluationRule, *, replace: bool = False) -> EvaluationRule:
    """Add a rule to the registry.

    Refuses to overwrite an existing rule unless ``replace=True``, which exists only
    so a test can build an isolated registry. Production code never passes it: a
    changed rule is a new ``rule_id``.
    """
    existing = _REGISTRY.get(rule.rule_id)
    if existing is not None and not replace:
        if existing == rule:
            return existing  # idempotent re-registration of the identical rule
        raise RuleFrozenError(
            f"{rule.rule_id} is already registered with a different definition. "
            "A rule is frozen once shipped (§21) -- publish a new rule_id instead of "
            "editing this one, or every stored result under it silently changes meaning."
        )
    _REGISTRY[rule.rule_id] = rule
    return rule


def get_rule(rule_id: str) -> EvaluationRule:
    """Look up a registered rule, or fail with the available ids."""
    try:
        return _REGISTRY[rule_id]
    except KeyError:
        raise KeyError(
            f"unknown evaluation rule {rule_id!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def registered_rules() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


register(EMA_OUTCOME_V1)
