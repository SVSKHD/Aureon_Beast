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
this symbol use" -- and, since P-6, one place that answers "may this symbol be observed
at all". The observer refuses to start on a symbol with no entry (``require_tuning``),
which is a behaviour change from the silent default it had before: gold's numbers on an
unreviewed instrument produce detections that look like sweeps and are noise, and nothing
downstream can tell, because a sweep is a sweep once it is written.

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

# ── 9B: volume profile and volatility context ────────────────────────────────
# Also placeholders, and worth saying so twice: a bin width decides what a "node" is, and
# a regime band decides what "high volatility" means. Both are choices about resolution,
# not measurements of the market.
#
# The bin width is in POINTS, so it is as instrument-specific as every other distance
# here: 10 points is $0.10 of gold and $0.01 of silver. Too wide and the POC is the
# session's mid; too narrow and every candle is its own node.
DEFAULT_VOLUME_BIN_POINTS = 10.0
#: The share of a scope's volume the value area covers (§19's 70%). Not per symbol -- it
#: is the definition of a value area, not a parameter of an instrument.
VALUE_AREA_FRACTION = 0.70
#: Session range as a multiple of the 20-day median, below which the regime reads `low`
#: and above which it reads `high`. Dimensionless ratios, so they are NOT scaled per
#: symbol -- a session half its usual size means the same thing on any instrument.
DEFAULT_LOW_VOLATILITY_RATIO = 0.70
DEFAULT_HIGH_VOLATILITY_RATIO = 1.40
#: Bumped when any regime band changes, so a stored `VolatilityContext` says which bands
#: produced its verdict. A regime is a label, and a label whose definition moved silently
#: is worse than no label (§21's discipline, applied to context rather than to outcomes).
VOLATILITY_BANDS_VERSION = 1


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

    # Volume profile and volatility (9B, §19)
    volume_bin_points: float = DEFAULT_VOLUME_BIN_POINTS
    low_volatility_ratio: float = DEFAULT_LOW_VOLATILITY_RATIO
    high_volatility_ratio: float = DEFAULT_HIGH_VOLATILITY_RATIO

    #: Set when any value differs from the shipped default, so a reader of a params
    #: snapshot can tell a tuned symbol from an untuned one without diffing.
    overridden: tuple[str, ...] = field(default=())

    @property
    def is_default(self) -> bool:
        return not self.overridden


#: The tuning table: every symbol Aureon is willing to observe, and what it uses.
#:
#: Membership is the point, not just the values. A symbol **absent from this table has no
#: reviewed entry**, and the observer refuses to start on one (``require_tuning``) rather
#: than running it on gold's numbers and producing detections that look like sweeps and
#: are noise. An entry may be empty -- XAUUSD's values ARE the shipped defaults -- but
#: somebody has to have written the line.
OVERRIDES: dict[str, dict[str, float]] = {
    # The symbol everything is calibrated against. The values live in the DEFAULT_*
    # constants above; the empty entry says "reviewed, and the defaults are the answer",
    # which is a different statement from "nobody has looked".
    "XAUUSD": {"point": 0.01},
    # ── XAGUSD: a PLACEHOLDER OF A PLACEHOLDER ────────────────────────────────────
    # Derived by arithmetic from gold's unresearched numbers, so it is one step further
    # from evidence than they are. Worth saying plainly before anyone reads a silver
    # detection as a measurement.
    #
    # The derivation, so it can be argued with rather than guessed at: each DISTANCE
    # threshold is the same **fraction of price** as gold's, re-expressed in silver's own
    # tick. Gold's 5 points of penetration is $0.05 against ~$2400, or 2.08e-5 of price;
    # the same fraction of ~$30 is $0.000625, which at a 0.001 tick is **0.625 of one
    # tick**. It is not representable, so it is clamped to 1 -- and that clamp is the
    # finding, not a detail: gold's placeholder thresholds are FINER THAN ONE SILVER
    # TICK, so silver cannot express them at all and its smallest possible penetration is
    # already ~1.6x larger in relative terms. Expect noise or nothing from this symbol
    # until somebody researches it, and read `is_default` before believing any of it.
    #
    # A second derivation, by the ratio of typical DAILY RANGES (gold ~$30 on ~$2400,
    # silver ~$0.60 on ~$30, so ~50x rather than ~80x), gives values about 1.6x larger:
    # 1 / 2 / 4 / 10. It was what this entry held before Phase 9A. The two disagree
    # because silver's daily range is a larger share of its price than gold's (~2% vs
    # ~1.25%), and that disagreement is the honest measure of how unresearched both are.
    # The price-fraction numbers are kept because they are the more conservative of the
    # two -- a smaller threshold selects more events, and over-selecting is visible in a
    # reached-N table where under-selecting is invisible.
    #
    # Changing these forks nothing and needs no agent_version bump (contrast decision
    # 120): no XAGUSD detection has ever been stored, and XAUUSD's values do not move.
    #
    # The RATIOS are deliberately not scaled. min_rejection_fraction, the wick ratios
    # and max_close_position are dimensionless -- a wick covering 25% of a candle means
    # the same thing on any instrument -- so scaling them would be inventing a
    # difference rather than correcting one.
    "XAGUSD": {
        "point": 0.001,
        # 0.625 by the arithmetic; clamped to one tick, which is the smallest a
        # penetration can be and is therefore not a chosen number at all.
        "min_penetration_points": 1.0,
        "min_close_beyond_points": 1.25,
        "min_range_points": 2.5,
        "flat_points": 6.25,
        # 10 points of gold is $0.10, 4.2e-5 of price; the same fraction of ~$30 is
        # $0.00125, or 1.25 ticks. Rounded to 2 rather than clamped to 1: a one-tick bin
        # would make every candle its own node on an instrument whose whole daily range is
        # ~600 ticks, and a profile with 600 bins has no peaks to speak of. The ratios
        # (low/high volatility) are dimensionless and deliberately unscaled.
        "volume_bin_points": 2.0,
    },
}


class UnknownSymbolTuning(KeyError):
    """Raised for a symbol with no entry in the tuning table."""


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


def known_symbols() -> tuple[str, ...]:
    """Every symbol with a reviewed tuning entry."""
    return tuple(sorted(OVERRIDES))


def has_tuning(symbol: str) -> bool:
    return symbol.upper() in OVERRIDES


def tuning_for(symbol: str, *, point: float | None = None) -> SymbolTuning:
    """The parameters for one symbol: shipped defaults unless the table says otherwise.

    **Tolerant on purpose.** A symbol with no entry gets the gold-researched values,
    because the reporting and verification tools call this for whatever symbol they are
    handed and must not explode mid-report. ``is_default`` says which case a reader is
    looking at. The place that must NOT be tolerant is the observer's start-up, and that
    calls ``require_tuning``.

    An explicitly passed ``point`` **wins over the table**. The table's value is what we
    expect the tick to be; ``symbol_info.point`` is what it is, and a caller passing it
    has read it from the broker.
    """
    overrides: dict[str, float] = dict(OVERRIDES.get(symbol.upper(), {}))
    overrides.update(_env_overrides(symbol))
    if point is not None:
        overrides["point"] = point

    base = SymbolTuning()
    changed = tuple(
        sorted(
            name
            for name, value in overrides.items()
            if getattr(base, name, None) != value
        )
    )
    return replace(base, **overrides, overridden=changed)


def require_tuning(symbol: str, *, point: float | None = None) -> SymbolTuning:
    """As ``tuning_for``, but refuses a symbol nobody has reviewed.

    The fail-fast the observer uses at start-up, and a deliberate behaviour change from
    the silent default it had before. Running a second symbol on gold's numbers produces
    detections that *look* like sweeps and are noise -- and nothing downstream says so,
    because a sweep is a sweep once it is written. Refusing at start-up costs a minute;
    a session of plausible-looking noise costs the session and everything computed from
    it afterwards.
    """
    if not has_tuning(symbol):
        raise UnknownSymbolTuning(
            f"{symbol} has no entry in aureon/config/symbol_tuning.py, so its agent "
            f"thresholds would silently be gold's. Reviewed symbols: "
            f"{', '.join(known_symbols())}. Add an entry -- an empty one is a valid "
            "answer, it means the shipped defaults were reviewed and kept -- and bump "
            "the agent versions if you change a value (§12)."
        )
    return tuning_for(symbol, point=point)
