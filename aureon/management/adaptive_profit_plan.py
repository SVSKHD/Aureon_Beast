"""Translate five adaptive target probabilities into a non-executing profit plan.

The plan is advisory only.  It is designed for Agent 16 / Discord consumption and
never modifies a broker order directly.
"""

from __future__ import annotations

from typing import Any


def adaptive_profit_plan(
    probabilities: dict[str, float | None],
    *,
    current_move: float,
    peak_move: float,
    threshold: float = 0.50,
    minimum_secure_move: float = 5.0,
) -> dict[str, Any]:
    """Return a transparent protection/runner plan from +5/+10/+20/+30/+40 outputs."""
    targets = (5, 10, 20, 30, 40)
    recommended = 0
    for target in targets:
        probability = probabilities.get(str(target))
        if probability is not None and probability >= threshold:
            recommended = target

    secure_ready = peak_move >= minimum_secure_move
    protected_floor = minimum_secure_move if secure_ready else 0.0

    if recommended >= 40:
        mode = "LONG_HOLD_CANDIDATE"
    elif recommended >= 30:
        mode = "STRONG_RUNNER"
    elif recommended >= 20:
        mode = "RUNNER"
    elif recommended >= 10:
        mode = "TARGET"
    elif recommended >= 5:
        mode = "SECURE_MINIMUM"
    else:
        mode = "NO_MODEL_EDGE"

    # Once +5 has been available, the plan never recommends giving the whole
    # move back. The actual executable stop still belongs to Agent 16 and the
    # management execution safety gates.
    if secure_ready and current_move < minimum_secure_move:
        action = "TIGHTEN_OR_EXIT"
    elif secure_ready:
        action = "PROTECT_AND_HOLD" if recommended >= 20 else "PROTECT"
    else:
        action = "HOLD_UNPROTECTED" if recommended >= 5 else "WAIT"

    return {
        "mode": mode,
        "action": action,
        "recommended_target": recommended,
        "minimum_secure_move": minimum_secure_move,
        "secure_ready": secure_ready,
        "protected_floor_recommendation": protected_floor,
        "long_hold_candidate": recommended >= 40,
        "probabilities": {str(target): probabilities.get(str(target)) for target in targets},
        "note": (
            "Advisory only. Agent 16 and execution guards decide whether a protection "
            "level is executable; the model cannot place, modify, or hold an order itself."
        ),
    }
