"""Volume profile and volatility context (9B, §19).

Both are **context**, never signals. Nothing here decides anything: an agent may record
that a cross happened at a low-volume node, and a review may group outcomes by volatility
regime, but no code path turns either into a detection or a trade.

## Why a profile is a document shape rather than a number

A "point of control" on its own is unfalsifiable. Two implementations can produce two
different POCs from the same candles and both look plausible, because the answer depends
entirely on the bin width, the scope, and how a candle's volume is spread across its range.
So the stored profile carries the *inputs* -- the scope, its window, and ``bin_points`` --
beside the outputs. A reader can then tell a POC computed over the Asia session at 10-point
bins from one computed over the whole day at 2-point bins, which are different facts wearing
the same field name.

## The volume is estimated, and the field names say so

MT5's M5 candle reports ``tick_volume`` for the bar, not a distribution within it. Spreading
it evenly across the bar's high--low is an **assumption**: it is what a profile built from
candles can do, and it is wrong in a specific way -- a bar that spent most of its life at one
end is reported as though it spent it evenly. Real tick data would answer properly, and
CLAUDE.md forbids storing ticks, so this is the honest best available. Hence ``estimated``
in the docstrings and ``tick_volume`` (not ``volume``) as the source field: a reader should
never believe this is a measured distribution.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import Timeframe

#: Scopes a profile can cover. Deliberately explicit rather than "any window": a profile
#: whose scope is a free-form string is one nobody can group by later.
PROFILE_SCOPES: tuple[str, ...] = (
    "asia",
    "london",
    "new_york",
    "day",
    "rolling_24h",
    "previous_session",
)

#: At most this many bins are stored. A profile is a shape, and 200 bins describe one;
#: the cap exists because a document is read by a human on a phone and because an
#: unbounded map is how a Firestore document grows without anybody deciding to.
MAX_PROFILE_BINS = 200

#: Volatility regimes. Labels over a measured ratio, never a judgement about what to do.
VOLATILITY_REGIMES: tuple[str, ...] = ("low", "normal", "high")


class ProfileBin(AureonModel):
    """One price bin and the volume estimated to have traded in it."""

    model_config = ConfigDict(extra="forbid")

    price: float = Field(description="The bin's LOWER edge, in price.")
    volume: float = Field(ge=0, description="Estimated tick volume in this bin.")


class VolumeProfile(AureonModel):
    """Estimated volume by price over one scope (9B, §19).

    Not a document of its own: it is embedded in ``SystemState`` for the live panels and
    referenced in compressed form on a detection. Storing it as its own collection would
    invite a reader to treat a profile as an observation with a life of its own, when it is
    a derived summary that any replay of the same candles reproduces exactly.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    scope: str = Field(description=f"One of {PROFILE_SCOPES}.")
    start_utc: UtcDatetime
    end_utc: UtcDatetime
    bin_points: float = Field(
        gt=0,
        description="Bin width in POINTS. Part of the record because the POC depends on it.",
    )

    poc_price: float | None = Field(
        default=None, description="Lower edge of the highest-volume bin, or None if empty."
    )
    value_area_high: float | None = None
    value_area_low: float | None = Field(
        default=None,
        description="The band around the POC holding 70% of estimated volume (§19).",
    )
    #: High- and low-volume nodes: local peaks and troughs of the profile, as prices.
    hvn: tuple[float, ...] = ()
    lvn: tuple[float, ...] = ()

    total_volume: float = Field(default=0.0, ge=0)
    bins: tuple[ProfileBin, ...] = Field(
        default=(), description=f"At most {MAX_PROFILE_BINS}, coarsest-first."
    )

    @model_validator(mode="after")
    def _window_ordered(self) -> VolumeProfile:
        if self.end_utc < self.start_utc:
            raise ValueError("profile end_utc is before start_utc")
        if self.scope not in PROFILE_SCOPES:
            raise ValueError(f"unknown profile scope {self.scope!r}")
        if len(self.bins) > MAX_PROFILE_BINS:
            raise ValueError(f"{len(self.bins)} bins exceeds the {MAX_PROFILE_BINS} cap")
        if (
            self.value_area_low is not None
            and self.value_area_high is not None
            and self.value_area_high < self.value_area_low
        ):
            raise ValueError("value_area_high is below value_area_low")
        return self

    @property
    def is_empty(self) -> bool:
        return not self.bins or self.total_volume <= 0


class VolumeProfileRef(AureonModel):
    """What a detection records about the profile that existed when it fired (9B).

    A reference rather than the profile itself, for two reasons. A detection is immutable
    and small, and embedding two hundred bins in each one would make the collection
    unreadable; and the only parts an agent's context needs are where price stood relative
    to the value area and which nodes were nearest.

    Every field is computed from candles that had **already closed** when the detection
    fired. Nothing here may be recomputed later from a fuller profile -- that would make a
    stored detection say something its own moment did not support (§12, CLAUDE.md).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    poc_price: float | None = None
    va_high: float | None = None
    va_low: float | None = None
    price_vs_va: str | None = Field(
        default=None, description="above | inside | below, at the detection's price."
    )
    nearest_lvn: float | None = None
    nearest_hvn: float | None = None

    @model_validator(mode="after")
    def _known_fields(self) -> VolumeProfileRef:
        if self.scope not in PROFILE_SCOPES:
            raise ValueError(f"unknown profile scope {self.scope!r}")
        if self.price_vs_va not in (None, "above", "inside", "below"):
            raise ValueError(f"unknown price_vs_va {self.price_vs_va!r}")
        return self


class VolatilityContext(AureonModel):
    """How big this candle, and this session, are by recent standards (9B).

    ``regime`` is a label over ``session_range_vs_median``, and the bands that produced it
    are versioned: a label whose definition moved silently is worse than no label, because
    every comparison across the change is then wrong without appearing to be.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    atr_14: float | None = Field(default=None, description="ATR(14) in PRICE.")
    atr_points: float | None = Field(default=None, description="The same, in points.")
    candle_range_pct_of_atr: float | None = Field(
        default=None, description="This candle's range as a fraction of ATR(14)."
    )
    session_range: float | None = Field(
        default=None, description="High-low of the session so far, in price."
    )
    session_range_vs_median: float | None = Field(
        default=None, description="That range over the 20-day median for this session."
    )
    regime: str | None = Field(default=None, description=f"One of {VOLATILITY_REGIMES}.")
    bands_version: int | None = Field(
        default=None, description="Which regime bands produced `regime` (9B)."
    )

    @model_validator(mode="after")
    def _known_regime(self) -> VolatilityContext:
        if self.regime is not None and self.regime not in VOLATILITY_REGIMES:
            raise ValueError(f"unknown volatility regime {self.regime!r}")
        return self
