"""The higher-timeframe context a detection carries (11D).

Stored on the detection because it was true at that candle's close and is not recoverable
afterwards: the EMA pair on H1 at 14:35 depends on every H1 bar before it, and re-deriving it
next year would need the whole history at the version of the aggregation that was running.
Same reasoning as 9B's volume-profile reference and volatility context.

## It is context, not a signal

No agent reads it, nothing gates on it, and no guard refuses a trade because H4 disagrees.
``alignment`` is recorded so a review can ask later whether alignment mattered -- and
answering that is what the evaluation rules are for. Guessing now and building a filter on the
guess is exactly how an unresearched threshold becomes folklore, which is the failure
``aureon/config/symbol_tuning.py`` exists to document at length.

## The bias is stored, not derived at read time

A stored document should keep saying what it said. ``Frame.bias`` in the engine derives the
bias from the EMA pair, and that is right for a live computation; a stored read carries the
answer as well as the inputs, so a reader who later changes their mind about what "bullish"
means cannot silently restate a year of detections. The EMA periods are stored beside it for
the same reason: a bias from 20/50 and one from 9/21 are different claims, and a document that
recorded only the verdict could not tell them apart.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from aureon.models.base import AureonModel, UtcDatetime
from aureon.models.enums import MtfAlignment, Timeframe, TrendBias


class TimeframeRead(AureonModel):
    """One timeframe's last closed bar, and the bias at it."""

    model_config = ConfigDict(extra="forbid")

    timeframe: Timeframe
    #: The last closed bar's OPEN time on that timeframe, so a reader can see how old the read
    #: is. An H1 read at 14:35 is from the bar that opened at 13:00, and pretending otherwise
    #: would make a stale bias look current.
    at: UtcDatetime
    ema_fast: float | None = None
    ema_slow: float | None = None
    close: float
    bias: TrendBias = TrendBias.SIDEWAYS

    @model_validator(mode="after")
    def _a_bias_needs_both_emas(self) -> TimeframeRead:
        missing = self.ema_fast is None or self.ema_slow is None
        if missing and self.bias is not TrendBias.SIDEWAYS:
            # A bias without the pair it came from is a verdict nobody can check, and the
            # engine returns SIDEWAYS in exactly this case. Refusing it here stops a
            # hand-built document making a claim the code never would.
            raise ValueError(
                f"{self.timeframe.value}: a {self.bias.value} bias needs both EMAs"
            )
        return self


class MtfContext(AureonModel):
    """Every timeframe's read at one detection's candle close, and the alignment."""

    model_config = ConfigDict(extra="forbid")

    reads: tuple[TimeframeRead, ...] = ()
    alignment: MtfAlignment = MtfAlignment.MIXED
    #: Which EMA pair produced every bias above. Stored because a bias from 20/50 and one from
    #: 9/21 are different claims.
    ema_fast_period: int = Field(default=0, ge=0)
    ema_slow_period: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _timeframes_are_unique_and_ordered(self) -> MtfContext:
        frames = [read.timeframe for read in self.reads]
        if len(set(frames)) != len(frames):
            raise ValueError(f"duplicate timeframes in an mtf context: {frames}")
        minutes = [frame.minutes for frame in frames]
        if minutes != sorted(minutes):
            # Smallest first, because that is how a reader scans it and how every renderer
            # prints it. An unordered tuple would put H4 above M15 on some screens and not
            # others for no reason a reader could see.
            raise ValueError(f"mtf reads are not smallest-first: {frames}")
        return self

    @property
    def by_timeframe(self) -> dict[Timeframe, TimeframeRead]:
        return {read.timeframe: read for read in self.reads}

    def bias_of(self, timeframe: Timeframe) -> TrendBias | None:
        """One timeframe's bias, or ``None`` when it was not read at all.

        ``None`` rather than SIDEWAYS: "there was not enough history for H4" and "H4 had no
        view" are different, and a renderer that showed them alike would report a flat H4 for
        a symbol whose H4 nobody had ever computed.
        """
        read = self.by_timeframe.get(timeframe)
        return None if read is None else read.bias
