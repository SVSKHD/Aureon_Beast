"""Per-symbol agent parameters (§13-§17, D-15).

The level and wick agents carry thresholds that were chosen to be *plausible on gold*
and have never been researched: 5 points of penetration for a sweep, a rejection wick
covering 25% of the candle, 20 points of range before a wick counts. They are v1.0.0
placeholders, and this module exists so that stays true rather than becoming folklore.

## Why a per-symbol hook now, before there is a second symbol

Because the thresholds are **not** dimensionless. ``min_penetration_points = 5`` is
$0.05 on XAUUSD at ``point = 0.01``; on XAGUSD, whose tick is a tenth of that and whose
daily range is a fiftieth, the same number means something entirely different. Running a
second symbol on gold's numbers would produce detections that look like sweeps and are
noise — and nothing downstream would say so, because a sweep is a sweep once it is
written.

So the shape is here before it is needed: one place that answers "what parameters does
this symbol use", defaulting to the researched-on-gold values for everything.

## Changing a value is a new agent_version

Not a preference — a consequence of §12. The parameters live in
``agent_params_snapshot``, and the agent version is part of the ``detection_id``. A
threshold changed without a version bump leaves the *old* detections sitting at ids the
new agent would never produce, with nothing to distinguish them. Bumping the version
makes the two populations separable, which is what makes a comparison possible at all.

That is exactly why D-1 mattered: with ``agent_version`` in the id, retuning these is
safe to do and safe to undo. Without it, every retune silently rewrote history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

#: The shipped values, researched on nothing. Named so the defaults are readable as the
#: placeholders they are rather than as settled numbers.
DEFAULT_MIN_PENETRATION_POINTS = 5.0
DEFAULT_MIN_REJECTION_FRACTION = 0.25
DEFAULT_MIN_CLOSE_BEYOND_POINTS = 10.0
DEFAULT_MIN_WICK_RANGE_RATIO = 0.55
DEFAULT_MIN_WICK_BODY_RATIO = 1.5
DEFAULT_MAX_CLOSE_POSITION = 0.35
DEFAULT_MIN_RANGE_POINTS = 20.0
DEFAULT_FLAT_POINTS = 50.0


@dataclass(frozen=True)
class SymbolTuning:
    """Agent parameters for one symbol.

    Frozen: a tuning that could be mutated after an agent read it would let two agents in
    one process disagree about the same symbol, which is the same class of bug as two
    repositories disagreeing about a collection prefix.
    """

    point: float = 0.01

    # Liquidity (§15)
    min_penetration_points: float = DEFAULT_MIN_PENETRATION_POINTS
    min_rejection_fraction: float = DEFAULT_MIN_REJECTION_FRACTION

    # Breakout (§17)
    min_close_beyond_points: float = DEFAULT_MIN_CLOSE_BEYOND_POINTS

    # Wick (§16)
    min_wick_range_ratio: float = DEFAULT_MIN_WICK_RANGE_RATIO
    min_wick_body_ratio: float = DEFAULT_MIN_WICK_BODY_RATIO
    max_close_position: float = DEFAULT_MAX_CLOSE_POSITION
    min_range_points: float = DEFAULT_MIN_RANGE_POINTS

    # Session trend (§18)
    flat_points: float = DEFAULT_FLAT_POINTS

    #: Set when any value differs from the shipped default, so a reader of a params
    #: snapshot can tell a tuned symbol from an untuned one without diffing.
    overridden: tuple[str, ...] = field(default=())

    @property
    def is_default(self) -> bool:
        return not self.overridden


#: Per-symbol overrides. Empty on purpose: XAUUSD runs the shipped values, and nothing
#: else has been researched. A second symbol adds an entry here rather than editing an
#: agent's defaults, which would silently change gold too.
OVERRIDES: dict[str, dict[str, float]] = {}


def _env_overrides(symbol: str) -> dict[str, float]:
    """``AUREON_TUNING_<SYMBOL>_<FIELD>``, for trying a value without a code change.

    Deliberately awkward to use. These are research parameters, and a threshold set from
    an environment variable is a threshold nobody can reconstruct from the repository six
    months later -- so anything worth keeping belongs in ``OVERRIDES``, in a commit, with
    an agent_version bump beside it.
    """
    found: dict[str, float] = {}
    prefix = f"AUREON_TUNING_{symbol.upper()}_"
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        field_name = key[len(prefix) :].lower()
        if field_name not in {f.name for f in SymbolTuning.__dataclass_fields__.values()}:
            continue
        try:
            found[field_name] = float(raw)
        except ValueError:
            continue
    return found


def tuning_for(symbol: str, *, point: float | None = None) -> SymbolTuning:
    """The parameters for one symbol: shipped defaults unless overridden.

    A symbol with no entry gets the gold-researched values, which is correct for XAUUSD
    and is a **known** approximation for anything else -- ``is_default`` says which.
    """
    overrides: dict[str, float] = dict(OVERRIDES.get(symbol.upper(), {}))
    overrides.update(_env_overrides(symbol))
    if point is not None:
        overrides.setdefault("point", point)

    base = SymbolTuning()
    changed = tuple(
        sorted(
            name
            for name, value in overrides.items()
            if getattr(base, name, None) != value
        )
    )
    return replace(base, **overrides, overridden=changed)
